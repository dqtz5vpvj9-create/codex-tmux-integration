#!/bin/bash
# Capture Codex hook stdin and forward it to an optional AndroidTools queue.

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
RAW_LOG="${ANDROIDTOOLS_NOTIFY_RAW_LOG:-$CACHE_DIR/codex_hook_raw_stdin.json}"
HOOK_LOG="${ANDROIDTOOLS_NOTIFY_HOOK_LOG:-$CACHE_DIR/codex_hook.log}"
TMP_DIR="${TMPDIR:-/tmp}"

if [ -z "$PYTHON" ]; then
    PYTHON="$(command -v python3 2>/dev/null)"
fi

mkdir -p "$CACHE_DIR" 2>/dev/null || true
chmod 700 "$CACHE_DIR" 2>/dev/null || true

STDIN_FILE="$(mktemp "$TMP_DIR/codex_hook_stdin.XXXXXX.json")" || exit 0
QUEUE_OUT="$(mktemp "$TMP_DIR/codex_hook_queue.XXXXXX.log")" || {
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

if [ -z "$PYTHON" ] || [ -z "$QUEUE" ] || [ ! -f "$QUEUE" ]; then
    printf 'notification backend unavailable: python=%s queue=%s\n' \
        "${PYTHON:-missing}" "$QUEUE" > "$HOOK_LOG"
    exit 0
fi

"$PYTHON" "$QUEUE" enqueue \
    --stdin-file "$STDIN_FILE" \
    --hook "$HOOK" \
    --client codex \
    --icon codex \
    --channel "$CHANNEL" > "$QUEUE_OUT" 2>&1 || true
mv -f "$QUEUE_OUT" "$HOOK_LOG" 2>/dev/null || true
exit 0
