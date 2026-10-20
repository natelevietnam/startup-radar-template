"""Stop re-attempting companies that have never had a board, and retire the ones we don't want.

Two separate mechanisms, deliberately sharing one piece of state.

STRIKES. companies_needing_resolution was re-attempting every failure forever:
776 companies queued against a cap of 35 per run and a 7-day retry, so each run
spent ~65s of a 216s runtime on companies that had already failed. One measured
run found no board for 24 of its 35 attempts. After BOARD_MAX_ATTEMPTS
consecutive failures a company is left alone.

RETIREMENT. retire_board() takes a company out of both passes — polling and
resolution — by clearing `provider` (which removes it from resolved_boards)
and pinning `attempts` at the strike limit (which keeps the resolver from
picking it straight back up). Used for companies that state in writing they do
not sponsor visas, and for companies on excluded_companies.

The subtle one is test_a_resolved_board_is_not_struck_out: a WORKING board
accrues attempts too, because poll_resolved records its HTTP errors on the same
row. Applying the strike rule to those would quietly stop polling boards that
work — the opposite of the intent.
"""

from __future__ import annotations

import sqlite3

import pytest

import database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    con = sqlite3.connect(str(path))
    con.execute("INSERT INTO job_matches (company_name, role_title, date_found) "
                "VALUES ('Acme', 'Product Manager', '2026-10-01')")
    con.commit()
    con.close()
    return path


def _board(db, *, provider, attempts, stale_days=99):
    """Put one ats_boards row in place, old enough to be past the TTL."""
    from datetime import datetime, timedelta, timezone
    when = (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=stale_days)).strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO ats_boards (canon, company_name, provider, token, "
                "resolved_at, last_attempt_at, attempts, last_error) "
                "VALUES ('acme','Acme',?,?,NULL,?,?,'')",
                (provider, "acme" if provider else None, when, attempts))
    con.commit()
    con.close()


def _pending(db):
    return {c["canon"] for c in database.companies_needing_resolution(limit=0)}


@pytest.mark.parametrize("attempts", [0, 1, 2])
def test_under_the_strike_limit_is_still_retried(db, attempts):
    _board(db, provider=None, attempts=attempts)
    assert "acme" in _pending(db)


@pytest.mark.parametrize("attempts", [3, 4, 9])
def test_at_or_over_the_strike_limit_is_left_alone(db, attempts):
    _board(db, provider=None, attempts=attempts)
    assert "acme" not in _pending(db)


def test_a_resolved_board_is_not_struck_out(db):
    """A working board accrues attempts from poll errors on the same row.

    Striking those out would stop polling boards that work, which is the
    opposite of what the rule is for.
    """
    _board(db, provider="ashby", attempts=9)
    assert "acme" in _pending(db), "a resolved board must still be re-checked after the TTL"


def test_a_company_with_no_board_row_is_pending(db):
    """The baseline, and the reason retire_board does not DELETE the row."""
    assert "acme" in _pending(db)


def test_retire_board_removes_it_from_polling(db):
    _board(db, provider="ashby", attempts=1)
    assert any(b["canon"] == "acme" for b in database.resolved_boards())
    assert database.retire_board("acme", "does not sponsor visas") is True
    assert not any(b["canon"] == "acme" for b in database.resolved_boards())


def test_retire_board_also_stops_it_being_re_resolved(db):
    """Clearing provider alone would leave it looking like a fresh failure."""
    _board(db, provider="ashby", attempts=1)
    database.retire_board("acme", "does not sponsor visas")
    assert "acme" not in _pending(db)


def test_retire_board_records_why(db):
    _board(db, provider="ashby", attempts=1)
    database.retire_board("acme", "does not sponsor visas")
    con = sqlite3.connect(str(db))
    err = con.execute("SELECT last_error FROM ats_boards WHERE canon='acme'").fetchone()[0]
    con.close()
    assert "retired" in err and "sponsor" in err


def test_retire_board_on_an_unknown_company_reports_it(db):
    assert database.retire_board("nosuch", "whatever") is False


def test_a_retired_board_can_be_put_back(db):
    """Retirement is not a grave: a company may adopt an ATS later."""
    _board(db, provider="ashby", attempts=1)
    database.retire_board("acme", "does not sponsor visas")
    assert "acme" not in _pending(db)
    con = sqlite3.connect(str(db))
    con.execute("UPDATE ats_boards SET attempts = 0 WHERE canon = 'acme'")
    con.commit()
    con.close()
    assert "acme" in _pending(db)
