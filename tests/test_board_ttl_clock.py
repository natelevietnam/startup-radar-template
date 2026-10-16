"""ats_boards timestamps and the TTL cutoff must be read off the same clock.

upsert_board filled two columns of one row from two different clocks:
last_attempt_at from SQLite's datetime('now'), which is UTC, and resolved_at
from Python's datetime.now(), which is local. Live rows were a clean 7 hours
apart on PDT — same instant, same INSERT, two values.

companies_needing_resolution then compared the UTC column against a cutoff
built from local time. West of Greenwich the stored values look NEWER than they
are by the whole offset, so a company was skipped for BOARD_TTL_DAYS plus the
offset; east of Greenwich it would be retried early. Either way
BOARD_TTL_DAYS did not mean what it says, and the error moved with the
machine's timezone, which is the kind of bug that never reproduces anywhere.

These tests pin the invariant rather than the arithmetic: whatever clock the
column is written in, the cutoff must agree with it. The timezone is forced to
a fixed non-UTC offset so a machine that happens to run in UTC cannot pass
these by accident.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

import database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    return path


@pytest.fixture()
def pdt(monkeypatch):
    """Run the test on a clock that is NOT UTC, or it proves nothing."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


def _row(db, canon):
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM ats_boards WHERE canon = ?", (canon,)).fetchone()
    con.close()
    return dict(r)


def test_both_timestamps_in_one_row_agree(db, pdt):
    """resolved_at and last_attempt_at describe the same instant."""
    database.upsert_board("acme", "Acme", "ashby", "acme")
    r = _row(db, "acme")
    a = datetime.strptime(r["resolved_at"], "%Y-%m-%d %H:%M:%S")
    b = datetime.strptime(r["last_attempt_at"], "%Y-%m-%d %H:%M:%S")
    assert abs((a - b).total_seconds()) < 5, (
        f"same INSERT, two clocks: resolved_at={a} last_attempt_at={b}")


def test_utc_now_matches_sqlites_own_now(db, pdt):
    """The helper has to track datetime('now'), not local time."""
    con = sqlite3.connect(str(db))
    sqlite_now = con.execute("SELECT datetime('now')").fetchone()[0]
    con.close()
    a = datetime.strptime(database._utc_now(), "%Y-%m-%d %H:%M:%S")
    b = datetime.strptime(sqlite_now, "%Y-%m-%d %H:%M:%S")
    assert abs((a - b).total_seconds()) < 5


def test_a_just_attempted_company_is_inside_the_ttl(db, pdt):
    """The regression itself: a fresh attempt must not come back as pending."""
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO job_matches (company_name, role_title, date_found) "
                "VALUES ('Acme', 'Product Manager', '2026-10-01')")
    con.commit()
    con.close()
    assert any(c["canon"] == "acme" for c in database.companies_needing_resolution(limit=0))

    database.upsert_board("acme", "Acme", "ashby", "acme")
    pending = {c["canon"] for c in database.companies_needing_resolution(limit=0)}
    assert "acme" not in pending, (
        "a company attempted seconds ago is being re-attempted — the cutoff and "
        "the stored timestamp are on different clocks")


def test_an_attempt_older_than_the_ttl_is_pending_again(db, pdt):
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO job_matches (company_name, role_title, date_found) "
                "VALUES ('Acme', 'Product Manager', '2026-10-01')")
    stale = (datetime.now(timezone.utc).replace(tzinfo=None)
             - timedelta(days=database.BOARD_TTL_DAYS + 1)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute("INSERT INTO ats_boards (canon, company_name, provider, token, "
                "resolved_at, last_attempt_at, attempts) "
                "VALUES ('acme','Acme',NULL,NULL,NULL,?,1)", (stale,))
    con.commit()
    con.close()
    pending = {c["canon"] for c in database.companies_needing_resolution(limit=0)}
    assert "acme" in pending


@pytest.mark.parametrize("hours_past_ttl", [1, 3, 6])
def test_an_attempt_just_past_the_ttl_is_pending(db, pdt, hours_past_ttl):
    """The ONLY window where the two-clock bug was observable.

    A 6-day-old or a 9-day-old attempt came out right even with the bug, which
    is why the first version of this file passed against it. The skew is the
    local UTC offset, so the wrong answer lives strictly between
    BOARD_TTL_DAYS and BOARD_TTL_DAYS + offset: on PDT (UTC-7) an attempt 7
    days and 3 hours old compared a UTC timestamp against a local cutoff and
    still read as fresh, so the company stayed un-retried for up to 7 extra
    hours. Parameterised across the offset so the test is about the invariant,
    not about one magic number.
    """
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO job_matches (company_name, role_title, date_found) "
                "VALUES ('Acme', 'Product Manager', '2026-10-01')")
    past = (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=database.BOARD_TTL_DAYS, hours=hours_past_ttl)
            ).strftime("%Y-%m-%d %H:%M:%S")
    con.execute("INSERT INTO ats_boards (canon, company_name, provider, token, "
                "resolved_at, last_attempt_at, attempts) "
                "VALUES ('acme','Acme','ashby','acme',?,?,1)", (past, past))
    con.commit()
    con.close()
    pending = {c["canon"] for c in database.companies_needing_resolution(limit=0)}
    assert "acme" in pending, (
        f"an attempt {database.BOARD_TTL_DAYS}d {hours_past_ttl}h old is past a "
        f"{database.BOARD_TTL_DAYS}-day TTL but was treated as fresh")


def test_an_attempt_just_inside_the_ttl_is_not_pending(db, pdt):
    """The other side of the same boundary, so the fix cannot simply invert it."""
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO job_matches (company_name, role_title, date_found) "
                "VALUES ('Acme', 'Product Manager', '2026-10-01')")
    inside = (datetime.now(timezone.utc).replace(tzinfo=None)
              - timedelta(days=database.BOARD_TTL_DAYS, hours=-3)
              ).strftime("%Y-%m-%d %H:%M:%S")
    con.execute("INSERT INTO ats_boards (canon, company_name, provider, token, "
                "resolved_at, last_attempt_at, attempts) "
                "VALUES ('acme','Acme','ashby','acme',?,?,1)", (inside, inside))
    con.commit()
    con.close()
    pending = {c["canon"] for c in database.companies_needing_resolution(limit=0)}
    assert "acme" not in pending
