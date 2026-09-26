#!/usr/bin/env bash
# hubmemory bootstrap.sh
# 幂等启动整个 hubmemory 系统：
#   1) 建目录 + 默认 config.yaml
#   2) 检查/安装 Python 依赖
#   3) 若 watcher 已运行，跳过；否则安装 launchd plist 并 load
#   4) 校验 live/index.json 是否可读

set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
SKILL_DIR="${HUBMEMORY_SKILL_DIR:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)}"
HUB_HOME="${HUBMEMORY_HOME:-$HOME/hubmemory}"

export HUBMEMORY_SKILL_DIR="$SKILL_DIR"
export HUBMEMORY_HOME="$HUB_HOME"

echo "== hubmemory bootstrap =="
echo "skill dir : $SKILL_DIR"
echo "hub home  : $HUB_HOME"

# --- 0. 建父目录 + 互斥锁（防止多个 agent 同时首次 bootstrap 造成 pip/launchctl 并发） ---
mkdir -p "$HUB_HOME"
LOCK_DIR="$HUB_HOME/.bootstrap.lock"
acquire_lock() {
    for attempt in $(seq 1 30); do
        if mkdir "$LOCK_DIR" 2>/dev/null; then
            echo "$$" > "$LOCK_DIR/pid"
            trap 'rm -rf "$LOCK_DIR"' EXIT
            return 0
        fi
        # 检查持锁者是否还活着，死了就抢锁
        if [ -f "$LOCK_DIR/pid" ]; then
            holder="$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")"
            if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
                rm -rf "$LOCK_DIR"
                continue
            fi
        fi
        [ "$attempt" -eq 1 ] && echo "another bootstrap in progress, waiting…"
        sleep 1
    done
    echo "ERROR: could not acquire bootstrap lock after 30s. Remove $LOCK_DIR manually if stale." >&2
    exit 1
}
acquire_lock

# --- 1. 建目录 ---
mkdir -p "$HUB_HOME"/{daily,monthly,memory,profile,live,state,logs,reports/requests}
touch "$HUB_HOME/memory/memories.jsonl"
touch "$HUB_HOME/profile/constraints.md" "$HUB_HOME/profile/preferences.md"

# --- 2. 默认 config.yaml ---
CONFIG="$HUB_HOME/config.yaml"
if [ ! -f "$CONFIG" ]; then
    cat > "$CONFIG" <<'YAML'
version: 1
timezone: Asia/Shanghai
listen:
  claude: ~/.claude/projects
  codex: ~/.codex/sessions
skip_content:
  - thinking
  - reasoning
# Optional diary enhancement. Disabled by default; the skill works without any local model.
llm:
  enabled: false
  # endpoint: "http://127.0.0.1:8080/v1/chat/completions"
  # model: "your-model-name"
  # timeout_seconds: 60
report:
  daily_hour: 3
  daily_minute: 7
retention:
  live_after_idle_hours: 24
watcher:
  reconcile_interval_seconds: 15
YAML
    echo "wrote default config.yaml"
fi

# --- 3. Python 依赖 ---
# 优先选系统 python（不受 venv 影响），launchd 需要绝对路径且不带 venv activation
pick_python() {
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
PYTHON_BIN="$(pick_python)"
if [ -z "$PYTHON_BIN" ]; then
    echo "ERROR: python3 not found (tried /usr/bin, /opt/homebrew/bin, /usr/local/bin, PATH)" >&2
    exit 1
fi
export HUBMEMORY_PYTHON="$PYTHON_BIN"
echo "python    : $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"

# 检查依赖
missing=()
for mod in watchdog yaml; do
    if ! "$PYTHON_BIN" -c "import $mod" >/dev/null 2>&1; then
        missing+=("$mod")
    fi
done

if [ "${#missing[@]}" -gt 0 ]; then
    echo "installing missing deps: ${missing[*]}"
    # yaml 对应包名是 pyyaml
    pip_names=()
    for m in "${missing[@]}"; do
        case "$m" in
            yaml) pip_names+=("pyyaml") ;;
            *)    pip_names+=("$m") ;;
        esac
    done
    "$PYTHON_BIN" -m pip install --user "${pip_names[@]}"
fi

# --- 4. 检查 watcher 是否已跑 ---
PID_FILE="$HUB_HOME/state/watcher.pid"
watcher_alive=false
if [ -f "$PID_FILE" ]; then
    pid="$(cat "$PID_FILE" 2>/dev/null || echo "")"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        watcher_alive=true
        echo "watcher   : already running (pid=$pid)"
    else
        echo "watcher   : stale pid file, will restart"
        rm -f "$PID_FILE"
    fi
fi

# 进程存在但心跳过期同样视为故障，避免“假活”。旧版本没有 health.json，
# bootstrap 会重启一次以启用心跳和周期补采。
if $watcher_alive; then
    HEALTH_FILE="$HUB_HOME/state/health.json"
    if ! "$PYTHON_BIN" - "$HEALTH_FILE" "$pid" <<'PY'
import datetime, json, pathlib, sys
path = pathlib.Path(sys.argv[1])
pid = int(sys.argv[2])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    heartbeat = datetime.datetime.fromisoformat(data["heartbeat_at"]).astimezone()
    age = (datetime.datetime.now().astimezone() - heartbeat).total_seconds()
    healthy = data.get("status") == "running" and data.get("pid") == pid and age <= 90
except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
    healthy = False
raise SystemExit(0 if healthy else 1)
PY
    then
        echo "watcher   : process is alive but heartbeat is stale, restarting"
        launchctl unload "$HOME/Library/LaunchAgents/com.hubmemory.watcher.plist" >/dev/null 2>&1 || true
        watcher_alive=false
        rm -f "$PID_FILE"
    fi
fi

if ! $watcher_alive; then
    echo "installing launchd plists…"
    bash "$SKILL_DIR/scripts/install_launchd.sh"

    # 等 3 秒让 watcher 起来
    for i in 1 2 3 4 5; do
        sleep 1
        if [ -f "$PID_FILE" ]; then
            pid="$(cat "$PID_FILE" 2>/dev/null || echo "")"
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                echo "watcher   : started (pid=$pid)"
                watcher_alive=true
                break
            fi
        fi
    done

    if ! $watcher_alive; then
        echo "WARN: watcher did not report a live pid after 5s." >&2
        echo "  Last 20 lines of $HUB_HOME/logs/watcher.log:" >&2
        tail -n 20 "$HUB_HOME/logs/watcher.log" 2>/dev/null || echo "  (log file missing)" >&2
        exit 1
    fi
fi

# --- 5. 简单健康检查 ---
INDEX="$HUB_HOME/live/index.json"
if [ ! -f "$INDEX" ]; then
    echo "{}" > "$INDEX"
fi
echo
echo "bootstrap OK. Next steps:"
echo "  python3 $SKILL_DIR/scripts/status.py"
echo "  python3 $SKILL_DIR/scripts/query.py <关键词>"
