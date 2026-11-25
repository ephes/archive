import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.db.models.query import QuerySet

from archive.management.commands import reclassify_items
from archive.models import EnrichmentStatus, Item, ItemKind

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("selectors", [[], ["--host", "example.test"], ["--all"]])
def test_replay_rejects_blank_explicit_host_before_inventory(selectors, django_assert_num_queries):
    Item.objects.bulk_create(
        [
            Item(original_url="https://example.test/story", classification_engine_version=0),
            Item(original_url="https://other.test/story", classification_engine_version=0),
        ]
    )
    with django_assert_num_queries(0):
        with pytest.raises(CommandError, match="--host must not be blank"):
            call_command(
                "reclassify_items", *selectors, "--host", "  ", "--apply", stdout=StringIO()
            )
    assert list(Item.objects.values_list("classification_engine_version", flat=True)) == [0, 0]


@pytest.mark.parametrize(
    ("selector", "urls"),
    [
        (
            " ExAMPLE.test ",
            (
                "https://example.test/story",
                "https://EXAMPLE.test:8443/story",
                "https://adjacent.example.test/story",
            ),
        ),
        (
            "[2001:0DB8::1]",
            (
                "https://[2001:db8::1]/story",
                "https://[2001:db8:0:0::1]:8443/story",
                "https://[2001:db8::2]/story",
            ),
        ),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_replay_host_matches_hostname_across_ports(selector, urls):
    first, second, adjacent = Item.objects.bulk_create(
        [Item(original_url=url, classification_engine_version=0) for url in urls]
    )
    call_command("reclassify_items", "--host", selector, "--apply", stdout=StringIO())
    changed_ids = set(
        Item.objects.exclude(classification_engine_version=0).values_list("pk", flat=True)
    )
    assert changed_ids == {first.pk, second.pk}
    assert adjacent.pk not in changed_ids


@pytest.mark.parametrize("selector", ["example.test", " EXAMPLE.test. "])
def test_replay_host_matches_with_or_without_dns_root_dot(selector):
    plain, fqdn, adjacent = Item.objects.bulk_create(
        [
            Item(original_url="https://example.test/story", classification_engine_version=0),
            Item(original_url="https://EXAMPLE.test.:8443/story", classification_engine_version=0),
            Item(
                original_url="https://adjacent.example.test./story", classification_engine_version=0
            ),
        ]
    )
    stdout = StringIO()

    call_command("reclassify_items", "--host", selector, stdout=stdout)

    report = stdout.getvalue()
    assert "Summary: inspected=2 changed=2 unchanged=0 mode=dry-run skipped=0" in report
    assert f"WOULD UPDATE item {plain.pk}:" in report
    assert f"WOULD UPDATE item {fqdn.pk}:" in report
    assert f"item {adjacent.pk}:" not in report


@pytest.mark.parametrize(
    "selector", ["example.test:8443", "[2001:db8::1]:8443", "2001:db8::1", ".", " . ", "..", "a.."]
)
def test_replay_rejects_host_with_port_or_unbracketed_ipv6(selector, django_assert_num_queries):
    with django_assert_num_queries(0):
        with pytest.raises(CommandError, match="--host requires a hostname without a port"):
            call_command("reclassify_items", "--host", selector, "--apply", stdout=StringIO())


@pytest.mark.django_db(transaction=True)
def test_replay_uses_edits_and_completed_work_after_selection(monkeypatch):
    item = Item.objects.create(original_url="https://example.com/story", kind=ItemKind.LINK)
    select = reclassify_items.Command._matching_items

    def interleave(self, options):
        selected = list(select(self, options))
        Item.objects.filter(pk=item.pk).update(
            kind=ItemKind.VIDEO,
            media_url="https://example.com/video.mp4",
            classification_rule="operator_override",
            transcript="new completed transcript",
            transcript_status=EnrichmentStatus.COMPLETE,
            archived_audio_path="new/audio.mp3",
            media_archive_status=EnrichmentStatus.COMPLETE,
        )
        return iter(selected)

    monkeypatch.setattr(reclassify_items.Command, "_matching_items", interleave)
    call_command(
        "reclassify_items", "--all", "--apply", "--normalize-downstream", stdout=StringIO()
    )
    item.refresh_from_db()
    assert item.kind == ItemKind.VIDEO
    assert item.classification_rule == "operator_override"
    assert item.media_url == "https://example.com/video.mp4"
    assert item.transcript == "new completed transcript"
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert item.archived_audio_path == "new/audio.mp3"
    assert item.media_archive_status == EnrichmentStatus.COMPLETE


@pytest.mark.django_db(transaction=True)
def test_replay_decides_and_saves_in_one_transaction(monkeypatch):
    Item.objects.create(original_url="https://example.com/story")
    classify = reclassify_items.classify_item
    save = Item.save
    phases = []

    def check_classify(**kwargs):
        phases.append("classify")
        assert len(transaction.get_connection().atomic_blocks) == 1
        return classify(**kwargs)

    def check_save(self, *args, **kwargs):
        phases.append("save")
        assert len(transaction.get_connection().atomic_blocks) == 1
        return save(self, *args, **kwargs)

    monkeypatch.setattr(reclassify_items, "classify_item", check_classify)
    monkeypatch.setattr(Item, "save", check_save)
    call_command("reclassify_items", "--all", "--apply", stdout=StringIO())
    assert phases == ["classify", "save"]


@pytest.mark.parametrize("size", [1, 501])
def test_selection_streams_only_ids_and_urls(size, monkeypatch, django_assert_num_queries):
    Item.objects.bulk_create(
        [
            Item(original_url=f"https://example.com/{n}", transcript="payload" * 10000)
            for n in range(size)
        ]
    )
    fetch_all = QuerySet._fetch_all
    classify = reclassify_items.classify_item
    batches = []
    classified = 0
    selection_sql = []

    def observe(self):
        is_selection = self._fields == ("id", "original_url") and self._result_cache is None
        result = fetch_all(self)
        if is_selection:
            batches.append(len(self._result_cache))
            selection_sql.append(str(self.query))
        return result

    def first_decision(**kwargs):
        nonlocal classified
        if classified == 0:
            assert batches == [min(200, size)]
        classified += 1
        return classify(**kwargs)

    monkeypatch.setattr(QuerySet, "_fetch_all", observe)
    monkeypatch.setattr(reclassify_items, "classify_item", first_decision)
    output = StringIO()
    call_command("reclassify_items", "--all", stdout=output)
    assert classified == size
    assert f"inspected={size}" in output.getvalue()
    assert sum(batches) == size
    assert max(batches) <= 200
    assert all('"transcript"' not in sql and "LIMIT 200" in sql for sql in selection_sql)
    options = {
        "item_ids": [],
        "rule": [],
        "empty_rule": False,
        "empty_evidence": False,
        "stale_only": False,
        "host": ["example.com"],
        "limit": None,
    }
    with django_assert_num_queries(size // 200 + 2):
        ids = list(reclassify_items.Command()._matching_items(options))
    assert len(ids) == size
    assert all(isinstance(pk, int) for pk in ids)


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_selection_releases_sqlite_cursor_before_concurrent_edit(tmp_path, journal):
    script = r"""
import django
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from django.db import connections
from django.core.management import call_command

django.setup()
from archive.management.commands.reclassify_items import Command
from archive.models import Item, ItemKind, EnrichmentStatus
call_command("migrate", verbosity=0)
with connections["default"].cursor() as cursor:
    cursor.execute("PRAGMA journal_mode=" + JOURNAL)
Item.objects.bulk_create([
    Item(original_url=f"https://example.com/{n}") for n in range(201)
])
target = Item.objects.order_by("id")[1]
original = Command._matching_items

def edit():
    try:
        Item.objects.filter(pk=target.pk).update(
            kind=ItemKind.VIDEO, classification_rule="operator_override",
            media_url="https://example.com/new.mp4", transcript="completed",
            transcript_status=EnrichmentStatus.COMPLETE,
            archived_audio_path="new/audio.mp3", media_archive_status=EnrichmentStatus.COMPLETE)
    finally:
        connections.close_all()

def selected(self, options):
    for index, pk in enumerate(original(self, options)):
        if index == 0:
            # Selection has started but its cursor must be closed before this
            # independent connection commits. The command then sees these edits.
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(edit).result(timeout=10)
        yield pk
Command._matching_items = selected
call_command("reclassify_items", "--all", "--apply", "--normalize-downstream", stdout=StringIO())
target.refresh_from_db()
assert target.kind == ItemKind.VIDEO
assert target.classification_rule == "operator_override"
assert target.transcript == "completed"
assert target.media_archive_status == EnrichmentStatus.COMPLETE
assert target.archived_audio_path == "new/audio.mp3"
connections.close_all()
"""
    script = "JOURNAL = " + repr(journal) + "\n" + script
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "DJANGO_DB_PATH": str(tmp_path / "replay.sqlite3"),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=Path(__file__).resolve().parents[1] / "src" / "django",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.django_db(transaction=True)
def test_replay_diagnostics_hide_private_url_components(apply):
    url = "https://test-user:test-password@example.com/private-token.mp3?signature=secret-query#secret-frag"
    item = Item.objects.create(original_url=url, audio_url=url)
    output = StringIO()
    args = ["--all", *(["--apply"] if apply else [])]
    call_command("reclassify_items", *args, stdout=output)
    if not apply:
        call_command("reclassify_items", "--all", "--apply", stdout=StringIO())
    # Exercise UNCHANGED as well as WOULD UPDATE / UPDATED.
    call_command("reclassify_items", *args, stdout=output)
    text = output.getvalue()
    assert f"item {item.pk}" in text
    assert "https://example.com" in text
    assert "UNCHANGED" in text
    for secret in ("test-user", "test-password", "private-token", "secret-query", "secret-frag"):
        assert secret not in text


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.django_db(transaction=True)
def test_replay_stops_at_starting_horizon_despite_new_batches(monkeypatch, apply):
    Item.objects.bulk_create([Item(original_url=f"https://example.com/{n}") for n in range(400)])
    original_ids = set(Item.objects.values_list("pk", flat=True))
    process = reclassify_items.Command._process_item
    processed = []

    def insert_while_processing(self, item, options):
        processed.append(item.pk)
        if len(processed) % 200 == 1 and len(processed) < 800:
            Item.objects.bulk_create(
                [
                    Item(original_url=f"https://example.com/later-{len(processed)}-{n}")
                    for n in range(200)
                ]
            )
        return process(self, item, options)

    monkeypatch.setattr(reclassify_items.Command, "_process_item", insert_while_processing)
    args = ["--all", *(["--apply"] if apply else [])]
    output = StringIO()
    call_command("reclassify_items", *args, stdout=output)
    assert set(processed) == original_ids
    assert "inspected=400" in output.getvalue()
    later_ids = set(Item.objects.values_list("pk", flat=True)) - original_ids
    processed.clear()
    monkeypatch.setattr(reclassify_items.Command, "_process_item", process)
    second = StringIO()
    call_command("reclassify_items", *args, stdout=second)
    assert f"inspected={len(original_ids) + len(later_ids)}" in second.getvalue()


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("host_filter", [False, True])
@pytest.mark.django_db(transaction=True)
def test_replay_malformed_legacy_url_has_fixed_diagnostic(apply, host_filter):
    item = Item.objects.create(original_url="https://[private-token]/private-path?signature=secret")
    args = [
        "--all",
        *(["--apply"] if apply else []),
        *(["--host", "example.com"] if host_filter else []),
    ]
    if not host_filter:
        output = StringIO()
        call_command("reclassify_items", *args, stdout=output)
        assert "private" not in output.getvalue()
        assert "secret" not in output.getvalue()
        if apply:
            item.refresh_from_db()
            assert item.classification_rule == "default_link"
            assert item.classification_evidence["media_candidates"] == []
        return
    with pytest.raises(CommandError) as error:
        call_command("reclassify_items", *args, stdout=StringIO())
    assert f"item {item.pk}" in str(error.value)
    assert "private" not in str(error.value)
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_replay_rechecks_mutable_selectors_after_independent_commit(tmp_path, journal):
    script = r"""
import django
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from django.db import connections
from django.core.management import call_command

django.setup()
from archive.classification import CURRENT_CLASSIFICATION_ENGINE_VERSION
from archive.management.commands.reclassify_items import Command
from archive.models import Item
call_command("migrate", verbosity=0)
with connections["default"].cursor() as cursor:
    cursor.execute("PRAGMA journal_mode=" + JOURNAL)
original = Command._matching_items
scenarios = [
    (["--host", "example.test"], {"original_url": "https://other.test/story"}),
    (["--rule", "old"], {"classification_rule": "new"}),
    (["--empty-rule"], {"classification_rule": "new"}),
    (["--empty-evidence"], {"classification_evidence": {"manual": True}}),
    (["--stale-only"], {"classification_engine_version": CURRENT_CLASSIFICATION_ENGINE_VERSION}),
]
for args, edit in scenarios:
    Item.objects.all().delete()
    initial = dict(original_url="https://example.test/story", classification_engine_version=1,
                   classification_rule="old" if "--rule" in args else "")
    target = Item.objects.create(**initial)
    sibling = Item.objects.create(**initial)

    def commit_edit():
        try:
            Item.objects.filter(pk=target.pk).update(**edit)
        finally:
            connections.close_all()

    def selected(self, options):
        ids = list(original(self, options))
        assert ids == [target.pk, sibling.pk]
        # A separate SQLite connection commits between selection and the row lock.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(commit_edit).result(timeout=10)
        return iter(ids)

    Command._matching_items = selected
    output = StringIO()
    call_command("reclassify_items", *args, "--apply", "--normalize-downstream", stdout=output)
    target.refresh_from_db()
    for key, value in {**initial, **edit}.items():
        assert getattr(target, key) == value, (args, key, output.getvalue())
    sibling.refresh_from_db()
    assert sibling.classification_engine_version == CURRENT_CLASSIFICATION_ENGINE_VERSION
    assert "inspected=1 changed=1" in output.getvalue(), output.getvalue()
    assert "skipped=1" in output.getvalue(), output.getvalue()
    assert f"SKIPPED item {target.pk}: selector changed" in output.getvalue()
Command._matching_items = original
connections.close_all()
"""
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "DJANGO_DB_PATH": str(tmp_path / "selectors.sqlite3"),
    }
    result = subprocess.run(
        [sys.executable, "-c", "JOURNAL = " + repr(journal) + "\n" + script],
        env=env,
        cwd=Path(__file__).resolve().parents[1] / "src" / "django",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.django_db(transaction=True)
def test_replay_checks_selectors_inside_row_transaction(monkeypatch):
    item = Item.objects.create(original_url="https://example.test/story")
    match = reclassify_items.Command._matches_current_selectors
    checked = []

    def check(self, current, options):
        checked.append(current.pk)
        assert len(transaction.get_connection().atomic_blocks) == 1
        return match(self, current, options)

    monkeypatch.setattr(reclassify_items.Command, "_matches_current_selectors", check)
    call_command("reclassify_items", "--all", "--apply", stdout=StringIO())
    assert checked == [item.pk]


@pytest.mark.django_db(transaction=True)
def test_replay_damaged_url_preserves_override_and_valid_independent_audio():
    item = Item.objects.create(
        original_url="https://[synthetic-secret]/private-path",
        kind=ItemKind.ARTICLE,
        classification_rule="operator_override",
        audio_url="https://example.test/download?id=123",
    )
    output = StringIO()
    call_command("reclassify_items", "--item-id", str(item.pk), "--apply", stdout=output)
    item.refresh_from_db()
    assert item.kind == ItemKind.ARTICLE
    assert item.classification_rule == "operator_override"
    assert item.classification_evidence["selected_media"] == {"audio": item.audio_url, "video": ""}
    assert "synthetic-secret" not in output.getvalue()
    assert "private-path" not in output.getvalue()
    assert "inspected=1 changed=1" in output.getvalue()


@pytest.mark.django_db(transaction=True)
def test_replay_apply_refuses_outer_atomic_before_inventory(django_assert_num_queries):
    item = Item.objects.create(
        original_url="https://example.test/story", classification_engine_version=0
    )
    output = StringIO()
    args = ("reclassify_items", "--item-id", str(item.pk))
    with transaction.atomic():
        with django_assert_num_queries(0), pytest.raises(CommandError, match="autocommit"):
            call_command(*args, "--apply", stdout=output)
        assert output.getvalue() == ""
        call_command(*args, stdout=output)
        assert "WOULD UPDATE" in output.getvalue()
        item.refresh_from_db()
        assert item.classification_engine_version == 0

    class CommitCheckingOutput(StringIO):
        def write(self, value):
            if value.startswith("UPDATED "):
                assert transaction.get_autocommit()
                item.refresh_from_db()
                assert item.classification_engine_version > 0
            return super().write(value)

    committed_output = CommitCheckingOutput()
    call_command(*args, "--apply", stdout=committed_output)
    assert f"UPDATED item {item.pk}" in committed_output.getvalue()


def test_replay_escapes_and_bounds_legacy_classification_diagnostics():
    kind = "legacy\r\n\x1b"
    rule = "legacy\n\x1b[31m\r" + "\u2028" * 45 + "tail"
    item = Item.objects.create(
        original_url="https://example.test/story", kind=kind, classification_rule=rule
    )
    output = StringIO()
    call_command("reclassify_items", "--item-id", str(item.pk), stdout=output)

    rendered = output.getvalue()
    assert "\r" not in rendered
    assert "\x1b" not in rendered
    assert "\u2028" not in rendered
    lines = rendered.splitlines()
    assert f"WOULD UPDATE item {item.pk}: https://example.test/[redacted]" in lines
    assert r"  - kind: legacy\r\n\x1b -> link" in lines
    [rule_line] = [line for line in lines if line.startswith("  - rule: ")]
    assert rule_line.startswith(r"  - rule: legacy\n\x1b[31m\r\u2028")
    assert rule_line.endswith("… -> default_link")
    assert len(rule_line.removeprefix("  - rule: ").removesuffix(" -> default_link")) <= 160
    item.refresh_from_db()
    assert item.kind == kind
    assert item.classification_rule == rule
