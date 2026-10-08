"""The Workday CXS reader.

Workday renders a posting client-side, so fetching the page returns a shell
with no requirements in it. That made every Workday employer a sponsorship
blind spot: the stance came back NULL — meaning only "never successfully read" —
and require_visa_sponsorship had nothing to act on. PNC, General Motors,
Capital One and USAA all reached the board that way, and two were caught only
because a human opened the posting in a browser.

No network here: the reader's URL parsing and the classifier are tested against
the real sentences those two employers publish.
"""

from __future__ import annotations

import pytest

import enrich_sponsorship as es


@pytest.mark.parametrize("url", [
    "https://capitalone.wd12.myworkdayjobs.com/Capital_One/job/McLean-VA/Manager--Product-Management_R246457-2",
    "https://generalmotors.wd5.myworkdayjobs.com/Careers_GM/job/Warren-Michigan/Senior-PM_JR-1",
    "https://pnc.wd5.myworkdayjobs.com/External/job/PA---Pittsburgh-15222/Product-Manager_R1",
    "https://guidewire.wd5.myworkdayjobs.com/external/job/United-States---Remote/Senior-PM_R2",
])
def test_workday_urls_are_recognised(url, monkeypatch):
    """The reader must accept every Workday tenant shape on the board."""
    seen = {}

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"jobPostingInfo": {"jobDescription": "nothing relevant"}}

    def fake_get(u, **kw):
        seen["url"] = u
        return FakeResp()

    monkeypatch.setattr(es.requests, "get", fake_get)
    out = es._text_workday(url)
    assert out is not None, "a Workday URL must be recognised"
    assert out[0] == "workday-cxs"
    assert "/wday/cxs/" in seen["url"], "must call the CXS API, not the page"


@pytest.mark.parametrize("url", [
    "https://job-boards.greenhouse.io/vercel/jobs/6209001004",
    "https://jobs.ashbyhq.com/Jerry.ai/abc",
    "https://www.linkedin.com/jobs/view/123",
    "https://capitalone.com/careers/job/1",          # right company, not Workday
    "",
])
def test_non_workday_urls_are_declined(url):
    assert es._text_workday(url) is None


def test_the_reader_is_registered_last_so_it_cannot_shadow_an_ats_api():
    assert es._text_workday in es._TEXT_SOURCES
    sources = list(es._TEXT_SOURCES)
    assert sources.index(es._text_workday) > sources.index(es._text_greenhouse)
    assert sources.index(es._text_workday) > sources.index(es._text_ashby)


@pytest.mark.parametrize("sentence,company", [
    ("At this time, Capital One will not sponsor a new applicant for employment "
     "authorization for this position.", "Capital One"),
    ("USAA does not provide visa sponsorship for this role.", "USAA"),
])
def test_the_real_refusals_classify_as_refused(sentence, company):
    """The exact sentences these employers publish, which the page never served."""
    stance, evidence = es.classify(sentence)
    assert stance == es.REFUSED, company
    assert evidence, "a refusal must carry the sentence that proves it"


def test_a_payload_naming_no_stance_is_silent_not_refused():
    """Silence is never a refusal — the rule the whole module is built on."""
    stance, _ = es.classify(
        "Own the roadmap for our payments platform. Collaborate with engineering.")
    assert stance != es.REFUSED
