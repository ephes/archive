from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PEER = Path(__file__).with_name("worker_recovery_peer.py")
BASE = datetime(2026, 11, 8, 12, tzinfo=UTC)


@pytest.mark.parametrize("point", ["after-claim", "before-publication"])
def test_killed_worker_reopens_recovers_once_and_fences_late_media(tmp_path, point):
    # Every process connects independently to this synthetic on-disk database.
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            str(PEER.parents[1] / path) for path in ("src/django", "src")
        ),
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "DJANGO_DB_PATH": str(tmp_path / "worker.sqlite3"),
        "JOURNEY_ROOT": str(tmp_path),
        "JOURNEY_POINT": point,
    }
    cwd = PEER.parents[1] / "src" / "django"

    def command_env(mode, seconds):
        return {
            **env,
            "JOURNEY_MODE": mode,
            "JOURNEY_NOW": (BASE + timedelta(seconds=seconds)).isoformat(),
        }

    def run(mode, seconds=0):
        result = subprocess.run(
            [sys.executable, str(PEER)],
            env=command_env(mode, seconds),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    def inspect():
        return json.loads(run("inspect").stdout)

    def start(mode, seconds=0):
        return subprocess.Popen(
            [sys.executable, str(PEER)],
            env=command_env(mode, seconds),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def ready(process, mode):
        receipt = tmp_path / f"{mode}.ready"
        deadline = time.monotonic() + 15
        while not receipt.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=5)
                pytest.fail(stdout + stderr)
            if time.monotonic() > deadline:
                pytest.fail(f"{mode} did not reach its barrier")
            time.sleep(0.02)
        return json.loads(receipt.read_text())

    run("setup")
    killed = start("killed")
    late = None
    try:
        abandoned = ready(killed, "killed")
        assert abandoned["started_at"] == BASE.isoformat()
        # Popen.kill is the portable hard-kill API (SIGKILL on POSIX).
        killed.kill()
        killed.communicate(timeout=10)
        assert killed.returncode != 0
        fresh = run("probe", seconds=60)
        assert "Processed 0 item(s)." in fresh.stdout
        assert "Recovered" not in fresh.stderr
        assert inspect()["started_at"] == abandoned["started_at"]

        late = start("late", seconds=60)
        delayed = ready(late, "late")
        assert delayed["started_at"] == abandoned["started_at"]
        restarted = run("restart", seconds=121)
        assert "Recovered 1 stale processing item(s)" in restarted.stderr
        assert "Processed 1 item(s)." in restarted.stdout
        published = inspect()
        assert published["status"] == "complete"
        assert published["started_at"] is None
        assert published["bytes"] == "restart"
        assert published["shared_bytes"] == "referenced bytes"
        assert published["successes"] == 1

        (tmp_path / "late.release").touch()
        stdout, stderr = late.communicate(timeout=15)
        assert late.returncode == 0, stdout + stderr
        after = inspect()
        assert after["audio"] == published["audio"]
        assert after["bytes"] == "restart"
        assert after["shared_bytes"] == "referenced bytes"
        assert after["successes"] == 1
        assert not any(delayed["staged"].startswith(prefix) for prefix in after["reservations"])
        assert not (tmp_path / "media" / delayed["staged"]).exists()
        # A killed upload stays reserved for the existing orphan-recovery workflow.
        if abandoned["staged"]:
            assert any(abandoned["staged"].startswith(prefix) for prefix in after["reservations"])
            assert (tmp_path / "media" / abandoned["staged"]).exists()
        again = run("probe", seconds=122)
        assert "Recovered" not in again.stderr
        assert "Processed 0 item(s)." in again.stdout
    finally:
        for process in (killed, late):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)
