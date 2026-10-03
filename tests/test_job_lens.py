"""Tests for the forward-deployed lens scorer.

Every fixture in tests/fixtures/ is a real posting read during research, except
three marked synthetic in their own `_source` field: the deployment-strategist
title class (which the ingest gate blocked before tier 1 was admitted, so no
real one has ever been stored), and the two tier-3 probes, because tier 3 is not
admitted at ingest yet and there are likewise none on the board.

The assertions are on TIER and REASONS as well as the number, per the spec: a
score nobody can audit is not useful, and the reasons are what the author will
actually read.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

import job_lens

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parent.parent

# The lens is config-driven, so the tests run against the real taxonomy rather
# than a stub. config.yaml is gitignored, so fall back to the tracked example —
# they carry a byte-identical job_matching block by construction.
_CFG_PATH = REPO / "config.yaml"
if not _CFG_PATH.exists():
    _CFG_PATH = REPO / "config.example.yaml"
_FULL = yaml.safe_load(_CFG_PATH.read_text())
CFG = _FULL["job_matching"]
TARGET_LOCATIONS = _FULL["targets"]["locations"]

# Fixed so the recency component cannot drift as the calendar moves.
TODAY = date(2026, 10, 1)

STRONG_COMPANY = {"co": "Nirvana Insurance", "score": 82, "gates": {"status": "ready"}}
WEAK_COMPANY = {"co": "Ascension", "score": 40, "gates": {"status": "ask"}}


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def score(name: str, company=None):
    return job_lens.score_posting(
        load(name), CFG, company=company,
        target_locations=TARGET_LOCATIONS, today=TODAY)


def reasons_text(result) -> str:
    return " | ".join(result.reasons).lower()


# --------------------------------------------------------------------------
# Tier 1 — the hypothesis
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "method_financial_fdpm",
    "exa_fdpm",
    "surge_strategic_pm",
    "deployment_strategist",
])
def test_tier_1_titles_are_tier_1(name):
    r = score(name)
    assert r.tier == 1
    assert "tier 1 title match" in reasons_text(r)
    assert r.score >= CFG["tier_points"]["tier_1"]


def test_tier_1_titles_without_a_pm_substring_now_reach_the_board():
    """The whole reason tier 1 is admitted at ingest.

    Six of the twelve tier-1 titles contain no "product manager" substring, so
    under targets.roles alone they never reached job_matches and the lens scored
    an empty set for half its own taxonomy. The config documented them as
    admitted for a day before filters.py actually read the key; this test is
    what makes that claim true rather than aspirational.
    """
    import filters
    from config_loader import load_config

    flt = filters.JobFilter(load_config())
    for title in ("Forward Deployed PM", "Deployment Strategist", "Agent Operator",
                  "Agent Operations", "AI Strategist", "Customer Impact"):
        assert flt.role_matches(title), title
        assert not any(r in title.lower() for r in flt.roles), \
            f"{title} should be admitted by the lens list, not by targets.roles"

    assert score("deployment_strategist").tier == 1


def test_tier_1_admission_does_not_reopen_the_excluded_shapes():
    """Widening the positive match must not undo the 2026-09-09 narrowing."""
    import filters
    from config_loader import load_config

    flt = filters.JobFilter(load_config())
    for title in ("Founding Deployment Strategist", "Principal Forward Deployed PM",
                  "Director, AI Strategist", "Staff Agent Operator",
                  "Group Product Manager", "Lead Product Manager"):
        assert not flt.role_matches(title), title


def test_tier_3_is_not_admitted_at_ingest_while_its_flag_is_false():
    import filters
    import yaml
    from config_loader import load_config

    cfg = load_config()
    tiers = cfg["job_matching"]["title_tiers"]
    if tiers.get("tier_3_admitted_at_ingest"):
        pytest.skip("tier 3 has been turned on at ingest")
    flt = filters.JobFilter(cfg)
    for title in tiers["tier_3"]:
        assert not flt.role_matches(title), title


def test_exa_fdpm_reads_its_signals_and_names_them():
    r = score("exa_fdpm")
    text = reasons_text(r)
    assert "tier 1 title match" in text
    assert "strong signal" in text
    # the signals actually present in that posting body
    for phrase in ("forward deployed", "enterprise customers", "evaluation period"):
        assert phrase in text
    assert "no band published (not penalised)" in text


def test_unknown_comp_is_not_penalised():
    r = score("method_financial_fdpm")
    assert "not penalised" in reasons_text(r)
    pts = CFG["compensation"]["points_if_unknown"]
    assert pts > CFG["compensation"]["points_below_floor"]


# --------------------------------------------------------------------------
# No taxonomy match — signals still score, because that is how a mislabelled
# posting gets caught
# --------------------------------------------------------------------------

def test_vercel_has_no_taxonomy_title_but_still_scores_well():
    r = score("vercel_pm_compute")
    assert r.tier is None
    text = reasons_text(r)
    assert "no taxonomy title match" in text
    assert "agent" in text                      # AI and agent workloads
    assert "enterprise customers" in text
    assert "bay area location" in text
    assert "at or above $170,000" in text
    assert r.score > 0


def test_comp_below_floor_scores_zero_and_says_so():
    r = score("ascension_app_pm")
    text = reasons_text(r)
    assert "below the $150,000 floor" in text
    assert "negotiating against the ceiling" in text

    # Assert the POINTS, not just the wording. An earlier version of this test
    # checked only the reason string and the config value, and a mutation that
    # paid below-floor postings the unknown-comp rate passed it unnoticed.
    base = load("ascension_app_pm")
    unknown = job_lens.score_posting(
        dict(base, comp_known=0, comp_min=None), CFG,
        target_locations=TARGET_LOCATIONS, today=TODAY)
    assert r.score == unknown.score - CFG["compensation"]["points_if_unknown"]

    at_target = job_lens.score_posting(
        dict(base, comp_min=CFG["compensation"]["target_usd"]), CFG,
        target_locations=TARGET_LOCATIONS, today=TODAY)
    assert at_target.score - r.score == CFG["compensation"]["points_at_target"]


def test_account_executive_never_reads_as_a_match():
    """Acceptance criterion 5, from the lens side.

    The ingest filter is what actually keeps this title off the board; the lens
    must not undo that by scoring it like a target. It picks up a few points for
    generic words, which is correct — but no tier, and well under the digest
    threshold.
    """
    r = score("account_executive")
    assert r.tier is None
    assert r.score < CFG["digest_min_score"]

    import filters
    from config_loader import load_config
    assert not filters.JobFilter(load_config()).role_matches("Account Executive, Enterprise")


# --------------------------------------------------------------------------
# Tier 3 — only when the company itself clears a high bar
# --------------------------------------------------------------------------

def test_tier_3_counts_at_a_strong_company():
    r = score("chief_of_staff_strong_company", company=STRONG_COMPANY)
    assert r.tier == 3
    assert "tier 3 title match" in reasons_text(r)
    assert r.score >= CFG["tier_points"]["tier_3"]


def test_tier_3_does_not_count_at_a_weak_company():
    r = score("chief_of_staff_weak_company", company=WEAK_COMPANY)
    assert r.tier == 3, "the tier is still recorded"
    text = reasons_text(r)
    assert "not a strong enough fit to count it" in text
    assert "tier 3 title match" not in text


def test_tier_3_does_not_count_with_no_dossier_at_all():
    """Strength cannot be asserted about a company nobody has researched."""
    r = score("chief_of_staff_strong_company", company=None)
    assert r.tier == 3
    assert "not a strong enough fit" in reasons_text(r)


@pytest.mark.parametrize("company,expected", [
    ({"score": 75, "gates": {"status": "ready"}}, True),     # exactly the bar
    ({"score": 74, "gates": {"status": "ready"}}, False),    # one under
    ({"score": 90, "gates": {"status": "ask"}}, False),      # high but gated
    ({"score": 90, "gates": {"status": "blocked"}}, False),
    ({"score": None, "gates": {"status": "ready"}}, False),
    ({}, False),
    (None, False),
])
def test_tier_3_company_bar(company, expected):
    assert job_lens.company_allows_tier_3(company, CFG) is expected


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------

def test_recency_window_is_inclusive_and_bounded():
    p = dict(load("exa_fdpm"))
    within = CFG["recency"]["within_days"]
    for days, should_score in ((0, True), (within, True), (within + 1, False)):
        p["posted_at"] = date.fromordinal(TODAY.toordinal() - days).isoformat()
        r = job_lens.score_posting(p, CFG, target_locations=TARGET_LOCATIONS, today=TODAY)
        assert ("posted" in reasons_text(r)) is should_score, days


def test_a_future_posted_at_scores_no_recency_rather_than_negative_days():
    p = dict(load("exa_fdpm"))
    p["posted_at"] = "2026-12-25"
    r = job_lens.score_posting(p, CFG, target_locations=TARGET_LOCATIONS, today=TODAY)
    assert "posted" not in reasons_text(r)


def test_unparseable_posted_at_is_ignored_not_fatal():
    p = dict(load("exa_fdpm"))
    p["posted_at"] = "last Tuesday"
    r = job_lens.score_posting(p, CFG, target_locations=TARGET_LOCATIONS, today=TODAY)
    assert r.tier == 1


def test_sponsorship_offered_adds_points_and_refused_never_subtracts():
    base = dict(load("exa_fdpm"))
    neutral = job_lens.score_posting(base, CFG, target_locations=TARGET_LOCATIONS, today=TODAY)

    offered = dict(base, sponsorship="offered")
    r_off = job_lens.score_posting(offered, CFG, target_locations=TARGET_LOCATIONS, today=TODAY)
    assert r_off.score == neutral.score + CFG["visa_points_positive"]

    # A refused row is already cut upstream; if one ever reaches the scorer it
    # must not go negative or be silently rewarded.
    refused = dict(base, sponsorship="refused")
    r_ref = job_lens.score_posting(refused, CFG, target_locations=TARGET_LOCATIONS, today=TODAY)
    assert r_ref.score == neutral.score


def test_missing_description_is_reported_not_guessed():
    p = dict(load("method_financial_fdpm"), description_text="")
    r = job_lens.score_posting(p, CFG, target_locations=TARGET_LOCATIONS, today=TODAY)
    assert "no posting body stored" in reasons_text(r)


def test_score_is_clamped_to_the_cap():
    p = dict(load("deployment_strategist"))
    # Every component at once: tier 1, Bay Area, above target, sponsored, fresh,
    # and a body stuffed with every strong signal in the config.
    p["description_text"] = " ".join(CFG["description_signals"]["strong"]
                                     + CFG["description_signals"]["weak"])
    r = job_lens.score_posting(p, CFG, target_locations=TARGET_LOCATIONS, today=TODAY)
    assert r.score == CFG["score_cap"]
    assert "clamped" in reasons_text(r)


def test_signal_caps_hold():
    p = dict(load("deployment_strategist"))
    p["description_text"] = " ".join(CFG["description_signals"]["strong"])
    strong_only = job_lens.score_posting(
        p, CFG, target_locations=TARGET_LOCATIONS, today=TODAY)
    p["description_text"] = CFG["description_signals"]["strong"][0]
    one_only = job_lens.score_posting(
        p, CFG, target_locations=TARGET_LOCATIONS, today=TODAY)
    gain = strong_only.score - one_only.score
    assert gain <= CFG["description_signals"]["strong_cap"]


def test_remote_and_bay_area_are_paid_differently():
    p = dict(load("surge_strategic_pm"))
    remote = job_lens.score_posting(p, CFG, target_locations=TARGET_LOCATIONS, today=TODAY)
    bay = job_lens.score_posting(dict(p, location="Palo Alto, CA"), CFG,
                                 target_locations=TARGET_LOCATIONS, today=TODAY)
    assert bay.score - remote.score == (
        CFG["location_points"]["bay_area"] - CFG["location_points"]["remote_us"])


def test_as_row_maps_onto_the_three_db_columns():
    r = score("exa_fdpm")
    row = r.as_row()
    assert set(row) == {"lens_tier", "lens_score", "lens_reasons"}
    assert row["lens_tier"] == 1
    assert json.loads(row["lens_reasons"]) == r.reasons


def test_scorer_is_pure_and_does_not_mutate_its_input():
    p = load("vercel_pm_compute")
    before = json.dumps(p, sort_keys=True)
    job_lens.score_posting(p, CFG, target_locations=TARGET_LOCATIONS, today=TODAY)
    assert json.dumps(p, sort_keys=True) == before


def test_empty_posting_does_not_raise():
    r = job_lens.score_posting({}, CFG, target_locations=TARGET_LOCATIONS, today=TODAY)
    assert r.tier is None
    assert r.score >= 0
