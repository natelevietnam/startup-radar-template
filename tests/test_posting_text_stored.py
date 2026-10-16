"""The sponsorship pass must keep the posting body it reads.

Every reader in enrich_sponsorship — and the HTML fallback behind them — fetches
a whole posting and, until this change, discarded all of it but one sentence of
evidence. The cost showed up on the lens: 48 of the rows on the 11 Oct screen
scored with the reason "No posting body stored — signals could not be read", so
23 of them came out at an identical 20, which is the Bay Area location constant
and nothing else. The text was never unavailable; nothing kept it.

No network: a fake text source stands in for the ATS readers.
"""

from __future__ import annotations

import sqlite3

import pytest

import database
import enrich_sponsorship as es
import filters


REAL_BODY = (
    "We are hiring a forward deployed product manager to sit with enterprise "
    "customers, build evals for agent behaviour, and own the deployment "
    "playbook. You will work with llm tooling and define guardrails. "
) * 6                                      # comfortably past the 400-char guard

REFUSAL_BODY = REAL_BODY + " At this time we will not sponsor a new applicant for employment authorization."


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    monkeypatch.setattr(es, "DB", path)
    return path


def seed(db, rows):
    con = sqlite3.connect(str(db))
    for r in rows:
        con.execute(
            "INSERT INTO job_matches (company_name, role_title, url, status, "
            "sponsorship, description_text, lens_tier, lens_score, lens_reasons) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (r["co"], r.get("role", "Senior Product Manager"), r["url"],
             r.get("status", ""), r.get("sponsorship"), r.get("text", ""),
             r.get("lens_tier"), r.get("lens_score"), r.get("lens_reasons", "")))
    con.commit()
    con.close()


def read(db):
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    out = {r["company_name"]: dict(r) for r in con.execute("SELECT * FROM job_matches")}
    con.close()
    return out


def use_body(monkeypatch, mapping):
    """Point every reader at `mapping`, keyed by url. Missing url -> unreadable."""
    def fake(url):
        body = mapping.get(url)
        return ("fake-api", body) if body else None
    monkeypatch.setattr(es, "_TEXT_SOURCES", (fake,))

    def no_html(*a, **kw):                 # the fallback must not reach the network
        raise AssertionError("HTML fallback attempted")
    monkeypatch.setattr(es.requests, "get", no_html)


# The pass refuses to run unless targets.require_visa_sponsorship is set, so
# the gate is configured rather than patched out: this is the only config key
# the behaviour under test depends on.
CFG = {"targets": {"require_visa_sponsorship": True}}


def require_sponsorship(monkeypatch):
    monkeypatch.setattr(es, "load_config", lambda: CFG)
    assert filters.JobFilter(CFG).require_visa_sponsorship


# --- _fetch hands the body back ------------------------------------------
def test_fetch_returns_the_body_it_read(monkeypatch):
    use_body(monkeypatch, {"u1": REAL_BODY})
    row, stance, evidence, note, text = es._fetch({"url": "u1", "id": 1})
    assert stance == es.SILENT          # says nothing about visas
    assert text == REAL_BODY            # ...and the body still comes back


def test_a_refusal_also_returns_the_body(monkeypatch):
    use_body(monkeypatch, {"u1": REFUSAL_BODY})
    _, stance, evidence, _, text = es._fetch({"url": "u1", "id": 1})
    assert stance == es.REFUSED
    assert "sponsor" in evidence
    assert text == REFUSAL_BODY


def test_thin_shell_body_is_not_returned(monkeypatch):
    """A client-rendered shell must not be stored: to the lens it would read
    exactly like a posting that mentions none of its signals."""
    use_body(monkeypatch, {"u1": "Jobs | Careers | About | Sign in"})
    _, stance, _, note, text = es._fetch({"url": "u1", "id": 1})
    assert stance is None and "too thin" in note
    assert text == ""


def test_unreadable_posting_returns_no_body(monkeypatch):
    use_body(monkeypatch, {})
    _, stance, _, _, text = es._fetch({"url": "nope", "id": 1})
    assert stance is None and text == ""


def test_missing_url_returns_no_body(monkeypatch):
    use_body(monkeypatch, {})
    _, stance, _, note, text = es._fetch({"url": "", "id": 1})
    assert stance is None and note == "no url" and text == ""


# --- main() stores it, and reopens those rows for scoring ----------------
def test_main_stores_the_body_and_reopens_the_row(db, monkeypatch):
    seed(db, [{"co": "Readable", "url": "u1", "lens_tier": 1, "lens_score": 20,
               "lens_reasons": '["No posting body stored"]'},
              {"co": "Unreadable", "url": "u2", "lens_tier": 1, "lens_score": 20,
               "lens_reasons": '["No posting body stored"]'}])
    use_body(monkeypatch, {"u1": REAL_BODY})
    require_sponsorship(monkeypatch)

    es.main([])
    rows = read(db)
    assert "forward deployed" in rows["Readable"]["description_text"]
    # Scored blind, so the score is withdrawn rather than left standing.
    assert rows["Readable"]["lens_score"] is None
    assert rows["Readable"]["lens_tier"] is None
    assert rows["Readable"]["lens_reasons"] == ""
    # The row whose posting could not be read keeps everything it had.
    assert rows["Unreadable"]["description_text"] == ""
    assert rows["Unreadable"]["lens_score"] == 20
    assert rows["Unreadable"]["lens_tier"] == 1


def test_dry_run_stores_nothing(db, monkeypatch):
    seed(db, [{"co": "Readable", "url": "u1", "lens_score": 20}])
    use_body(monkeypatch, {"u1": REAL_BODY})
    require_sponsorship(monkeypatch)

    es.main(["--dry-run"])
    row = read(db)["Readable"]
    assert row["description_text"] == "" and row["lens_score"] == 20


def test_body_is_capped(db, monkeypatch):
    seed(db, [{"co": "Verbose", "url": "u1"}])
    use_body(monkeypatch, {"u1": "forward deployed " * 40_000})
    require_sponsorship(monkeypatch)

    es.main([])
    assert len(read(db)["Verbose"]["description_text"]) == es._TEXT_CAP


def test_default_pass_skips_a_row_that_already_has_both(db, monkeypatch):
    """Default selection is unchanged: stance stored means the row is done."""
    seed(db, [{"co": "Done", "url": "u1", "sponsorship": es.SILENT,
               "text": "already stored"}])
    use_body(monkeypatch, {"u1": REAL_BODY})
    require_sponsorship(monkeypatch)

    es.main([])
    assert read(db)["Done"]["description_text"] == "already stored"


def test_missing_text_reads_a_row_whose_stance_is_already_stored(db, monkeypatch):
    """--missing-text is the backfill: stance settled, body never kept."""
    seed(db, [{"co": "Backfill", "url": "u1", "sponsorship": es.SILENT,
               "lens_score": 20}])
    use_body(monkeypatch, {"u1": REAL_BODY})
    require_sponsorship(monkeypatch)

    es.main(["--missing-text"])
    row = read(db)["Backfill"]
    assert "forward deployed" in row["description_text"]
    assert row["lens_score"] is None


def test_decided_rows_are_never_touched(db, monkeypatch):
    seed(db, [{"co": "Applied", "url": "u1", "status": "Applied", "lens_score": 20}])
    use_body(monkeypatch, {"u1": REAL_BODY})
    require_sponsorship(monkeypatch)

    es.main(["--missing-text"])
    row = read(db)["Applied"]
    assert row["description_text"] == "" and row["lens_score"] == 20


# --- the HTML fallback must keep only the posting ------------------------
# A whole careers page carries the posting and every other job advertised
# beside it. The two shapes sit in opposite places, which is the reason
# _strip_chrome excises regions instead of truncating at the first marker:
# Google's jobs-search list is at the TOP, before the posting, and LinkedIn's
# "Similar jobs" rail is at the BOTTOM, after the requirements and after the
# sponsorship and EEO text.
GOOGLE_PAGE = (
    "job details . arrow_back . Back to jobs search . Jobs search results "
    "3,411 jobs matched. "
    "AI Outcome Engineer, Forward Deployed Engineering Milan, Italy . "
    "Group Product Manager, Finance Agent, AI Workflows San Jose, CA . "
    "Evals Engineer, Polaris, DeepMind Mountain View, CA . "
    "Showing 1 to 20 of 3411 rows 1-20 of 3411. navigate_next . "
    "Equal Opportunity Google is proud to be an equal opportunity employer. "
    + "Minimum qualifications: experience with guardrails and LLM interfaces. " * 8
)

LINKEDIN_PAGE = (
    "About the role: you will own the platform. "
    + "Requirements: experience shipping enterprise customers tooling. " * 8
    + "We are an equal opportunity employer. Visa sponsorship is not available "
      "for this role. "
    + "Similar jobs . Product Manager, Ads Agent TikTok San Jose, CA . "
      "People also viewed . Product Manager, Pro Fulfillment Thumbtack . "
)


def test_googles_leading_jobs_list_is_excised_and_the_posting_kept():
    out = es._strip_chrome(GOOGLE_PAGE)
    assert "forward deployed" not in out.lower()      # a job in the list
    assert "finance agent" not in out.lower()         # another one
    assert "evals engineer" not in out.lower()        # and another
    assert "minimum qualifications" in out.lower()    # the posting itself
    assert "guardrails" in out.lower()                # its real signals
    assert "equal opportunity" in out.lower()         # and its legal text


def test_linkedins_trailing_rail_is_removed():
    out = es._strip_chrome(LINKEDIN_PAGE)
    assert "ads agent" not in out.lower()
    assert "people also viewed" not in out.lower()
    assert "pro fulfillment" not in out.lower()
    assert "enterprise customers" in out.lower()


def test_the_sponsorship_sentence_survives_the_rail_being_cut():
    """The rail sits after the sponsorship and EEO text on every LinkedIn row
    checked, so cleaning must not cost a refusal. This is the regression that
    matters most: losing it would turn a refusal into silence."""
    out = es._strip_chrome(LINKEDIN_PAGE)
    assert "visa sponsorship is not available" in out.lower()
    assert es.classify(out)[0] == es.REFUSED


def test_a_page_with_no_other_jobs_on_it_is_untouched():
    body = "Senior Product Manager. You will own agent evaluation and guardrails. " * 9
    assert es._strip_chrome(body) == body.strip()


def test_an_unterminated_jobs_list_is_left_alone():
    """The excision needs both ends. Without the pagination line that closes
    Google's list there is no evidence of where the chrome stops, and removing
    to the end of the document would take the posting with it."""
    body = ("Back to jobs search . Jobs search results 12 jobs matched. "
            + "Minimum qualifications: you have shipped agent tooling. " * 9)
    out = es._strip_chrome(body)
    assert "minimum qualifications" in out.lower()


def test_fallback_text_is_cleaned_but_ats_text_is_not(monkeypatch):
    """_strip_chrome runs on the fallback only: an ATS API returns one
    posting's own description, where a phrase like "similar jobs" would be the
    employer's own words."""
    def fake_source(url):
        return ("fake-api", LINKEDIN_PAGE) if url == "ats" else None
    monkeypatch.setattr(es, "_TEXT_SOURCES", (fake_source,))

    _, _, _, _, ats_text = es._fetch({"url": "ats", "id": 1})
    assert "ads agent" in ats_text.lower()            # untouched

    class FakeResp:
        status_code = 200
        text = LINKEDIN_PAGE
    monkeypatch.setattr(es.requests, "get", lambda *a, **kw: FakeResp())
    _, _, _, via, html_text = es._fetch({"url": "elsewhere", "id": 2})
    assert via == "html"
    assert "ads agent" not in html_text.lower()       # cleaned


def test_a_page_that_is_all_chrome_reads_as_unread_not_silent(monkeypatch):
    """Cleaning can leave almost nothing. The existing 400-character guard then
    does its job: the row stays NULL and is retried, rather than being recorded
    as a posting that mentioned none of its signals."""
    page = ("Back to jobs search . Jobs search results 9 jobs matched. "
            "Product Manager, Ads Agent TikTok . "
            "Showing 1 to 9 of 9 rows . Equal opportunity employer.")
    monkeypatch.setattr(es, "_TEXT_SOURCES", ())

    class FakeResp:
        status_code = 200
        text = page
    monkeypatch.setattr(es.requests, "get", lambda *a, **kw: FakeResp())
    _, stance, _, note, text = es._fetch({"url": "u", "id": 1})
    assert stance is None and "too thin" in note and text == ""
