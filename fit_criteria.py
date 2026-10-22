"""Score a company's open roles against the stated search criteria.

A SECOND AXIS, NEVER A GATE — the same contract `job_lens` keeps. The research
score in `fit_dossiers.json` says how good a company is: team, funding, comp,
sponsorship evidence, what the dossier found. This says how well its open roles
match the criteria that were set on 2026-10-22:

    Company stage     Public or Series C+
    Ideal role        Product Manager or Sr. Product Manager
    Customer type     B2B, Enterprise
    Domains           AI/LLMs, AI evaluation & quality, AI agents,
                      workflow automation, API integrations
    Industries        Fintech, Enterprise SaaS, Healthtech
    Location          SF, Bay Area, or remote
    Role type         IC, not people manager

Three readings of those criteria were settled by Nate rather than guessed, and
each one is the reason a component is shaped the way it is:

  * Forward-deployed titles. "Forward Deployed Product Manager" is a PM title
    and scores full marks. "Deployment Strategist" and "AI Strategist" are
    customer-facing solutions roles, so they score a notch below PM rather than
    being demoted out of sight — half the tier-1 board carries those titles.
  * Stage is a heavy weight, not a gate. Public and Series C+ earn full marks;
    Series B and earlier lose most of those points but stay on the board, named.
    A gate here would delete most AI-agent companies, which is where the domain
    criteria point.
  * Industry rewards the three named sectors, treats other B2B-enterprise
    sectors as neutral, and penalises consumer. Devtools and security are not
    named targets but they are not misses either.

Nothing here can cut a row, and nothing here writes to the database. It ranks
what the artifact already shows, so the worst it can do is sort badly.

The reasons are the product. Every component reports what it found and what it
could not read, because a number that cannot be audited gets quietly mistrusted
and then ignored — and half of these signals come from aggregator text that is
frequently thin and occasionally wrong.

Pure functions, no network, no database. Config is injected so the caller
controls which config is in play.

Matching is `job_lens`'s, imported rather than reimplemented: the same
whole-phrase, boundary-anchored rules that decide a lens signal decide a domain
hit here. Two copies of that logic would drift, and the drift would be silent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from job_lens import _norm, _phrase_in


# Component ceilings. They sum to 100 so the number reads as a percentage of
# the criteria met, and they are weighted by how much each criterion actually
# decided a yes or no in past screens: role shape and domain are what Nate
# sorts on, stage is the new constraint, industry and customer type are
# qualifiers he has mostly treated as context.
MAX_STAGE = 20
MAX_ROLE = 25
MAX_IC = 15
MAX_CUSTOMER = 10
MAX_DOMAIN = 20
MAX_INDUSTRY = 10
MAX_TOTAL = MAX_STAGE + MAX_ROLE + MAX_IC + MAX_CUSTOMER + MAX_DOMAIN + MAX_INDUSTRY

# An unread signal scores the midpoint, not zero. Most of these facts come from
# aggregator blurbs that are present for some sources and absent for others, so
# zeroing silence would rank companies by how talkative their job board is.
# Every such midpoint says "not read" in its reason.
_UNKNOWN_FRACTION = 0.5


def _unknown(ceiling: int) -> int:
    return round(ceiling * _UNKNOWN_FRACTION)


# --- stage ------------------------------------------------------------------
# Ordered most specific first: "series c" must be tested before the bare
# "series" shapes, and "pre-ipo" before "ipo".
_STAGE_FULL = (
    "public", "publicly traded", "public company", "nasdaq", "nyse", "ipo",
    "pre-ipo", "post-ipo", "late stage", "series c", "series d", "series e",
    "series f", "series g", "series h", "series i", "series j",
    # A PE-owned firm with a thousand staff is past Series C in substance. The
    # bare phrase "private equity" is deliberately NOT here: Accordion's
    # posting uses it to describe its clients, not its own cap table.
    "pe backed", "private equity backed", "privately held",
)
_STAGE_MID = ("series b", "growth stage")
_STAGE_EARLY = (
    "seed", "pre-seed", "preseed", "series a", "early stage", "pre-revenue",
    "stealth", "founding team", "yc ", "y combinator",
)

# What each bucket scores. Series B keeps 8 of 20 rather than 4: it is one
# round under the bar, and the difference between "nearly there" and "seed"
# is exactly the distinction the ranking is for.
_STAGE_POINTS = {"full": MAX_STAGE, "mid": 8, "early": 4}


@dataclass
class Part:
    """One component's verdict: points, ceiling, and why."""

    key: str
    label: str
    points: int
    ceiling: int
    reason: str

    @property
    def unread(self) -> bool:
        return "not read" in self.reason or "nothing" in self.reason.lower()


@dataclass
class CriteriaFit:
    """What the criteria concluded about one role, and why."""

    score: int = 0
    role: str = ""
    parts: list[Part] = field(default_factory=list)

    def part(self, key: str) -> Optional[Part]:
        return next((p for p in self.parts if p.key == key), None)

    @property
    def reasons(self) -> list[str]:
        return [f"{p.label}: {p.reason} ({p.points}/{p.ceiling})" for p in self.parts]

    def as_dict(self) -> dict:
        """The shape the artifact template consumes."""
        return {
            "score": self.score,
            "role": self.role,
            "parts": [
                {"key": p.key, "label": p.label, "pts": p.points,
                 "max": p.ceiling, "why": p.reason}
                for p in self.parts
            ],
        }


def _cfg(cfg: dict) -> dict:
    return ((cfg or {}).get("fit_ranking") or {})


def _vocab(cfg: dict, key: str, default: tuple) -> tuple:
    """A config list, falling back to the shipped default.

    The defaults are the spec. Config overrides them so the vocabulary can be
    tuned without a code change, which is the same arrangement `job_matching`
    has — and the same reason: these lists are judgement, not logic.
    """
    got = _cfg(cfg).get(key)
    if isinstance(got, (list, tuple)) and got:
        return tuple(str(x) for x in got)
    return default


def stage_points(blob: str, cfg: dict) -> Part:
    """Stage from whatever text the caller could assemble about the company.

    The caller is responsible for *what* goes in the blob — dossier snapshot,
    `startups.funding_stage`, the aggregator's stage word, a configured list of
    public employers. This only reads it. That split is deliberate: the sources
    disagree often enough that which one won has to be visible at the call
    site, not buried here.
    """
    hay = _norm(blob)
    for bucket, phrases in (("full", _vocab(cfg, "stage_full", _STAGE_FULL)),
                            ("mid", _vocab(cfg, "stage_mid", _STAGE_MID)),
                            ("early", _vocab(cfg, "stage_early", _STAGE_EARLY))):
        hit = next((p for p in phrases if _phrase_in(p, hay)), None)
        if hit:
            pts = _STAGE_POINTS[bucket]
            if bucket == "full":
                why = f"“{hit.strip()}” — public or Series C+, at or above your bar"
            elif bucket == "mid":
                why = (f"“{hit.strip()}” — one round under your Series C+ bar "
                       "(an aggregator “growth stage” spans Series A to C)")
            else:
                why = f"“{hit.strip()}” — well below your Series C+ bar"
            return Part("stage", "Stage", pts, MAX_STAGE, why)
    return Part("stage", "Stage", _unknown(MAX_STAGE), MAX_STAGE,
                "not read from the dossier, the funding feed or the posting")


# --- role title -------------------------------------------------------------
# The ideal: PM or Sr PM, including the forward-deployed and AI variants, which
# are PM titles whatever the prefix.
_ROLE_IDEAL = (
    "product manager", "senior product manager", "sr product manager",
    "sr. product manager", "product management", "pm",
)
# IC but above the stated level. Still strong, one point under ideal.
_ROLE_SENIOR_IC = ("principal product manager", "staff product manager",
                   "lead product manager")
# A notch below PM: customer-facing solutions work, the shape Palantir named.
_ROLE_ADJACENT = (
    "deployment strategist", "ai strategist", "strategist",
    "forward deployed engineer", "solutions architect", "solutions engineer",
    "solutions consultant", "applied ai", "customer impact", "agent operator",
    "agent operations", "deployment engineer", "technical account manager",
)
# People-manager or executive levels — scored low here *and* in the IC
# component, because a director title is both the wrong level and the wrong
# role type, and the criteria state both.
_ROLE_MANAGER_LEVEL = (
    "director", "head of", "vp", "vice president", "chief", "group product manager",
    "senior director", "sr director", "manager of product",
)
# Below the level bar in the other direction.
_ROLE_JUNIOR = ("associate", "intern", "internship", "graduate", "new grad",
                "apprentice", "rotational")
# PM-adjacent titles that are not the job: program/project delivery and
# marketing. Scored low rather than excluded, since the board does surface them.
_ROLE_OFF_SHAPE = ("program manager", "project manager", "product marketing",
                   "product owner", "business analyst", "product designer")

_ROLE_POINTS = {"ideal": MAX_ROLE, "senior_ic": 20, "adjacent": 17,
                "off_shape": 8, "junior": 6, "manager_level": 6}


def role_points(title: str, cfg: dict) -> Part:
    """Score the title shape against "Product Manager or Sr. Product Manager".

    Order matters and is the whole design. "Director of Product Management"
    contains "product management"; "Associate Product Manager" contains
    "product manager". The disqualifying shapes are therefore tested first, so
    a level word beats a title match rather than being swamped by it.
    """
    t = _norm(title)
    if not t:
        return Part("role", "Role", _unknown(MAX_ROLE), MAX_ROLE, "no title on the row")

    for key, phrases, why in (
        ("manager_level", _vocab(cfg, "role_manager_level", _ROLE_MANAGER_LEVEL),
         "a people-manager or executive level, not the IC PM seat you want"),
        ("junior", _vocab(cfg, "role_junior", _ROLE_JUNIOR),
         "below your level"),
        ("senior_ic", _vocab(cfg, "role_senior_ic", _ROLE_SENIOR_IC),
         "senior IC PM — above “Sr PM” but the right shape"),
        ("off_shape", _vocab(cfg, "role_off_shape", _ROLE_OFF_SHAPE),
         "PM-adjacent, but not product management"),
        ("adjacent", _vocab(cfg, "role_adjacent", _ROLE_ADJACENT),
         "forward-deployed / solutions work — a notch below PM proper, "
         "by your call on 22 Oct"),
        ("ideal", _vocab(cfg, "role_ideal", _ROLE_IDEAL),
         "Product Manager — exactly the stated target"),
    ):
        hit = next((p for p in phrases if _phrase_in(p, t)), None)
        if hit:
            return Part("role", "Role", _ROLE_POINTS[key], MAX_ROLE,
                        f"“{hit.strip()}” — {why}")

    return Part("role", "Role", _unknown(MAX_ROLE), MAX_ROLE,
                "title matches none of your role shapes")


# --- IC vs people manager ---------------------------------------------------
# Checked against all 207 posting bodies on the board before this list was
# settled, because a people-management false positive costs a role 12 of 15
# points. Two candidate phrases were cut on the evidence:
#
#   "hire and grow"  — 4 hits, 4 of them values boilerplate ("We hire and grow
#                      against three leadership principles" — TRM Labs,
#                      Vitalize). Zero were duties.
#   "lead a team"    — 1 hit, and it meant influence, not reports ("a futurist
#                      who can collaboratively lead a team to build towards a
#                      vision" — Kikoff). Kept only in the "lead a team of"
#                      form, which is how a real reporting line is written.
#
# "build and lead" stays: 2 of its 3 hits are genuine (Wonderschool's growth
# lead wants "ability to build and lead high-performing teams"), and the third
# is Thyme Care's "individual contributor role today, with the opportunity to
# build and lead a product team over time" — which _IC_SIGNALS catches first,
# on purpose. That ordering is load-bearing, not incidental.
_MANAGER_SIGNALS = (
    "direct reports", "manage a team", "manage the team", "lead a team of",
    "lead the team", "build and lead", "hire and manage", "people management",
    "people manager", "manage and coach", "coach and develop",
    "team of product managers", "lead and manage", "managing a team",
    "performance reviews",
)
_IC_SIGNALS = ("individual contributor", "no direct reports", "ic role")


def ic_points(title: str, body: str, cfg: dict) -> Part:
    """IC or people manager.

    Default is IC. Most PM postings never say either way, and in this market
    an unqualified "Product Manager" is an IC seat — so silence here is read
    as IC rather than scored as unknown. What flips it is an explicit
    people-management duty in the body, or a managing title.
    """
    hay = _norm(f"{title} {body}")
    hit = next((p for p in _vocab(cfg, "ic_signals", _IC_SIGNALS)
                if _phrase_in(p, hay)), None)
    if hit:
        return Part("ic", "Role type", MAX_IC, MAX_IC, f"“{hit.strip()}” — IC, as you want")

    hit = next((p for p in _vocab(cfg, "manager_signals", _MANAGER_SIGNALS)
                if _phrase_in(p, hay)), None)
    if hit:
        return Part("ic", "Role type", 3, MAX_IC,
                    f"“{hit.strip()}” — carries people management")

    return Part("ic", "Role type", MAX_IC, MAX_IC,
                "no people-management duty stated — read as IC")


# --- customer type ----------------------------------------------------------
_B2B_SIGNALS = ("b2b", "enterprise", "saas", "business customers",
                "enterprise customers", "fortune 500", "mid-market",
                "business to business", "commercial customers")
_CONSUMER_SIGNALS = ("b2c", "consumer", "direct to consumer", "households",
                     "shoppers", "fans", "players", "subscribers", "families")


def customer_points(blob: str, cfg: dict) -> Part:
    """B2B/Enterprise against consumer.

    Both can appear in one posting — a consumer marketplace selling SaaS to its
    providers is exactly Wonderschool — so B2B is tested first and a stated
    enterprise motion wins. The reason names the token either way, because this
    is the component most likely to be read off a one-line aggregator blurb.
    """
    hay = _norm(blob)
    hit = next((p for p in _vocab(cfg, "b2b_signals", _B2B_SIGNALS)
                if _phrase_in(p, hay)), None)
    if hit:
        return Part("customer", "Customers", MAX_CUSTOMER, MAX_CUSTOMER,
                    f"“{hit.strip()}” — B2B / enterprise, as you want")
    hit = next((p for p in _vocab(cfg, "consumer_signals", _CONSUMER_SIGNALS)
                if _phrase_in(p, hay)), None)
    if hit:
        return Part("customer", "Customers", 2, MAX_CUSTOMER,
                    f"“{hit.strip()}” — reads consumer, not B2B")
    return Part("customer", "Customers", _unknown(MAX_CUSTOMER), MAX_CUSTOMER,
                "no customer type stated")


# --- domains ----------------------------------------------------------------
# One entry per named domain. Each hit is worth DOMAIN_PER_HIT, capped at
# MAX_DOMAIN, so matching four of the five is full marks: the criteria list
# alternatives, not a checklist to complete.
# One entry per named domain, each with its own weight. Flat scoring put twelve
# companies on a perfect 100 in the first run, because "AI" and "API" appear in
# nearly every PM posting on this board and four hits of five was easy. The
# weights say which domains are the point: agents and evaluation are the charter
# Nate is hunting, AI/LLM generally is table stakes, and workflow automation and
# API integrations are adjacent surfaces worth a nod. Five hits is 20; so is
# agents + evals + AI + either tail.
_DOMAINS = {
    "AI agents": (5, ("agent", "agents", "agentic", "multi agent", "tool use",
                      "autonomous agent", "copilot")),
    "AI eval & quality": (5, ("eval", "evals", "evaluation", "evaluations",
                              "benchmark", "benchmarks", "hallucination",
                              "guardrail", "guardrails", "model quality",
                              "observability", "golden set", "red team")),
    "AI / LLMs": (4, ("ai", "llm", "llms", "genai", "generative ai",
                      "foundation model", "machine learning", "gpt")),
    "Workflow automation": (3, ("workflow", "workflows", "automation", "automate",
                                "orchestration", "rpa", "process automation",
                                "back office")),
    "API integrations": (3, ("api", "apis", "integration", "integrations",
                             "webhook", "webhooks", "connector", "connectors",
                             "sdk")),
}


def domain_points(blob: str, cfg: dict) -> Part:
    """How many of the five named domains the posting speaks to, by weight.

    Capped at MAX_DOMAIN, so a posting that says "AI" forty times does not
    outrank one that genuinely spans agents, evals and integrations.
    """
    hay = _norm(blob)
    override = _cfg(cfg).get("domains")
    domains = override if isinstance(override, dict) and override else _DOMAINS

    found: list[str] = []
    pts = 0
    for label, (weight, phrases) in domains.items():
        hit = next((p for p in phrases if _phrase_in(p, hay)), None)
        if hit:
            found.append(f"{label} (\u201c{hit.strip()}\u201d)")
            pts += weight

    if not found:
        return Part("domain", "Domains", 0, MAX_DOMAIN,
                    "none of your five domains appear in the posting")
    return Part("domain", "Domains", min(MAX_DOMAIN, pts), MAX_DOMAIN,
                f"{len(found)} of {len(domains)} \u2014 " + "; ".join(found))


# --- industry ---------------------------------------------------------------
_INDUSTRY_TARGET = {
    "Fintech": ("fintech", "financial services", "payments", "banking", "lending",
                "insurance", "insurtech", "capital markets", "accounting",
                "treasury", "wealth"),
    "Enterprise SaaS": ("enterprise saas", "enterprise software", "b2b saas",
                        "saas platform"),
    "Healthtech": ("healthtech", "healthcare", "health care", "clinical", "patient",
                   "payer", "provider network", "ehr", "medical records",
                   "digital health", "life sciences"),
}
# B2B-enterprise sectors that are not named targets and not misses. Neutral by
# Nate's call on 22 Oct: they still satisfy B2B/Enterprise.
_INDUSTRY_NEUTRAL = (
    "software", "information technology", "developer tools", "devtools",
    "cyber security", "security", "big data", "data infrastructure", "cloud",
    "legal", "legaltech", "govtech", "public sector", "logistics", "supply chain",
    "manufacturing", "marketing", "hr", "recruiting", "real estate", "proptech",
    "telecom", "communications", "artificial intelligence", "analytics",
)
_INDUSTRY_CONSUMER = (
    "consumer", "consumer goods", "gaming", "games", "media", "entertainment",
    "social", "travel", "food", "retail", "fashion", "fitness", "dating",
    "education", "edtech", "family care", "childcare", "sports",
)
_INDUSTRY_POINTS = {"target": MAX_INDUSTRY, "neutral": 6, "consumer": 2}


def industry_points(blob: str, cfg: dict) -> Part:
    """Fintech / Enterprise SaaS / Healthtech, then other B2B, then consumer."""
    hay = _norm(blob)
    override = _cfg(cfg).get("industry_target")
    targets = override if isinstance(override, dict) and override else _INDUSTRY_TARGET

    for label, phrases in targets.items():
        hit = next((p for p in phrases if _phrase_in(p, hay)), None)
        if hit:
            return Part("industry", "Industry", _INDUSTRY_POINTS["target"],
                        MAX_INDUSTRY, f"{label} (“{hit.strip()}”) — a named target")

    hit = next((p for p in _vocab(cfg, "industry_neutral", _INDUSTRY_NEUTRAL)
                if _phrase_in(p, hay)), None)
    if hit:
        return Part("industry", "Industry", _INDUSTRY_POINTS["neutral"], MAX_INDUSTRY,
                    f"“{hit.strip()}” — B2B, but not one of your three sectors")

    hit = next((p for p in _vocab(cfg, "industry_consumer", _INDUSTRY_CONSUMER)
                if _phrase_in(p, hay)), None)
    if hit:
        return Part("industry", "Industry", _INDUSTRY_POINTS["consumer"], MAX_INDUSTRY,
                    f"“{hit.strip()}” — consumer, outside your sectors")

    return Part("industry", "Industry", _unknown(MAX_INDUSTRY), MAX_INDUSTRY,
                "no industry stated")


# --- the whole thing --------------------------------------------------------
def score_role(role: dict, cfg: dict) -> CriteriaFit:
    """Score one open role.

    `role` carries the text this reads, assembled by the caller:
        title        the role title
        body         posting body, if one was stored
        stage_blob   every text we hold that might state the company's stage
        company_blob company description / dossier text, for industry+customers
    """
    title = role.get("title") or ""
    body = role.get("body") or ""
    company_blob = role.get("company_blob") or ""
    # Domain and customer type are properties of the work, so the posting body
    # leads; the company blurb is the fallback for the many rows with no body.
    work_blob = f"{title} {body} {company_blob}"

    parts = [
        stage_points(role.get("stage_blob") or "", cfg),
        role_points(title, cfg),
        ic_points(title, body, cfg),
        customer_points(work_blob, cfg),
        domain_points(work_blob, cfg),
        # Industry is a property of the employer, so this reads the company
        # blurb and dossier only. Reading the posting body scored Accordion
        # as Healthtech off "Retail, CPG, Healthcare, Media & Entertainment"
        # — a list of its consulting clients.
        industry_points(company_blob, cfg),
    ]
    return CriteriaFit(score=sum(p.points for p in parts), role=title, parts=parts)


def score_company(roles: list[dict], cfg: dict) -> Optional[CriteriaFit]:
    """The best-scoring open role, which is the one worth applying to.

    A company is ranked by its best role rather than its average: Wonderschool
    has an agent PM seat, a payments PM seat and a growth lead that manages a
    team, and averaging those three would describe no decision Nate can make.
    Ties break toward the earlier role, which is the order the pipeline found
    them in.
    """
    scored = [score_role(r, cfg) for r in roles if (r.get("title") or "").strip()]
    if not scored:
        return None
    return max(scored, key=lambda f: f.score)
