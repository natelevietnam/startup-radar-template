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
