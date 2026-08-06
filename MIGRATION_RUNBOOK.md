# Startup Radar — Migration Runbook

**Scenario this runbook covers**
- ✅ GitHub `natelevietnam` is **personal and stays** → the repo, GitHub Actions cron, and repo secrets survive. No repo recreation needed.
- 🔴 Google account `nate.le@anatomy.com` will be **lost** → OAuth must be rebuilt under a **personal Gmail**, and all email alerts re-subscribed there. This is the bulk of the work.
- 🖥️ Running on a **new machine** → local files, Python env, `gh` auth, and Claude account/MCP must be re-established.
- 🤖 Using a **personal Claude account** → skills travel with the repo; memory + the fit-artifact URL must be re-established.

> **Do Step 2 (backup) BEFORE you unplug anything.** Once Anatomy access is cut you cannot re-mint `token.json`, and `startup_radar.db` is irreplaceable.

---

## 0. Order of operations
1. **Back up** crown-jewel files off the old Mac (Step 2).
2. Leave the Anatomy org / lose the Google account (your action, any time after backup).
3. New machine: clone repo + restore backup + build venv (Step 3).
4. Rebuild Google auth under personal Gmail (Step 4) — the big one.
5. Redirect email alerts to personal Gmail (Step 5).
6. Update `config.yaml` email (Step 6).
7. Refresh GitHub secrets (Step 7).
8. Rebind Claude account: memory + new artifact URL + MCP (Step 8).
9. Verify end-to-end (Step 9).

---

## 1. System overview (full context)

**What it is:** a personal job/startup radar. A pipeline (`main.py`) pulls from several sources into a SQLite DB (`startup_radar.db`); a Streamlit app (`app.py`) is the dashboard; a GitHub Actions cron runs the pipeline daily in the cloud; `sync_from_cloud.py` pulls the cloud DB down to the local one; a fit-scored HTML dashboard is published as a Claude Artifact.

**Sources (`sources/`, toggled in `config.yaml`):**
- `rss`, `hackernews`, `sec_edgar` — funding/startup signal (no auth).
- `gmail` — funding newsletters (needs Google OAuth). Label: `Startup Funding`.
- `newpmjobs`, `remoteok` — public PM job feeds (no auth).
- `applyfyi` — curated PM **company** list → `startups` watchlist (parsed from the page's RSC payload).
- `workatastartup` — YC listing scrape. **Disabled** (Algolia key is scoped / Inertia prop empty → 0 rows without a real browser).
- `gmail_jobs` — recruiter/job-alert emails → `job_matches`. Parsers per sender in `sources/gmail_jobs.py` (`dgcsearch`, `wellfound`; `yc_work_at_a_startup`/`welcometothejungle` are stubs awaiting a first email).
- `waas_messages` — YC company DMs (`workatastartup@ycombinator.com`) → Application Tracker leads. **Local-only** (see nuance below).

**Data flow:** cloud cron runs `main.py` → writes cloud `startup_radar.db` (uploaded as an Actions artifact) → local `sync_from_cloud.py` merges it down. Local dashboard opened via the `/startup-radar` skill.

**Key nuances (don't relearn these the hard way):**
- **`sync_from_cloud` only carries `startups`, `job_matches`, `processed_items`** — NOT `activities`/`tracker_status`. So anything that writes the tracker must run **locally**. That's why `waas_messages` is enabled locally but **disabled in the cloud `CONFIG_YAML` secret** (if the cloud ran it, it'd mark the emails read and strand the leads). `ingest_waas_inbound.py` runs it locally as step 3 of the `/startup-radar` skill.
- **Reappearance protection:** deleting a job tombstones it in `deleted_jobs`; `insert_job_matches` skips tombstoned `(company|role)` keys, and there's a `UNIQUE INDEX (company_name, role_title COLLATE NOCASE)`. Both the pipeline and sync honor tombstones. "Not Interested" rows also block re-insert via the unique index. Reappearance is only possible if the scraper emits a *different title string* for the same job.
- **Exclusion / fit criteria** live in `config.yaml → targets` (`roles`, `seniority_exclusions`, `excluded_companies`, `locations`, `industries`). The recurring manual "exclusion run" screens Uncategorized `job_matches` against: target industries (AI / health / data-infra / fintech) and cuts off-function roles (internal IT/finance/HR/legal, trust&safety, ads, supply-chain, QA/test infra), off-industry (defense, logistics, gaming, rideshare, edtech, energy, consulting), and non-US locations.

---

## 2. Back up crown-jewel files (RUN ON OLD MAC, BEFORE UNPLUGGING)

These are **not in git**. Run the bundled script (or copy manually):

```bash
cd /Users/nate/startup-radar-template
./backup_for_migration.sh        # produces radar-migration-backup-<date>.tar.gz
```

Manifest (what the bundle contains):

| File | Why it's irreplaceable |
|------|------------------------|
| `startup_radar.db` | All curated state: Applied/Not-Interested statuses, ~300 tombstones, tracker, activities, connections |
| `config.yaml` | Your targeting + exclusion criteria and source/sender config |
| `fit_dossiers.json` | Cached deep-dive research for the fit dashboard |
| `data/linkedin_connections.csv` | 7,500+ connections for warm-intro matching |
| `credentials.json`, `token.json` | Old Google auth — **keep for reference only; both die with Anatomy** (see Step 4) |
| `.secrets/waas_cookie.txt` | WaaS cookie (low value; listing disabled) |
| `~/.claude/.../memory/*.md` (radar-relevant) | `MEMORY.md`, `pm-fit-artifact.md`, `applied-jobs-stay-out.md`, `dont-overwrite-manual-edits.md`, `live-install-location.md` |
| `~/CLAUDE.md` | Global assistant rules (trim Anatomy-specific Notion/Redshift lines) |

Also **push any uncommitted code** so the repo clone is current:
```bash
git status   # commit/push anything outstanding
```

---

## 3. New machine setup

```bash
git clone https://github.com/natelevietnam/startup-radar-template.git
cd startup-radar-template
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# hooks aren't cloned — bind them, or the artifact-URL guard is silently off
git config core.hooksPath hooks

# restore the backup bundle over the clone:
tar xzf ~/Downloads/radar-migration-backup-<date>.tar.gz -C .
#   -> restores startup_radar.db, config.yaml, fit_dossiers.json,
#      data/linkedin_connections.csv, .secrets/waas_cookie.txt

# gh CLI (personal account)
brew install gh && gh auth login    # authenticate as natelevietnam
```

Restore memory files to the new machine's Claude memory dir (path mirrors the project path):
`~/.claude/projects/<new-project-path>/memory/` — copy the 5 radar-relevant `.md` files there, and `~/CLAUDE.md` to home.

---

## 4. Rebuild Google auth (the big one)

`credentials.json` belongs to the Anatomy GCP project **`eob-ocr-and-extraction-dev`** — it dies with your Anatomy access. Rebuild under a **personal** Google:

1. **Google Cloud Console** (signed in as your *personal* Gmail) → create a new project (e.g. `startup-radar`).
2. **APIs & Services → Enable APIs** → enable **Gmail API**.
3. **OAuth consent screen** → External → add your personal Gmail as a **Test user** (keeps it in testing; no verification needed for personal use).
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → download JSON → save as `credentials.json` (overwrite the old one).
5. Delete the stale `token.json`, then run the auth flow to mint a fresh token for your personal Gmail:
   ```bash
   rm token.json
   .venv/bin/python main.py     # first run opens the browser consent flow → writes token.json
   ```
   *(or whatever the repo's auth entrypoint is — `check_token.py` will confirm health afterward.)*
6. `.venv/bin/python check_token.py` → expect `✓ Authenticated as <your-personal-gmail>`.

---

## 5. Redirect email alerts to personal Gmail

All email sources read the authed mailbox. Re-subscribe / re-point these to your personal Gmail, and recreate the `Startup Funding` label:

**Funding newsletters** (label `Startup Funding`): StrictlyVC (`connie@strictlyvc.com`), Venture Daily Digest (`venturedailydigest@substack.com`), Fortune Term Sheet (`termsheet@mail.fortune.com`), Axios Pro Rata (`pro-rata@axios.com`, `newsletters@axios.com`).

**Job / lead senders** (`gmail_jobs`): Wellfound alerts (`team@hi.wellfound.com`), YC/WaaS (`@workatastartup.com`, `@ycombinator.com`), Welcome to the Jungle, recruiter `denis@dgcsearch.com`.

**WaaS inbound DMs:** `workatastartup@ycombinator.com` — update your WaaS/YC profile email to the personal Gmail so company messages arrive there.

> Tip: set these up **before** losing the Anatomy inbox so no alert cycles are missed.

---

## 6. Update `config.yaml`

- `user.email:` → your personal Gmail.
- Sender maps in `sources.gmail.senders` and `sources.gmail_jobs.senders` stay the same (they match on From address, which doesn't change) — unless you switch newsletters.

---

## 7. GitHub secrets (repo stays under `natelevietnam`)

The cron keeps running under your personal GitHub. After Step 4–6, refresh the three secrets so the cloud run uses the new auth/config:

```bash
gh secret set CREDENTIALS_JSON -R natelevietnam/startup-radar-template < credentials.json
gh secret set TOKEN_JSON       -R natelevietnam/startup-radar-template < token.json
# cloud CONFIG_YAML = your config with waas_messages DISABLED (local-only). See nuance in §1.
python3 - <<'PY' > /tmp/config.cloud.yaml
import re; t=open('config.yaml').read()
print(re.sub(r'(  waas_messages:\n(?:    .*\n)*)', lambda m: re.sub(r'(\n\s+enabled:\s*)true', r'\1false', m.group(0), 1), t, 1), end='')
PY
gh secret set CONFIG_YAML -R natelevietnam/startup-radar-template < /tmp/config.cloud.yaml
```

---

## 8. Rebind the Claude account

- **Skills** — already in the repo (`.claude/skills/`: `startup-radar`, `deepdive`, `setup-radar`). They work as soon as you open the project in the new Claude account. No action beyond the clone.
- **Memory** — copy the 5 radar-relevant `.md` files (Step 3). New account starts fresh otherwise.
- **Fit-artifact URL** — an artifact URL is owned by the Claude account that published it and can't be redeployed to from another one. First run of `generate_fit_artifact.py` + publishing from the new account mints a **new URL**. Set it in **one** place:
  ```yaml
  # config.yaml
  output:
    fit_artifact_url: "https://claude.ai/code/artifact/<new-id>"
  ```
  `generate_fit_artifact.py`, `app.py`, and the `startup-radar` skill all read it from there via `artifact_url()` / `--artifact-url`. Do not paste copies into code, the skill, or memory notes — an earlier migration did exactly that and left the skill publishing to a dead URL while the code used the live one. Verify with `python generate_fit_artifact.py --artifact-url`.
- **MCP integrations** — reconnect Gmail/Drive on the new Claude account if you want ad-hoc email reads. *(Not required for the cron — that uses `token.json`.)*

---

## 9. Verify end-to-end

```bash
.venv/bin/python check_token.py                 # ✓ Authenticated as <personal gmail>
.venv/bin/python sync_from_cloud.py             # pulls latest cloud run
.venv/bin/python ingest_waas_inbound.py         # WaaS DMs → tracker (local)
.venv/bin/python generate_fit_artifact.py       # regenerates fit dashboard
.venv/bin/streamlit run app.py                  # dashboard on :8501
```
Then run the `/startup-radar` skill — it should complete all 7 steps. Trigger the GitHub Action manually (`gh workflow run` or the Actions tab) to confirm the cloud cron still runs green with the new secrets.

---

## 10. Quick reference — exclusion criteria
Screen Uncategorized `job_matches` and mark off-fit ones `Not Interested`:
- **Keep:** AI / ML, health & life-sciences, data infra / analytics, fintech; roles like AI/ML PM, founding PM, head of product, product lead.
- **Cut — off-function:** internal IT / finance systems / HR / legal, trust & safety, ads/adtech, supply-chain, QA/test infra, billing/claims.
- **Cut — off-industry:** defense, logistics/freight, gaming, rideshare, edtech, energy/utilities, consulting/agency, generic retail.
- **Cut — other:** non-US locations, over/under-seniority terms in `seniority_exclusions`, `excluded_companies` (Allstate, Stripe-until-2027).
