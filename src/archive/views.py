from __future__ import annotations

import hmac
import json

from django.conf import settings
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods
from django.views.generic.edit import CreateView

from archive.forms import ItemForm
from archive.models import Item
from archive.services import infer_kind, to_week_page, week_bounds

url_validator = URLValidator()


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
    source = (payload.get("source") or "").strip()
    explicit_kind = (payload.get("kind") or "").strip()

    try:
        url_validator(url)
        if audio_url:
            url_validator(audio_url)
    except ValidationError:
        return JsonResponse({"error": "Invalid or missing url"}, status=400)

    item = Item.objects.create(
        original_url=url,
        title=title,
        notes=notes,
        audio_url=audio_url,
        source=source,
        kind=infer_kind(url=url, explicit_kind=explicit_kind, audio_url=audio_url),
    )
    return JsonResponse(
        {
            "id": item.pk,
            "detail_url": request.build_absolute_uri(item.get_absolute_url()),
        },
        status=201,
    )
