from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlparse

from django.core.management.base import BaseCommand
from django.utils import timezone

from archive.models import Item, ItemKind
from archive.quote_classifier import (
    CURRENT_QUOTE_CLASSIFIER_VERSION,
    QuoteClassificationError,
    classify_quote_item,
    quote_classifier_processed,
)

SOCIAL_QUOTE_HOSTS = {
    "x.com",
    "twitter.com",
    "mastodon.social",
    "chaos.social",
    "beige.party",
}
QUOTE_HINTS = {"quote", "quotes", "quotation", "zitat", "zitate"}


class Command(BaseCommand):
    help = "Classify recent archive items that may be reusable weeknote opening quotes."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=25,
            help="Maximum number of unprocessed candidate items to inspect.",
        )
        parser.add_argument(
            "--since-days",
            type=int,
            default=14,
            help="Only inspect items shared in the last N days.",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=30,
            help="Per-request timeout in seconds for source fetch and classifier API calls.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview classifications without updating items.",
        )

    def handle(self, *args, **options) -> None:
        limit = options["limit"]
        if limit < 1:
            self.stdout.write("No items inspected: --limit must be at least 1.")
            return

        items = self._candidate_items(limit=limit, since_days=options["since_days"])
        mode = "dry-run" if options["dry_run"] else "apply"
        if not items:
            self.stdout.write(
                "No quote-classifier candidates matched "
                f"(version={CURRENT_QUOTE_CLASSIFIER_VERSION}, mode={mode})."
            )
            return

        inspected = 0
        yes = 0
        no = 0
        changed = 0
        failed = 0
        for item in items:
            inspected += 1
            try:
                result = classify_quote_item(
                    item=item,
                    timeout=options["timeout"],
                    dry_run=options["dry_run"],
                )
            except QuoteClassificationError as exc:
                failed += 1
                self.stdout.write(f"FAILED item {item.pk}: {item.original_url}")
                self.stdout.write(f"  - {exc}")
                continue

            decision = result.decision
            if decision.high_confidence_yes:
                yes += 1
                label = "WOULD MARK QUOTE" if options["dry_run"] else "MARKED QUOTE"
            elif decision.is_quote:
                no += 1
                label = (
                    "WOULD STORE LOW-CONFIDENCE YES"
                    if options["dry_run"]
                    else "STORED LOW-CONFIDENCE YES"
                )
            else:
                no += 1
                label = "WOULD STORE NO" if options["dry_run"] else "STORED NO"

            if result.update_fields:
                changed += 1
            elif options["dry_run"] and decision.high_confidence_yes:
                changed += 1

            self.stdout.write(f"{label} item {item.pk}: {item.original_url}")
            self.stdout.write(
                "  - "
                f"decision={'yes' if decision.is_quote else 'no'}; "
                f"confidence={decision.confidence:.2f}; "
                f"threshold={result.evidence['confidence_threshold']:.2f}"
            )
            self.stdout.write(f"  - rationale={decision.rationale}")

        self.stdout.write(
            "Summary: "
            f"inspected={inspected} yes={yes} no={no} changed={changed} "
            f"failed={failed} mode={mode} version={CURRENT_QUOTE_CLASSIFIER_VERSION}"
        )

    def _candidate_items(self, *, limit: int, since_days: int) -> list[Item]:
        since = timezone.now() - timedelta(days=since_days)
        queryset = (
            Item.objects.filter(shared_at__gte=since)
            .exclude(kind=ItemKind.QUOTE)
            .exclude(classification_rule="operator_override")
            .order_by("shared_at", "id")
        )

        items: list[Item] = []
        for item in queryset.iterator():
            if quote_classifier_processed(item) or not _looks_like_quote_classifier_candidate(item):
                continue
            items.append(item)
            if len(items) >= limit:
                break
        return items


def _looks_like_quote_classifier_candidate(item: Item) -> bool:
    if item.kind in {ItemKind.PODCAST_EPISODE, ItemKind.VIDEO}:
        return False
    host = urlparse(item.original_url).netloc.lower().removeprefix("www.")
    if host in SOCIAL_QUOTE_HOSTS or host.endswith(".social") or host.endswith(".party"):
        return True
    haystack = "\n".join(
        [
            item.title,
            item.source,
            item.author,
            item.notes,
            item.tags,
            item.original_url,
        ]
    ).lower()
    return any(hint in haystack for hint in QUOTE_HINTS)
