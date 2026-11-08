import io

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import PBKDF2PasswordHasher
from django.core.management import call_command
from django.core.management.base import CommandError

pytestmark = pytest.mark.django_db


def run(*args, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    kwargs = {"stdout": out, "stderr": err}
    if stdin is not None:
        kwargs["stdin"] = io.StringIO(stdin)
    call_command("ensure_admin_user", *args, **kwargs)
    return out.getvalue(), err.getvalue()


def test_reads_password_from_env(monkeypatch):
    monkeypatch.setenv("ARCHIVE_ADMIN_PASSWORD", "env-secret")

    out, err = run("--username", "owner", "--email", "owner@example.com")

    user = get_user_model().objects.get(username="owner")
    assert user.check_password("env-secret")
    assert user.is_staff and user.is_superuser
    assert user.email == "owner@example.com"
    assert "Created admin user 'owner'" in out
    assert "env-secret" not in out + err
    assert err == ""


def test_reads_password_from_stdin(monkeypatch):
    monkeypatch.setenv("ARCHIVE_ADMIN_PASSWORD", "ignored")

    run("--username", "owner", "--password-stdin", stdin="it's a 'quoted' secret\n")

    user = get_user_model().objects.get(username="owner")
    assert user.check_password("it's a 'quoted' secret")


def test_stdin_strips_only_the_line_ending():
    run("--username", "owner", "--password-stdin", stdin="  padded  \r\nsecond line\n")

    assert get_user_model().objects.get(username="owner").check_password("  padded  ")


def test_unchanged_password_is_not_rehashed(monkeypatch):
    monkeypatch.setenv("ARCHIVE_ADMIN_PASSWORD", "env-secret")
    run("--username", "owner")
    user = get_user_model().objects.get(username="owner")
    original_hash = user.password
    original_session_hash = user.get_session_auth_hash()

    out, _ = run("--username", "owner", "--email", "new@example.com")

    user.refresh_from_db()
    assert user.password == original_hash
    assert user.get_session_auth_hash() == original_session_hash
    assert user.email == "new@example.com"
    assert "Updated admin user 'owner' (password unchanged)" in out


def test_matching_password_with_outdated_hash_is_not_upgraded(monkeypatch):
    user = get_user_model().objects.create_user(username="owner", is_staff=True)
    user.password = PBKDF2PasswordHasher().encode("env-secret", "fixedsalt", iterations=1000)
    user.save()
    original_hash = user.password
    original_session_hash = user.get_session_auth_hash()
    monkeypatch.setenv("ARCHIVE_ADMIN_PASSWORD", "env-secret")

    out, _ = run("--username", "owner")

    user.refresh_from_db()
    assert user.password == original_hash
    assert user.get_session_auth_hash() == original_session_hash
    assert "(password unchanged)" in out


def test_changed_password_is_rehashed(monkeypatch):
    monkeypatch.setenv("ARCHIVE_ADMIN_PASSWORD", "old-secret")
    run("--username", "owner")
    monkeypatch.setenv("ARCHIVE_ADMIN_PASSWORD", "new-secret")

    out, _ = run("--username", "owner")

    user = get_user_model().objects.get(username="owner")
    assert user.check_password("new-secret")
    assert not user.check_password("old-secret")
    assert "(password unchanged)" not in out


def test_existing_user_is_promoted(monkeypatch):
    get_user_model().objects.create_user(username="owner", password="env-secret")
    monkeypatch.setenv("ARCHIVE_ADMIN_PASSWORD", "env-secret")

    run("--username", "owner")

    user = get_user_model().objects.get(username="owner")
    assert user.is_staff and user.is_superuser


def test_deprecated_password_argument_still_works_with_warning(monkeypatch):
    monkeypatch.delenv("ARCHIVE_ADMIN_PASSWORD", raising=False)

    out, err = run("--username", "owner", "--password", "argv-secret")

    assert get_user_model().objects.get(username="owner").check_password("argv-secret")
    assert "--password is deprecated" in err
    assert "argv-secret" not in out + err


def test_password_argument_overrides_env(monkeypatch):
    monkeypatch.setenv("ARCHIVE_ADMIN_PASSWORD", "env-secret")

    run("--username", "owner", "--password", "argv-secret")

    assert get_user_model().objects.get(username="owner").check_password("argv-secret")


def test_missing_password_fails(monkeypatch):
    monkeypatch.delenv("ARCHIVE_ADMIN_PASSWORD", raising=False)

    with pytest.raises(CommandError, match="ARCHIVE_ADMIN_PASSWORD"):
        run("--username", "owner")
    assert not get_user_model().objects.filter(username="owner").exists()


@pytest.mark.parametrize(
    ("args", "env", "stdin"),
    [
        ((), "", None),
        (("--password-stdin",), None, "\n"),
        (("--password", ""), None, None),
    ],
)
def test_empty_password_fails(monkeypatch, args, env, stdin):
    if env is None:
        monkeypatch.delenv("ARCHIVE_ADMIN_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("ARCHIVE_ADMIN_PASSWORD", env)

    with pytest.raises(CommandError, match="password must not be empty"):
        run("--username", "owner", *args, stdin=stdin)
    assert not get_user_model().objects.filter(username="owner").exists()


def test_password_and_password_stdin_conflict():
    with pytest.raises(CommandError, match="not both"):
        run("--username", "owner", "--password", "x", "--password-stdin", stdin="y\n")


def test_blank_username_fails(monkeypatch):
    monkeypatch.setenv("ARCHIVE_ADMIN_PASSWORD", "env-secret")

    with pytest.raises(CommandError, match="username must not be empty"):
        run("--username", "   ")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("fault", ["hash", "save"])
def test_failed_provisioning_rolls_back_new_account(monkeypatch, fault):
    user_model = get_user_model()
    monkeypatch.setenv("ARCHIVE_ADMIN_PASSWORD", "synthetic-secret")
    original_save = user_model.save
    fault_seen = []

    def fail_hash(self, password):
        fault_seen.append(True)
        raise RuntimeError("hash failed")

    def fail_save(self, *args, **kwargs):
        if not kwargs.get("force_insert"):
            fault_seen.append(True)
            raise RuntimeError("save failed")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(
        user_model,
        "set_password" if fault == "hash" else "save",
        fail_hash if fault == "hash" else fail_save,
    )
    with pytest.raises(RuntimeError, match=f"{fault} failed"):
        run("--username", "owner")
    assert fault_seen
    assert not user_model.objects.filter(username="owner").exists()


@pytest.mark.django_db(transaction=True)
def test_provisioning_locks_current_row_and_saves_only_changed_fields(monkeypatch):
    from django.db import connection
    from django.db.models.query import QuerySet

    user_model = get_user_model()
    user = user_model.objects.create_user(username="owner", password="old-secret")
    original_lock = QuerySet.select_for_update
    original_save = user_model.save
    locks, saved_fields = [], []
    monkeypatch.setenv("ARCHIVE_ADMIN_PASSWORD", "current-secret")

    def lock_current(self, *args, **kwargs):
        if self.model is user_model:
            assert connection.in_atomic_block
            # An edit committed before lock acquisition must be read by provisioning.
            current = user_model.objects.get(pk=user.pk)
            current.set_password("current-secret")
            user_model.objects.filter(pk=user.pk).update(
                password=current.password, is_active=False, first_name="Current"
            )
            locks.append(current.password)
        return original_lock(self, *args, **kwargs)

    def save_fields(self, *args, **kwargs):
        saved_fields.append(set(kwargs.get("update_fields") or ()))
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", lock_current)
    monkeypatch.setattr(user_model, "save", save_fields)
    run("--username", "owner", "--email", "owner@example.com")
    user.refresh_from_db()
    assert locks == [user.password]
    assert user.is_active is False
    assert user.first_name == "Current"
    assert saved_fields == [{"email", "is_staff", "is_superuser"}]
