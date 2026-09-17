#!/bin/bash
# Capture Claude hook stdin and forward it to an optional AndroidTools queue.

set +e
umask 077

HOOK="${1:-}"
CHANNEL="${ANDROIDTOOLS_NOTIFY_CHANNEL:-default}"
CONFIG_FILE="${CODEX_TMUX_NOTIFY_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/codex-tmux-integration/notifications.env}"
ENV_QUEUE_SET="${ANDROIDTOOLS_NOTIFY_QUEUE+x}"
ENV_QUEUE_VALUE="${ANDROIDTOOLS_NOTIFY_QUEUE:-}"
ENV_PYTHON_SET="${ANDROIDTOOLS_NOTIFY_PYTHON+x}"
ENV_PYTHON_VALUE="${ANDROIDTOOLS_NOTIFY_PYTHON:-}"
if [ -r "$CONFIG_FILE" ]; then
    # This file is generated locally with mode 0600 and is never committed.
    . "$CONFIG_FILE"
fi
if [ "$ENV_QUEUE_SET" = x ]; then
    ANDROIDTOOLS_NOTIFY_QUEUE="$ENV_QUEUE_VALUE"
fi
if [ "$ENV_PYTHON_SET" = x ]; then
    ANDROIDTOOLS_NOTIFY_PYTHON="$ENV_PYTHON_VALUE"
fi
PYTHON="${ANDROIDTOOLS_NOTIFY_PYTHON:-}"
QUEUE="${ANDROIDTOOLS_NOTIFY_QUEUE:-}"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/codex-tmux-integration"
RAW_LOG="${ANDROIDTOOLS_NOTIFY_CLAUDE_RAW_LOG:-$CACHE_DIR/claude_hook_raw_stdin.json}"
HOOK_LOG="${ANDROIDTOOLS_NOTIFY_CLAUDE_HOOK_LOG:-$CACHE_DIR/claude_hook.log}"
TMP_DIR="${TMPDIR:-/tmp}"

if [ -z "$PYTHON" ]; then
    PYTHON="$(command -v python3 2>/dev/null)"
fi

mkdir -p "$CACHE_DIR" 2>/dev/null || true
chmod 700 "$CACHE_DIR" 2>/dev/null || true

STDIN_FILE="$(mktemp "$TMP_DIR/claude_hook_stdin.XXXXXX.json")" || exit 0
QUEUE_OUT="$(mktemp "$TMP_DIR/claude_hook_queue.XXXXXX.log")" || {
    rm -f "$STDIN_FILE"
    exit 0
}

cleanup() {
    rm -f "$STDIN_FILE" "$QUEUE_OUT" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

if ! cat > "$STDIN_FILE"; then
    printf '%s\n' "failed to capture hook stdin" > "$HOOK_LOG"
    exit 0
fi

if [ -n "$PYTHON" ]; then
    CANONICAL_HOOK=$("$PYTHON" - "$STDIN_FILE" 2>/dev/null <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        value = json.load(stream).get("hook_event_name", "")
    if isinstance(value, str):
        print(value)
except Exception:
    pass
PY
)
    [ -n "$CANONICAL_HOOK" ] && HOOK="$CANONICAL_HOOK"
fi

RAW_TMP="${RAW_LOG}.tmp.$$"
if cp "$STDIN_FILE" "$RAW_TMP" 2>/dev/null; then
    chmod 600 "$RAW_TMP" 2>/dev/null || true
    mv -f "$RAW_TMP" "$RAW_LOG" 2>/dev/null || true
fi

if [ -n "$PYTHON" ]; then
    SUBAGENT_SKIP=$("$PYTHON" - "$STDIN_FILE" 2>/dev/null <<'PY'
import json
import os
import re
import sys

SUBAGENT_PATH_RE = re.compile(
    r"/\.claude/projects/[^/]+/[^/]+/subagents/agent-[^/]+\.jsonl$"
)

def _sidechain_tip(path):
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            step = 4096
            buf = b""
            while end > 0 and buf.count(b"\n") < 2:
                chunk = min(step, end)
                end -= chunk
                fh.seek(end)
                buf = fh.read(chunk) + buf
        line = buf.decode("utf-8", errors="replace").splitlines()[-1] if buf else ""
        if not line.strip():
            return False
        return bool(json.loads(line).get("isSidechain"))
    except Exception:
        return False

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        payload = {}
except Exception:
    payload = {}

hook_name = str(payload.get("hook_event_name") or "")
transcript = str(payload.get("transcript_path") or "")
skip = (
    hook_name == "SubagentStop"
    or bool(SUBAGENT_PATH_RE.search(transcript))
    or _sidechain_tip(transcript)
)
print("1" if skip else "0")
PY
)
    if [ "$SUBAGENT_SKIP" = "1" ]; then
        printf 'skipped: sub-agent event (hook=%s)\n' "$HOOK" > "$HOOK_LOG"
        exit 0
    fi
fi

if [ -z "$PYTHON" ] || [ -z "$QUEUE" ] || [ ! -f "$QUEUE" ]; then
    printf 'notification backend unavailable: python=%s queue=%s\n' \
        "${PYTHON:-missing}" "$QUEUE" > "$HOOK_LOG"
    exit 0
fi

"$PYTHON" "$QUEUE" enqueue \
    --stdin-file "$STDIN_FILE" \
    --hook "$HOOK" \
    --client claude \
    --icon claude \
    --channel "$CHANNEL" > "$QUEUE_OUT" 2>&1 || true
mv -f "$QUEUE_OUT" "$HOOK_LOG" 2>/dev/null || true
exit 0
