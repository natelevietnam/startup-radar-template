#!/usr/bin/env bash
# Push the local config.yaml into the CONFIG_YAML repo secret, with
# waas_messages turned off.
#
# The cloud must never run waas_messages: it marks the source emails read, and
# sync_from_cloud does not carry activities/tracker_status back, so those leads
# are stranded. See MIGRATION_RUNBOOK.md section 1. This script disables it,
# verifies the edit actually took before uploading anything, and prints the
# fingerprint the daily workflow logs so a mismatch is obvious.
#
# Usage:  ./push_cloud_config.sh [path/to/config.yaml]
set -euo pipefail

REPO="${REPO:-natelevietnam/startup-radar-template}"
SRC="${1:-config.yaml}"
[ -f "$SRC" ] || { echo "no such file: $SRC" >&2; exit 1; }

OUT="$(mktemp -t config.cloud)"
trap 'rm -f "$OUT"' EXIT

python3 - "$SRC" > "$OUT" <<'PREP'
import re, sys
text = open(sys.argv[1]).read()
def disable(block):
    return re.sub(r'(\n\s+enabled:\s*)true', r'\1false', block.group(0), 1)
sys.stdout.write(re.sub(r'(  waas_messages:\n(?:    .*\n)*)', disable, text, 1))
PREP

# Refuse to upload a config the edit missed - a renamed key or a changed indent
# would otherwise sail through and ship the exact setting this guards against.
python3 - "$OUT" <<'CHECK'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
waas = ((cfg.get("sources") or {}).get("waas_messages") or {})
if not waas:
    sys.exit("sources.waas_messages not found - has the config layout changed? "
             "Nothing was uploaded.")
if waas.get("enabled") is True:
    sys.exit("waas_messages is still enabled after the edit. Nothing was uploaded.")
CHECK

gh secret set CONFIG_YAML -R "$REPO" < "$OUT"
echo "CONFIG_YAML updated on $REPO"
echo "config fingerprint: $(shasum -a 256 "$OUT" | cut -c1-16)"
echo "The next daily run prints the same line; if they differ, the secret is stale."
