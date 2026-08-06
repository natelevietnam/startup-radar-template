"""Config-driven filters for startups and jobs."""

import re

from models import Startup, JobMatch

# ---------------------------------------------------------------------------
# Remote-location scoping
#
# Job boards happily describe a posting as "Remote" when it is remote *within
# Iberia* or *within Germany*. A bare substring test for "remote" therefore
# lets non-US listings through. `remote_scope: us` uses the signals below to
# keep only US-eligible remote roles.
# ---------------------------------------------------------------------------

# A remote listing that names any of these — and names no US signal — is
# treated as based outside the US. Names only; two-letter country codes are
# handled separately (below) because most of them collide with US state
# abbreviations (DE=Delaware, IN=Indiana, CA=California, ...).
_NON_US_NAMES = (
    # Multi-country regions
    "emea", "apac", "latam", "europe", "european union", "eea", "iberia",
    "nordics", "benelux", "dach", "middle east", "asia pacific",
    # Countries / nations
    "united kingdom", "great britain", "england", "scotland", "wales",
    "northern ireland", "ireland", "canada", "mexico", "brazil", "argentina",
    "chile", "colombia", "peru", "uruguay", "costa rica", "germany", "france",
    "spain", "portugal", "italy", "netherlands", "belgium", "luxembourg",
    "switzerland", "austria", "poland", "czechia", "czech republic", "slovakia",
    "hungary", "romania", "bulgaria", "greece", "turkey", "ukraine", "serbia",
    "croatia", "sweden", "norway", "denmark", "finland", "iceland", "estonia",
    "latvia", "lithuania", "india", "pakistan", "bangladesh", "china",
    "hong kong", "taiwan", "japan", "south korea", "singapore", "malaysia",
    "thailand", "vietnam", "indonesia", "philippines", "australia",
    "new zealand", "israel", "united arab emirates", "saudi arabia", "qatar",
    "egypt", "nigeria", "kenya", "ghana", "south africa", "morocco",
    # Cities that appear as the only geographic signal on foreign postings
    "london", "manchester", "edinburgh", "dublin", "berlin", "munich",
    "cologne", "koln", "köln", "hamburg", "frankfurt", "paris", "lyon",
    "madrid", "barcelona", "lisbon", "porto", "amsterdam", "rotterdam",
    "brussels", "zurich", "geneva", "basel", "vienna", "prague", "warsaw",
    "krakow", "budapest", "bucharest", "stockholm", "oslo", "copenhagen",
    "helsinki", "milan", "rome", "athens", "istanbul", "tel aviv", "dubai",
    "bangalore", "bengaluru", "hyderabad", "mumbai", "delhi", "pune",
    "chennai", "tokyo", "osaka", "seoul", "shanghai", "beijing", "shenzhen",
    "taipei", "sydney", "melbourne", "auckland", "toronto", "vancouver",
    "montreal", "ottawa", "calgary", "sao paulo", "são paulo", "mexico city",
    "buenos aires", "bogota", "bogotá", "lagos", "nairobi", "cape town",
)

# Job boards (Wellfound, Welcome to the Jungle) tail their location strings
# with an ISO country code: "Köln, NRW, de (Remote)", "Homebased, England, gb
# (Remote)", "Remote, pt (Remote)". In *this* position the token is
# unambiguously a country, so two-letter codes are safe to read here.
_TRAILING_COUNTRY_CODE = re.compile(r",\s*([a-z]{2})\s*\(\s*remote\s*\)")

# Any US signal in the string means the role is open to US-based candidates,
# even when other countries are also listed ("Remote - US | Remote - Canada").
_US_SIGNAL = re.compile(r"\b(u\s?s\s?a?|united states)\b")


def _normalize_location(location: str) -> str:
    """Lowercase and flatten punctuation so 'U.S.' reads as 'u s'."""
    return re.sub(r"[.'’]", " ", location.lower())


def is_non_us_remote(location: str) -> bool:
    """True if `location` describes a remote role based outside the US.

    Conservative by design: a listing is rejected only when it names a remote
    arrangement, names a non-US place, and names no US signal at all. Bare
    "Remote", "Boston or Remote", and "Remote, Los Angeles" are all kept.
    """
    if not location:
        return False
    norm = _normalize_location(location)
    if "remote" not in norm:
        return False
    if _US_SIGNAL.search(norm):
        return False
    code = _TRAILING_COUNTRY_CODE.search(norm)
    if code and code.group(1) != "us":
        return True
    return any(re.search(r"\b" + re.escape(n) + r"\b", norm) for n in _NON_US_NAMES)


_STAGE_ORDER = {
    "pre-seed": 0,
    "preseed": 0,
    "seed": 1,
    "series-a": 2,
    "series a": 2,
    "series-b": 3,
    "series b": 3,
    "series-c": 4,
    "series c": 4,
    "series-d": 5,
    "series d": 5,
}


def _stage_rank(stage: str) -> int:
    if not stage:
        return -1
    s = stage.lower().strip()
    for key, rank in _STAGE_ORDER.items():
        if key in s:
            return rank
    m = re.search(r"series\s+([a-f])", s)
    if m:
        return 2 + (ord(m.group(1)) - ord("a"))
    return -1


def _excluded_company_patterns(targets: dict) -> list:
    """Compile word-boundary patterns for `excluded_companies` config.

    Word boundaries avoid false positives (e.g. 'stripe' won't match
    'Pinstripe') while still catching 'Allstate Insurance', 'Stripe, Inc.'
    """
    names = [c.lower().strip() for c in targets.get("excluded_companies", []) if c.strip()]
    return [re.compile(r"\b" + re.escape(n) + r"\b") for n in names]


def _company_excluded(company_name: str, patterns: list) -> bool:
    if not company_name or not patterns:
        return False
    name = company_name.lower()
    return any(p.search(name) for p in patterns)


def _parse_amount_musd(amount: str) -> float:
    if not amount:
        return 0.0
    m = re.search(r"\$?\s*([\d,.]+)\s*(m|million|b|billion)", amount, re.IGNORECASE)
    if not m:
        return 0.0
    val = float(m.group(1).replace(",", ""))
    if m.group(2).lower().startswith("b"):
        val *= 1000
    return val


class StartupFilter:
    def __init__(self, cfg: dict):
        targets = cfg["targets"]
        self.locations = [loc.lower() for loc in targets.get("locations", [])]
        self.industries = [ind.lower() for ind in targets.get("industries", [])]
        self.min_stage = targets.get("min_stage", "any").lower()
        self.min_stage_rank = _stage_rank(self.min_stage) if self.min_stage != "any" else -1
        self.large_seed_threshold = float(targets.get("large_seed_threshold_musd", 50))
        self._ind_patterns = [
            re.compile(r"\b" + re.escape(k) + r"\b") for k in self.industries
        ]
        self._excl_co_patterns = _excluded_company_patterns(targets)

    def passes(self, s: Startup) -> bool:
        return (
            not _company_excluded(s.company_name, self._excl_co_patterns)
            and self._stage_ok(s.funding_stage, s.amount_raised)
            and self._location_ok(s.location)
            and self._industry_ok(s)
        )

    def filter(self, startups: list[Startup]) -> list[Startup]:
        return [s for s in startups if self.passes(s)]

    def _stage_ok(self, stage: str, amount: str) -> bool:
        if self.min_stage == "any" or not stage:
            return True
        rank = _stage_rank(stage)
        if rank < 0:
            return True
        if rank >= self.min_stage_rank:
            return True
        if rank == 1 and _parse_amount_musd(amount) >= self.large_seed_threshold:
            return True
        return False

    def _location_ok(self, location: str) -> bool:
        if not self.locations:
            return True
        if not location:
            return True
        lower = location.lower()
        return any(loc in lower for loc in self.locations)

    def _industry_ok(self, s: Startup) -> bool:
        if not self._ind_patterns:
            return True
        text = f"{s.company_name} {s.description}".lower()
        return any(p.search(text) for p in self._ind_patterns)


class JobFilter:
    def __init__(self, cfg: dict):
        targets = cfg["targets"]
        self.roles = [r.lower() for r in targets.get("roles", [])]
        self.exclusions = [e.lower() for e in targets.get("seniority_exclusions", [])]
        self.locations = [loc.lower() for loc in targets.get("locations", [])]
        self._excl_co_patterns = _excluded_company_patterns(targets)
        # Word boundaries, so "intern" doesn't swallow "Internal Financial Data"
        # and "lead" wouldn't swallow "Leadership".
        self._excl_seniority_patterns = [
            re.compile(r"\b" + re.escape(e) + r"\b") for e in self.exclusions
        ]
        # Regex forms of the seniority rules, for tiers plain substrings can't
        # express (e.g. "Lead *Data* Product Manager").
        self._title_excl_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in targets.get("title_exclusion_patterns", [])
            if str(p).strip()
        ]
        # Matched against the feed-supplied "level: X" marker, not the title.
        self.level_exclusions = [
            lv.lower().strip() for lv in targets.get("level_exclusions", []) if lv.strip()
        ]
        # "us" keeps only US-eligible remote roles; "any" preserves the old
        # behaviour of accepting every listing containing the word "remote".
        self.remote_scope = str(targets.get("remote_scope", "any")).lower().strip()

    def company_excluded(self, company_name: str) -> bool:
        return _company_excluded(company_name, self._excl_co_patterns)

    def role_matches(self, title: str) -> bool:
        if not title:
            return False
        t = title.lower()
        if any(p.search(t) for p in self._excl_seniority_patterns):
            return False
        if not self.roles:
            return True
        return any(r in t for r in self.roles)

    def seniority_excluded(self, title: str, description: str = "") -> bool:
        """True if the role sits outside the seniority band you're targeting.

        Three rules, any of which excludes:
          1. `seniority_exclusions`      — substring match on the title
          2. `title_exclusion_patterns`  — regex match on the title
          3. `level_exclusions`          — match on the feed's "level: X" marker

        Used to drop over/under-qualified roles at ingestion without requiring
        a positive role match (unlike role_matches).
        """
        t = (title or "").lower()
        if t:
            if any(p.search(t) for p in self._excl_seniority_patterns):
                return True
            if any(p.search(title) for p in self._title_excl_patterns):
                return True
        if description and self.level_exclusions:
            m = re.search(r"level:\s*([\w -]+)", description, re.IGNORECASE)
            if m and m.group(1).strip().lower() in self.level_exclusions:
                return True
        return False

    def location_excluded(self, location: str) -> bool:
        """True if the location is disqualifying regardless of `location_filter`.

        Unlike `location_matches` (a positive "is this one of my cities?" test
        that individual sources opt into), this is a hard gate: a remote role
        based outside the US is never eligible, even on all-remote boards that
        skip location filtering entirely.
        """
        return self.remote_scope == "us" and is_non_us_remote(location)

    def location_matches(self, location: str) -> bool:
        if self.location_excluded(location):
            return False
        if not self.locations:
            return True
        if not location:
            return False
        lower = location.lower()
        if "remote" in lower:
            return True
        return any(loc in lower for loc in self.locations)

    def passes(self, j: JobMatch) -> bool:
        return (
            not self.company_excluded(j.company_name)
            and self.role_matches(j.role_title)
            and self.location_matches(j.location)
        )

    def filter(self, jobs: list[JobMatch]) -> list[JobMatch]:
        return [j for j in jobs if self.passes(j)]
