"""Tests for the duplicate sweeps, written when the retitle sweep was rebuilt.

The sweep compared every row against every other row and then discarded the
pair unless both sides shared a canonical company — 746,031 comparisons at 1222
rows, for a function the dashboard called on every interaction. Bucketing by
employer first makes the cost the sum of the squares of the group sizes.

A rewrite for speed has to return exactly what it returned before, so these
assert the behaviour rather than the speed: the Revel pair that motivated the
detector (2026-09-25), and the near-misses it must keep ignoring.
"""

from __future__ import annotations

import sqlite3

import pytest

import database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A real schema on a throwaway file, with set_db_path restored after."""
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    return path


def add(db, rows):
    con = sqlite3.connect(str(db))
    con.executemany(
        "INSERT INTO job_matches (company_name, role_title, location, url, status) "
        "VALUES (?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()


def ids(pairs):
    return sorted((a["id"], b["id"]) for a, b in pairs)


def test_finds_the_revel_pair(db):
    """The real 2026-09-25 case: one employer, one city, a band annotation apart."""
    add(db, [
        ("Revel", "Product Manager", "San Francisco, CA", "https://jobright.ai/a", ""),
        ("Revel", "Product Manager (Mid-Senior)", "San Francisco, CA",
         "https://www.linkedin.com/jobs/view/1/", ""),
    ])
    assert ids(database.retitled_repost_candidates()) == [(1, 2)]


def test_ignores_a_genuinely_different_role_at_the_same_employer(db):
    add(db, [
        ("Vercel", "Product Manager, Compute", "San Francisco", "https://a", ""),
        ("Vercel", "Product Manager, Networking + CDN", "San Francisco", "https://b", ""),
    ])
    assert database.retitled_repost_candidates() == []


def test_ignores_the_same_title_in_two_cities(db):
    """Two cities is two openings — the rule the sweep has always applied."""
    add(db, [
        ("Stripe", "Product Manager", "San Francisco, CA", "https://a", ""),
        ("Stripe", "Product Manager (Mid-Senior)", "New York, NY", "https://b", ""),
    ])
    assert database.retitled_repost_candidates() == []


def test_ignores_two_different_employers(db):
    add(db, [
        ("Alpha", "Product Manager", "San Francisco, CA", "https://a", ""),
        ("Beta", "Product Manager (Mid-Senior)", "San Francisco, CA", "https://b", ""),
    ])
    assert database.retitled_repost_candidates() == []


def test_ignores_rows_with_no_city(db):
    add(db, [
        ("Gamma", "Product Manager", "", "https://a", ""),
        ("Gamma", "Product Manager (Mid-Senior)", "", "https://b", ""),
    ])
    assert database.retitled_repost_candidates() == []


def test_identical_titles_are_left_to_the_unique_index(db):
    add(db, [
        ("Delta", "Product Manager", "San Francisco, CA", "https://a", ""),
        ("Delta", "Product Manager (Remote)", "San Francisco, CA", "https://b", ""),
    ])
    # canon_role already strips "(Remote)", so both canonicalise the same and the
    # sweep stays out of it.
    assert database.retitled_repost_candidates() == []


def test_a_settled_retitle_is_reported_only_when_the_decision_was_an_application(db):
    """The asymmetry the report was built with, re-asserted after the rewrite.

    Two copies up, one cut, the survivor a deliberate keep is how the cut-list
    workflow ends — reporting that would nag about a choice just made.
    """
    add(db, [
        ("Epsilon", "Product Manager", "San Francisco, CA", "https://a", "Not Interested"),
        ("Epsilon", "Product Manager (Mid-Senior)", "San Francisco, CA", "https://b", ""),
    ])
    assert database.duplicate_employer_pairs()["settled"] == []

    add(db, [
        ("Zeta", "Product Manager", "San Francisco, CA", "https://c", "Applied"),
        ("Zeta", "Product Manager (Mid-Senior)", "San Francisco, CA", "https://d", ""),
    ])
    settled = database.duplicate_employer_pairs()["settled"]
    assert [(p["cut"]["company_name"], p["kind"]) for p in settled] == [("Zeta", "retitle")]


def test_scales_without_comparing_every_pair(db):
    """800 rows across 40 employers used to be 320k comparisons; now it is ~8k."""
    rows = []
    for c in range(40):
        for r in range(20):
            rows.append((f"Company{c}", f"Product Manager, Surface {r}",
                         "San Francisco, CA", f"https://x/{c}/{r}", ""))
    # One real pair hidden in the pile.
    rows.append(("Company7", "Product Manager, Surface 3 (Mid-Senior)",
                 "San Francisco, CA", "https://x/7/dup", ""))
    add(db, rows)
    found = database.retitled_repost_candidates()
    assert len(found) == 1
    a, b = found[0]
    assert a["company_name"] == b["company_name"] == "Company7"
