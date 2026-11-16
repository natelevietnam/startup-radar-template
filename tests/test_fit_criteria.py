"""The criteria scorer has to be wrong in the right direction.

It ranks the PM-Fit artifact against the criteria set on 2026-10-22 (public or
Series C+, PM/Sr PM, IC, B2B/enterprise, five named domains, three named
sectors). Nothing it does can cut a row, so the risk is not damage — it is a
ranking nobody trusts, which is the same as no ranking at all.

Two failure modes are worth more than the happy path, and both were real in the
first run against live data:

  * matching the employer's prose about *other people*. Accordion's AI PM
    posting lists its consulting clients — "Retail, CPG, Healthcare, Media &
    Entertainment, Technology, and Logistics" — and scored a full Healthtech
    match off it. TRM Labs and Vitalize both carry "We hire and grow against
    three leadership principles", which read as people management.
  * flat scoring. Every component at full marks put twelve companies on a
    perfect 100 in the first run, because "AI" and "API" appear in nearly every
    posting on this board.

The cases below pin both, plus the orderings that are load-bearing: level words
beat title words, and an explicit IC statement beats a "build and lead".
"""

from __future__ import annotations

import pytest

import fit_criteria as fc


# --- stage ------------------------------------------------------------------
@pytest.mark.parametrize("blob,expected", [
    ("Public consumer marketplace; far past startup stage", fc.MAX_STAGE),
    ("Series F · $7.2B · ~$300M ARR", fc.MAX_STAGE),
    ("Series C", fc.MAX_STAGE),
    ("$175K/yr - $220K/yr • Software · Late Stage • 94%", fc.MAX_STAGE),
    ("Nasdaq-listed, ~3,000 employees", fc.MAX_STAGE),
    ("Series B · $25M (Jan 2022, Goldman Sachs)", 8),
    ("Software · Growth Stage • 91%", 8),
    ("Series A · Lightspeed, Redpoint", 4),
    ("Seed · $4.22M (Apr 2025)", 4),
    ("Artificial Intelligence (AI) · Early Stage • 90%", 4),
])
def test_stage_buckets(blob, expected):
    assert fc.stage_points(blob, {}).points == expected


def test_stage_is_unknown_rather_than_zero_when_nothing_says():
    part = fc.stage_points("A consulting firm headquartered in New York", {})
    assert part.points == fc._unknown(fc.MAX_STAGE)
    assert "not read" in part.reason


def test_a_consultancys_private_equity_clients_are_not_its_own_stage():
    """Accordion's posting opens "As private equity's value creation partner".

    That is a sentence about its customers. Reading it as "this employer is
    PE-backed and therefore mature" is the same class of error as scoring its
    clients' sectors as its industry, so the bare phrase is deliberately not in
    the stage vocabulary — only "PE-backed" and "private equity backed" are.
    """
    part = fc.stage_points(
        "As private equity's value creation partner, we sit where sponsors and "
        "CFOs meet. We partner with Private Equity clients and their Portfolio "
        "Companies across Retail, CPG and Healthcare.", {})
    assert part.points == fc._unknown(fc.MAX_STAGE), part.reason
    assert part.points < fc.MAX_STAGE


def test_pe_backed_employer_does_clear_the_bar():
    assert fc.stage_points("PE-backed, 1,600 staff, 11 offices", {}).points == fc.MAX_STAGE


# --- role title -------------------------------------------------------------
@pytest.mark.parametrize("title,expected", [
    ("Product Manager", fc.MAX_ROLE),
    ("Senior Product Manager", fc.MAX_ROLE),
    ("Sr. Product Manager", fc.MAX_ROLE),
    ("AI Product Manager", fc.MAX_ROLE),
    # A PM title whatever the prefix — Nate's call, 22 Oct.
    ("Forward Deployed Product Manager", fc.MAX_ROLE),
    ("Principal Product Manager", 20),
    # A notch below PM proper, also his call.
    ("Deployment Strategist", 17),
    ("AI Strategist", 17),
    ("Provider Growth & AI Agent Operations Lead", 17),
    # Wrong level, in both directions.
    ("Director of Product Management", 6),
    ("Group Product Manager", 6),
    ("Head of Product", 6),
    ("Associate Product Manager", 6),
    ("AI Product Manager Graduate (TikTok) - 2027 Start", 6),
    # Adjacent but not the job.
    ("Senior Program Manager", 8),
    ("Product Marketing Manager", 8),
])
def test_role_shapes(title, expected):
    assert fc.role_points(title, {}).points == expected


def test_level_words_beat_title_words():
    """The ordering inside role_points is the whole design.

    "Director of Product Management" contains "product management" and
    "Associate Product Manager" contains "product manager". If the ideal list
    were tested first, every disqualifying level word would be swamped by the
    title match it sits next to.
    """
    for title in ("Director of Product Management", "Associate Product Manager",
                  "VP Product Management", "Group Product Manager"):
        assert fc.role_points(title, {}).points < fc.MAX_ROLE, title


# --- IC vs people manager ---------------------------------------------------
def test_values_boilerplate_is_not_people_management():
    """TRM Labs and Vitalize, verbatim. Four hits on the board, none a duty."""
    body = ("LEADERSHIP PRINCIPLES. We hire and grow against three leadership "
            "principles. They're the standard we hold ourselves to.")
    part = fc.ic_points("Deployment Strategist", body, {})
    assert part.points == fc.MAX_IC, part.reason


def test_influence_is_not_people_management():
    """Kikoff: "collaboratively lead a team to build towards a vision"."""
    part = fc.ic_points(
        "Product Manager, Retention & Monetization",
        "A futurist who can collaboratively lead a team to build towards a vision.",
        {})
    assert part.points == fc.MAX_IC, part.reason


def test_a_real_reporting_line_is_read_as_management():
    for body in ("You will lead a team of four product managers.",
                 "Lead and manage provider success team executing customer calls.",
                 "3 years of people management experience with direct reports.",
                 "Proven ability to build and lead high-performing teams."):
        assert fc.ic_points("Product Manager", body, {}).points == 3, body


def test_an_explicit_ic_statement_wins_over_a_future_team():
    """Thyme Care, verbatim: IC now, maybe a team later. Today's role is IC.

    This passes only because the IC vocabulary is tested before the manager
    vocabulary. Swap the two blocks and this case breaks.
    """
    body = ("This is an individual contributor role today, with the opportunity "
            "to build and lead a product team over time as this business line grows.")
    assert fc.ic_points("Principal Product Manager", body, {}).points == fc.MAX_IC


def test_silence_is_read_as_ic():
    part = fc.ic_points("Product Manager", "You will own the agent roadmap.", {})
    assert part.points == fc.MAX_IC
    assert "no people-management duty stated" in part.reason


# --- domains ----------------------------------------------------------------
def test_domain_hits_are_weighted_not_flat():
    """One mention of AI must not score what agents + evals + AI scores."""
    thin = fc.domain_points("We use AI to help our users.", {})
    deep = fc.domain_points(
        "Own LLM agent evaluation: build evals, review failures, track "
        "hallucination rates, and ship agentic workflows against our public API.",
        {})
    assert 0 < thin.points < deep.points
    assert deep.points == fc.MAX_DOMAIN


def test_the_domains_he_is_hunting_outscore_the_adjacent_ones():
    """Two hits are not two hits.

    Agents and evaluation are the charter; workflow automation and API
    integrations are adjacent surfaces that nearly every B2B posting mentions.
    Flat per-hit scoring makes these two postings identical, which is how a
    board of "AI-ish" roles outranks the handful that are actually the job.
    """
    charter = fc.domain_points("Own the agents and their evals.", {})
    adjacent = fc.domain_points("Own the workflow automation and the API.", {})
    assert charter.points > adjacent.points, (charter.reason, adjacent.reason)


def test_domain_score_is_capped():
    body = " ".join(["ai llm agents evals workflow automation api integrations"] * 40)
    assert fc.domain_points(body, {}).points == fc.MAX_DOMAIN


def test_no_domain_language_scores_zero_not_a_midpoint():
    """Silence on domain is a real signal: this posting is not the work."""
    part = fc.domain_points(
        "Own the tuition billing experience for childcare providers.", {})
    assert part.points == 0
    assert "none of your five domains" in part.reason


def test_boundary_anchoring_is_inherited_from_job_lens():
    """"ai" must not fire inside "said", nor "api" inside "rapid"."""
    assert fc.domain_points("She said the rapid rollout went well.", {}).points == 0


# --- industry and customers -------------------------------------------------
def test_a_consultancys_client_sectors_are_not_its_industry():
    """The live bug this component was rewritten for.

    Accordion scored a full Healthtech match off a list of the sectors its
    PE clients operate in. score_role now passes industry_points the company
    blurb only, so the sentence below cannot reach it from a posting body.
    """
    client_prose = ("We partner with Private Equity clients and their Portfolio "
                    "Companies across Retail, CPG, Healthcare, Media & "
                    "Entertainment, Technology, and Logistics.")
    fit = fc.score_role({"title": "AI Product Manager", "body": client_prose,
                         "company_blob": "consulting • level: mid",
                         "stage_blob": ""}, {})
    industry = fit.part("industry")
    assert industry.points < fc.MAX_INDUSTRY, industry.reason


@pytest.mark.parametrize("blurb,expected", [
    ("fintech • level: mid", fc.MAX_INDUSTRY),
    ("$140K/yr • Financial Services · Late Stage", fc.MAX_INDUSTRY),
    ("healthcare • level: mid", fc.MAX_INDUSTRY),
    ("Enterprise SaaS platform for revenue teams", fc.MAX_INDUSTRY),
    ("$200K/yr • Software · Growth Stage", 6),
    ("Cyber Security · Late Stage", 6),
    ("consumer • level: mid", 2),
    ("Family & Care Education Child Care EdTech", 2),
])
def test_industry_buckets(blurb, expected):
    assert fc.industry_points(blurb, {}).points == expected


def test_b2b_wins_over_consumer_when_a_posting_says_both():
    """Wonderschool sells SaaS to providers inside a consumer marketplace."""
    part = fc.customer_points(
        "A marketplace helping families find care, and a SaaS suite for the "
        "enterprise childcare networks that serve them.", {})
    assert part.points == fc.MAX_CUSTOMER
    assert "B2B" in part.reason


# --- the whole thing --------------------------------------------------------
def test_total_is_bounded_and_components_sum():
    fit = fc.score_role({
        "title": "Senior Product Manager, Agents",
        "body": "Own agent evals and the public API. Enterprise customers.",
        "company_blob": "fintech • level: mid", "stage_blob": "Series D · $4.15B",
    }, {})
    assert fit.score == sum(p.points for p in fit.parts)
    assert 0 <= fit.score <= fc.MAX_TOTAL == 100


def test_company_takes_its_best_role_not_its_average():
    """Wonderschool's three roles differ by ~20 points.

    Averaging them would describe no decision Nate can make: he applies to a
    role, not to a company.
    """
    common = {"stage_blob": "Series B", "company_blob": "Family & Care EdTech"}
    roles = [
        {"title": "Provider Growth & AI Agent Operations Lead",
         "body": "Lead and manage provider success team. Own the funnel.", **common},
        {"title": "AI Agent Product Manager",
         "body": "Own agent evals, tool access, escalation paths, enterprise "
                 "workflows and our integrations API.", **common},
    ]
    best = fc.score_company(roles, {})
    assert best.role == "AI Agent Product Manager"
    assert best.score == max(fc.score_role(r, {}).score for r in roles)


def test_no_titled_roles_scores_nothing_rather_than_zero():
    assert fc.score_company([{"title": "  ", "body": "x"}], {}) is None
    assert fc.score_company([], {}) is None


def test_config_can_override_a_vocabulary():
    """The lists are judgement, so they live in config like job_matching's."""
    cfg = {"fit_ranking": {"role_ideal": ["widget wrangler"]}}
    assert fc.role_points("Widget Wrangler", cfg).points == fc.MAX_ROLE
    assert fc.role_points("Product Manager", cfg).points == fc._unknown(fc.MAX_ROLE)
