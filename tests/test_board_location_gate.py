"""Tests for the location gate the ATS board poller applies.

Written against a real failure: on 2026-10-04 the poller put Snowflake's
"AI Deployment Strategist — JP-Tokyo" on the board, along with Washington D.C.,
a five-city Databricks posting and four rows reading only "United States". The
chain in main.py called `location_excluded`, which despite the name is a hard
gate against non-US *remote* roles rather than a check that the city is one of
the author's — "JP-Tokyo" names no remote arrangement, so nothing caught it.

The positive test is `location_matches`, and these assert both halves of it:
that it rejects what the poller let through, and that it still admits a
nationwide US posting, which is how an employer's own board writes a
location-free role and which a literal reading would otherwise drop.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from filters import JobFilter, is_us_nationwide

REPO = Path(__file__).resolve().parent.parent

# config.yaml is gitignored; the tracked example carries the same targets block.
_CFG_PATH = REPO / "config.yaml"
if not _CFG_PATH.exists():
    _CFG_PATH = REPO / "config.example.yaml"
CFG = yaml.safe_load(_CFG_PATH.read_text())


@pytest.fixture(scope="module")
def flt() -> JobFilter:
    return JobFilter(CFG)


# The exact strings the poller admitted on 2026-10-04, by row id.
@pytest.mark.parametrize("location", [
    "JP-Tokyo",                                                     # 2047 Snowflake
    "Washington, D.C.",                                             # 2043 Databricks
    "Atlanta, Georgia; Boston, Massachusetts; Chicago, Illinois; "
    "New York City, New York; Washington, D.C.",                    # 2040 Databricks
])
def test_rejects_the_locations_the_poller_let_through(flt, location):
    assert not flt.location_matches(location)


def test_tokyo_is_not_caught_by_the_gate_that_was_being_used(flt):
    """The precise reason the bug existed, asserted so it cannot be re-introduced."""
    assert not flt.location_excluded("JP-Tokyo")
    assert not flt.location_matches("JP-Tokyo")


@pytest.mark.parametrize("location", [
    "United States",
    "USA",
    "U.S.",
    "Remote - United States",
    "United States of America",
    "United States (Any Time Zone) (Remote)",
])
def test_admits_a_nationwide_us_posting(flt, location):
    assert flt.location_matches(location)


@pytest.mark.parametrize("location", [
    "JP-Tokyo",
    "London",
    "Remote, United Kingdom",
    "Armenia - Remote",
    "Remote - Americas",
    "Itasca, Illinois, us",      # names a city: targets.locations decides, not this
    "Washington, D.C.",
    "",
])
def test_is_us_nationwide_claims_nothing_it_should_not(location):
    assert not is_us_nationwide(location)


@pytest.mark.parametrize("location", [
    "San Francisco, CA",
    "Remote",
    "Mountain View, CA",
    "San Mateo (Remote)",
    "SF Bay Area",
    "South San Francisco",
])
def test_still_admits_the_cities_and_remote_it_always_did(flt, location):
    assert flt.location_matches(location)


def test_non_us_remote_is_still_rejected_by_both(flt):
    """The original hard gate keeps working; the new rule does not weaken it."""
    for location in ("Remote, United Kingdom", "Remote, Canada", "Remote - India"):
        assert flt.location_excluded(location)
        assert not flt.location_matches(location)


@pytest.mark.xfail(reason="pre-existing: _NON_US_NAMES lists neither Armenia nor "
                          "a catch-all for 'Global Anywhere', so is_non_us_remote "
                          "does not fire and the bare word 'remote' admits the row. "
                          "Unrelated to the board gate; recorded rather than widened here.",
                   strict=True)
@pytest.mark.parametrize("location", ["Armenia - Remote", "Remote - Global Anywhere"])
def test_non_us_remote_misses_some_countries(flt, location):
    assert not flt.location_matches(location)
