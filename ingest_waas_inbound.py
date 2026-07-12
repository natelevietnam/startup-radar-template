#!/usr/bin/env python
"""Ingest Work at a Startup inbound messages into the local Application Tracker.

Standalone, local-only counterpart to the cloud pipeline: `sync_from_cloud`
doesn't carry activities/tracker rows, so WaaS company DMs
(workatastartup@ycombinator.com) are logged here, on the machine that owns the
tracker. Safe to run repeatedly — idempotent via `processed_items`.

Prints a single summary line so the /startup-radar skill can surface it.
"""

import sys

from config_loader import load_config
import database
from sources import waas_messages


def main() -> int:
    cfg = load_config()
    wm_cfg = cfg.get("sources", {}).get("waas_messages", {})
    if not wm_cfg.get("enabled"):
        print("WaaS inbound: disabled")
        return 0

    sqlite_cfg = cfg.get("output", {}).get("sqlite", {})
    if sqlite_cfg.get("path"):
        database.set_db_path(sqlite_cfg["path"])
    database.init_db()

    try:
        leads = waas_messages.fetch(wm_cfg)
    except Exception as e:
        print(f"WaaS inbound: skipped ({e})")
        return 0

    if leads:
        cos = ", ".join(dict.fromkeys(l["company_name"] for l in leads))
        print(f"WaaS inbound: +{len(leads)} lead(s) → tracker ({cos})")
    else:
        print("WaaS inbound: no new messages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
