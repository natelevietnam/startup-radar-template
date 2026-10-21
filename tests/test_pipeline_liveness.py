"""A finished pipeline must read as finished, even before anyone reaps it.

The dashboard's "Run pipeline now" button spawns main.py as a subprocess and
then, on every rerun, asks a file on disk whether that run is still going. The
Popen handle is deliberately NOT kept: session state is per-browser-session and
a reload destroys it, so liveness has to survive losing it.

The consequence is that nothing ever calls wait() on the child. When the run
ends, the child is not reaped and stays in the process table as a ZOMBIE — and
os.kill(pid, 0) SUCCEEDS on a zombie, because the pid is still there. The first
version of the liveness check used exactly that signal, so it reported a
finished run as running, forever:

    $ ps -o pid,command -p 9551
     9551 <defunct>

and the sidebar sat on "Pipeline running — pid 9551" indefinitely.

These tests use a real zombie rather than a mock, because the whole point is
what the operating system does, not what a stub says it does.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest


def _state(pid: int) -> str:
    return subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()


def is_zombie(pid: int) -> bool:
    """Mirror of app._is_zombie. app.py cannot be imported (it runs Streamlit)."""
    try:
        out = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return out.upper().startswith("Z")


@pytest.fixture()
def zombie():
    """A real, unreaped, exited child."""
    proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    for _ in range(50):
        if _state(proc.pid).upper().startswith("Z"):
            break
        time.sleep(0.05)
    yield proc.pid
    try:
        os.waitpid(proc.pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def test_signal_zero_cannot_tell_a_zombie_from_a_runner(zombie):
    """Pins WHY the first check was wrong, so nobody reinstates it."""
    os.kill(zombie, 0)          # does not raise — the pid still exists


def test_a_zombie_is_recognised_as_finished(zombie):
    assert is_zombie(zombie) is True


def test_a_live_process_is_not_a_zombie():
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 5"])
    try:
        assert is_zombie(proc.pid) is False
    finally:
        proc.kill()
        proc.wait()


def test_reaping_clears_the_zombie(zombie):
    assert is_zombie(zombie) is True
    os.waitpid(zombie, os.WNOHANG)
    assert _state(zombie) == "", "pid should be gone from the process table"


@pytest.mark.parametrize("code", [0, 1, 2])
def test_the_shell_records_main_pys_exit_code(tmp_path, code):
    """Completion is a fact on disk, not an inference about a pid.

    The run that exposed all this exited NON-ZERO — five sources failed on a
    DNS outage — so the failure path matters as much as the success path.
    """
    import shlex
    import sys
    done = tmp_path / "run.done"
    cmd = "{py} -c 'import sys; sys.exit({c})'; printf %s $? > {d}".format(
        py=shlex.quote(sys.executable), c=code, d=shlex.quote(str(done)))
    proc = subprocess.Popen(["/bin/sh", "-c", cmd],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    proc.wait()
    for _ in range(50):
        if done.exists():
            break
        time.sleep(0.05)
    assert done.read_text().strip() == str(code)


def test_the_done_file_settles_it_without_consulting_the_pid(tmp_path):
    """With an exit code on disk, a stale or reused pid cannot matter."""
    done = tmp_path / "run.done"
    done.write_text("0")
    # _pipeline_pid checks PIPELINE_DONE first and returns None regardless of pid.
    assert done.exists() and done.read_text().strip() == "0"


# --- two jobs, one copy of the machinery -----------------------------------
#
# The sidebar runs two subprocesses with identical liveness rules: "pipeline"
# (main.py, which fetches from the sources) and "sync" (sync_from_cloud.py,
# which imports what the cloud cron already found). They share _job_pid and
# friends rather than owning a copy each, because the zombie rule above is
# subtle enough that a second copy would drift.

import pathlib


def job_files(root: pathlib.Path, job: str):
    """Mirror of app._job_files."""
    cache = root / ".cache"
    stem = "pipeline_run" if job == "pipeline" else f"{job}_run"
    return (cache / f"{stem}.log", cache / f"{stem}.pid", cache / f"{stem}.done")


def test_the_pipeline_keeps_its_original_filenames(tmp_path):
    """Backwards compatibility, and it is not cosmetic.

    A run started by the previous version wrote .cache/pipeline_run.{log,pid}.
    If adding a second job had renamed those to pipeline/pipeline.log, an
    in-flight run would have been orphaned at upgrade: no pid file where the new
    code looks, so it reports nothing, while the old process keeps going.
    """
    log, pid, done = job_files(tmp_path, "pipeline")
    assert log.name == "pipeline_run.log"
    assert pid.name == "pipeline_run.pid"
    assert done.name == "pipeline_run.done"


def test_each_job_gets_its_own_files(tmp_path):
    """Otherwise one job's exit code would be read as the other's."""
    p = set(job_files(tmp_path, "pipeline"))
    s = set(job_files(tmp_path, "sync"))
    assert p.isdisjoint(s)
    assert job_files(tmp_path, "sync")[0].name == "sync_run.log"


def test_one_jobs_done_file_does_not_answer_for_the_other(tmp_path):
    """The bug this shape prevents: sync finishing marks the pipeline finished."""
    (tmp_path / ".cache").mkdir()
    _, _, pipe_done = job_files(tmp_path, "pipeline")
    _, _, sync_done = job_files(tmp_path, "sync")
    sync_done.write_text("0")
    assert sync_done.exists() and not pipe_done.exists()


# --- the outcome must outlive the browser session ---------------------------
#
# The first version stored each job's verdict in st.session_state and POPPED it
# on display. That made it visible exactly once, to exactly one browser session,
# and only if that session was connected at the moment the job ended. A sync
# that finished while the tab was closed reported nothing: the sidebar still
# read "running" on the next visit, over a job that had been done for six
# minutes. Reading it from the exit-code file instead gives every session the
# same answer at any time, and _start_job deletes that file when the next run
# begins, so "the last outcome" cannot go stale.


def job_outcome(root: pathlib.Path, job: str):
    """Mirror of app._job_outcome."""
    _log, _pid, done = job_files(root, job)
    try:
        return int(done.read_text().strip())
    except (OSError, ValueError):
        return None


@pytest.mark.parametrize("job", ["pipeline", "sync"])
def test_both_jobs_report_an_outcome(job, tmp_path):
    """The renderer is shared, so neither job can report and the other not."""
    (tmp_path / ".cache").mkdir(exist_ok=True)
    _l, _p, done = job_files(tmp_path, job)
    done.write_text("0")
    assert job_outcome(tmp_path, job) == 0


@pytest.mark.parametrize("rc,expected", [("0", 0), ("1", 1), ("2", 2)])
def test_the_exit_code_is_the_verdict(rc, expected, tmp_path):
    (tmp_path / ".cache").mkdir(exist_ok=True)
    _l, _p, done = job_files(tmp_path, "sync")
    done.write_text(rc)
    assert job_outcome(tmp_path, "sync") == expected


def test_the_outcome_survives_being_read_twice(tmp_path):
    """It was popped before, so a reload lost it. It must not be consumed."""
    (tmp_path / ".cache").mkdir(exist_ok=True)
    _l, _p, done = job_files(tmp_path, "sync")
    done.write_text("0")
    assert job_outcome(tmp_path, "sync") == 0
    assert job_outcome(tmp_path, "sync") == 0, "reading the outcome must not clear it"


def test_no_outcome_before_a_job_has_ever_run(tmp_path):
    (tmp_path / ".cache").mkdir(exist_ok=True)
    assert job_outcome(tmp_path, "pipeline") is None


def test_starting_a_run_clears_the_previous_outcome(tmp_path):
    """Otherwise a running job would show the last run's verdict beside it."""
    (tmp_path / ".cache").mkdir(exist_ok=True)
    _l, _p, done = job_files(tmp_path, "pipeline")
    done.write_text("1")
    assert job_outcome(tmp_path, "pipeline") == 1
    done.unlink(missing_ok=True)          # what _start_job does
    assert job_outcome(tmp_path, "pipeline") is None


def test_one_jobs_outcome_is_not_the_others(tmp_path):
    (tmp_path / ".cache").mkdir(exist_ok=True)
    _l, _p, sync_done = job_files(tmp_path, "sync")
    sync_done.write_text("0")
    assert job_outcome(tmp_path, "sync") == 0
    assert job_outcome(tmp_path, "pipeline") is None
