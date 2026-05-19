---
name: startup-radar
description: Open the Startup Radar dashboard with fresh data. Verifies OAuth token health, syncs the latest cloud cron results into the local DB, launches Streamlit if not running, and opens the dashboard in the user's browser.
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

### 3. Check if Streamlit is already running
```
lsof -i :8501 -sTCP:LISTEN
```
- If something is listening: report `Streamlit: already running`.
- If nothing: start it in the background with `.venv/bin/streamlit run app.py`. Confirm port 8501 starts listening within ~3s.

### 4. Open the dashboard
Run `open http://radar.local:8501` if `radar.local` resolves (check `grep radar.local /etc/hosts`), otherwise `open http://localhost:8501`.

### 5. Final report (1–3 lines, no preamble)
- Token status
- Sync result
- Dashboard URL

Example:
```
Token: ✓  Sync: +2 startups +5 jobs  Dashboard: http://radar.local:8501
```

## Tone & behavior

- Be terse. The user wants the dashboard open, not a tutorial.
- Don't run the pipeline (`main.py`) from this skill — sync handles cloud results; running locally duplicates work.
- Don't ask permission for the steps above; they are read-only or idempotent.
- If Streamlit starts but the browser doesn't open (e.g. headless terminal), still report the URL so the user can click it.
