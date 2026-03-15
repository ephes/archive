from __future__ import annotations

import hmac
import json

from django.conf import settings
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, Paginator
from django.core.validators import URLValidator
from django.db.models import Q
from django.db.models.functions import Trim
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods
from django.views.generic.edit import CreateView

from archive.article_audio import ArticleAudioGenerationError, download_generated_article_audio
from archive.classification import classify_item, podcast_feed_decision_for_item
from archive.forms import ItemForm
from archive.media_archival import MediaArchivalError, open_archived_audio
from archive.models import Item
from archive.services import prepare_item_for_enrichment, to_week_page, week_bounds

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


def _public_feed_items():
    return (
        Item.objects.filter(is_public=True, published_at__isnull=False)
        .annotate(feed_title=Trim("title"))
        .exclude(feed_title="")
        .order_by("-published_at", "-id")
    )


def _public_podcast_feed_items():
    candidates = (
        Item.objects.filter(
            is_public=True,
            published_at__isnull=False,
        )
        .filter(Q(archived_audio_path__gt="") | Q(article_audio_artifact_path__gt=""))
        .annotate(feed_title=Trim("title"), feed_summary=Trim("short_summary"))
        .exclude(feed_title="")
        .exclude(feed_summary="")
        .order_by("-published_at", "-id")
    )
    # Feed policy currently depends on per-item application logic, so this first
    # slice filters in Python after a bounded candidate query.
    return [
        item
        for item in candidates
        if podcast_feed_decision_for_item(item).eligible
    ]


def _render_feed(
    request: HttpRequest,
    *,
    page: int,
    items_queryset,
    canonical_view_name: str,
    paged_view_name: str,
    feed_title: str,
    feed_description: str,
    page_description: str,
    include_enclosures: bool = False,
) -> HttpResponse:
    if page == 1 and request.path != reverse(canonical_view_name):
        return redirect(canonical_view_name, permanent=True)

    paginator = Paginator(items_queryset, FEED_PAGE_SIZE)

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
        newer_url = (
            request.build_absolute_uri(reverse(canonical_view_name))
            if page_obj.has_previous() and page - 1 == 1
            else (
                request.build_absolute_uri(reverse(paged_view_name, kwargs={"page": page - 1}))
                if page_obj.has_previous()
                else None
            )
        )
        older_url = (
            request.build_absolute_uri(reverse(paged_view_name, kwargs={"page": page + 1}))
            if page_obj.has_next()
            else None
        )

    if page <= 1:
        self_url = request.build_absolute_uri(reverse(canonical_view_name))
    else:
        self_url = request.build_absolute_uri(reverse(paged_view_name, kwargs={"page": page}))

    feed_items = []
    for item in page_items:
        entry = {
            "title": item.display_title,
            "link": request.build_absolute_uri(item.get_absolute_url()),
            "description": (
                item.short_summary.strip() if include_enclosures else item.feed_description
            ),
            "pub_date": item.feed_published_at,
            "kind": item.get_kind_display(),
        }
        if include_enclosures:
            entry.update(_podcast_enclosure_attributes(request, item))
        feed_items.append(entry)

    return render(
        request,
        "archive/rss.xml",
        {
            "feed_title": feed_title if page == 1 else f"{feed_title} (page {page})",
            "feed_description": (
                feed_description if page == 1 else page_description.format(page=page)
            ),
            "site_url": request.build_absolute_uri(reverse("archive:overview")),
            "self_url": self_url,
            "previous_url": newer_url,
            "next_url": older_url,
            "last_build_date": page_items[0].feed_published_at if page_items else timezone.now(),
            "feed_items": feed_items,
        },
        content_type="application/rss+xml; charset=utf-8",
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
    return _render_feed(
        request,
        page=page,
        items_queryset=_public_feed_items(),
        canonical_view_name="archive:rss-feed",
        paged_view_name="archive:rss-feed-page",
        feed_title="Archive",
        feed_description="Public archive feed for links, episodes, videos, and articles.",
        page_description="Archive feed page {page}.",
    )


@require_GET
def podcast_feed(request: HttpRequest, page: int = 1) -> HttpResponse:
    return _render_feed(
        request,
        page=page,
        items_queryset=_public_podcast_feed_items(),
        canonical_view_name="archive:podcast-feed",
        paged_view_name="archive:podcast-feed-page",
        feed_title="Archive Podcast",
        feed_description="Podcast-style archive feed for items with stable local audio enclosures.",
        page_description="Archive podcast feed page {page}.",
        include_enclosures=True,
    )


@require_GET
def item_archived_audio(request: HttpRequest, pk: int) -> HttpResponse:
    item = get_object_or_404(Item, pk=pk, is_public=True)
    if not item.has_archived_audio:
        raise Http404("Archived audio is not available")

    try:
        audio_file = open_archived_audio(item)
    except MediaArchivalError:
        return HttpResponse("Archived audio is temporarily unavailable.", status=502)

    response = FileResponse(
        audio_file,
        content_type=item.archived_audio_content_type or "audio/mpeg",
    )
    if item.archived_audio_size_bytes:
        response["Content-Length"] = str(item.archived_audio_size_bytes)
    response["Cache-Control"] = "public, max-age=3600"
    return response


@require_GET
def item_detail(request: HttpRequest, pk: int) -> HttpResponse:
    item = get_object_or_404(Item, pk=pk, is_public=True)
    return render(request, "archive/detail.html", {"item": item})


@require_GET
def item_article_audio(request: HttpRequest, pk: int) -> HttpResponse:
    item = get_object_or_404(Item, pk=pk, is_public=True)
    if not item.has_generated_article_audio:
        raise Http404("Article audio is not available")

    try:
        audio = download_generated_article_audio(item=item)
    except ArticleAudioGenerationError:
        return HttpResponse("Article audio is temporarily unavailable.", status=502)

    response = HttpResponse(audio.payload, content_type=audio.content_type)
    response["Cache-Control"] = "public, max-age=3600"
    return response


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
        decision = classify_item(
            original_url=item.original_url,
            explicit_kind=item.kind,
            audio_url=item.audio_url,
            media_url=item.media_url,
        )
        item.kind = decision.kind
        item.classification_rule = decision.rule
        item.classification_evidence = decision.evidence
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
    )
    decision = classify_item(
        original_url=url,
        explicit_kind=explicit_kind,
        audio_url=audio_url,
        media_url=media_url,
    )
    item.kind = decision.kind
    item.classification_rule = decision.rule
    item.classification_evidence = decision.evidence
    prepare_item_for_enrichment(item)
    item.save()
    return JsonResponse(
        {
            "id": item.pk,
            "detail_url": request.build_absolute_uri(item.get_absolute_url()),
        },
        status=201,
    )


def _podcast_enclosure_attributes(request: HttpRequest, item: Item) -> dict[str, str | int]:
    decision = podcast_feed_decision_for_item(item)
    if decision.enclosure_source == "generated_article_audio":
        return {
            "enclosure_url": request.build_absolute_uri(
                reverse("archive:item-article-audio", kwargs={"pk": item.pk})
            ),
            "enclosure_type": "audio/mpeg",
            "enclosure_length": 0,
        }
    return {
        "enclosure_url": request.build_absolute_uri(item.stable_audio_enclosure_url),
        "enclosure_type": item.stable_audio_content_type,
        "enclosure_length": item.stable_audio_size_bytes,
    }
