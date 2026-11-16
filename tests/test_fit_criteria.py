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


# --- stage decoys -----------------------------------------------------------
# Every blob below is live text from fit_dossiers.json or a stored posting, cut
# down to the clause that matters. Swept over all 572 dossiers, the bug this
# pins scored 17 companies as public that are not, and gave 4 more full marks
# for the wrong reason. Two families are worth telling apart: a company that
# *sells to* the public sector, and a dossier saying it could find no public
# record of the company at all.
@pytest.mark.parametrize("blob", [
    # Nearmap, Quantifind, Sierra, TRM Labs, Vision Government Solutions — five
    # companies between Series B and Series C, every one selling to government.
    "Solution Product Manager, Government — public sector solutions, "
    "not core platform",
    "Property tax software; CAMA is computer-assisted mass appraisal, "
    "public sector assessment systems for local government",
    "What is open now is the public-sector twin: same team, same craft",
    "FedRAMP High, which is what makes the public sector seat real",
])
def test_selling_to_the_public_sector_is_not_being_a_public_company(blob):
    part = fc.stage_points(blob, {})
    assert part.points < fc.MAX_STAGE, part.reason
    assert "public" not in part.reason.split("—")[0]


@pytest.mark.parametrize("blob", [
    "A1 returns no usable public record — no funding, founder or headcount",
    "A single-word company name with no resolvable public identity",
    "An SF AI startup with essentially no public footprint",
    "No round found in any public source; the founder goes by a first name",
    "The thinnest public record of the seventeen",
    "No public funding record found; no product the board can point at",
    "Long-running public criticism over AI-driven candidate scoring",
    "Mixed public reputation, some scam allegations",
    "The only thing the public web surfaces is a Ford autonomous-vehicle unit",
])
def test_a_dossier_that_found_nothing_is_not_a_public_company(blob):
    """The worst of the two families, because it inverts the evidence.

    Twelve dossiers said in prose that the company could not be verified — and
    scored full marks for stage on the word "public" inside that sentence. The
    thinner the research, the higher the rank.
    """
    part = fc.stage_points(blob, {})
    assert part.points == fc._unknown(fc.MAX_STAGE), part.reason
    assert "not read" in part.reason


def test_an_unverifiable_company_keeps_whatever_stage_it_did_state():
    """WISEcode's dossier, verbatim in shape: early stage *and* unverifiable.

    Masking the decoy does not blank the blob — the bucket the dossier really
    supports is still there to be read, one notch above nothing.
    """
    part = fc.stage_points(
        "Cisco alum, early stage; funding detail thin in public sources", {})
    assert part.points == 4, part.reason
    assert "early stage" in part.reason


def test_masking_a_decoy_leaves_the_real_stage_signal_standing():
    """Veeva is a public benefit corporation *and* listed on the NYSE.

    "Public benefit" is a charter rather than a listing — plenty of private AI
    labs carry it — so it is masked, and the company still clears the bar on
    the ticker that proves it.
    """
    part = fc.stage_points(
        "Public benefit corporation, NYSE: VEEV, life sciences vertical SaaS", {})
    assert part.points == fc.MAX_STAGE
    assert "nyse" in part.reason.lower()


def test_quantifind_keeps_full_marks_for_the_right_reason():
    """Full marks either way, which is exactly why this was hard to notice.

    The score did not move; the reason did, from "public" — read off "the
    public sector" — to the round the dossier actually states.
    """
    part = fc.stage_points(
        "Financial crime AI: AML, KYC, risk screening for financial services "
        "and the public sector. Founded 2009, $117.9M raised, latest round "
        "Series H-II, $22M (2025)", {})
    assert part.points == fc.MAX_STAGE
    assert "series h" in part.reason


@pytest.mark.parametrize("blob", [
    "Public",                                     # startups.funding_stage
    "$175K/yr • Software · Public Company • 89%",  # the aggregator blurb
    "Public; leadership in flux",                  # MongoDB
    "Public, security software, ~10,000 staff",     # CrowdStrike, Okta
    "Public healthcare data and clinical AI",       # Doximity, Tempus
    "Public (NYSE: Z), large cap",                  # Zillow
])
def test_a_real_public_company_still_scores_full_marks(blob):
    """The guard is a list of named contexts, never a neighbouring word.

    Three of these look exactly like a decoy — "public" followed by an
    unrelated noun — and all three are companies that really are listed. A
    rule that keyed on the next word would break them.
    """
    part = fc.stage_points(blob, {})
    assert part.points == fc.MAX_STAGE, part.reason


def test_a_decoy_is_cut_only_on_a_whole_phrase():
    """"Republic sector" is not "public sector", and "publicly" is its own word."""
    assert fc._mask("Republic Sector Holdings", ("public sector",)) == "republic sector holdings"
    assert fc.stage_points("Publicly traded, 4,000 staff", {}).points == fc.MAX_STAGE


def test_config_can_add_a_stage_decoy():
    cfg = {"fit_ranking": {"stage_decoys": ["public transit"]}}
    assert fc.stage_points("Public transit ticketing, Series A", cfg).points == 4
    assert fc.stage_points("Public transit ticketing, Series A", {}).points == fc.MAX_STAGE


# --- plurals ----------------------------------------------------------------
@pytest.mark.parametrize("phrase,plural", [
    ("enterprise", "enterprises"),
    ("individual contributor", "individual contributors"),
    ("connector", "connectors"),
    ("business", "businesses"),          # sibilant: + "es"
    # Too short to pluralise safely: "hrs" is hours, and the rest already have
    # their plural listed outright.
    ("hr", ""),
    ("ai", ""),
    ("api", ""),
    ("sdk", ""),
    # No stem changes. "securities" is not "security".
    ("security", ""),
    ("model quality", ""),
    ("information technology", ""),
])
def test_the_plural_rule_is_narrow(phrase, plural):
    assert fc._plural(phrase) == plural


@pytest.mark.parametrize("blob", [
    "We sell to enterprises in regulated industries",
    "Our customers are enterprises, not individuals",
])
def test_a_plural_customer_signal_counts(blob):
    """The gap that started this: "enterprises" missed the `enterprise` signal.

    `job_lens._phrase_in` is boundary-anchored, so the vocabularies only scored
    the exact form someone happened to write down.
    """
    part = fc.customer_points(blob, {})
    assert part.points == fc.MAX_CUSTOMER, part.reason


def test_an_explicit_ic_plural_beats_a_manager_phrase():
    """Ashby's Sr PM posting, which this moved from #51 to #13 on the board.

    The body says "we believe individual contributors do their best work when
    they have a deep understanding" — and, in its list of who the role is *not*
    for, "you dislike written documentation and prefer to manage the team via
    meetings". Singular-only matching saw the second and not the first.
    """
    body = ("At Ashby it's different: we believe individual contributors do "
            "their best work when they have a deep understanding of the "
            "problem. You're primarily a fit elsewhere if you dislike written "
            "documentation and prefer to manage the team via meetings.")
    part = fc.ic_points("Senior Product Manager", body, {})
    assert part.points == fc.MAX_IC, part.reason
    assert "individual contributor" in part.reason


def test_a_plural_domain_hit_is_reported_in_the_singular_it_matched():
    """The reason quotes the vocabulary entry, not the word in the posting.

    Worth pinning: the entry is what a config edit would have to change, and 41
    companies' reason strings changed this way with no change in score.
    """
    part = fc.domain_points("Own the agent orchestrations and their guardrails", {})
    assert "orchestration" in part.reason
    assert "guardrail" in part.reason


def test_a_short_token_does_not_pluralise_into_a_false_industry():
    """"40 hrs" must not read as HR tech, which is why the floor is 4."""
    part = fc.industry_points("Full-time, 40 hrs per week, Austin TX", {})
    assert part.points == fc._unknown(fc.MAX_INDUSTRY), part.reason
