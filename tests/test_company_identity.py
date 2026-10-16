"""Two spellings of one job must canonicalise to one key.

Both cases here were found the same way: a role Nate had already decided about
came back into Uncategorized as an undecided arrival, because the decision was
filed under a different spelling of the same employer or the same title.

  * "HealthLeap AI" (Jobright) vs "Healthleap" (the company's own Ashby board).
    The "AI" is a SPACED suffix, so _COMPANY_TLD — which only strips ".ai" and
    friends attached to the name — could not touch it, and the two keys stayed
    apart. An Applied decision on 2026-09-23 therefore did not suppress the same
    employer's Technical PM req arriving on 2026-10-12.
  * "Technical PM" vs "Technical Product Manager", which is the same pair of
    rows seen through the two feeds.

Fixing only one of these would have left the pair split, so both are asserted
together, along with the abbreviations that must NOT be expanded: TPM, PMO and
PMM are different roles, and collapsing them into "product manager" would hide
real openings — the exact failure mode canon_role's docstring warns about.
"""

from __future__ import annotations

import pytest

import database


@pytest.mark.parametrize("spelling", ["HealthLeap AI", "Healthleap", "HealthLeap", "healthleap ai"])
def test_healthleap_spellings_are_one_company(spelling):
    assert database.canon_company(spelling) == "healthleap"


@pytest.mark.parametrize("title,expected", [
    ("Technical PM", "technical product manager"),
    ("Technical Product Manager", "technical product manager"),
    ("PM", "product manager"),
    ("Senior PM, Payments", "senior product manager payments"),
    ("Sr. PM", "sr product manager"),
])
def test_pm_expands_to_product_manager(title, expected):
    assert database.canon_role(title) == expected


@pytest.mark.parametrize("title", ["TPM", "PMO Lead", "PMM", "Product Marketing Manager (PMM)"])
def test_pm_lookalikes_are_left_alone(title):
    """These are other roles. Expanding them would merge unrelated openings."""
    assert "product manager" not in database.canon_role(title)


def test_the_healthleap_pair_now_shares_one_job_key():
    """The end-to-end property: one key, so a decision on either covers both."""
    jobright = database._canon_job_key("HealthLeap AI", "Technical PM")
    own_board = database._canon_job_key("Healthleap", "Technical Product Manager")
    assert jobright == own_board == "healthleap|technical product manager"
