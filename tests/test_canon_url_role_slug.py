"""canon_url must recognise a company-hosted posting path, and still refuse an index.

canon_url accepted a path only if some segment carried a digit or the path had
three or more segments. Companies that host their own board write neither:

    hex.tech/careers/senior-product-manager-monetization-

Two segments, no digits, so canon_url returned "" and decided_duplicate's
"same posting URL" test — its only CERTAIN signal — could never fire for that
host. The cost was visible in the data: two rows for that one posting, #1706
Applied and #2144 Not Interested, and nothing in the pipeline able to see they
were the same link.

The risk in widening this is the thing canon_url exists to prevent: collapsing
two different roles that share one careers-index URL. That is guarded
separately by _INDEX_PATH, which runs first, so the tests below pin both sides
— the slug is accepted, and every index shape still comes back "".
"""

from __future__ import annotations

import pytest

import database


@pytest.mark.parametrize("url", [
    "https://hex.tech/careers/senior-product-manager-monetization-",
    "https://acme.com/careers/senior-product-manager-payments",
    "https://acme.com/jobs/staff-product-manager-growth",
])
def test_a_role_slug_identifies_one_posting(url):
    assert database.canon_url(url), f"{url} should resolve to a posting key"


@pytest.mark.parametrize("url", [
    "https://binti.com/careers",
    "https://www.zoominfo.com/careers",
    "https://acme.com/jobs",
    "https://acme.com/careers/",
    "https://acme.com/open-positions",
    "https://acme.com/join-us",
])
def test_an_index_page_still_identifies_nothing(url):
    """These are the URLs that must never merge two roles into one."""
    assert database.canon_url(url) == "", f"{url} is an index and must stay unkeyed"


@pytest.mark.parametrize("url", [
    "https://acme.com/about-us",
    "https://acme.com/careers/product-manager",   # 2 words, under the length floor
    "https://acme.com/team",
])
def test_a_short_or_two_word_segment_is_not_a_role_slug(url):
    """A role slug is three-plus words and long. Less than that is a section."""
    assert database.canon_url(url) == ""


@pytest.mark.parametrize("url", [
    "https://acme.com/company/about/careers",
    "https://acme.com/en/us/jobs",
    "https://acme.com/global/2026/open-positions",
])
def test_a_DEEP_index_path_is_still_an_index(url):
    """Only _INDEX_PATH can reject these, so only these prove it is doing work.

    Written after mutation-testing this file: deleting the _INDEX_PATH guard
    entirely left every other case in here passing, because a shallow
    /careers is already refused by the posting-shape test for having one
    short segment. An index that is three segments deep — or carries a year —
    satisfies the shape test and is caught by nothing else. That is the shape
    that would collapse two roles behind one careers URL.
    """
    assert database.canon_url(url) == "", f"{url} is an index and must stay unkeyed"


def test_the_hex_pair_now_shares_one_key():
    """The regression this exists for: one posting, two rows, one link."""
    a = database.canon_url("https://hex.tech/careers/senior-product-manager-monetization-")
    b = database.canon_url("https://hex.tech/careers/senior-product-manager-monetization-")
    assert a and a == b


def test_two_different_slugs_on_one_host_stay_apart():
    """Widening must not make every posting on a host look like one job."""
    a = database.canon_url("https://hex.tech/careers/senior-product-manager-monetization-")
    b = database.canon_url("https://hex.tech/careers/senior-product-manager-platform")
    assert a and b and a != b


def test_the_ats_urls_this_never_touched_are_unchanged():
    """Ashby/Greenhouse/Lever paths already qualified on their ids."""
    u = "https://jobs.ashbyhq.com/clipboard/b2dd9381-765e-4633-bd64-087551a7bfea"
    assert database.canon_url(u) == "//jobs.ashbyhq.com/clipboard/b2dd9381-765e-4633-bd64-087551a7bfea"


def test_same_posting_url_is_a_certain_duplicate_signal():
    """End to end: the widened key reaches decided_duplicate's CERTAIN branch."""
    decided = [{"role_title": "Senior Product Manager, Monetization",
                "url": "https://hex.tech/careers/senior-product-manager-monetization-",
                "status": "Applied"}]
    v = database.decided_duplicate(
        "output-arrowSenior Product Manager, Monetization",
        "https://hex.tech/careers/senior-product-manager-monetization-",
        decided)
    assert v is not None
    assert v["certain"] is True
    assert v["reason"] == "same posting URL"
    assert v["status"] == "Applied"
