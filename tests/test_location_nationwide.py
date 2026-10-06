"""is_us_nationwide: "anywhere in the US" versus a city list with a country on it.

The rule exists because an employer's own board writes a distributed US role as
a bare "United States" — Databricks posts its forward-deployed roles that way —
and such a role is as reachable as the plain "remote" the location list already
accepts.

It was checking whether ANY "|"-separated part looked nationwide, so Workday's
habit of appending the country to a list of offices defeated it: "McLean, VA |
Richmond, VA | Chicago, IL | New York, NY | United States of America" read as
location-free on the strength of its last segment. Four named cities, none in
the Bay Area, admitted as though the role had no location at all.
"""

from __future__ import annotations

import pytest

import filters


@pytest.mark.parametrize("location", [
    "United States",
    "United States of America",
    "USA",
    "US",
    "Remote - US | United States",
    "United States | USA",
])
def test_genuinely_nationwide(location):
    assert filters.is_us_nationwide(location) is True


@pytest.mark.parametrize("location", [
    # The case this fixes: real offices with the country appended.
    "McLean, VA | Richmond, VA | United States of America",
    "McLean, VA | Richmond, VA | Chicago, IL | New York, NY | United States of America",
    "Charlotte, NC, USA | Dallas, TX, USA | New York, NY - 730 Third Avenue",
    "CUMBERLAND PKWY OFFICE - 1147 | United States of America",
    "San Antonio Home Office I | Phoenix Campus (Main) | Tampa Campus",
    # A Bay Area city with the country appended is matched by the location list
    # itself, so it must not need this rule — and must not be called nationwide.
    "San Francisco, CA | United States of America",
    "",
    "San Francisco, CA",
])
def test_not_nationwide(location):
    assert filters.is_us_nationwide(location) is False


def test_a_bay_area_posting_still_passes_the_gate_without_this_rule():
    """Narrowing the nationwide rule must not drop a genuine Bay Area listing."""
    from config_loader import load_config

    flt = filters.JobFilter(load_config())
    for location in ("San Francisco, CA | United States of America",
                     "Palo Alto, CA | New York, NY",
                     "Mountain View, CA"):
        assert flt.location_matches(location), location


def test_remote_still_wins_regardless():
    from config_loader import load_config

    flt = filters.JobFilter(load_config())
    assert flt.location_matches("McLean, VA | Remote") is True
