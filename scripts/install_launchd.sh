#!/usr/bin/env bash
# hubmemory install_launchd.sh
# 从模板生成 launchd plist 到 ~/Library/LaunchAgents/，并 load。
# 幂等：已存在的 plist 会先 unload 再重写并重载。

set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
SKILL_DIR="${HUBMEMORY_SKILL_DIR:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)}"
HUB_HOME="${HUBMEMORY_HOME:-$HOME/hubmemory}"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PLIST_SRC_DIR="$SKILL_DIR/plists"

# 选 launchd 用的 python3：优先系统 python（不受 venv 影响），可用 HUBMEMORY_PYTHON 覆盖
pick_launchd_python() {
    if [ -n "${HUBMEMORY_PYTHON:-}" ] && [ -x "${HUBMEMORY_PYTHON}" ]; then
        echo "$HUBMEMORY_PYTHON"
        return
    fi
    for candidate in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return
        fi
    done
    command -v python3 || true
}
PYTHON_BIN="$(pick_launchd_python)"
if [ -z "$PYTHON_BIN" ]; then
    echo "ERROR: python3 not found (tried /usr/bin, /opt/homebrew/bin, /usr/local/bin, PATH)" >&2
    exit 1
fi
# 解析成绝对路径（readlink -f 在旧 macOS 上可能不存在）
if command -v readlink >/dev/null 2>&1; then
    resolved="$(readlink "$PYTHON_BIN" 2>/dev/null || echo "$PYTHON_BIN")"
    case "$resolved" in
        /*) PYTHON_BIN="$resolved" ;;
    esac
fi

mkdir -p "$LAUNCH_AGENTS" "$HUB_HOME/logs"

render_plist() {
    local name="$1"
    local src="$PLIST_SRC_DIR/${name}.plist.tmpl"
    local dst="$LAUNCH_AGENTS/${name}.plist"

    if [ ! -f "$src" ]; then
        echo "ERROR: template not found: $src" >&2
        return 1
    fi

    # 先 unload 已存在的
    if [ -f "$dst" ]; then
        launchctl unload "$dst" >/dev/null 2>&1 || true
    fi

    # 变量替换（用 | 作为分隔符，因为路径含 /）
    sed \
        -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" \
        -e "s|__SKILL_DIR__|${SKILL_DIR}|g" \
        -e "s|__HUB_HOME__|${HUB_HOME}|g" \
        "$src" > "$dst"

    chmod 644 "$dst"
    launchctl load -w "$dst"
    echo "installed: $dst"
}

render_plist "com.hubmemory.watcher"
render_plist "com.hubmemory.dailyreport"

echo "launchd install done."
