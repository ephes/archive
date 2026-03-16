from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError

from archive.classification import (
    CURRENT_CLASSIFICATION_ENGINE_VERSION,
    classify_item,
    normalized_classification_evidence,
    selected_media_from_evidence,
)
from archive.models import Item
from archive.services import (
    describe_item_downstream_normalization,
    normalize_item_downstream_state,
    set_item_classification,
)


class Command(BaseCommand):
    help = (
        "Replay classification and media-resolution decisions for existing items. "
        "Dry-run by default."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--item-id",
            action="append",
            type=int,
            dest="item_ids",
            default=[],
            help="Specific item id to inspect. Repeat for multiple items.",
        )
        parser.add_argument(
            "--host",
            action="append",
            default=[],
            help="Only include items whose original URL host exactly matches this value.",
        )
        parser.add_argument(
            "--rule",
            action="append",
            default=[],
            help="Only include items with the given stored classification rule.",
        )
        parser.add_argument(
            "--empty-rule",
            action="store_true",
            help="Only include items with an empty stored classification rule.",
        )
        parser.add_argument(
            "--empty-evidence",
            action="store_true",
            help="Only include items with empty stored classification evidence.",
        )
        parser.add_argument(
            "--stale-only",
            action="store_true",
            help="Only include items older than the current classification engine version.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of matched items to inspect after filtering.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Allow replay against all items when no narrower selector is provided.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Persist kind/rule/evidence/version updates for matching items. "
                "Does not queue metadata, archival, transcript, summary, or article-audio work."
            ),
        )
        parser.add_argument(
            "--normalize-downstream",
            action="store_true",
            help=(
                "Also preview or apply explicit downstream status cleanup for the selected items. "
                "This only clears stale unsupported/materialized transcript, media-archive, and "
                "article-audio state; it does not queue newly eligible work."
            ),
        )

    def handle(self, *args, **options) -> None:
        selectors_used = any(
            (
                options["item_ids"],
                options["host"],
                options["rule"],
                options["empty_rule"],
                options["empty_evidence"],
                options["stale_only"],
            )
        )
        if not options["all"] and not selectors_used:
            raise CommandError("Provide a selector such as --item-id/--host/--rule, or use --all.")

        items = self._matching_items(options)
        mode = "apply" if options["apply"] else "dry-run"
        if not items:
            self.stdout.write(f"No items matched ({mode}).")
            return

        self.stdout.write(self._mode_banner(options))

        changed = 0
        unchanged = 0
        for item in items:
            decision = classify_item(
                original_url=item.original_url,
                current_kind=item.kind,
                audio_url=item.audio_url,
                media_url=item.media_url,
                existing_rule=item.classification_rule,
                existing_evidence=item.classification_evidence,
            )
            change_descriptions = self._change_descriptions(
                item=item,
                decision=decision,
                normalize_downstream=options["normalize_downstream"],
            )

            if change_descriptions:
                changed += 1
                label = "UPDATED" if options["apply"] else "WOULD UPDATE"
                self.stdout.write(f"{label} item {item.pk}: {item.original_url}")
                for description in change_descriptions:
                    self.stdout.write(f"  - {description}")
                if options["apply"]:
                    update_fields = set_item_classification(item=item, decision=decision)
                    if options["normalize_downstream"]:
                        normalize_item_downstream_state(item=item, update_fields=update_fields)
                    if update_fields:
                        item.save(update_fields=sorted(set(update_fields)))
            else:
                unchanged += 1
                self.stdout.write(f"UNCHANGED item {item.pk}: {item.original_url}")
                self.stdout.write(
                    "  - "
                    f"kind={item.kind}; "
                    f"rule={item.classification_rule or 'none'}; "
                    f"engine_version={item.classification_engine_version}"
                )

        self.stdout.write(
            f"Summary: inspected={len(items)} changed={changed} unchanged={unchanged} mode={mode}"
        )

    def _matching_items(self, options: dict[str, Any]) -> list[Item]:
        queryset = Item.objects.order_by("id")
        if options["item_ids"]:
            queryset = queryset.filter(pk__in=options["item_ids"])
        if options["rule"]:
            queryset = queryset.filter(classification_rule__in=options["rule"])
        if options["empty_rule"]:
            queryset = queryset.filter(classification_rule="")
        if options["empty_evidence"]:
            queryset = queryset.filter(classification_evidence={})
        if options["stale_only"]:
            queryset = queryset.filter(
                classification_engine_version__lt=CURRENT_CLASSIFICATION_ENGINE_VERSION
            )

        allowed_hosts = {host.strip().lower() for host in options["host"] if host.strip()}
        limit = options["limit"]
        items: list[Item] = []
        for item in queryset.iterator():
            if allowed_hosts and urlparse(item.original_url).netloc.lower() not in allowed_hosts:
                continue
            items.append(item)
            if limit is not None and len(items) >= limit:
                break
        return items

    def _mode_banner(self, options: dict[str, Any]) -> str:
        if options["apply"] and options["normalize_downstream"]:
            return (
                "Apply mode: updating classification fields plus explicit downstream "
                "normalization only. No downstream jobs will be queued."
            )
        if options["apply"]:
            return (
                "Apply mode: updating classification fields only. "
                "No downstream jobs will be queued."
            )
        if options["normalize_downstream"]:
            return (
                "Dry-run mode: no items will be updated. Downstream normalization is "
                "previewed only and no downstream jobs will be queued."
            )
        return "Dry-run mode: no items will be updated and no downstream jobs will be queued."

    def _change_descriptions(
        self,
        *,
        item: Item,
        decision,
        normalize_downstream: bool,
    ) -> list[str]:
        descriptions: list[str] = []

        if item.kind != decision.kind:
            descriptions.append(f"kind: {item.kind} -> {decision.kind}")
        if item.classification_rule != decision.rule:
            descriptions.append(
                f"rule: {item.classification_rule or 'none'} -> {decision.rule or 'none'}"
            )

        current_selected_media = selected_media_from_evidence(item.classification_evidence)
        proposed_selected_media = selected_media_from_evidence(decision.evidence)
        for key in ("audio", "video"):
            if current_selected_media[key] != proposed_selected_media[key]:
                descriptions.append(
                    f"selected_{key}: "
                    f"{current_selected_media[key] or 'none'} -> "
                    f"{proposed_selected_media[key] or 'none'}"
                )

        if (
            normalized_classification_evidence(item.classification_evidence)
            != normalized_classification_evidence(decision.evidence)
            and current_selected_media == proposed_selected_media
        ):
            descriptions.append("classification evidence updated")

        if item.classification_engine_version != CURRENT_CLASSIFICATION_ENGINE_VERSION:
            descriptions.append(
                "engine_version: "
                f"{item.classification_engine_version} -> "
                f"{CURRENT_CLASSIFICATION_ENGINE_VERSION}"
            )

        if normalize_downstream:
            preview_item = self._preview_item_with_decision(item=item, decision=decision)
            descriptions.extend(describe_item_downstream_normalization(preview_item))

        return descriptions

    def _preview_item_with_decision(self, *, item: Item, decision) -> Item:
        preview = Item()
        for field in item._meta.concrete_fields:
            setattr(preview, field.attname, getattr(item, field.attname))
        set_item_classification(item=preview, decision=decision, update_fields=[])
        return preview
