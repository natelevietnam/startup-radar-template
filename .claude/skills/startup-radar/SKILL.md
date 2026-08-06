---
name: startup-radar
description: Open the Startup Radar dashboard with fresh data. Verifies OAuth token health, syncs the latest cloud cron results into the local DB, regenerates and re-publishes the PM-Fit artifact from the current Uncategorized Job Matches, launches Streamlit if not running, and opens the dashboard in the user's browser.
---

# Startup Radar Skill

The user wants to open their dashboard. Get it open as fast as possible with fresh data, reporting briefly along the way.

## Steps (run in order; continue even if non-fatal steps fail)

### 1. Verify OAuth token health
Run `.venv/bin/python check_token.py`.
- If it exits 0: report `Token: ✓`
- If it fails: report the exact failure line. Print the recovery one-liner and stop unless the user says continue anyway:
  ```
  rm token.json && python main.py && gh secret set TOKEN_JSON --repo natelevietnam/startup-radar-template < token.json
  ```

### 2. Sync the latest cloud run into the local DB
Run `.venv/bin/python sync_from_cloud.py`.
- Report the summary line (e.g. `+3 startups, +0 jobs from run 26054661038`).
- If `gh` is not installed or not authenticated: note it, continue (cloud sync is optional).
- If no successful cloud runs exist yet: note it, continue.

### 3. Log new Work at a Startup inbound messages
Run `.venv/bin/python ingest_waas_inbound.py`.
- Parses YC company DMs (`workatastartup@ycombinator.com`) into the local Application Tracker as "In Progress" leads. Local-only by design — cloud sync doesn't carry tracker/activity rows.
- Report the summary line (e.g. `WaaS inbound: +1 lead → tracker (Shor)` or `no new messages`).
- Idempotent and non-fatal. If it fails (e.g. Gmail hiccup), note it and continue.

### 4. Refresh the PM-Fit dashboard artifact
Regenerate the fit dashboard from the current Uncategorized Job Matches and re-publish it to the existing Artifact URL so it stays in sync with the pipeline.
Research is cached, never redone just because the dashboard was opened. Each dossier in `fit_dossiers.json` carries `researchedAt` + `researchedRoles`; a company is only revisited when it posts a role that isn't on that list.

**a. Auto-refresh the top scorers that re-posted.** Before generating, run:
```
.venv/bin/python generate_fit_artifact.py --stale --auto
```
Each line is `company · score · AUTO · researched <date> · new: <roles>`. These scored **≥ 75**, so a changed read matters — re-run deep research for each (same method as `/deepdive`), replace that company's entry in `fit_dossiers.json`, then stamp the new baseline so it stops flagging:
```
.venv/bin/python generate_fit_artifact.py --mark-researched "Hex" "Plaid"
```
If the list is empty, skip straight to (c). If it's long (>6 companies), refresh the top 6 by score and say which ones you deferred — don't silently truncate.

**b. Leave the rest flagged.** `--stale` (without `--auto`) lists the sub-75 companies that also re-posted. Don't research these; they render with a `re-posted` badge and their new roles highlighted, and Nate refreshes them on demand.

**c. Generate and publish.**
- Run `.venv/bin/python generate_fit_artifact.py`. It writes `reports/pm_fit_dashboard.html` from the live DB + cached dossiers and prints a summary + the publish URL.
- Re-publish with the **Artifact tool**: `file_path=reports/pm_fit_dashboard.html`, `favicon=🎯`, and `url=` the URL the generator prints (or `.venv/bin/python generate_fit_artifact.py --artifact-url`). Passing `url` is what keeps the link stable.
- **Never hardcode that URL here or anywhere else.** It lives in `config.yaml` under `output.fit_artifact_url` and is account-specific; a past migration left a stale copy in this file and the skill published to a dead URL for weeks. Always read it at run time.
- Report one line, e.g. `Fit dashboard: 31 companies (3 new/pending, 2 re-posted → refreshed) → published`.
- Idempotent and safe. If the generator or publish fails, note it and continue — never block the dashboard on it.

**d. Brand-new companies** (no dossier at all) show as "pending" cards — JD facts + cheap gate flags, no research. To deep-dive on demand: `.venv/bin/python generate_fit_artifact.py --missing`, research those companies, append entries to `fit_dossiers.json`, run `--mark-researched` for them, and re-run. Don't do this automatically; it's an explicit ask.

### 5. Check if Streamlit is already running
```
lsof -i :8501 -sTCP:LISTEN
```
- If something is listening: report `Streamlit: already running`.
- If nothing: start it in the background with `.venv/bin/streamlit run app.py`. Confirm port 8501 starts listening within ~3s.

### 6. Open the dashboard
Run `open http://radar.local:8501` if `radar.local` resolves (check `grep radar.local /etc/hosts`), otherwise `open http://localhost:8501`.

### 7. Final report (1–3 lines, no preamble)
- Token status
- Sync result
- WaaS inbound result
- Fit dashboard refresh result + Artifact URL
- Dashboard URL

Example:
```
Token: ✓  Sync: +2 startups +5 jobs  WaaS inbound: +1 lead (Shor)  Fit: 31 cos (3 new) refreshed  Dashboard: http://radar.local:8501
```

## Tone & behavior

- Be terse. The user wants the dashboard open, not a tutorial.
- Don't run the full pipeline (`main.py`) from this skill — sync handles cloud results; running locally duplicates work. (Step 3's `ingest_waas_inbound.py` is the one exception: it's a single local-only source the cloud can't run, not the whole pipeline.)
- Don't ask permission for the steps above; they are read-only or idempotent.
- If Streamlit starts but the browser doesn't open (e.g. headless terminal), still report the URL so the user can click it.
