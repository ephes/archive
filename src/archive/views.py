from __future__ import annotations

import hmac
import json

from django.conf import settings
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, Paginator
from django.core.validators import URLValidator
from django.db.models.functions import Trim
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods
from django.views.generic.edit import CreateView

from archive.forms import ItemForm
from archive.models import Item
from archive.services import infer_kind, prepare_item_for_enrichment, to_week_page, week_bounds

url_validator = URLValidator()
FEED_PAGE_SIZE = 50


class ArchiveLoginView(LoginView):
    template_name = "registration/login.html"


class ArchiveLogoutView(LogoutView):
    pass


def _build_week_navigation(items) -> list:
    week_pages = []
    seen = set()
    for shared_at in items.values_list("shared_at", flat=True):
        page = to_week_page(shared_at)
        if page.token in seen:
            continue
        seen.add(page.token)
        week_pages.append(page)
    return week_pages


def _feed_page_url(request: HttpRequest, page: int) -> str:
    if page <= 1:
        return request.build_absolute_uri(reverse("archive:rss-feed"))
    return request.build_absolute_uri(reverse("archive:rss-feed-page", kwargs={"page": page}))


def _public_feed_items():
    return (
        Item.objects.filter(is_public=True, published_at__isnull=False)
        .annotate(feed_title=Trim("title"))
        .exclude(feed_title="")
        .order_by("-published_at", "-id")
    )


@require_GET
def overview(request: HttpRequest) -> HttpResponse:
    public_items = Item.objects.filter(is_public=True)
    week_pages = _build_week_navigation(public_items)
    requested_week = request.GET.get("week")

    if week_pages:
        if requested_week:
            current_index = next(
                (i for i, page in enumerate(week_pages) if page.token == requested_week), None
            )
            if current_index is None:
                return HttpResponseBadRequest("Unknown week")
        else:
            current_index = 0
        current_page = week_pages[current_index]
        starts_at, ends_at = week_bounds(current_page)
        items = public_items.filter(shared_at__gte=starts_at, shared_at__lt=ends_at)
        newer_page = week_pages[current_index - 1] if current_index > 0 else None
        older_page = week_pages[current_index + 1] if current_index + 1 < len(week_pages) else None
    else:
        current_page = None
        items = public_items.none()
        newer_page = None
        older_page = None

    return render(
        request,
        "archive/overview.html",
        {
            "items": items,
            "current_page": current_page,
            "newer_page": newer_page,
            "older_page": older_page,
        },
    )


@require_GET
def rss_feed(request: HttpRequest, page: int = 1) -> HttpResponse:
    if page == 1 and request.resolver_match and request.resolver_match.url_name == "rss-feed-page":
        return redirect("archive:rss-feed", permanent=True)

    public_items = _public_feed_items()
    paginator = Paginator(public_items, FEED_PAGE_SIZE)

    if paginator.count == 0:
        if page != 1:
            raise Http404("Unknown feed page")
        page_items = []
        newer_url = None
        older_url = None
    else:
        try:
            page_obj = paginator.page(page)
        except EmptyPage as exc:
            raise Http404("Unknown feed page") from exc
        page_items = list(page_obj.object_list)
        newer_url = _feed_page_url(request, page - 1) if page_obj.has_previous() else None
        older_url = _feed_page_url(request, page + 1) if page_obj.has_next() else None

    feed_items = [
        {
            "title": item.display_title,
            "link": request.build_absolute_uri(item.get_absolute_url()),
            "description": item.feed_description,
            "pub_date": item.feed_published_at,
            "kind": item.get_kind_display(),
        }
        for item in page_items
    ]

    return render(
        request,
        "archive/rss.xml",
        {
            "feed_title": "Archive" if page == 1 else f"Archive (page {page})",
            "feed_description": (
                "Public archive feed for links, episodes, videos, and articles."
                if page == 1
                else f"Archive feed page {page}."
            ),
            "site_url": request.build_absolute_uri(reverse("archive:overview")),
            "self_url": _feed_page_url(request, page),
            "previous_url": newer_url,
            "next_url": older_url,
            "last_build_date": page_items[0].feed_published_at if page_items else timezone.now(),
            "feed_items": feed_items,
        },
        content_type="application/rss+xml; charset=utf-8",
    )


@require_GET
def item_detail(request: HttpRequest, pk: int) -> HttpResponse:
    item = get_object_or_404(Item, pk=pk, is_public=True)
    return render(request, "archive/detail.html", {"item": item})


class ItemCreateView(CreateView):
    model = Item
    form_class = ItemForm
    template_name = "archive/item_form.html"

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if not request.user.is_authenticated:
            return redirect(f"{reverse('archive:login')}?next={request.path}")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        item = form.save(commit=False)
        item.kind = infer_kind(
            url=item.original_url,
            explicit_kind=item.kind,
            audio_url=item.audio_url,
        )
        prepare_item_for_enrichment(item)
        item.save()
        return redirect(item.get_absolute_url())


@csrf_exempt
@require_http_methods(["POST"])
def api_create_item(request: HttpRequest) -> JsonResponse:
    auth_header = request.headers.get("Authorization", "")
    expected_header = f"Bearer {settings.ARCHIVE_API_TOKEN}"
    if not settings.ARCHIVE_API_TOKEN or not hmac.compare_digest(auth_header, expected_header):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    url = (payload.get("url") or "").strip()
    title = (payload.get("title") or "").strip()
    notes = (payload.get("notes") or "").strip()
    audio_url = (payload.get("audio_url") or "").strip()
    media_url = (payload.get("media_url") or "").strip()
    source = (payload.get("source") or "").strip()
    author = (payload.get("author") or "").strip()
    explicit_kind = (payload.get("kind") or "").strip()
    original_published_at_raw = (payload.get("original_published_at") or "").strip()

    try:
        url_validator(url)
        if audio_url:
            url_validator(audio_url)
        if media_url:
            url_validator(media_url)
    except ValidationError:
        return JsonResponse({"error": "Invalid or missing url"}, status=400)

    original_published_at = None
    if original_published_at_raw:
        original_published_at = parse_datetime(original_published_at_raw)
        if original_published_at is None:
            return JsonResponse({"error": "Invalid original_published_at"}, status=400)
        if timezone.is_naive(original_published_at):
            original_published_at = timezone.make_aware(
                original_published_at,
                timezone.get_current_timezone(),
            )

    item = Item(
        original_url=url,
        title=title,
        notes=notes,
        audio_url=audio_url,
        media_url=media_url,
        source=source,
        author=author,
        original_published_at=original_published_at,
        kind=infer_kind(url=url, explicit_kind=explicit_kind, audio_url=audio_url),
    )
    prepare_item_for_enrichment(item)
    item.save()
    return JsonResponse(
        {
            "id": item.pk,
            "detail_url": request.build_absolute_uri(item.get_absolute_url()),
        },
        status=201,
    )
