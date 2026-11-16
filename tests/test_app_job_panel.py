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

This file starts with the two cases the panel already satisfies, so the
harness lands green and on its own: what an idle panel offers, and that a pid
file with no process behind it is not a live run.
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


def test_a_dead_pid_is_not_a_live_run(radar_app):
    """The zombie bug, in the form a test can reach.

    A pid file alone cannot answer whether a run is going. Here the process is
    simply gone; the panel must offer to start a run, not to stop one.
    """
    radar_app.start("sync", pid=DEAD_PID).run()
    assert "Stop sync" not in radar_app.buttons
    assert "Sync from cloud" in radar_app.buttons
    assert not any("running" in line for line in radar_app.status_lines)
