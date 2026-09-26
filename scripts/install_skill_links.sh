#!/usr/bin/env bash
# Install one hubmemory checkout into multiple agent skill directories.
# Existing targets are moved to timestamped backups before links are created.

set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
SOURCE_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"
TARGET_SCOPE="all"
DRY_RUN=false

usage() {
    cat <<'EOF'
Usage: install_skill_links.sh [options]

Options:
  --source PATH                  Skill source checkout (default: script parent)
  --target all|claude|codex      Link target selection (default: all)
  --dry-run                      Show planned operations without changing files
  -h, --help                     Show this help

Existing files, directories, or incorrect links are never overwritten. They are
moved to a sibling path named hubmemory.backup-YYYYMMDD-HHMMSS first.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --source)
            [ "$#" -ge 2 ] || { echo "ERROR: --source requires a path" >&2; exit 2; }
            SOURCE_DIR="$2"
            shift 2
            ;;
        --target)
            [ "$#" -ge 2 ] || { echo "ERROR: --target requires all, claude, or codex" >&2; exit 2; }
            TARGET_SCOPE="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$TARGET_SCOPE" in
    all|claude|codex) ;;
    *) echo "ERROR: --target must be all, claude, or codex" >&2; exit 2 ;;
esac

if [ ! -d "$SOURCE_DIR" ]; then
    echo "ERROR: source directory not found: $SOURCE_DIR" >&2
    exit 1
fi
SOURCE_DIR="$(CDPATH= cd -- "$SOURCE_DIR" && pwd -P)"
if [ ! -f "$SOURCE_DIR/SKILL.md" ] || [ ! -f "$SOURCE_DIR/scripts/bootstrap.sh" ]; then
    echo "ERROR: source does not look like hubmemory: $SOURCE_DIR" >&2
    exit 1
fi

path_resolves_to_source() {
    local target="$1"
    [ -e "$target" ] || return 1
    [ "$(CDPATH= cd -- "$target" 2>/dev/null && pwd -P)" = "$SOURCE_DIR" ]
}

next_backup_path() {
    local target="$1"
    local stamp candidate counter
    stamp="$(date +%Y%m%d-%H%M%S)"
    candidate="${target}.backup-${stamp}"
    counter=1
    while [ -e "$candidate" ] || [ -L "$candidate" ]; do
        candidate="${target}.backup-${stamp}-${counter}"
        counter=$((counter + 1))
    done
    printf '%s\n' "$candidate"
}

install_target() {
    local label="$1"
    local target="$2"
    local parent backup target_physical
    parent="$(dirname -- "$target")"

    if path_resolves_to_source "$target"; then
        if [ -L "$target" ]; then
            echo "[$label] already linked: $target -> $SOURCE_DIR"
        else
            echo "[$label] source is already installed at: $target"
        fi
        return
    fi

    if [ -d "$target" ]; then
        target_physical="$(CDPATH= cd -- "$target" && pwd -P)"
        case "$SOURCE_DIR/" in
            "$target_physical/"*)
                echo "ERROR: source is nested inside target and cannot be safely linked: $SOURCE_DIR" >&2
                echo "Move the checkout outside $target first (for example ~/.local/share/hubmemory-skill)." >&2
                exit 1
                ;;
        esac
    fi

    if [ -e "$target" ] || [ -L "$target" ]; then
        backup="$(next_backup_path "$target")"
        echo "[$label] backup: $target -> $backup"
        if ! $DRY_RUN; then
            mv "$target" "$backup"
        fi
    fi

    echo "[$label] link: $target -> $SOURCE_DIR"
    if ! $DRY_RUN; then
        mkdir -p "$parent"
        ln -s "$SOURCE_DIR" "$target"
    fi
}

echo "hubmemory source: $SOURCE_DIR"
$DRY_RUN && echo "mode: dry-run"

if [ "$TARGET_SCOPE" = "all" ] || [ "$TARGET_SCOPE" = "claude" ]; then
    install_target "claude" "$HOME/.claude/skills/hubmemory"
fi
if [ "$TARGET_SCOPE" = "all" ] || [ "$TARGET_SCOPE" = "codex" ]; then
    install_target "codex" "$HOME/.codex/skills/hubmemory"
fi

echo "skill links ready"
