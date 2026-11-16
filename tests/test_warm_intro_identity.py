"""A warm intro has to be a real one.

Found on 2026-11-05 while writing the Box dossier. `search_connections_by_company`
matched the company field with `LIKE '%Box%'`, so the four people it offered as
introductions to Box worked at Inbox Health, BoxGroup, BLACKBOX AI and Juicebox.
Nothing downstream could tell, because the card only prints a name and a title.

The asymmetry is what makes this worth a test file of its own: everywhere else
in the pipeline a fuzzy match costs a duplicate row or a ranking place, and here
it costs an email to a stranger on a claim of shared employment. So the rule is
exact canonical equality for a claim about a person, and the cases below pin
both sides of it — the strangers that must be rejected, and the real spellings
of one employer that must still be found.
"""

from __future__ import annotations

import pandas as pd
import pytest

import database


# --- the live bug -----------------------------------------------------------
@pytest.mark.parametrize("stranger", [
    "Inbox Health",
    "BoxGroup",
    "BLACKBOX AI",
    "Juicebox",
    "Boxed",
    "Dropbox",
])
def test_a_substring_is_not_an_employer(stranger):
    assert not database.connection_matches("Box", stranger)


def test_box_is_still_box():
    for spelling in ["Box", "box", "Box, Inc.", "Box Inc", "BOX", "Box | Content Cloud"]:
        assert database.connection_matches("Box", spelling), spelling


# --- the free-text field ----------------------------------------------------
@pytest.mark.parametrize("field,target", [
    ("Box | Content Cloud", "Box"),
    ("Stripe (Payments)", "Stripe"),
    ("Google / Alphabet", "Google"),
    ("Mercury - Banking for startups", "Mercury"),
    ("Snowflake, Inc.", "Snowflake"),
    ("Product Manager at Verkada", "Verkada"),
    ("Applied Intuition, Inc", "Applied Intuition"),
])
def test_a_name_plus_a_tagline_still_names_the_employer(field, target):
    assert database.connection_matches(target, field)


def test_keys_cover_the_whole_field_and_its_segments():
    keys = database.connection_company_keys("Box | Content Cloud")
    assert "box" in keys
    assert "content cloud" in keys


@pytest.mark.parametrize("field", ["", "   ", None])
def test_an_empty_company_field_names_nobody(field):
    assert database.connection_company_keys(field) == set()
    assert not database.connection_matches("Box", field)


def test_an_empty_target_matches_nobody():
    """Otherwise a company with no name would match every connection at once."""
    assert not database.connection_matches("", "Box")
    assert not database.connection_matches("   ", "Box")
    assert not database.connection_matches("Inc.", "Box, Inc.")


# --- one employer, two spellings --------------------------------------------
def test_the_rebrands_canon_company_knows_still_match():
    """canon_company already folds these, and the matcher inherits it."""
    assert database.connection_matches("Abnormal Security", "Abnormal AI")
    assert database.connection_matches("C3 AI", "C3")
    assert database.connection_matches("Clipboard", "Clipboard Health")


def test_a_hyphenated_name_is_not_split_into_two_employers():
    """Only a spaced hyphen joins a name to a tagline.

    A bare hyphen is part of the name, so splitting on it would make every
    Mercedes-Benz employee a match for an employer called Benz.
    """
    keys = database.connection_company_keys("Mercedes-Benz")
    assert keys == {"mercedes benz"}
    assert not database.connection_matches("Benz", "Mercedes-Benz")


# --- the frame filter -------------------------------------------------------
def _frame(*companies: str) -> pd.DataFrame:
    return pd.DataFrame({
        "first_name": [f"P{i}" for i in range(len(companies))],
        "last_name": [f"L{i}" for i in range(len(companies))],
        "url": [f"https://example.invalid/{i}" for i in range(len(companies))],
        "company": list(companies),
        "position": ["Product Manager"] * len(companies),
    })


def test_the_filter_keeps_only_the_real_employer():
    frame = _frame("Inbox Health", "Box", "BoxGroup", "Box, Inc.", "Juicebox")
    kept = database._exact_connections(frame, ["Box"])
    assert list(kept["company"]) == ["Box", "Box, Inc."]


def test_the_filter_accepts_any_of_several_targets():
    """Tier-2 intros pass a list of investor firms, not one company."""
    frame = _frame("Sequoia Capital", "BoxGroup", "Lightspeed Venture Partners")
    kept = database._exact_connections(frame, ["Sequoia Capital", "Lightspeed Venture Partners"])
    assert list(kept["company"]) == ["Sequoia Capital", "Lightspeed Venture Partners"]


def test_an_empty_frame_stays_empty():
    assert database._exact_connections(_frame(), ["Box"]).empty


# --- what the exact rule exposed --------------------------------------------
def test_ambience_is_one_employer():
    """Found by this matcher, fixed in canon_company rather than around it.

    An exact search for "Ambience" dropped the one real connection at
    "Ambience Healthcare". Both spellings post from
    jobs.ashbyhq.com/ambiencehealthcare, so they are one employer, and the
    split was already keeping a Not Interested decision on one spelling from
    reaching the other's open req.
    """
    assert database.canon_company("Ambience") == database.canon_company("Ambience Healthcare")
    assert database.connection_matches("Ambience", "Ambience Healthcare")
