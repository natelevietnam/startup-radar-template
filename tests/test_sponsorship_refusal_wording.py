"""A refusal has to be read as a refusal whichever noun the employer uses.

A flat "we will not sponsor" is the one stance that takes a row off the board
outright, so the words that express it are load-bearing. The refusal list spelled
out "visa", "work" and "employment" sponsorship and stopped there, which left
"immigration sponsorship" — the wording in-house counsel tends to use — reading
as silence. Accordion's AI Product Manager (row 2351) closes with "This position
is not eligible for immigration sponsorship." and still arrived on the 21 Oct
artifact with its visa gate open; so did its Presales PM posting and Cloudera's
Senior PM, which carry the same sentence.

Silence and the hedged offer are checked alongside, because the cheap way to pass
the cases below is to match the word "sponsorship" near a negation and sweep up
every employer who merely mentions the subject.
"""

from __future__ import annotations

import pytest

import enrich_sponsorship as es


REFUSALS = [
    # The wording this test exists for.
    "This position is not eligible for immigration sponsorship.",
    "This role is not eligible for immigration sponsorship at this time.",
    "We are seeking candidates who do not require immigration sponsorship.",
    "Applicants must not require immigration sponsorship, now or in the future.",
    # The nouns that already worked, kept here so a rewrite can't trade one for
    # another.
    "This position is not eligible for visa sponsorship.",
    "This position is not eligible for work sponsorship.",
    "This position is not eligible for employment sponsorship.",
    "This position is not eligible for sponsorship.",
]

NOT_REFUSALS = [
    # Says nothing either way.
    "We are an equal opportunity employer and value diverse perspectives.",
    "You will own the agent evaluation stack end to end.",
    # An offer, and the hedge that reads as one on purpose: Anthropic's "we do
    # sponsor visas" followed by a caveat must not be flipped into a refusal.
    "We offer visa sponsorship for this role.",
    "Visa sponsorship: we do sponsor visas! We aren't able to successfully "
    "sponsor visas for every role and every candidate.",
]


@pytest.mark.parametrize("sentence", REFUSALS)
def test_refusal_wording_is_read_as_refused(sentence):
    stance, evidence = es.classify(sentence)
    assert stance == es.REFUSED, f"read as {stance!r}: {sentence}"
    assert "sponsor" in evidence, "the stored evidence should quote the sentence"


@pytest.mark.parametrize("sentence", NOT_REFUSALS)
def test_silence_and_offers_are_not_refusals(sentence):
    stance, _ = es.classify(sentence)
    assert stance != es.REFUSED, f"read as a refusal: {sentence}"
