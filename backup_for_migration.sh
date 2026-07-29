#!/usr/bin/env bash
# Bundle the not-in-git crown-jewel files + Claude memory for migrating
# Startup Radar to a new machine / account. See MIGRATION_RUNBOOK.md.
set -euo pipefail
cd "$(dirname "$0")"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="radar-migration-backup-${STAMP}.tar.gz"
STAGE="$(mktemp -d)/radar-migration-backup"
mkdir -p "$STAGE" "$STAGE/memory"

# --- local state (gitignored) ---
for f in startup_radar.db config.yaml fit_dossiers.json \
         credentials.json token.json .secrets/waas_cookie.txt \
         data/linkedin_connections.csv; do
  if [ -e "$f" ]; then
    mkdir -p "$STAGE/$(dirname "$f")"
    cp "$f" "$STAGE/$f"
    echo "  + $f"
  else
    echo "  - $f (absent, skipped)"
  fi
done

# --- Claude memory (radar-relevant) + global rules ---
MEMDIR="$HOME/.claude/projects/-Users-nate-startup-radar-template/memory"
for m in MEMORY.md pm-fit-artifact.md applied-jobs-stay-out.md \
         dont-overwrite-manual-edits.md live-install-location.md; do
  if [ -e "$MEMDIR/$m" ]; then cp "$MEMDIR/$m" "$STAGE/memory/$m"; echo "  + memory/$m"; fi
done
[ -e "$HOME/CLAUDE.md" ] && cp "$HOME/CLAUDE.md" "$STAGE/CLAUDE.md" && echo "  + CLAUDE.md"

# --- also drop a copy of the runbook in the bundle ---
[ -e MIGRATION_RUNBOOK.md ] && cp MIGRATION_RUNBOOK.md "$STAGE/MIGRATION_RUNBOOK.md"

tar czf "$OUT" -C "$(dirname "$STAGE")" "$(basename "$STAGE")"
rm -rf "$(dirname "$STAGE")"
echo
echo "Wrote $OUT ($(du -h "$OUT" | cut -f1)). Move this off the machine before unplugging Anatomy access."
