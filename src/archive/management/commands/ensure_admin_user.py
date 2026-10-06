import os
import sys

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.core.management.base import BaseCommand, CommandError

PASSWORD_ENV_VAR = "ARCHIVE_ADMIN_PASSWORD"


class Command(BaseCommand):
    help = (
        "Create or update the archive admin/editor user. The password is read from the "
        f"{PASSWORD_ENV_VAR} environment variable or, with --password-stdin, from the first "
        "line of standard input, so it never appears in the process list or deploy logs."
    )
    stealth_options = ("stdin",)

    def add_arguments(self, parser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument(
            "--password-stdin",
            action="store_true",
            help="Read the password from the first line of standard input.",
        )
        parser.add_argument(
            "--password",
            default=None,
            help=(
                f"Deprecated: visible in the process list. Use {PASSWORD_ENV_VAR} or "
                "--password-stdin instead."
            ),
        )
        parser.add_argument("--email", default="")

    def handle(self, *args, **options) -> None:
        username = options["username"].strip()
        email = options["email"].strip()

        if not username:
            raise CommandError("username must not be empty")
        password = self._resolve_password(options)
        if not password:
            raise CommandError("password must not be empty")

        user_model = get_user_model()
        user, created = user_model.objects.get_or_create(
            username=username,
            defaults={"email": email, "is_staff": True, "is_superuser": True},
        )
        user.email = email
        user.is_staff = True
        user.is_superuser = True
        # Re-hashing an unchanged password changes the session auth hash and logs the
        # user out everywhere, so only set it when it actually differs. The plain hasher
        # check is used on purpose: user.check_password() would also re-hash and save a
        # matching password whose hasher settings are outdated.
        password_changed = created or not check_password(password, user.password)
        if password_changed:
            user.set_password(password)
        user.save()

        action = "Created" if created else "Updated"
        detail = "" if created or password_changed else " (password unchanged)"
        self.stdout.write(self.style.SUCCESS(f"{action} admin user '{username}'{detail}"))

    def _resolve_password(self, options) -> str:
        argv_password = options["password"]
        if options["password_stdin"]:
            if argv_password is not None:
                raise CommandError("use either --password or --password-stdin, not both")
            stdin = options.get("stdin") or sys.stdin
            return stdin.readline().removesuffix("\n").removesuffix("\r")
        if argv_password is not None:
            self.stderr.write(
                "Warning: --password is deprecated because it exposes the password in the "
                f"process list; set {PASSWORD_ENV_VAR} or use --password-stdin instead."
            )
            return argv_password
        password = os.environ.get(PASSWORD_ENV_VAR)
        if password is None:
            raise CommandError(
                f"no password given: set {PASSWORD_ENV_VAR} or pass --password-stdin"
            )
        return password
