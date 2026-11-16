"""A harness for driving app.py under `streamlit.testing`.

`app.py` had no automated coverage at all, which is uncomfortable for the one
file whose bugs are invisible to every other test: the dashboard's job panel has
been wrong three times — a zombie pid read as a live run, a poll that left the
page blank for the length of a run, and a finished run that never said so — and
each was found by watching the browser.

`AppTest` runs the real script in-process and exposes the element tree, so those
are testable. Two things have to be arranged first.

**The app is copied into a temp directory.** `PROJECT_DIR` is
`Path(__file__).parent`, and every file the job panel reads — the pid, the exit
code, the log — lives in `PROJECT_DIR/.cache`. Driving the checked-in file would
mean tests writing pid files that a dashboard Nate has open reads as live runs,
and `.cache` litter in the working tree. A copy puts `.cache` and `reports/`
inside `tmp_path` instead. The copy is byte-identical and still imports
`database` and `config_loader` off `sys.path`, so the code under test is the
real app, not a stub of it.

**The database is a fresh temp file.** `app.py` reads its path from the config
at import, so `load_config` is patched and `database.DB_PATH` restored
afterwards; a test that left the global pointing at a temp file would silently
redirect every later test in the session.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import config_loader
import database

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class RadarApp:
    """One app.py under test, with its job files on disk where it expects them."""

    def __init__(self, at, home: Path):
        self.at = at
        self.home = home
        self.cache = home / ".cache"
        self.cache.mkdir(exist_ok=True)

    # --- the three files a job writes ---------------------------------------
    def _files(self, job: str):
        stem = "pipeline_run" if job == "pipeline" else f"{job}_run"
        return (self.cache / f"{stem}.log", self.cache / f"{stem}.pid",
                self.cache / f"{stem}.done")

    def start(self, job: str, log: str = "working...\n", pid: int | None = None):
        """Make `job` look like a live run.

        The default pid is this test process, which is the only pid a test can
        be sure is alive and is not a zombie — the two things `_job_pid` checks
        before it believes a pid file.
        """
        log_file, pid_file, done = self._files(job)
        done.unlink(missing_ok=True)
        log_file.write_text(log)
        pid_file.write_text(str(os.getpid() if pid is None else pid))
        return self

    def finish(self, job: str, rc: int = 0, log: str | None = None):
        """End `job` the way a real run ends: an exit code on disk."""
        log_file, _pid, done = self._files(job)
        if log is not None:
            log_file.write_text(log)
        done.write_text(str(rc))
        return self

    def stop_by_hand(self, job: str):
        """The Stop button's effect: the pid file goes, no exit code is written."""
        _log, pid_file, done = self._files(job)
        pid_file.unlink(missing_ok=True)
        done.unlink(missing_ok=True)
        return self

    # --- reading the page ---------------------------------------------------
    def run(self):
        self.at.run()
        return self

    @property
    def toasts(self) -> list[str]:
        return [t.value for t in self.at.toast]

    @property
    def status_lines(self) -> list[str]:
        side = self.at.sidebar
        return ([i.value for i in side.info] + [s.value for s in side.success]
                + [e.value for e in side.error])

    @property
    def buttons(self) -> list[str]:
        return [b.label for b in self.at.sidebar.button]

    @property
    def titles(self) -> list[str]:
        return [t.value for t in self.at.title]


@pytest.fixture
def radar_app(tmp_path, monkeypatch):
    """A copy of app.py in `tmp_path`, pointed at an empty database."""
    from streamlit.testing.v1 import AppTest

    shutil.copy(PROJECT_ROOT / "app.py", tmp_path / "app.py")
    db_path = tmp_path / "radar-test.db"
    monkeypatch.setattr(
        config_loader, "load_config",
        lambda *a, **k: {"output": {"sqlite": {"path": str(db_path)}}})
    original_db = database.DB_PATH
    try:
        yield RadarApp(AppTest.from_file(str(tmp_path / "app.py"), default_timeout=60),
                       tmp_path)
    finally:
        database.DB_PATH = original_db
