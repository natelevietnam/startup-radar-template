"""Tests for ATS board resolution.

Resolution is the one place in this pipeline that can attach a WRONG EMPLOYER
to a posting, because the last strategy guesses a board token from a company
name. That breaks dedupe, lets applied roles resurface, and gets a dossier
written against the wrong business — so most of these tests are about the guard
that stops it.

No network: the connectors are monkeypatched. The live behaviour was verified
separately against Jerry.ai, Vercel, SonarSource, AfterQuery and Latent.
"""

from __future__ import annotations

import pytest

import database
from sources.jobs import resolve


# --------------------------------------------------------------------------
# URL -> provider
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://jobs.ashbyhq.com/Jerry.ai/abc", "ashby"),
    ("https://job-boards.greenhouse.io/vercel/jobs/1", "greenhouse"),
    ("https://boards.greenhouse.io/anthropic/jobs/1", "greenhouse"),
    ("https://boards.eu.greenhouse.io/x/jobs/1", "greenhouse"),
    ("https://jobs.lever.co/sonarsource/x", "lever"),
    ("https://www.linkedin.com/jobs/view/123", None),
    ("https://jobright.ai/jobs/info/abc", None),
    ("https://wellfound.com/jobs?job_listing_slug=1", None),
    ("", None),
    ("not a url", None),
])
def test_provider_for_url(url, expected):
    assert resolve.provider_for_url(url) == expected


# --------------------------------------------------------------------------
# The identity guard on guessed tokens
# --------------------------------------------------------------------------

def _rows(body: str, company: str = ""):
    return [{"description_text": body, "company_name": company}]


def test_guard_accepts_a_board_that_names_the_company():
    rows = _rows("ABOUT LATENT. Latent is the enterprise pharmacy intelligence platform.")
    assert resolve._board_mentions_company(rows, "Latent") is True


def test_guard_ignores_separators_and_case():
    rows = _rows("About AfterQuery — we build training data.")
    assert resolve._board_mentions_company(rows, "after query") is True
    assert resolve._board_mentions_company(rows, "AFTERQUERY") is True


def test_guard_rejects_a_board_for_a_different_company():
    """The Arovy/SonarSource shape: a slug owned by someone else."""
    rows = _rows("Who is Sonar? Sonar is driving the future of code review.")
    assert resolve._board_mentions_company(rows, "Arovy") is False
    assert resolve._board_mentions_company(rows, "Nirvana Insurance") is False


def test_guard_rejects_names_too_short_to_be_evidence():
    rows = _rows("We use AI everywhere and our AI is the best AI.")
    assert resolve._board_mentions_company(rows, "AI") is False
    assert resolve._board_mentions_company(rows, "C3") is False


def test_guard_handles_empty_input():
    assert resolve._board_mentions_company([], "Latent") is False
    assert resolve._board_mentions_company(_rows(""), "Latent") is False


def test_slug_guess_is_rejected_when_the_board_does_not_corroborate(monkeypatch):
    """A board that answers with postings is still refused if it is someone else's."""
    class FakeMod:
        PROVIDER = "ashby"

        @staticmethod
        def fetch_board(token, company_name="", **kw):
            return [{"description_text": "Who is Sonar? We do code review.",
                     "company_name": company_name}]

    monkeypatch.setattr(resolve, "PROVIDERS", {"ashby": FakeMod})
    assert resolve._from_slug("Arovy") is None


def test_slug_guess_is_accepted_when_corroborated(monkeypatch):
    class FakeMod:
        PROVIDER = "ashby"

        @staticmethod
        def fetch_board(token, company_name="", **kw):
            return [{"description_text": "About Latent: clinical AI for pharmacy.",
                     "company_name": company_name}]

    monkeypatch.setattr(resolve, "PROVIDERS", {"ashby": FakeMod})
    assert resolve._from_slug("Latent") == ("ashby", "latent")


def test_empty_board_is_not_accepted(monkeypatch):
    """Every provider 200s with an empty list for a plausible-looking token."""
    class FakeMod:
        PROVIDER = "ashby"

        @staticmethod
        def fetch_board(token, company_name="", **kw):
            return []

    monkeypatch.setattr(resolve, "PROVIDERS", {"ashby": FakeMod})
    assert resolve._from_slug("Whoever") is None


# --------------------------------------------------------------------------
# Slug candidates
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Nirvana Insurance", ["nirvanainsurance", "nirvana-insurance"]),
    ("AfterQuery", ["afterquery"]),
    ("C3 AI", ["c3ai", "c3-ai"]),
    ("Jerry.ai", ["jerryai", "jerry-ai"]),
    ("", []),
    ("X", []),                      # one character is not a candidate
])
def test_slug_candidates(name, expected):
    assert resolve._slug_candidates(name) == expected


# --------------------------------------------------------------------------
# resolve_one never raises, and records every outcome
# --------------------------------------------------------------------------

def test_a_failing_strategy_is_recorded_not_raised(monkeypatch):
    def boom(_):
        raise RuntimeError("network on fire")

    monkeypatch.setattr(resolve, "_from_stored_urls", boom)
    recorded = {}
    monkeypatch.setattr(database, "upsert_board",
                        lambda *a, **k: recorded.update({"args": a, "kw": k}))

    out = resolve.resolve_one({"canon": "x", "company_name": "X Co", "website": ""})
    assert out["ok"] is False
    assert "network on fire" in out["error"]
    assert recorded, "the failure must be persisted, or every run retries it"


def test_stored_url_strategy_wins_and_costs_nothing(monkeypatch):
    monkeypatch.setattr(resolve, "_from_stored_urls", lambda c: ("ashby", "tok"))
    monkeypatch.setattr(resolve, "_from_website",
                        lambda w: pytest.fail("should not fetch the website"))
    monkeypatch.setattr(resolve, "_from_slug",
                        lambda n: pytest.fail("should not guess a slug"))
    monkeypatch.setattr(database, "upsert_board", lambda *a, **k: None)

    out = resolve.resolve_one({"canon": "c", "company_name": "C", "website": "https://c.com"})
    assert out["ok"] is True and out["strategy"] == "stored-url"


def test_resolve_pending_respects_the_limit_and_the_ranking(monkeypatch):
    companies = [{"canon": f"c{i}", "company_name": f"C{i}", "website": ""} for i in range(10)]
    monkeypatch.setattr(database, "companies_needing_resolution", lambda limit=0: companies)
    seen: list[str] = []

    def fake_one(c):
        seen.append(c["canon"])
        return {"canon": c["canon"], "ok": True, "strategy": "stored-url"}

    monkeypatch.setattr(resolve, "resolve_one", fake_one)
    rank = {"c7": 90, "c3": 80, "c0": 10}
    out = resolve.resolve_pending(limit=3, rank=rank)

    assert len(out) == 3
    assert seen == ["c7", "c3", "c0"], "highest dossier score first"
