"""The daily run must read postings and score them, not leave it to a human.

enrich_sponsorship.py and score_lens.py were scripts only. daily.yml runs one
command — `python daily_run.py` -> main.run() — and neither file invoked either
script, so every cloud run produced rows with no posting body and no lens score.
Measured on 2026-10-16: the cloud database held 1,916 job rows and 4 posting
bodies, all 4 written by the board poller at ingest.

These pin the wiring: both passes run, in the order that makes the second one
useful, under a cap, with refusals left for the user to file.
"""

from __future__ import annotations

import pytest

import main


@pytest.fixture()
def calls(monkeypatch):
    """Record what the step invokes, without touching the network or the DB."""
    seen = []

    import enrich_sponsorship, score_lens
    monkeypatch.setattr(enrich_sponsorship, "main", lambda argv: seen.append(("enrich", list(argv))) or 0)
    monkeypatch.setattr(score_lens, "main", lambda argv: seen.append(("score", list(argv))) or 0)
    return seen


LENS_ON = {"job_matching": {"enabled": True}}


def test_both_passes_run_and_in_that_order(calls):
    main.enrich_postings(LENS_ON)
    assert [c[0] for c in calls] == ["enrich", "score"]


def test_the_enricher_reads_rows_whose_body_is_missing(calls):
    """--missing-text is the point: a row whose stance is stored but whose body
    is not would otherwise never be re-read."""
    main.enrich_postings(LENS_ON)
    assert "--missing-text" in calls[0][1]


def test_the_fetch_is_capped_and_the_cap_is_configurable(calls):
    main.enrich_postings(LENS_ON)
    assert calls[0][1][calls[0][1].index("--limit") + 1] == "60"
    calls.clear()
    main.enrich_postings({**LENS_ON, "enrichment": {"max_postings_per_run": 5}})
    assert calls[0][1][calls[0][1].index("--limit") + 1] == "5"


def test_refusals_are_not_filed_unless_asked(calls):
    """An unattended run surfaces a refusal; it does not set a status."""
    main.enrich_postings(LENS_ON)
    assert "--apply" not in calls[0][1]
    calls.clear()
    main.enrich_postings({**LENS_ON, "enrichment": {"file_refusals": True}})
    assert "--apply" in calls[0][1]


def test_the_step_can_be_switched_off(calls):
    main.enrich_postings({**LENS_ON, "enrichment": {"enabled": False}})
    assert calls == []


def test_scoring_is_skipped_when_the_lens_is_off(calls):
    main.enrich_postings({"job_matching": {"enabled": False}})
    assert [c[0] for c in calls] == ["enrich"]


def test_a_failed_fetch_does_not_stop_the_scoring(calls, capsys, monkeypatch):
    """Both passes read the network. One failing is a bad day, not a bad run —
    and it must not cost the scoring of rows whose text is already stored."""
    import enrich_sponsorship
    def boom(argv):
        raise RuntimeError("upstream 503")
    monkeypatch.setattr(enrich_sponsorship, "main", boom)

    main.enrich_postings(LENS_ON)
    assert [c[0] for c in calls] == ["score"]
    assert "Posting enrichment skipped" in capsys.readouterr().out


def test_a_failed_score_does_not_raise(calls, capsys, monkeypatch):
    import score_lens
    def boom(argv):
        raise RuntimeError("no dossiers")
    monkeypatch.setattr(score_lens, "main", boom)

    main.enrich_postings(LENS_ON)          # must not raise
    assert "Lens scoring skipped" in capsys.readouterr().out


def test_the_follow_up_hint_is_suppressed_in_the_pipeline(calls):
    """Standalone, the enricher tells you to run score_lens. Inside the run it
    must not, because the next line of the log is score_lens running."""
    main.enrich_postings(LENS_ON)
    assert "--chained" in calls[0][1]
