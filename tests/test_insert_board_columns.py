"""insert_job_matches must store what the board poller computes.

The poller scores every polled posting, drops anything under
board_ingest_min_score, merges the verdict in with j.update(verdict.as_row())
and inserts the survivors — and the INSERT listed nine fixed columns, so the
score that admitted the row was thrown away at the moment of writing it. The
six rows the poller had produced by 2026-10-03 all carried lens_tier NULL,
lens_score NULL and an empty description_text.

These assert the columns arrive, and that a row carrying none of them is
written exactly as before.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

import database
from sources.jobs import _shape


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    return path


def fetch(db, company):
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM job_matches WHERE company_name = ?", (company,)).fetchone()
    con.close()
    return dict(row) if row else None


def board_row(**over):
    row = _shape.row(
        company_name="Netic", role_title="Deployment Strategist",
        location="San Francisco", url="https://jobs.ashbyhq.com/netic/abc",
        provider="ashby", provider_job_id="abc", source="Ashby board",
        posted_at="2026-10-02",
        description_text="forward deployed; enterprise customers; evals",
        comp_min=180000, comp_max=220000,
    )
    row.update(over)
    return row


def test_a_board_row_keeps_its_lens_verdict(db):
    reasons = ["Tier 1 title: deployment strategist", "strong signal: forward deployed"]
    assert database.insert_job_matches([board_row(
        lens_tier="tier_1", lens_score=72, lens_reasons=json.dumps(reasons))]) == 1

    stored = fetch(db, "Netic")
    assert stored["lens_tier"] == "tier_1"
    assert stored["lens_score"] == 72
    assert json.loads(stored["lens_reasons"]) == reasons


def test_a_board_row_keeps_its_posting_fields(db):
    """description_text especially: score_lens.py re-derives from it later."""
    database.insert_job_matches([board_row()])
    stored = fetch(db, "Netic")
    assert stored["provider"] == "ashby"
    assert stored["provider_job_id"] == "abc"
    assert stored["posted_at"] == "2026-10-02"
    assert "forward deployed" in stored["description_text"]
    assert (stored["comp_min"], stored["comp_max"], stored["comp_known"]) == (180000, 220000, 1)


def test_a_row_without_the_extras_is_written_as_before(db):
    database.insert_job_matches([{
        "company_name": "Acme", "role_title": "Product Manager",
        "url": "https://acme.example/1", "source": "NewPMJobs",
    }])
    stored = fetch(db, "Acme")
    assert stored["role_title"] == "Product Manager"
    # Each optional column takes its schema default, which is what an untouched
    # column means — NULL for most, but comp_known DEFAULT 0 and lens_reasons
    # DEFAULT ''. The point is that nothing from a board leaks onto a row that
    # came from a newsletter.
    defaults = {"comp_known": 0, "lens_reasons": "", "description_text": ""}
    for col in database._OPTIONAL_JOB_COLUMNS:
        assert stored[col] == defaults.get(col), f"{col} = {stored[col]!r}"


def test_a_jobmatch_object_still_inserts(db):
    """The dashboard's own path builds JobMatch, which carries none of this."""
    from models import JobMatch
    n = database.insert_job_matches([JobMatch(
        company_name="Beta", company_description="", role_title="Product Manager",
        location="Remote", url="https://beta.example/1", priority="", source="Manual")])
    assert n == 1
    stored = fetch(db, "Beta")
    assert stored["lens_score"] is None and stored["provider"] is None
    assert stored["description_text"] == ""        # schema default


def test_a_stray_key_never_becomes_a_column(db):
    """_OPTIONAL_JOB_COLUMNS is a list, not a reflection of the caller's dict."""
    database.insert_job_matches([board_row(
        **{"lens_score": 61, "drop_table": "x", "notes_from_the_feed": "y"})])
    stored = fetch(db, "Netic")
    assert stored["lens_score"] == 61
    assert "drop_table" not in stored


def test_a_none_valued_extra_is_left_to_the_column_default(db):
    """posted_at is Optional in _shape.row; absent must not mean 'write NULL over a default'."""
    row = board_row()
    row["posted_at"] = None
    database.insert_job_matches([row])
    assert fetch(db, "Netic")["posted_at"] is None
