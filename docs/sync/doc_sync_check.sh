#!/bin/sh
# doc_sync_check.sh — dependency-free drift gate for the Human-AI doc-sync system.
#
# Mirrors the pairing logic of Human-AI-doc-sync-tool's DocumentationSynchronizer
# (pair docs/<root>/human/<name>.md with docs/<root>/ai/<name>.yaml) without
# requiring Node, network access, or any third-party package. POSIX sh only.
#
# Exit status: 0 = in sync; 1 = drift (orphan pair or missing required field).
#
# Usage:
#   ./doc_sync_check.sh            # uses the directory of this script as root
#   ./doc_sync_check.sh <root>     # explicit root containing human/ and ai/
set -eu

ROOT="${1:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"
HUMAN_DIR="$ROOT/human"
AI_DIR="$ROOT/ai"

fail=0
paired=0

note() { printf '%s\n' "$1"; }
flag() { printf '  DRIFT: %s\n' "$1"; fail=1; }

if [ ! -d "$HUMAN_DIR" ] || [ ! -d "$AI_DIR" ]; then
  note "doc-sync: expected $HUMAN_DIR and $AI_DIR to exist"
  exit 1
fi

note "# Doc-Sync Report"
note "Root: $ROOT"
note ""

# Human -> AI: every human .md must have a matching ai .yaml, and that yaml
# must carry the minimum TaskDefinition fields the AI side relies on.
for md in "$HUMAN_DIR"/*.md; do
  [ -e "$md" ] || continue
  stem=$(basename "$md" .md)
  yaml="$AI_DIR/$stem.yaml"
  if [ ! -f "$yaml" ]; then
    flag "human '$stem.md' has no AI counterpart ($stem.yaml)"
    continue
  fi
  missing=""
  for key in id title status priority; do
    if ! grep -Eq "^${key}:" "$yaml"; then
      missing="$missing $key"
    fi
  done
  if [ -n "$missing" ]; then
    flag "$stem.yaml missing required field(s):$missing"
  else
    paired=$((paired + 1))
    note "  OK: $stem  (human + ai paired)"
  fi
done

# AI -> Human: catch yaml files with no human-readable counterpart.
for yaml in "$AI_DIR"/*.yaml; do
  [ -e "$yaml" ] || continue
  stem=$(basename "$yaml" .yaml)
  if [ ! -f "$HUMAN_DIR/$stem.md" ]; then
    flag "AI '$stem.yaml' has no human counterpart ($stem.md)"
  fi
done

note ""
note "Paired & valid: $paired"
if [ "$fail" -ne 0 ]; then
  note "Status: OUT OF SYNC"
  exit 1
fi
note "Status: IN SYNC"
