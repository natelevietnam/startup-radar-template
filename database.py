"""SQLite database — generic startup + job + connections store."""

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Iterable

import pandas as pd

from models import Startup, JobMatch

DB_PATH = Path(__file__).parent / "startup_radar.db"


def set_db_path(path: str) -> None:
    global DB_PATH
    DB_PATH = Path(path)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS startups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                description TEXT DEFAULT '',
                funding_stage TEXT DEFAULT '',
                amount_raised TEXT DEFAULT '',
                location TEXT DEFAULT '',
                website TEXT DEFAULT '',
                source TEXT DEFAULT '',
                source_url TEXT DEFAULT '',
                date_found TEXT,
                status TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_startups_name
                ON startups(company_name COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS job_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                company_description TEXT DEFAULT '',
                role_title TEXT DEFAULT '',
                location TEXT DEFAULT '',
                url TEXT DEFAULT '',
                priority TEXT DEFAULT 'Medium',
                source TEXT DEFAULT '',
                status TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                date_found TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_company_role
                ON job_matches(company_name COLLATE NOCASE, role_title COLLATE NOCASE);

            -- Tombstones for jobs the user has deleted. Any automated source
            -- (local pipeline or cloud sync) must skip re-adding these, so a
            -- manual deletion stays deleted across runs.
            CREATE TABLE IF NOT EXISTS deleted_jobs (
                company_name TEXT NOT NULL,
                role_title TEXT DEFAULT '',
                deleted_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (company_name COLLATE NOCASE, role_title COLLATE NOCASE)
            );

            CREATE TABLE IF NOT EXISTS connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                url TEXT DEFAULT '',
                email TEXT DEFAULT '',
                company TEXT DEFAULT '',
                position TEXT DEFAULT '',
                connected_on TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_connections_company
                ON connections(company COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS connections_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_uploaded TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS hidden_intros (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_url TEXT NOT NULL,
                company_name TEXT NOT NULL,
                UNIQUE(connection_url, company_name)
            );

            CREATE TABLE IF NOT EXISTS processed_items (
                source TEXT NOT NULL,
                item_id TEXT NOT NULL,
                processed_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (source, item_id)
            );

            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                role_title TEXT DEFAULT '',
                activity_type TEXT NOT NULL,
                contact_name TEXT DEFAULT '',
                contact_title TEXT DEFAULT '',
                contact_email TEXT DEFAULT '',
                date TEXT NOT NULL,
                follow_up_date TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS tracker_status (
                company_name TEXT PRIMARY KEY,
                status TEXT DEFAULT 'In Progress',
                role TEXT DEFAULT '',
                notes TEXT DEFAULT ''
            );
        """)
        conn.commit()
    finally:
        conn.close()


# ---------- dedup helpers ----------

def get_existing_companies() -> set[str]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT company_name FROM startups").fetchall()
        return {r[0].lower().strip() for r in rows}
    finally:
        conn.close()


def get_rejected_companies() -> set[str]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT company_name FROM startups WHERE LOWER(TRIM(status)) = 'not interested'"
        ).fetchall()
        return {r[0].lower().strip() for r in rows}
    finally:
        conn.close()


def get_existing_job_keys() -> set[str]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT company_name, role_title FROM job_matches").fetchall()
        return {f"{r[0].lower().strip()}|{r[1].lower().strip()}" for r in rows}
    finally:
        conn.close()


def get_existing_canon_keys() -> set[str]:
    """Canonical (company|role) keys for every row, at any status.

    The unique index compares literal strings, so the same opening syndicated
    to two boards lands twice whenever the spelling differs at all — "ALSO."
    from LinkedIn and "Also" from Next Play are one job. Callers check this
    before inserting so a posting is stored once regardless of which feed
    reached it first.
    """
    conn = _connect()
    try:
        rows = conn.execute("SELECT company_name, role_title FROM job_matches").fetchall()
        return {_canon_job_key(r[0], r[1]) for r in rows}
    finally:
        conn.close()


def get_existing_canon_urls() -> set[str]:
    """Normalised posting URLs already stored, at any status."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT url FROM job_matches").fetchall()
        return {u for u in (canon_url(r[0]) for r in rows) if u}
    finally:
        conn.close()


def get_deleted_job_keys() -> set[str]:
    """Tombstoned canonical (company|role) keys — jobs the user has deleted.

    Canonical rather than literal, for the same reason decided rows are: a
    deleted posting that comes back as "Product Manager (Remote)" is the job
    you already deleted, and must not reappear on the Uncategorized board.
    """
    conn = _connect()
    try:
        rows = conn.execute("SELECT company_name, role_title FROM deleted_jobs").fetchall()
        return {_canon_job_key(r[0], r[1]) for r in rows}
    finally:
        conn.close()


# Statuses that mean "I have already categorized this job." A row carrying one
# of these must never reappear on the Uncategorized board, even if the source
# re-posts it under a cosmetically different title. Covers every non-blank
# option in the dashboard's status picker, so anything you have filed once
# stays filed.
DECIDED_STATUSES = ("Applied", "Not Interested", "Interested", "Wishlist")

# Decorations that job boards bolt onto a title without changing the role:
# "[Pipeline] Product Manager" and "Product Manager (Remote)" are both just
# "Product Manager". Bracketed and parenthesised tags are noise at either end.
_LEADING_DECORATION = re.compile(r"^\s*[\[\(][^\]\)]{0,40}[\]\)]\s*")

# A trailing suffix — bracketed or after a dash — is only noise when it is a
# location, a work arrangement or a requisition number. This vocabulary is
# deliberately closed, because a trailing parenthetical usually carries the
# *scope* that distinguishes two live reqs: Traba's "Senior Product Manager
# (Marketplace)" and "(AI Agents)" are different jobs, as are Palo Alto
# Networks' "(Prisma AIRS)" and "(Application Security)". Collapsing those
# would bury a real opening under an unrelated decision — the exact failure
# this canonicalisation exists to prevent, inverted.
_NOISE = (
    r"remote(\s*[-–—,]?\s*\(?(us|usa|united\s+states)\)?)?"
    r"|hybrid|on-?site|in-?office"
    r"|(full|part)[-\s]?time|contract(or)?|temporary|permanent"
    r"|u\.?s\.?a?\.?|united\s+states"
    r"|req(uisition)?\.?\s*#?\s*\d+|r[-–—]?\d{3,}|#\s*\d{3,}"
    r"|[A-Za-z][A-Za-z .'\-]{1,28},\s*[A-Za-z]{2}"
)
_TRAILING_DECORATION = re.compile(rf"\s*[\[\(]\s*({_NOISE})\s*[\]\)]\s*$", re.IGNORECASE)
_TRAILING_NOISE = re.compile(rf"\s*[-–—,]\s*({_NOISE})\s*$", re.IGNORECASE)

# Legal/formatting suffixes that don't distinguish an employer. "ALSO." and
# "Also" are one company; so are "Acme, Inc." and "Acme". Parenthesised
# aliases go too, so "Amazon Web Services (AWS)" matches "Amazon Web Services".
_COMPANY_ALIAS = re.compile(r"\s*[\(\[][^\)\]]{0,40}[\)\]]\s*")
_COMPANY_SUFFIX = re.compile(
    r"\b(inc|llc|l\.l\.c|ltd|limited|corp|corporation|gmbh|plc|pty|holdings)\b"
)


def canon_role(role_title: str) -> str:
    """Canonical form of a role title, for same-job matching across re-posts.

    Strips bracketed tags at either end, closed-vocabulary location/arrangement
    /req-number suffixes, punctuation and repeated whitespace, then lowercases.
    Deliberately *keeps* seniority and scope words: "Product Manager I, Ads"
    and "Product Manager II, Ads" are different reqs, as are "Product Manager"
    and "Senior Product Manager", and collapsing them would hide genuinely new
    openings — the opposite of the bug this guards against.
    """
    t = role_title or ""
    # Decorations stack ("[Pipeline] PM (Remote) - San Francisco, CA"), so peel
    # until nothing more comes off rather than making a single pass.
    for _ in range(4):
        before = t
        t = _LEADING_DECORATION.sub("", t)
        t = _TRAILING_DECORATION.sub("", t)
        t = _TRAILING_NOISE.sub("", t)
        if t == before:
            break
    t = re.sub(r"[^a-z0-9]+", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


_ZERO_WIDTH = re.compile(r"[​‌‍﻿]")


def clean_text(value: str) -> str:
    """Collapse exotic whitespace to plain single spaces.

    Feeds paste non-breaking and zero-width characters into names and titles —
    Lenny's Jobs sends "Komodo\\xa0Health", NewPMJobs "Sr.\\xa0 Product Manager".
    The unique index on (company_name, role_title) is COLLATE NOCASE, which
    folds case but not whitespace, so "Komodo\\xa0Health" and "Komodo Health"
    read as different employers and both rows land. Normalising on write keeps
    that index doing the job it was added for.
    """
    return re.sub(r"\s+", " ", _ZERO_WIDTH.sub("", value or "")).strip()


def canon_company(company_name: str) -> str:
    """Canonical employer name — punctuation, aliases and legal suffixes out."""
    c = _COMPANY_ALIAS.sub(" ", company_name or "")
    c = re.sub(r"[^a-z0-9]+", " ", c.lower())
    c = _COMPANY_SUFFIX.sub(" ", c)
    return re.sub(r"\s+", " ", c).strip()


# Query parameters that identify the *referrer*, not the posting. Everything
# else is kept, because plenty of boards carry the job id in the query string.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "gh_jid_src", "src", "source", "ref", "referrer", "trk",
    "trackingid", "lipi", "originalsubdomain", "position", "pagenum",
})

# Paths that are a company's careers *index*, not one posting. Keying on these
# would block every future role at that employer: binti.com/careers/ already
# carries two unrelated openings in this database.
_INDEX_PATH = re.compile(
    r"(^|/)(careers?|jobs?|openings?|positions?|vacancies|join-?us)/?$", re.IGNORECASE
)


def canon_url(url: str) -> str:
    """Normalised posting URL, or "" when the URL can't identify one posting.

    Returns "" for careers-index pages and for paths with no posting-shaped
    segment, so that a shared index URL never collapses two distinct roles.
    """
    u = (url or "").strip()
    if not u:
        return ""
    try:
        parts = urlsplit(u if "//" in u else "//" + u)
    except ValueError:
        return ""
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "").rstrip("/")
    if not host or _INDEX_PATH.search(path):
        return ""
    segments = [s for s in path.split("/") if s]
    # A real posting URL carries an id or a slug deep in the path. A bare
    # /company or / is an index by another name.
    if not segments or not (
        any(any(ch.isdigit() for ch in s) for s in segments) or len(segments) >= 3
    ):
        return ""
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    )
    return urlunsplit(("", host, path.lower(), urlencode(query), ""))


def _canon_job_key(company_name: str, role_title: str) -> str:
    return f"{canon_company(company_name)}|{canon_role(role_title)}"


def get_decided_job_keys() -> set[str]:
    """Canonical (company|role) keys the user has already decided about.

    The unique index already stops an identical (company, role) row from
    being re-inserted, so a decided row keeps its status. What it does not
    stop is the same job arriving under a re-decorated title, which lands as
    a fresh status='' row on the Uncategorized board. Callers filter on these
    keys so a decision survives re-posting.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT company_name, role_title FROM job_matches "
            f"WHERE status IN ({','.join('?' * len(DECIDED_STATUSES))})",
            DECIDED_STATUSES,
        ).fetchall()
        return {_canon_job_key(r[0], r[1]) for r in rows}
    finally:
        conn.close()


def get_decided_job_urls() -> set[str]:
    """Normalised posting URLs the user has already decided about.

    A second, independent identity for "this exact posting". Titles get
    rewritten between re-posts; the posting URL usually doesn't, so this
    catches re-posts that :func:`canon_role` can't — Airbnb's "Product
    Manager" and "Product Manager, Passport" are one LinkedIn posting.
    Index pages resolve to "" in :func:`canon_url` and are never keys.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT url FROM job_matches "
            f"WHERE status IN ({','.join('?' * len(DECIDED_STATUSES))})",
            DECIDED_STATUSES,
        ).fetchall()
        return {u for u in (canon_url(r[0]) for r in rows) if u}
    finally:
        conn.close()


def add_job_tombstones(pairs: list[tuple[str, str]]) -> int:
    """Record (company_name, role_title) pairs as deleted. Returns count added."""
    if not pairs:
        return 0
    conn = _connect()
    try:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO deleted_jobs (company_name, role_title) VALUES (?, ?)",
            pairs,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def is_processed(source: str, item_id: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM processed_items WHERE source = ? AND item_id = ?",
            (source, item_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_processed(source: str, item_ids: Iterable[str]) -> None:
    conn = _connect()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO processed_items (source, item_id) VALUES (?, ?)",
            [(source, i) for i in item_ids],
        )
        conn.commit()
    finally:
        conn.close()


# ---------- inserts ----------

def insert_startups(startups: list) -> int:
    if not startups:
        return 0
    conn = _connect()
    count = 0
    try:
        for s in startups:
            if isinstance(s, Startup):
                values = (
                    s.company_name, s.description, s.funding_stage, s.amount_raised,
                    s.location, s.website, s.source, s.source_url,
                    (s.date_found or datetime.now()).strftime("%Y-%m-%d"), "",
                )
            else:
                values = (
                    s["company_name"], s.get("description", ""),
                    s.get("funding_stage", ""), s.get("amount_raised", ""),
                    s.get("location", ""), s.get("website", ""),
                    s.get("source", ""), s.get("source_url", ""),
                    s.get("date_found", datetime.now().strftime("%Y-%m-%d")),
                    s.get("status", ""),
                )
            try:
                conn.execute(
                    """INSERT INTO startups
                       (company_name, description, funding_stage, amount_raised,
                        location, website, source, source_url, date_found, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                count += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
    finally:
        conn.close()
    return count


def insert_job_matches(jobs: list) -> int:
    if not jobs:
        return 0
    tombstoned = get_deleted_job_keys()
    decided = get_decided_job_keys()
    decided_urls = get_decided_job_urls()
    # Seeded from the table, then extended as this batch inserts, so a feed
    # that carries the same job twice in one run stores it once.
    seen_keys = get_existing_canon_keys()
    seen_urls = get_existing_canon_urls()
    conn = _connect()
    count = 0
    try:
        for j in jobs:
            if isinstance(j, JobMatch):
                values = (
                    j.company_name, j.company_description, j.role_title,
                    j.location, j.url, j.priority, j.source,
                    "", (j.date_found or datetime.now()).strftime("%Y-%m-%d"),
                )
            else:
                values = (
                    j["company_name"], j.get("company_description", ""),
                    j.get("role_title", ""), j.get("location", ""),
                    j.get("url", ""), j.get("priority", ""),
                    j.get("source", ""), j.get("status", ""),
                    j.get("date_found", datetime.now().strftime("%Y-%m-%d")),
                )
            # values[0] = company_name, values[2] = role_title,
            # values[4] = url, values[7] = status
            values = list(values)
            values[0] = clean_text(values[0])
            values[2] = clean_text(values[2])
            values = tuple(values)

            ckey = _canon_job_key(values[0], values[2])
            curl = canon_url(values[4])
            if ckey in tombstoned:
                continue
            # Only suppress rows arriving *undecided*: those are the ones that
            # would land on the Uncategorized board. A caller inserting with a
            # status already set (the dashboard's "Add Applied" form) is making
            # a deliberate record and must not be silently dropped.
            if not (values[7] or "").strip():
                if ckey in decided or (curl and curl in decided_urls):
                    continue
                # Cross-source duplicate: the same opening already stored under
                # a different spelling, or reached through a second feed.
                if ckey in seen_keys or (curl and curl in seen_urls):
                    continue
            try:
                conn.execute(
                    """INSERT INTO job_matches
                       (company_name, company_description, role_title, location,
                        url, priority, source, status, date_found)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                count += 1
                seen_keys.add(ckey)
                if curl:
                    seen_urls.add(curl)
            except sqlite3.IntegrityError:
                pass
        conn.commit()
    finally:
        conn.close()
    return count


def update_startup_website(company_name: str, website: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE startups SET website = ? WHERE company_name = ? COLLATE NOCASE",
            (website, company_name),
        )
        conn.commit()
    finally:
        conn.close()


# ---------- read ----------

def get_all_startups() -> pd.DataFrame:
    conn = _connect()
    try:
        df = pd.read_sql_query(
            """SELECT company_name, website, description, funding_stage, amount_raised,
                      location, source, date_found, status
               FROM startups ORDER BY date_found DESC, id DESC""",
            conn,
        )
    finally:
        conn.close()
    df.columns = [
        "Company Name", "Website", "Description", "Funding Stage",
        "Amount Raised", "Location", "Source", "Date Found", "Status",
    ]
    df["Website"] = df["Website"].fillna("")
    df["Website"] = df["Website"].apply(
        lambda x: f"https://{x}" if x and not x.startswith("http") else x
    )
    df["Status"] = df["Status"].fillna("")
    return df


def get_all_job_matches() -> pd.DataFrame:
    conn = _connect()
    try:
        df = pd.read_sql_query(
            """SELECT company_name, company_description, role_title,
                      location, url, priority, status, date_found, notes
               FROM job_matches ORDER BY date_found DESC, id DESC""",
            conn,
        )
    finally:
        conn.close()
    df.columns = [
        "Company", "Company Description", "Role",
        "Location", "Link", "Priority", "Status", "Date Found", "Notes",
    ]
    df["Status"] = df["Status"].fillna("")
    df["Notes"] = df["Notes"].fillna("")
    return df


# ---------- status updates ----------

def update_startup_status(company_name: str, status: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE startups SET status = ? WHERE company_name = ? COLLATE NOCASE",
            (status, company_name),
        )
        conn.commit()
    finally:
        conn.close()


def update_job_status(company_name: str, role_title: str, status: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            """UPDATE job_matches SET status = ?
               WHERE company_name = ? COLLATE NOCASE
                 AND role_title = ? COLLATE NOCASE""",
            (status, company_name, role_title),
        )
        conn.commit()
    finally:
        conn.close()


def update_job_notes(company_name: str, role_title: str, notes: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            """UPDATE job_matches SET notes = ?
               WHERE company_name = ? COLLATE NOCASE
                 AND role_title = ? COLLATE NOCASE""",
            (notes, company_name, role_title),
        )
        conn.commit()
    finally:
        conn.close()


def delete_startup(company_name: str) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM startups WHERE company_name = ? COLLATE NOCASE", (company_name,))
        conn.commit()
    finally:
        conn.close()


def delete_job_match(company_name: str, role_title: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM job_matches WHERE company_name = ? COLLATE NOCASE AND role_title = ? COLLATE NOCASE",
            (company_name, role_title),
        )
        # Tombstone it so the pipeline / cloud sync never resurrects it.
        conn.execute(
            "INSERT OR IGNORE INTO deleted_jobs (company_name, role_title) VALUES (?, ?)",
            (company_name, role_title),
        )
        conn.commit()
    finally:
        conn.close()


# ---------- activities ----------

def insert_activity(activity: dict) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO activities
               (company_name, role_title, activity_type, contact_name,
                contact_title, contact_email, date, follow_up_date, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                activity["company_name"],
                activity.get("role_title", ""),
                activity["activity_type"],
                activity.get("contact_name", ""),
                activity.get("contact_title", ""),
                activity.get("contact_email", ""),
                activity["date"],
                activity.get("follow_up_date", ""),
                activity.get("notes", ""),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_activities(company_name: str = None) -> pd.DataFrame:
    conn = _connect()
    try:
        if company_name:
            return pd.read_sql_query(
                """SELECT id, company_name, role_title, activity_type,
                          contact_name, contact_title, contact_email,
                          date, follow_up_date, notes
                   FROM activities WHERE company_name = ? COLLATE NOCASE
                   ORDER BY date DESC, id DESC""",
                conn, params=(company_name,),
            )
        return pd.read_sql_query(
            """SELECT id, company_name, role_title, activity_type,
                      contact_name, contact_title, contact_email,
                      date, follow_up_date, notes
               FROM activities ORDER BY date DESC, id DESC""",
            conn,
        )
    finally:
        conn.close()


def get_overdue_followups(today: str) -> pd.DataFrame:
    conn = _connect()
    try:
        return pd.read_sql_query(
            """SELECT id, company_name, role_title, activity_type,
                      contact_name, contact_title, date, follow_up_date, notes
               FROM activities
               WHERE follow_up_date != '' AND follow_up_date <= ?
               ORDER BY follow_up_date ASC""",
            conn, params=(today,),
        )
    finally:
        conn.close()


# ---------- tracker ----------

def get_tracker_status(company_name: str) -> dict:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT status, role, notes FROM tracker_status WHERE company_name = ? COLLATE NOCASE",
            (company_name,),
        ).fetchone()
        return {"status": row[0], "role": row[1], "notes": row[2]} if row else {}
    finally:
        conn.close()


def upsert_tracker_status(company_name: str, status: str, role: str = "", notes: str = "") -> None:
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO tracker_status (company_name, status, role, notes)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(company_name) DO UPDATE SET
                   status = excluded.status,
                   role = excluded.role,
                   notes = excluded.notes""",
            (company_name, status, role, notes),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_tracker_statuses() -> dict:
    conn = _connect()
    try:
        rows = conn.execute("SELECT company_name, status, role, notes FROM tracker_status").fetchall()
        return {r[0]: {"status": r[1], "role": r[2], "notes": r[3]} for r in rows}
    finally:
        conn.close()


def delete_tracker_entry(company_name: str) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM tracker_status WHERE company_name = ? COLLATE NOCASE", (company_name,))
        conn.execute("DELETE FROM activities WHERE company_name = ? COLLATE NOCASE", (company_name,))
        conn.commit()
    finally:
        conn.close()


def get_tracker_summary() -> pd.DataFrame:
    conn = _connect()
    try:
        companies = conn.execute(
            "SELECT DISTINCT company_name FROM activities ORDER BY company_name"
        ).fetchall()
        rows = []
        for (name,) in companies:
            ts = conn.execute(
                "SELECT status, role, notes FROM tracker_status WHERE company_name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
            status = ts[0] if ts else "In Progress"
            role = ts[1] if ts else ""
            tracker_notes = ts[2] if ts else ""

            acts = conn.execute(
                """SELECT activity_type, contact_name, contact_title, date, follow_up_date, notes, role_title
                   FROM activities WHERE company_name = ? COLLATE NOCASE ORDER BY date ASC""",
                (name,),
            ).fetchall()

            contacts = []
            for a in acts:
                if a[1]:
                    c = f"{a[1]} ({a[2]})" if a[2] else a[1]
                    if c not in contacts:
                        contacts.append(c)

            timeline = []
            for a in acts:
                entry = f"{a[3]}: {a[0]}"
                if a[1]:
                    entry += f" {a[1]}"
                timeline.append(entry)

            follow_ups = [a[4] for a in acts if a[4]]
            next_followup = min(follow_ups) if follow_ups else ""

            if not role:
                for a in acts:
                    if a[6]:
                        role = a[6]
                        break

            notes_parts = []
            if tracker_notes:
                notes_parts.append(tracker_notes)
            for a in acts:
                if a[5]:
                    notes_parts.append(f"{a[3]}: {a[5]}")

            rows.append({
                "Company": name, "Status": status, "Role": role,
                "Contacts": ", ".join(contacts),
                "Activities": " → ".join(timeline),
                "Follow-up": next_followup,
                "Notes": " | ".join(notes_parts),
            })

        return pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=["Company", "Status", "Role", "Contacts", "Activities", "Follow-up", "Notes"]
        )
    finally:
        conn.close()


# ---------- connections ----------

def import_connections(rows: list[dict]) -> int:
    conn = _connect()
    try:
        conn.execute("DELETE FROM connections")
        count = 0
        for r in rows:
            conn.execute(
                """INSERT INTO connections
                   (first_name, last_name, url, email, company, position, connected_on)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    r.get("First Name", ""),
                    r.get("Last Name", ""),
                    r.get("URL", ""),
                    r.get("Email Address", ""),
                    r.get("Company", ""),
                    r.get("Position", ""),
                    r.get("Connected On", ""),
                ),
            )
            count += 1
        conn.execute("DELETE FROM connections_meta")
        conn.execute(
            "INSERT INTO connections_meta (id, last_uploaded) VALUES (1, ?)",
            (datetime.now().isoformat(),),
        )
        conn.commit()
        return count
    finally:
        conn.close()


def get_connections_count() -> int:
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) FROM connections").fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def get_connections_last_uploaded() -> str:
    conn = _connect()
    try:
        row = conn.execute("SELECT last_uploaded FROM connections_meta WHERE id = 1").fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def search_connections_by_company(company_name: str) -> pd.DataFrame:
    conn = _connect()
    try:
        return pd.read_sql_query(
            """SELECT first_name, last_name, url, company, position
               FROM connections
               WHERE company LIKE ? COLLATE NOCASE
               ORDER BY last_name""",
            conn,
            params=(f"%{company_name}%",),
        )
    finally:
        conn.close()


def search_connections_by_companies(company_names: list[str]) -> pd.DataFrame:
    if not company_names:
        return pd.DataFrame()
    conn = _connect()
    try:
        placeholders = " OR ".join(["company LIKE ? COLLATE NOCASE"] * len(company_names))
        params = [f"%{n}%" for n in company_names]
        return pd.read_sql_query(
            f"""SELECT first_name, last_name, url, company, position
                FROM connections WHERE {placeholders}
                ORDER BY last_name""",
            conn,
            params=params,
        )
    finally:
        conn.close()


def hide_intro(connection_url: str, company_name: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO hidden_intros (connection_url, company_name) VALUES (?, ?)",
            (connection_url, company_name),
        )
        conn.commit()
    finally:
        conn.close()


def get_hidden_intros(company_name: str) -> set[str]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT connection_url FROM hidden_intros WHERE company_name = ? COLLATE NOCASE",
            (company_name,),
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()
