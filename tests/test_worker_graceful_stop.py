import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

PEER = Path(__file__).with_name("worker_graceful_peer.py")


def test_sigterm_finishes_current_item_and_leaves_next_claim_for_restart(tmp_path):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            str(PEER.parents[1] / path) for path in ("src/django", "src")
        ),
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "DJANGO_DB_PATH": str(tmp_path / "graceful.sqlite3"),
        "JOURNEY_ROOT": str(tmp_path),
    }

    def run(mode):
        result = subprocess.run(
            [sys.executable, str(PEER)],
            env={**env, "JOURNEY_MODE": mode},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    run("setup")
    process = subprocess.Popen(
        [sys.executable, str(PEER)],
        env={**env, "JOURNEY_MODE": "worker"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / "stage.ready").exists():
            if process.poll() is not None:
                out, err = process.communicate(timeout=5)
                pytest.fail(out + err)
            assert time.monotonic() < deadline, "worker did not reach upstream barrier"
            time.sleep(0.01)
        process.send_signal(signal.SIGTERM)
        # The handler's flushed stderr is the independent receipt before release.
        assert process.stderr is not None
        import select

        readable, _, _ = select.select([process.stderr], [], [], 5)
        assert readable, "worker did not acknowledge SIGTERM"
        assert "shutting down after current item" in process.stderr.readline()
        assert process.poll() is None
        (tmp_path / "stage.release").touch()
        out, err = process.communicate(timeout=15)
        assert process.returncode == 0, out + err
        assert "exiting cleanly" in out
        state = json.loads(run("inspect"))
        assert state["successes"] == 1
        assert [row["summary_status"] for row in state["rows"]] == ["complete", "pending"]
        assert [row["short_summary"] for row in state["rows"]] == ["Committed short", ""]
        assert all(row["processing_started_at"] is None for row in state["rows"])
        run("restart")
        state = json.loads(run("inspect"))
        assert state["successes"] == 2
        assert [row["summary_status"] for row in state["rows"]] == ["complete", "complete"]
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
