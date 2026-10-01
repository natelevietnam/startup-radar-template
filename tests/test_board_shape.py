"""Tests for the board-payload parsers.

Compensation parsing gets most of the attention because it is the only place a
connector can produce a confidently WRONG number: comp_known=1 on a misread
figure changes how the lens scores the row and whether it clears the floor,
whereas comp_known=0 merely scores as "no band published".

The OTE cases are not hypothetical. Spot-checking the first four text-parsed
bands on Vercel's live board found two that were on-target earnings, including
"$170,000-$209,000 OTE" — base could be half that.
"""

from __future__ import annotations

import pytest

from sources.jobs import _shape


@pytest.mark.parametrize("text,expected", [
    # kept — these are base salary
    ("The San Francisco, CA base pay range for this role is $208,000 - $312,000",
     (208_000, 312_000)),
    ("$150K - $200K base", (150_000, 200_000)),
    ("compensation is $160,000 - $210,000", (160_000, 210_000)),
    ("Base salary $180,000 - $220,000. OTE with commission is higher.",
     (180_000, 220_000)),
    ("The OTE range is $300,000 - $400,000. Base salary is $150,000 - $200,000.",
     (150_000, 200_000)),

    # rejected — not base salary
    ("The San Francisco, CA OTE range for this role is $170,000-$209,000", (None, None)),
    ("OTE pay range for this role is $100,000 - $120,000 OTE", (None, None)),
    ("$170,000 - $209,000 OTE", (None, None)),
    ("Total compensation ranges from $200,000 to $400,000", (None, None)),
    ("commission-based $90,000 - $140,000", (None, None)),

    # rejected — not a plausible annual base
    ("Hourly $30 - $45", (None, None)),
    ("Equity grant valued $3,000,000 - $5,000,000", (None, None)),

    # nothing to find
    ("", (None, None)),
    ("Competitive salary and equity", (None, None)),
    ("Salary is $180,000", (None, None)),          # single figure, not a range
])
def test_parse_comp_from_text(text, expected):
    assert _shape.parse_comp_from_text(text) == expected


@pytest.mark.parametrize("word", ["remote", "note", "quote", "promote", "footnote"])
def test_words_containing_ote_do_not_veto_a_band(word):
    """"ote" was matched as a substring, so "for remote roles" killed the band."""
    text = f"Base pay is $150,000 - $200,000 for {word} roles"
    assert _shape.parse_comp_from_text(text) == (150_000, 200_000)


def test_strip_html_handles_greenhouse_double_encoding():
    """Greenhouse serves `content` escaped, so one unescape pass is not enough."""
    raw = "&lt;div&gt;&lt;p&gt;Own the&amp;nbsp;roadmap&lt;/p&gt;&lt;/div&gt;"
    out = _shape.strip_html(raw)
    assert "<" not in out and "&lt;" not in out
    assert "&nbsp;" not in out and "&amp;" not in out
    assert "roadmap" in out


def test_strip_html_on_empty_and_plain():
    assert _shape.strip_html("") == ""
    assert "plain text" in _shape.strip_html("plain text")


@pytest.mark.parametrize("value,expected", [
    ("2026-09-29T16:24:41.888+00:00", "2026-09-29"),       # Ashby
    ("2026-09-28T18:08:11-04:00", "2026-09-28"),           # Greenhouse
    (1789479220358, "2026-09-15"),                          # Lever, ms epoch
    ("2026-09-29", "2026-09-29"),
    ("2026-09-29T16:24:41Z", "2026-09-29"),
    (None, None),
    ("", None),
    ("last Tuesday", None),
    ("not-a-date-at-all", None),
])
def test_iso_date(value, expected):
    assert _shape.iso_date(value) == expected


def test_row_sets_comp_known_only_when_both_ends_are_present():
    base = dict(company_name="X", role_title="PM", location="SF", url="u",
                provider="ashby", provider_job_id="1", source="Ashby board")
    assert _shape.row(**base, comp_min=150_000, comp_max=200_000)["comp_known"] == 1
    assert _shape.row(**base, comp_min=150_000, comp_max=None)["comp_known"] == 0
    assert _shape.row(**base)["comp_known"] == 0


def test_row_shape_matches_what_the_db_expects():
    r = _shape.row(company_name=" X ", role_title=" PM ", location=" SF ", url="u",
                   provider="lever", provider_job_id=99, source="Lever board")
    assert r["company_name"] == "X" and r["role_title"] == "PM" and r["location"] == "SF"
    assert r["provider_job_id"] == "99", "ids are stringified, Lever's are UUIDs"
    for key in ("company_name", "company_description", "role_title", "location",
                "url", "source", "provider", "provider_job_id", "posted_at",
                "description_text", "comp_min", "comp_max", "comp_known"):
        assert key in r
