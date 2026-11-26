from __future__ import annotations

from botocore.exceptions import BotoCoreError, ClientError
from django.core.management.base import BaseCommand, CommandError
from django.db import router

from archive.media_storage import (
    IncompleteArchiveMediaListingError,
    ReservationReleaseError,
    ReservationStorageDeleteError,
    reconcile_upload_reservation,
)
from archive.models import PendingArchiveMediaUpload


class Command(BaseCommand):
    help = (
        "Report upload reservations and optionally release those whose owning "
        "processing attempt is provably retired."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Delete unreferenced objects of retired reservations and release them "
                "instead of only reporting."
            ),
        )

    def handle(self, *args, **options) -> None:
        apply = options["apply"]
        using = router.db_for_write(PendingArchiveMediaUpload)
        counts = dict.fromkeys(("live", "retired", "unowned", "oversized", "gone"), 0)
        deleted_count = released_count = failed_count = 0
        unverified_release_count = 0
        self.stdout.write(
            "Apply mode: releasing retired upload reservations."
            if apply
            else "Dry-run mode: advisory report. Concurrent changes may affect results."
        )
        reservation_ids = list(
            PendingArchiveMediaUpload.objects.using(using)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        for ordinal, reservation_id in enumerate(reservation_ids, start=1):
            try:
                outcome = reconcile_upload_reservation(reservation_id, using=using, apply=apply)
            except ReservationReleaseError as exc:
                deleted_count += exc.deleted_count
                failed_count += 1
                if exc.reservation_retained:
                    detail = "Reservation remains for retry."
                else:
                    unverified_release_count += 1
                    detail = "Reservation retention could not be confirmed."
                self.stderr.write(
                    f"Database failure for reservation {ordinal} (reservation_release_failed). "
                    f"Confirmed object deletes: {exc.deleted_count}. {detail}"
                )
                continue
            except IncompleteArchiveMediaListingError:
                failed_count += 1
                self.stderr.write(
                    f"Storage listing failed for reservation {ordinal} (storage_listing_failed)."
                )
                continue
            except (ReservationStorageDeleteError, OSError, BotoCoreError, ClientError) as exc:
                # The release rolled back; the reservation keeps protecting what is left.
                if isinstance(exc, ReservationStorageDeleteError):
                    deleted_count += exc.deleted_count
                failed_count += 1
                self.stderr.write(
                    f"Storage deletion failed for reservation {ordinal} (storage_delete_failed)."
                )
                continue
            counts[outcome.state] += 1
            if outcome.state in {"live", "gone"}:
                continue
            owner = outcome.owner_item_id if outcome.owner_item_id is not None else "-"
            label = outcome.state.upper()
            self.stdout.write(f"{label} {outcome.prefix} item={owner}")
            for name in outcome.kept_referenced:
                self.stdout.write(f"  KEEP-REFERENCED {name}")
            for name in outcome.deleted:
                self.stdout.write(f"  {'DELETED' if apply else 'WOULD-DELETE'} {name}")
            if outcome.released:
                released_count += 1
            if apply:
                deleted_count += len(outcome.deleted)

        if counts["retired"] and not apply:
            self.stdout.write("Re-run with --apply to release the retired reservations.")
        self.stdout.write(
            "Summary: "
            f"reservations={len(reservation_ids)} "
            f"live={counts['live']} "
            f"retired={counts['retired']} "
            f"unowned={counts['unowned']} "
            f"oversized={counts['oversized']} "
            f"released={released_count} "
            f"deleted={deleted_count} "
            f"mode={'apply' if apply else 'dry-run'} "
            f"failed={failed_count}"
        )
        follow_up = []
        retryable_count = failed_count - unverified_release_count
        if retryable_count:
            follow_up.append(f"Retry the command for {retryable_count} failed reservations")
        if unverified_release_count:
            follow_up.append(
                f"Verify the reservation state for {unverified_release_count} database failures"
            )
        if apply and counts["oversized"]:
            follow_up.append(
                f"{counts['oversized']} oversized reservations require manual recovery"
            )
        if follow_up:
            raise CommandError(f"Reservation reconciliation incomplete. {'. '.join(follow_up)}.")
