"""The feed title cleaner.

NewPMJobs renders client-side and its API has twice served a DOM element name
welded onto the front of a title: "output-arrowSenior Product Manager,
Monetization". It has to be stripped at ingest, because the prefix defeats every
dedupe guard simultaneously — it changes the (company_name, role_title) unique
index key and canon_role together, so the same posting re-enters as a new row.

On 2026-10-04 that resurfaced a Hex role already marked Applied. Cleaning the
database after the fact does not help: the next run re-inserts it, which is
exactly what happened after the 2026-09-21 cleanup.
"""

from __future__ import annotations

import pytest

import database
from sources.newpmjobs import _clean_title


@pytest.mark.parametrize("raw,expected", [
    ("output-arrowSenior Product Manager, Monetization",
     "Senior Product Manager, Monetization"),
    ("output-arrowSenior Product Manager, Growth", "Senior Product Manager, Growth"),
    ("OUTPUT-ARROWProduct Manager", "Product Manager"),
    ("output_arrowProduct Manager", "Product Manager"),
    ("arrow-rightProduct Manager", "Product Manager"),
    ("chevron-rightProduct Manager", "Product Manager"),
    ("output-arrowoutput-arrowProduct Manager", "Product Manager"),
    ("  output-arrowProduct Manager  ", "Product Manager"),
])
def test_junk_prefixes_are_stripped(raw, expected):
    assert _clean_title(raw) == expected


@pytest.mark.parametrize("title", [
    "Senior Product Manager, Monetization",
    "Product Manager",
    "Product Manager, Output Arrows",          # the words, not the prefix
    "Senior PM — Arrow Platform",
    "",
])
def test_legitimate_titles_are_untouched(title):
    assert _clean_title(title) == title.strip()


def test_the_prefix_defeated_both_dedupe_guards():
    """Why this must be fixed at ingest rather than in the database.

    The unique index is on (company_name, role_title) and the decided-row check
    goes through canon_role. The prefix changes both, so neither can see that
    the clean row already exists.
    """
    dirty = "output-arrowSenior Product Manager, Monetization"
    clean = "Senior Product Manager, Monetization"
    assert dirty != clean, "the unique index would not have caught it"
    assert database.canon_role(dirty) != database.canon_role(clean), \
        "canon_role would not have caught it either"
    assert _clean_title(dirty) == clean, "which is why it is stripped on the way in"
