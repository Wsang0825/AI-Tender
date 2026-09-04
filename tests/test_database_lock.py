import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from threading import Event, Thread

from tender_ai.storage.database import create_engine_for, database_write_lock


def test_database_write_lock_is_reentrant(tmp_path):
    engine = create_engine_for(tmp_path / "reentrant.db")
    with database_write_lock(engine, timeout_seconds=2, purpose="outer"):
        with database_write_lock(engine, timeout_seconds=2, purpose="inner"):
            assert (tmp_path / "reentrant.db.lock").exists()


def test_database_write_lock_queues_another_process(tmp_path):
    database = tmp_path / "queue.db"
    ready = tmp_path / "holder.ready"
    release = tmp_path / "holder.release"
    script = textwrap.dedent(
        """
        import sys
        import time
        from pathlib import Path

        from tender_ai.storage.database import create_engine_for, database_write_lock

        database = Path(sys.argv[1])
        ready = Path(sys.argv[2])
        release = Path(sys.argv[3])
        with database_write_lock(create_engine_for(database), timeout_seconds=10, purpose="subprocess-holder"):
            ready.write_text("ready", encoding="utf-8")
            while not release.exists():
                time.sleep(0.05)
        """
    )
    child_environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    child_environment["PYTHONPATH"] = source_root + os.pathsep + child_environment.get("PYTHONPATH", "")
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(database), str(ready), str(release)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=child_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    acquired = Event()
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                stderr = child.stderr.read() if child.stderr else ""
                raise AssertionError(f"database lock holder exited early: {stderr}")
            time.sleep(0.05)
        assert ready.exists()

        started = time.monotonic()

        def wait_for_lock():
            with database_write_lock(create_engine_for(database), timeout_seconds=10, purpose="queued-process"):
                acquired.set()

        worker = Thread(target=wait_for_lock)
        worker.start()
        assert not acquired.wait(0.5)
        release.write_text("release", encoding="utf-8")
        assert acquired.wait(10)
        worker.join(10)
        assert not worker.is_alive()
        assert time.monotonic() - started >= 0.4
    finally:
        release.touch()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=10)
        if child.stderr:
            child.stderr.close()
        if child.stdout:
            child.stdout.close()
