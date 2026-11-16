"""The dashboard's job panel, which has been wrong three times.

Every one of those bugs read the same way in the browser — the panel says a run
is still going, or never says it ended — and none of them could be seen by any
other test in this suite, because nothing drove `app.py` at all.

  * a finished child is never reaped, so it sits in the process table as a
    zombie, and `os.kill(pid, 0)` succeeds on a zombie: a completed run
    reported itself as running forever;
  * the refresh was a poll at the bottom of the script — sleep 3, st.rerun() —
    which restarted the script from the top before the page body had drawn. The
    dashboard was blank for the whole length of a run;
  * and the poll's own liveness check sat 1,300 lines below the panel's, so a
    run that ended in between was drawn as live and then never redrawn, because
    the poll saw no live job and scheduled nothing further.

The cases below pin the behaviour the current panel owes: it says what is
running, it keeps the board on screen while it runs, and it announces an ending
exactly once, naming what happened.
"""

from __future__ import annotations

import os

DEAD_PID = 4_000_000        # above every pid macOS will issue
SYNC_LOG = ("Finding latest successful cloud run...\n"
            "  Run 43741514826\n"
            "  job_matches: +14 new, 0 already present, 2390 skipped\n"
            "Done.\n")


# --- what the panel says ----------------------------------------------------
def test_an_idle_panel_offers_both_jobs(radar_app):
    radar_app.run()
    assert radar_app.buttons == ["Sync from cloud", "Run pipeline now"]
    assert radar_app.status_lines == []
    assert radar_app.toasts == []


def test_a_live_run_says_so_and_offers_to_stop(radar_app):
    radar_app.start("sync").run()
    assert f"Cloud sync running — pid {os.getpid()}" in radar_app.status_lines
    assert "Stop sync" in radar_app.buttons
    assert "Run pipeline now" in radar_app.buttons


def test_a_dead_pid_is_not_a_live_run(radar_app):
    """The zombie bug, in the form a test can reach.

    A pid file alone cannot answer whether a run is going. Here the process is
    simply gone; the panel must offer to start a run, not to stop one.
    """
    radar_app.start("sync", pid=DEAD_PID).run()
    assert "Stop sync" not in radar_app.buttons
    assert "Sync from cloud" in radar_app.buttons
    assert not any("running" in line for line in radar_app.status_lines)


# --- the blank dashboard ----------------------------------------------------
def test_the_board_still_renders_while_a_job_runs(radar_app):
    """The bug that made the dashboard useless for the length of a run.

    The old poll rescheduled the script from the bottom, so a rerun reached
    st.rerun() before the page body had drawn and the body never did. Nothing
    about a running job may cost the page its content.
    """
    radar_app.start("pipeline").run()
    assert "Startup Radar" in radar_app.titles
    assert "Stop pipeline" in radar_app.buttons


# --- announcing the ending --------------------------------------------------
def test_a_finished_run_announces_itself(radar_app):
    radar_app.start("sync").run()
    assert radar_app.toasts == []       # nothing to announce yet

    radar_app.finish("sync", log=SYNC_LOG).run()
    assert any("Cloud sync finished" in t for t in radar_app.toasts), radar_app.toasts
    assert any("Cloud sync finished" in line for line in radar_app.status_lines)


def test_the_announcement_carries_the_runs_result_line(radar_app):
    """A toast that only says "finished" makes you go and look anyway."""
    radar_app.start("sync").run()
    radar_app.finish("sync", log=SYNC_LOG).run()
    assert any("job_matches: +14 new" in t for t in radar_app.toasts), radar_app.toasts


def test_an_ending_is_announced_exactly_once(radar_app):
    """The queue is drained by the run that raises it, so a later draw is quiet.

    Otherwise every refresh for the rest of the session would re-announce the
    same completed run.
    """
    radar_app.start("sync").run()
    radar_app.finish("sync", log=SYNC_LOG).run()
    assert radar_app.toasts

    radar_app.run()
    assert radar_app.toasts == []


def test_a_failed_run_names_the_exit_code(radar_app):
    radar_app.start("pipeline").run()
    radar_app.finish("pipeline", rc=1, log="Traceback...\nValueError\n").run()
    assert any("failed (exit 1)" in t for t in radar_app.toasts), radar_app.toasts
    assert any("failed" in line for line in radar_app.status_lines)


def test_a_run_stopped_by_hand_says_nothing(radar_app):
    """No exit code was written, so there is no outcome to report.

    Announcing "finished" for a run Nate killed himself would be a lie, and
    announcing "failed" would be a worse one.
    """
    radar_app.start("sync").run()
    radar_app.stop_by_hand("sync").run()
    assert radar_app.toasts == []
    assert "Sync from cloud" in radar_app.buttons


# --- two jobs, one panel ----------------------------------------------------
def test_one_job_running_holds_the_other_back(radar_app):
    """Both write job_matches, and two SQLite writers is how a run dies."""
    radar_app.start("sync").run()
    disabled = {b.label: b.disabled for b in radar_app.at.sidebar.button}
    assert disabled["Run pipeline now"] is True
    assert disabled["Stop sync"] is False


def test_one_job_ending_does_not_speak_for_the_other(radar_app):
    radar_app.start("sync").start("pipeline").run()
    radar_app.finish("sync", log=SYNC_LOG).run()
    assert any("Cloud sync finished" in t for t in radar_app.toasts), radar_app.toasts
    assert not any("Pipeline" in t for t in radar_app.toasts), radar_app.toasts
    assert "Stop pipeline" in radar_app.buttons
