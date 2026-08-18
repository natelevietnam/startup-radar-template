"""Startup Radar — pipeline entry point.

Runs enabled sources from config.yaml, filters results by user criteria,
and writes matches to SQLite (and optionally Google Sheets).
"""

import re
import sys
from datetime import datetime

from config_loader import load_config
from filters import StartupFilter
from models import Startup
import database


def _dedup(startups: list[Startup]) -> list[Startup]:
    seen: set[str] = set()
    out: list[Startup] = []
    for s in startups:
        key = re.sub(r"[\s.\-]+", "", s.company_name.lower())
        if key and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _record(failures: list, label: str, exc: Exception) -> None:
    """Print a source failure and remember it so run() can exit non-zero.

    Sources are deliberately isolated — one dead feed must not stop the others
    — but a caught exception used to leave the exit code at 0. That meant the
    GitHub Actions cron reported success for eight consecutive days while every
    Gmail-backed source was failing on an expired OAuth token, and nothing
    surfaced it. Collect failures here and fail the run at the end instead, so
    the scheduler emails on the first bad day rather than the thirtieth.
    """
    print(f"  {label} failed: {exc}")
    failures.append((label, str(exc)))


def run() -> int:
    print("=" * 60)
    print("Startup Radar")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    cfg = load_config()

    output_cfg = cfg.get("output", {})
    sqlite_cfg = output_cfg.get("sqlite", {})
    if sqlite_cfg.get("enabled", True) and sqlite_cfg.get("path"):
        database.set_db_path(sqlite_cfg["path"])

    database.init_db()

    all_startups: list[Startup] = []
    failures: list = []
    sources_cfg = cfg.get("sources", {})

    # --- RSS ---
    rss_cfg = sources_cfg.get("rss", {})
    if rss_cfg.get("enabled"):
        print("\n[RSS] Fetching...")
        from sources import rss
        found = rss.fetch_all(rss_cfg.get("feeds", []))
        print(f"  {len(found)} candidate(s)")
        all_startups.extend(found)

    # --- Hacker News ---
    hn_cfg = sources_cfg.get("hackernews", {})
    if hn_cfg.get("enabled"):
        print("\n[HN] Fetching...")
        from sources import hackernews
        found = hackernews.fetch(
            hn_cfg.get("queries", []),
            lookback_hours=int(hn_cfg.get("lookback_hours", 48)),
        )
        print(f"  {len(found)} candidate(s)")
        all_startups.extend(found)

    # --- SEC EDGAR ---
    edgar_cfg = sources_cfg.get("sec_edgar", {})
    if edgar_cfg.get("enabled"):
        print("\n[EDGAR] Fetching Form D filings...")
        from sources import sec_edgar
        user_cfg = cfg.get("user", {})
        ua_name = user_cfg.get("name") or "Startup Radar"
        ua_email = user_cfg.get("email") or ""
        user_agent = f"{ua_name} {ua_email}".strip() if ua_email else None
        found = sec_edgar.fetch(
            lookback_days=int(edgar_cfg.get("lookback_days", 7)),
            min_amount_musd=float(edgar_cfg.get("min_amount_musd", 5)),
            sic_codes=edgar_cfg.get("industry_sic_codes") or None,
            user_agent=user_agent,
        )
        print(f"  {len(found)} candidate(s)")
        all_startups.extend(found)

    # --- Optional: Gmail ---
    gmail_cfg = sources_cfg.get("gmail", {})
    if gmail_cfg.get("enabled"):
        print("\n[Gmail] Fetching...")
        try:
            from sources import gmail as gmail_src
            found = gmail_src.fetch(gmail_cfg)
            print(f"  {len(found)} candidate(s)")
            all_startups.extend(found)
        except Exception as e:
            _record(failures, "Gmail source", e)

    # --- Optional: ApplyFYI (curated company directory -> startups watchlist) ---
    afy_cfg = sources_cfg.get("applyfyi", {})
    if afy_cfg.get("enabled"):
        print("\n[ApplyFYI] Fetching...")
        try:
            from sources import applyfyi
            from filters import JobFilter
            companies = applyfyi.fetch(afy_cfg)
            print(f"  {len(companies)} curated company(ies)")
            if companies:
                flt = JobFilter(cfg)  # reuse excluded_companies patterns
                existing = database.get_existing_companies()
                rejected = database.get_rejected_companies()
                fresh = [
                    c for c in companies
                    if not flt.company_excluded(c.company_name)
                    and c.company_name.lower().strip() not in existing
                    and c.company_name.lower().strip() not in rejected
                ]
                if fresh:
                    added = database.insert_startups(fresh)
                    print(f"  Added {added} new company(ies) to watchlist")
                    for c in fresh[:10]:
                        stage = f" | {c.funding_stage}" if c.funding_stage else ""
                        print(f"    {c.company_name}{stage}  [{c.location}]")
                    if len(fresh) > 10:
                        print(f"    ... and {len(fresh) - 10} more")
                else:
                    print("  No new companies to add")
        except Exception as e:
            _record(failures, "ApplyFYI source", e)

    # --- Optional: Ali Rohde Jobs (newsletter companies -> startups watchlist) ---
    alr_cfg = sources_cfg.get("alirohde", {})
    if alr_cfg.get("enabled"):
        print("\n[Ali Rohde] Fetching...")
        try:
            from sources import alirohde
            from filters import JobFilter
            companies = alirohde.fetch(alr_cfg)
            print(f"  {len(companies)} tagged company(ies)")
            if companies:
                flt = JobFilter(cfg)  # reuse excluded_companies patterns
                existing = database.get_existing_companies()
                rejected = database.get_rejected_companies()
                fresh = [
                    c for c in companies
                    if not flt.company_excluded(c.company_name)
                    and c.company_name.lower().strip() not in existing
                    and c.company_name.lower().strip() not in rejected
                ]
                if fresh:
                    added = database.insert_startups(fresh)
                    print(f"  Added {added} new company(ies) to watchlist")
                    for c in fresh[:10]:
                        stage = f" | {c.funding_stage}" if c.funding_stage else ""
                        print(f"    {c.company_name}{stage}  [{c.location}]")
                    if len(fresh) > 10:
                        print(f"    ... and {len(fresh) - 10} more")
                else:
                    print("  No new companies to add")
        except Exception as e:
            _record(failures, "Ali Rohde source", e)

    # --- Optional: NewPMJobs.com (public PM job board API -> job_matches) ---
    npj_cfg = sources_cfg.get("newpmjobs", {})
    if npj_cfg.get("enabled"):
        print("\n[NewPMJobs] Fetching...")
        try:
            from sources import newpmjobs
            from filters import JobFilter
            jobs = newpmjobs.fetch(npj_cfg)
            print(f"  {len(jobs)} active PM role(s)")
            flt = JobFilter(cfg)
            _n = len(jobs)
            jobs = [
                j for j in jobs
                if not flt.seniority_excluded(
                    j.get("role_title", ""), j.get("company_description", "")
                )
            ]
            if len(jobs) != _n:
                print(f"  {len(jobs)} after seniority exclusion ({_n - len(jobs)} dropped)")
            # Hard gate — applies even to sources that skip location filtering.
            _n = len(jobs)
            jobs = [j for j in jobs if not flt.location_excluded(j.get("location", ""))]
            if len(jobs) != _n:
                print(f"  {len(jobs)} after non-US remote exclusion ({_n - len(jobs)} dropped)")
            if jobs and npj_cfg.get("location_filter", True):
                jobs = [j for j in jobs if flt.location_matches(j["location"])]
                print(f"  {len(jobs)} after location filter")
            if jobs:
                existing_keys = database.get_existing_job_keys()
                fresh = [
                    j for j in jobs
                    if f"{j['company_name'].lower().strip()}|{j['role_title'].lower().strip()}" not in existing_keys
                ]
                if fresh:
                    added = database.insert_job_matches(fresh)
                    print(f"  Added {added} new job(s) to SQLite")
                    for j in fresh[:10]:
                        loc = f" | {j['location'][:30]}" if j.get("location") else ""
                        print(f"    {j['company_name']} | {j['role_title'][:50]}{loc}")
                    if len(fresh) > 10:
                        print(f"    ... and {len(fresh) - 10} more")
                else:
                    print("  No new jobs to add")
        except Exception as e:
            _record(failures, "NewPMJobs source", e)

    # --- Optional: Remote OK (public remote job board API -> job_matches) ---
    rok_cfg = sources_cfg.get("remoteok", {})
    if rok_cfg.get("enabled"):
        print("\n[Remote OK] Fetching...")
        try:
            from sources import remoteok
            from filters import JobFilter
            jobs = remoteok.fetch(rok_cfg)
            print(f"  {len(jobs)} matching role(s)")
            flt = JobFilter(cfg)
            _n = len(jobs)
            jobs = [
                j for j in jobs
                if not flt.seniority_excluded(
                    j.get("role_title", ""), j.get("company_description", "")
                )
            ]
            if len(jobs) != _n:
                print(f"  {len(jobs)} after seniority exclusion ({_n - len(jobs)} dropped)")
            # Hard gate — applies even to sources that skip location filtering.
            _n = len(jobs)
            jobs = [j for j in jobs if not flt.location_excluded(j.get("location", ""))]
            if len(jobs) != _n:
                print(f"  {len(jobs)} after non-US remote exclusion ({_n - len(jobs)} dropped)")
            if jobs and rok_cfg.get("location_filter", True):
                jobs = [j for j in jobs if flt.location_matches(j["location"])]
                print(f"  {len(jobs)} after location filter")
            if jobs:
                existing_keys = database.get_existing_job_keys()
                fresh = [
                    j for j in jobs
                    if f"{j['company_name'].lower().strip()}|{j['role_title'].lower().strip()}" not in existing_keys
                ]
                if fresh:
                    added = database.insert_job_matches(fresh)
                    print(f"  Added {added} new job(s) to SQLite")
                    for j in fresh[:10]:
                        loc = f" | {j['location'][:30]}" if j.get("location") else ""
                        print(f"    {j['company_name']} | {j['role_title'][:50]}{loc}")
                    if len(fresh) > 10:
                        print(f"    ... and {len(fresh) - 10} more")
                else:
                    print("  No new jobs to add")
        except Exception as e:
            _record(failures, "Remote OK source", e)

    # --- Optional: Work at a Startup / YC (authenticated -> job_matches) ---
    waas_cfg = sources_cfg.get("workatastartup", {})
    if waas_cfg.get("enabled"):
        print("\n[WaaS] Fetching...")
        try:
            from sources import workatastartup
            from filters import JobFilter
            jobs = workatastartup.fetch(waas_cfg)
            print(f"  {len(jobs)} PM role(s)")
            flt = JobFilter(cfg)
            _n = len(jobs)
            jobs = [
                j for j in jobs
                if not flt.seniority_excluded(
                    j.get("role_title", ""), j.get("company_description", "")
                )
            ]
            if len(jobs) != _n:
                print(f"  {len(jobs)} after seniority exclusion ({_n - len(jobs)} dropped)")
            # Hard gate — applies even to sources that skip location filtering.
            _n = len(jobs)
            jobs = [j for j in jobs if not flt.location_excluded(j.get("location", ""))]
            if len(jobs) != _n:
                print(f"  {len(jobs)} after non-US remote exclusion ({_n - len(jobs)} dropped)")
            if jobs and waas_cfg.get("location_filter", False):
                jobs = [j for j in jobs if flt.location_matches(j["location"])]
                print(f"  {len(jobs)} after location filter")
            if jobs:
                existing_keys = database.get_existing_job_keys()
                fresh = [
                    j for j in jobs
                    if f"{j['company_name'].lower().strip()}|{j['role_title'].lower().strip()}" not in existing_keys
                ]
                if fresh:
                    added = database.insert_job_matches(fresh)
                    print(f"  Added {added} new job(s) to SQLite")
                    for j in fresh[:10]:
                        loc = f" | {j['location'][:30]}" if j.get("location") else ""
                        print(f"    {j['company_name']} | {j['role_title'][:50]}{loc}")
                    if len(fresh) > 10:
                        print(f"    ... and {len(fresh) - 10} more")
                else:
                    print("  No new jobs to add")
        except Exception as e:
            _record(failures, "WaaS source", e)

    # --- Optional: Gmail jobs (recruiter emails -> job_matches) ---
    jobs_cfg = sources_cfg.get("gmail_jobs", {})
    if jobs_cfg.get("enabled"):
        print("\n[Gmail Jobs] Fetching...")
        try:
            from sources import gmail_jobs
            from filters import JobFilter
            jobs = gmail_jobs.fetch(jobs_cfg)
            print(f"  {len(jobs)} role candidate(s)")
            flt = JobFilter(cfg)
            _n = len(jobs)
            jobs = [
                j for j in jobs
                if not flt.seniority_excluded(
                    j.get("role_title", ""), j.get("company_description", "")
                )
            ]
            if len(jobs) != _n:
                print(f"  {len(jobs)} after seniority exclusion ({_n - len(jobs)} dropped)")
            # Hard gate — applies even to sources that skip location filtering.
            _n = len(jobs)
            jobs = [j for j in jobs if not flt.location_excluded(j.get("location", ""))]
            if len(jobs) != _n:
                print(f"  {len(jobs)} after non-US remote exclusion ({_n - len(jobs)} dropped)")
            # Location-gate only the sources listed in config. Broad feeds
            # (LinkedIn alerts) need it; curated recruiter mail does not.
            _gated = set(jobs_cfg.get("location_filtered_sources", []) or [])
            if jobs and _gated:
                _n = len(jobs)
                jobs = [
                    j for j in jobs
                    if j.get("source") not in _gated
                    or flt.location_matches(j.get("location", ""))
                ]
                if len(jobs) != _n:
                    print(f"  {len(jobs)} after location filter ({_n - len(jobs)} dropped)")
            if jobs:
                existing_keys = database.get_existing_job_keys()
                fresh = [
                    j for j in jobs
                    if f"{j['company_name'].lower().strip()}|{j['role_title'].lower().strip()}" not in existing_keys
                ]
                if fresh:
                    added = database.insert_job_matches(fresh)
                    print(f"  Added {added} new job(s) to SQLite")
                    for j in fresh:
                        loc = f" | {j['location']}" if j.get("location") else ""
                        print(f"    {j['company_name']} | {j['role_title']}{loc}")
                else:
                    print("  No new jobs to add")
        except Exception as e:
            _record(failures, "Gmail jobs source", e)

    # --- Optional: WaaS inbound messages (company DMs -> tracker leads) ---
    waasmsg_cfg = sources_cfg.get("waas_messages", {})
    if waasmsg_cfg.get("enabled"):
        print("\n[WaaS Inbound] Fetching...")
        try:
            from sources import waas_messages
            leads = waas_messages.fetch(waasmsg_cfg)
            if leads:
                print(f"  Logged {len(leads)} inbound lead(s) to the tracker:")
                for l in leads:
                    who = f"{l['contact_name']}" + (f" ({l['contact_title']})" if l['contact_title'] else "")
                    print(f"    {l['company_name']} — {who}")
            else:
                print("  No new inbound messages")
        except Exception as e:
            _record(failures, "WaaS Inbound source", e)

    print(f"\nTotal extracted: {len(all_startups)}")

    # --- Filter ---
    flt = StartupFilter(cfg)
    filtered = flt.filter(all_startups)
    print(f"After filter: {len(filtered)}")

    # --- Dedup ---
    deduped = _dedup(filtered)
    if len(deduped) < len(filtered):
        print(f"After dedup: {len(deduped)}")

    # --- Write ---
    existing = database.get_existing_companies()
    rejected = database.get_rejected_companies()
    fresh = [
        s for s in deduped
        if s.company_name.lower().strip() not in existing
        and s.company_name.lower().strip() not in rejected
    ]
    skipped = len(deduped) - len(fresh)
    if skipped:
        print(f"Skipped {skipped} already-seen or rejected")

    if fresh:
        added = database.insert_startups(fresh)
        print(f"Added {added} new startup(s) to SQLite")
        for s in fresh:
            amount = f" | {s.amount_raised}" if s.amount_raised else ""
            stage = f" | {s.funding_stage}" if s.funding_stage else ""
            print(f"  {s.company_name}{stage}{amount}  [{s.source}]")
    else:
        print("No new startups to add")

    # --- Optional: Google Sheets sink ---
    sheets_cfg = output_cfg.get("google_sheets", {})
    if sheets_cfg.get("enabled") and fresh:
        try:
            from sinks import google_sheets
            google_sheets.append_startups(sheets_cfg["sheet_id"], fresh)
            print(f"Wrote {len(fresh)} to Google Sheet")
        except Exception as e:
            _record(failures, "Google Sheets write", e)

    if failures:
        print(f"\n{len(failures)} source(s) failed this run:")
        for label, msg in failures:
            print(f"  x {label}: {msg}")
        print("Exiting non-zero so a scheduled run is reported as failed.")
        return 1

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
