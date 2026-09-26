#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
INSTALLER="$SCRIPT_DIR/install_skill_links.sh"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/hubmemory-links.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

SOURCE="$TMP_ROOT/source"
FAKE_HOME="$TMP_ROOT/home"
mkdir -p "$SOURCE/scripts" "$FAKE_HOME/.codex/skills/hubmemory"
SOURCE="$(CDPATH= cd -- "$SOURCE" && pwd -P)"
touch "$SOURCE/SKILL.md" "$SOURCE/scripts/bootstrap.sh"
echo old > "$FAKE_HOME/.codex/skills/hubmemory/legacy.txt"

HOME="$FAKE_HOME" bash "$INSTALLER" --source "$SOURCE" --target all
[ -L "$FAKE_HOME/.claude/skills/hubmemory" ]
[ -L "$FAKE_HOME/.codex/skills/hubmemory" ]
[ "$(CDPATH= cd -- "$FAKE_HOME/.claude/skills/hubmemory" && pwd -P)" = "$SOURCE" ]
[ "$(CDPATH= cd -- "$FAKE_HOME/.codex/skills/hubmemory" && pwd -P)" = "$SOURCE" ]
find "$FAKE_HOME/.codex/skills" -maxdepth 1 -name 'hubmemory.backup-*' -type d | grep -q .

# Idempotent rerun must not create a second backup.
before="$(find "$FAKE_HOME/.codex/skills" -maxdepth 1 -name 'hubmemory.backup-*' | wc -l | tr -d ' ')"
HOME="$FAKE_HOME" bash "$INSTALLER" --source "$SOURCE" --target all >/dev/null
after="$(find "$FAKE_HOME/.codex/skills" -maxdepth 1 -name 'hubmemory.backup-*' | wc -l | tr -d ' ')"
[ "$before" = "$after" ]

# Dry-run reports a conflict but leaves it untouched.
rm "$FAKE_HOME/.codex/skills/hubmemory"
mkdir "$FAKE_HOME/.codex/skills/hubmemory"
echo dry > "$FAKE_HOME/.codex/skills/hubmemory/keep.txt"
HOME="$FAKE_HOME" bash "$INSTALLER" --source "$SOURCE" --target codex --dry-run >/dev/null
[ ! -L "$FAKE_HOME/.codex/skills/hubmemory" ]
[ -f "$FAKE_HOME/.codex/skills/hubmemory/keep.txt" ]

# A checkout nested below a target must be rejected before anything is moved.
NESTED_HOME="$TMP_ROOT/nested-home"
NESTED_SOURCE="$NESTED_HOME/.claude/skills/hubmemory/source"
mkdir -p "$NESTED_SOURCE/scripts"
touch "$NESTED_SOURCE/SKILL.md" "$NESTED_SOURCE/scripts/bootstrap.sh"
if HOME="$NESTED_HOME" bash "$INSTALLER" --source "$NESTED_SOURCE" --target claude >/dev/null 2>&1; then
    echo "nested source should have been rejected" >&2
    exit 1
fi
[ -f "$NESTED_SOURCE/SKILL.md" ]

echo "install_skill_links tests: OK"
