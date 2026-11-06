from __future__ import annotations

import hmac
import json
import logging
import re
from datetime import UTC

from django.conf import settings
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, Paginator
from django.core.validators import URLValidator
from django.db import connection
from django.db.models import Q
from django.db.models.functions import Trim
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods
from django.views.generic.edit import CreateView

from archive.article_audio import store_generated_article_audio
from archive.capture import capture_item
from archive.classification import (
    CURRENT_CLASSIFICATION_ENGINE_VERSION,
    classify_item,
    podcast_feed_decision_for_item,
)
from archive.forms import ArchiveAuthenticationForm, ItemForm
from archive.media_archival import MediaArchivalError, open_archived_audio
from archive.media_responses import ranged_file_response
from archive.models import Item, ItemKind, PodcastFeedPolicy
from archive.processing_health import processing_health_snapshot
from archive.services import (
    apply_operator_kind_override,
    normalize_item_kind_dependent_statuses,
    set_item_classification,
    to_week_page,
    week_bounds,
    week_page_from_token,
)
from archive.transcript_display import split_transcript_for_display

logger = logging.getLogger(__name__)

url_validator = URLValidator()
FEED_PAGE_SIZE = 50
SEARCH_PAGE_SIZE = 20
SEARCH_TOKEN_RE = re.compile(r"\w+")


class ArchiveLoginView(LoginView):
    authentication_form = ArchiveAuthenticationForm
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


def _public_podcast_feed_page(page: int):
    candidates = (
        Item.objects.filter(
            is_public=True,
            published_at__isnull=False,
        )
        .exclude(podcast_feed_policy=PodcastFeedPolicy.EXCLUDE)
        .filter(
            Q(archived_audio_path__gt="")
            | Q(kind=ItemKind.ARTICLE, article_audio_artifact_path__gt="")
        )
        .annotate(feed_title=Trim("title"), feed_summary=Trim("short_summary"))
        .exclude(feed_title="")
        .exclude(feed_summary="")
        .order_by("-published_at", "-id")
    )
    # Python owns whitespace and generated-summary policy. Stream only its inputs;
    # retain a count and one page of ids, then load full items for that page alone.
    eligible_count = 0
    page_ids = []
    start = (page - 1) * FEED_PAGE_SIZE
    policy_fields = (
        "id",
        "is_public",
        "published_at",
        "title",
        "short_summary",
        "long_summary",
        "kind",
        "podcast_feed_policy",
        "archived_audio_path",
        "article_audio_artifact_path",
    )
    for item in candidates.only(*policy_fields).iterator(chunk_size=100):
        if not podcast_feed_decision_for_item(item).eligible:
            continue
        if start <= eligible_count < start + FEED_PAGE_SIZE:
            page_ids.append(item.pk)
        eligible_count += 1
    page_obj = _feed_page(range(eligible_count), page)
    page_obj.object_list = [
        item
        for item in candidates.filter(pk__in=page_ids)
        if podcast_feed_decision_for_item(item).eligible
    ]
    return page_obj


def _feed_page(items, page: int):
    try:
        return Paginator(items, FEED_PAGE_SIZE).page(page)
    except EmptyPage as exc:
        raise Http404("Unknown feed page") from exc


def _build_search_match_query(raw_query: str) -> str:
    tokens = SEARCH_TOKEN_RE.findall(raw_query.lower())
    return " AND ".join(f"{token}*" for token in tokens)


def _search_public_item_page(raw_query: str, number):
    match_query = _build_search_match_query(raw_query)
    if not match_query:
        return Paginator([], SEARCH_PAGE_SIZE).get_page(number)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM archive_item_fts
            JOIN archive_item AS item ON item.id = archive_item_fts.rowid
            WHERE archive_item_fts MATCH %s
              AND item.is_public = 1
            """,
            [match_query],
        )
        result_count = cursor.fetchone()[0]
        # A range supplies exact pagination metadata without allocating matching ids.
        page_obj = Paginator(range(result_count), SEARCH_PAGE_SIZE).get_page(number)
        if not result_count:
            page_obj.object_list = []
            return page_obj
        cursor.execute(
            """
            SELECT item.id
            FROM archive_item_fts
            JOIN archive_item AS item ON item.id = archive_item_fts.rowid
            WHERE archive_item_fts MATCH %s
              AND item.is_public = 1
            ORDER BY bm25(archive_item_fts), item.shared_at DESC, item.id DESC
            LIMIT %s OFFSET %s
            """,
            [match_query, SEARCH_PAGE_SIZE, (page_obj.number - 1) * SEARCH_PAGE_SIZE],
        )
        page_obj.object_list = [row[0] for row in cursor.fetchall()]
        return page_obj


def _render_feed(
    request: HttpRequest,
    *,
    page_obj,
    canonical_view_name: str,
    paged_view_name: str,
    feed_title: str,
    feed_description: str,
    page_description: str,
    include_enclosures: bool = False,
    feed_image_path: str | None = None,
) -> HttpResponse:
    page = page_obj.number
    if page == 1 and request.path != reverse(canonical_view_name):
        return redirect(canonical_view_name, permanent=True)

    if page_obj.paginator.count == 0:
        if page != 1:
            raise Http404("Unknown feed page")
        page_items = []
        newer_url = None
        older_url = None
    else:
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
            "feed_image_url": (
                request.build_absolute_uri(static(feed_image_path)) if feed_image_path else ""
            ),
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
def search(request: HttpRequest) -> HttpResponse:
    raw_query = (request.GET.get("q") or "").strip()
    page_obj = _search_public_item_page(raw_query, request.GET.get("page") or 1)
    items_by_id = Item.objects.in_bulk(page_obj.object_list)
    results = [items_by_id[item_id] for item_id in page_obj.object_list if item_id in items_by_id]

    return render(
        request,
        "archive/search.html",
        {
            "search_query": raw_query,
            "results": results,
            "page_obj": page_obj,
            "result_count": page_obj.paginator.count,
        },
    )


@require_GET
def processing_health(request: HttpRequest) -> JsonResponse:
    """Unauthenticated processing health for monitoring: step counts and timestamps only."""
    response = JsonResponse(processing_health_snapshot())
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def robots_txt(request: HttpRequest) -> HttpResponse:
    return HttpResponse("User-agent: *\nDisallow: /\n", content_type="text/plain; charset=utf-8")


@require_GET
def rss_feed(request: HttpRequest, page: int = 1) -> HttpResponse:
    return _render_feed(
        request,
        page_obj=_feed_page(_public_feed_items(), page),
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
        page_obj=_public_podcast_feed_page(page),
        canonical_view_name="archive:podcast-feed",
        paged_view_name="archive:podcast-feed-page",
        feed_title="Archive Podcast",
        feed_description="Podcast-style archive feed for items with stable local audio enclosures.",
        page_description="Archive podcast feed page {page}.",
        include_enclosures=True,
        feed_image_path="archive/podcast-cover.png",
    )


@require_GET
def weekly_items_json(request: HttpRequest, week: str) -> JsonResponse:
    try:
        page = week_page_from_token(week)
    except ValueError:
        return JsonResponse({"error": "Invalid week"}, status=400)

    starts_at, ends_at = week_bounds(page)
    items = Item.objects.filter(
        is_public=True,
        shared_at__gte=starts_at,
        shared_at__lt=ends_at,
    ).order_by("shared_at", "id")

    return JsonResponse(
        {
            "week": page.token,
            "items": [
                {
                    "id": item.pk,
                    "kind": item.kind,
                    "kind_display": item.get_kind_display(),
                    "classification_rule": item.classification_rule,
                    "title": item.display_title,
                    "original_url": item.original_url,
                    "short_summary": item.short_summary,
                    "tags": item.tag_list,
                    "source": item.source,
                    "author": item.author,
                    "shared_at": item.shared_at.isoformat(),
                    "published_at": item.published_at.isoformat() if item.published_at else None,
                }
                for item in items
            ],
        }
    )


@require_http_methods(["GET", "HEAD"])
def item_archived_audio(request: HttpRequest, pk: int) -> HttpResponse:
    item = get_object_or_404(Item, pk=pk, is_public=True)
    if not item.has_archived_audio:
        raise Http404("Archived audio is not available")

    try:
        audio_file = open_archived_audio(item)
    except MediaArchivalError:
        return HttpResponse("Archived audio is temporarily unavailable.", status=502)

    return ranged_file_response(
        request,
        file_handle=audio_file,
        size=item.archived_audio_size_bytes,
        content_type=item.archived_audio_content_type or "audio/mpeg",
        cache_control="public, max-age=3600",
    )


@require_GET
def item_detail(request: HttpRequest, pk: int) -> HttpResponse:
    item = get_object_or_404(Item, pk=pk, is_public=True)
    return render(
        request,
        "archive/detail.html",
        {
            "item": item,
            "transcript_paragraphs": split_transcript_for_display(item.transcript),
        },
    )


@require_http_methods(["GET", "HEAD"])
def item_article_audio(request: HttpRequest, pk: int) -> HttpResponse:
    item = get_object_or_404(Item, pk=pk, is_public=True)
    if not item.has_generated_article_audio:
        raise Http404("Article audio is not available")

    try:
        store_generated_article_audio(item=item)
        audio_file = item.article_audio_file.open("rb")
    except Exception:
        logger.exception("Article audio is temporarily unavailable for item %s", item.pk)
        response = HttpResponse("Article audio is temporarily unavailable.", status=503)
        response["Retry-After"] = "60"
        return response

    return ranged_file_response(
        request,
        file_handle=audio_file,
        size=item.article_audio_size_bytes or 0,
        content_type=item.article_audio_content_type or "audio/mpeg",
        cache_control="public, max-age=3600",
    )


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
        set_item_classification(item=item, decision=decision)
        item, created = capture_item(item)
        if not created:
            return self.render_to_response(self.get_context_data(form=form, duplicate_item=item))
        return redirect(item.get_absolute_url())


def _api_request_is_authorized(request: HttpRequest) -> bool:
    auth_header = request.headers.get("Authorization", "")
    expected_header = f"Bearer {settings.ARCHIVE_API_TOKEN}"
    return bool(
        settings.ARCHIVE_API_TOKEN
        # Compare bytes: compare_digest raises TypeError on non-ASCII str.
        and hmac.compare_digest(auth_header.encode("utf-8"), expected_header.encode("utf-8"))
    )


@csrf_exempt
@require_http_methods(["POST"])
def api_create_item(request: HttpRequest) -> JsonResponse:
    if not _api_request_is_authorized(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    fields = {}
    for name in (
        "url",
        "title",
        "notes",
        "audio_url",
        "media_url",
        "source",
        "author",
        "kind",
        "original_published_at",
    ):
        value = payload.get(name)
        if value is None:
            value = ""
        if not isinstance(value, str):
            return JsonResponse({"error": f"Invalid {name}"}, status=400)
        model_name = "original_url" if name == "url" else name
        max_length = Item._meta.get_field(model_name).max_length
        if max_length is not None and len(value) > max_length:
            return JsonResponse({"error": f"Invalid {name}"}, status=400)
        fields[name] = value.strip()

    url = fields["url"]
    title = fields["title"]
    notes = fields["notes"]
    audio_url = fields["audio_url"]
    media_url = fields["media_url"]
    source = fields["source"]
    author = fields["author"]
    explicit_kind = fields["kind"]
    original_published_at_raw = fields["original_published_at"]

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
        try:
            original_published_at = parse_datetime(original_published_at_raw)
            if original_published_at is None:
                raise ValueError("Invalid timestamp")
            if timezone.is_naive(original_published_at):
                original_published_at = timezone.make_aware(
                    original_published_at,
                    timezone.get_current_timezone(),
                )
            original_published_at = original_published_at.astimezone(UTC)
            # Detail templates also need to render the instant in the active timezone.
            timezone.localtime(original_published_at)
        except (ValueError, OverflowError):
            return JsonResponse({"error": "Invalid original_published_at"}, status=400)

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
    set_item_classification(item=item, decision=decision)
    item, created = capture_item(item)
    return JsonResponse(
        {
            "id": item.pk,
            "detail_url": request.build_absolute_uri(item.get_absolute_url()),
            "duplicate": not created,
        },
        status=201 if created else 200,
    )


@csrf_exempt
@require_http_methods(["PATCH"])
def api_update_item(request: HttpRequest, pk: int) -> JsonResponse:
    if not _api_request_is_authorized(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    kind = str(payload.get("kind") or "").strip()
    if kind not in ItemKind.values:
        return JsonResponse({"error": "Invalid kind"}, status=400)

    item = get_object_or_404(Item, pk=pk)
    classification_rule = str(payload.get("classification_rule") or "").strip()
    if classification_rule:
        if classification_rule != "quote_classifier" or kind != ItemKind.QUOTE:
            return JsonResponse({"error": "Invalid classification_rule"}, status=400)
        try:
            update_fields = _apply_api_quote_classifier_kind(item=item)
        except ValueError as exc:
            if str(exc) == "operator_override_conflict":
                return JsonResponse({"error": "Item has an operator override"}, status=409)
            raise
    else:
        update_fields = apply_operator_kind_override(item=item, kind=kind)
    if update_fields:
        item.save(update_fields=sorted(set(update_fields)))

    return JsonResponse(
        {
            "id": item.pk,
            "kind": item.kind,
            "kind_display": item.get_kind_display(),
        }
    )


def _apply_api_quote_classifier_kind(*, item: Item) -> list[str]:
    update_fields: list[str] = []
    if item.kind != ItemKind.QUOTE:
        item.kind = ItemKind.QUOTE
        update_fields.append("kind")
    if item.classification_rule == "operator_override":
        raise ValueError("operator_override_conflict")
    if item.classification_rule != "quote_classifier":
        item.classification_rule = "quote_classifier"
        update_fields.append("classification_rule")
    if item.classification_engine_version != CURRENT_CLASSIFICATION_ENGINE_VERSION:
        item.classification_engine_version = CURRENT_CLASSIFICATION_ENGINE_VERSION
        update_fields.append("classification_engine_version")
    evidence = (
        item.classification_evidence if isinstance(item.classification_evidence, dict) else {}
    )
    next_evidence = {**evidence, "quote_classifier": {"kind": ItemKind.QUOTE}}
    if item.classification_evidence != next_evidence:
        item.classification_evidence = next_evidence
        update_fields.append("classification_evidence")
    normalize_item_kind_dependent_statuses(item=item, update_fields=update_fields)
    return update_fields


def _podcast_enclosure_attributes(request: HttpRequest, item: Item) -> dict[str, str | int]:
    decision = podcast_feed_decision_for_item(item)
    if decision.enclosure_source == "generated_article_audio":
        return {
            "enclosure_url": request.build_absolute_uri(
                reverse("archive:item-article-audio", kwargs={"pk": item.pk})
            ),
            "enclosure_type": item.article_audio_content_type or "audio/mpeg",
            "enclosure_length": item.article_audio_size_bytes or 0,
        }
    return {
        "enclosure_url": request.build_absolute_uri(item.stable_audio_enclosure_url),
        "enclosure_type": item.stable_audio_content_type,
        "enclosure_length": item.stable_audio_size_bytes,
    }
