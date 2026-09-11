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

# Prefer the project venv. A bare `python3` on macOS is /usr/bin/python3, which
# has no PyYAML, so the verification below died with ModuleNotFoundError and
# set -e aborted the script before anything was uploaded - silently, as far as
# the secret was concerned. The fallback check further down keeps this working
# even on an interpreter without PyYAML.
PY_BIN="python3"
for candidate in "$(dirname "$0")/.venv/bin/python" ./.venv/bin/python; do
  [ -x "$candidate" ] && { PY_BIN="$candidate"; break; }
done

# Written to a stable, predictable path rather than a random temp name, and
# kept on failure: if `gh` cannot write the secret for any reason, this file is
# what you paste into the GitHub web UI instead (Settings -> Secrets and
# variables -> Actions -> CONFIG_YAML -> Update).
OUT="${TMPDIR:-/tmp}/startup-radar-config.cloud.yaml"

"$PY_BIN" - "$SRC" > "$OUT" <<'PREP'
import re, sys
text = open(sys.argv[1]).read()
def disable(block):
    return re.sub(r'(\n\s+enabled:\s*)true', r'\1false', block.group(0), 1)
sys.stdout.write(re.sub(r'(  waas_messages:\n(?:    .*\n)*)', disable, text, 1))
PREP

# Refuse to upload a config the edit missed - a renamed key or a changed indent
# would otherwise sail through and ship the exact setting this guards against.
"$PY_BIN" - "$OUT" <<'CHECK'
import re, sys

path = sys.argv[1]
text = open(path).read()

try:
    import yaml
except ModuleNotFoundError:
    # No PyYAML on this interpreter. Fall back to reading the block textually
    # rather than skipping the check - an unverified upload is the one outcome
    # this script exists to prevent.
    block = re.search(r'(  waas_messages:\n(?:    .*\n)*)', text)
    if not block:
        sys.exit("sources.waas_messages not found - has the config layout "
                 "changed? Nothing was uploaded.")
    if re.search(r'\n\s+enabled:\s*true\b', block.group(0)):
        sys.exit("waas_messages is still enabled after the edit. Nothing was uploaded.")
    print("(verified without PyYAML - install it for the stricter check)")
    raise SystemExit(0)

cfg = yaml.safe_load(text) or {}
waas = ((cfg.get("sources") or {}).get("waas_messages") or {})
if not waas:
    sys.exit("sources.waas_messages not found - has the config layout changed? "
             "Nothing was uploaded.")
if waas.get("enabled") is True:
    sys.exit("waas_messages is still enabled after the edit. Nothing was uploaded.")
CHECK

FP="$(shasum -a 256 "$OUT" | cut -c1-16)"

# What GitHub says the secret's timestamp is BEFORE we write, so the write can
# be proven rather than assumed. Three rounds of this loop failed silently:
# the script reported success while the stored secret never changed, and the
# daily run kept failing on a config nobody could see was stale.
BEFORE="$(gh api "repos/$REPO/actions/secrets/CONFIG_YAML" -q .updated_at 2>/dev/null || echo "none")"

set +e
gh secret set CONFIG_YAML -R "$REPO" < "$OUT"
RC=$?
set -e
if [ "$RC" -ne 0 ]; then
  echo >&2
  echo "gh secret set exited $RC - the secret was NOT updated." >&2
  echo "Paste this file into the web UI instead:" >&2
  echo "  $OUT" >&2
  echo "  https://github.com/$REPO/settings/secrets/actions" >&2
  exit "$RC"
fi

AFTER="$(gh api "repos/$REPO/actions/secrets/CONFIG_YAML" -q .updated_at 2>/dev/null || echo "unknown")"
if [ "$AFTER" = "$BEFORE" ]; then
  echo >&2
  echo "gh reported success, but GitHub still shows the secret last updated at" >&2
  echo "  $AFTER" >&2
  echo "The write did NOT land. Paste this file into the web UI instead:" >&2
  echo "  $OUT" >&2
  echo "  https://github.com/$REPO/settings/secrets/actions" >&2
  exit 1
fi

rm -f "$OUT"
echo "CONFIG_YAML updated on $REPO"
echo "  was: $BEFORE"
echo "  now: $AFTER"
echo "config fingerprint: $FP"
echo "The next daily run prints the same line; if they differ, the secret is stale."
