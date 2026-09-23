#!/bin/bash
# End to end check of the session listener with a real Codex TUI, isolated from
# the user's own Codex and tmux: a sandbox CODEX_HOME with its own app-server, a
# second listener on that app-server (the installed capture helper, run through
# sudo like the service does), and a private tmux server for the TUIs.
#
# The sandbox logs in with a made-up API key. Nothing here needs the model, and
# the user's ChatGPT tokens are never copied: a sandbox app-server refreshing
# them would rotate the refresh token out from under the real one. The two
# messages sent below only exist so that /resume lists their sessions; their
# turns fail with 401 and are interrupted.
#
# Needs codex on PATH, passwordless sudo and an installed helper bundle.
set -u

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
listener_bin="$repo_root/features/session-listener/bin/codex-session-listener"
sandbox=$(mktemp -d "${E2E_TMPDIR:-${TMPDIR:-/tmp}}/listener-e2e.XXXXXX")
chmod 700 "$sandbox"
export CODEX_HOME="$sandbox/codex"
control="$CODEX_HOME/app-server-control/app-server-control.sock"
registry="$sandbox/state/sessions.json"
events="$sandbox/listener.jsonl"
mkdir -p "$CODEX_HOME" "$sandbox/work" "$sandbox/state"
unset TMUX TMUX_PANE

status=0
fail() { echo "FAIL: $*"; status=1; }
t() { tmux -S "$sandbox/tmux.sock" -f /dev/null "$@"; }
screen() { t capture-pane -p -t "$1"; }
# With --no-alt-screen what the TUI drew earlier stays on the screen, so a prompt
# counts as shown only once it appears more often than before the key was sent.
shown() { t capture-pane -p -S - -t "$1" | grep -c -- "$2"; }

# Wait until the pane shows a pattern; say which one never came.
wait_screen() {  # pane pattern [seconds]
    local deadline=$((SECONDS + ${3:-20}))
    until screen "$1" | grep -q -- "$2"; do
        ((SECONDS > deadline)) && { fail "$1 never showed [$2]"; screen "$1" | grep -v '^\s*$' | tail -5; return 1; }
        sleep 0.3
    done
}

wait_new() {  # pane pattern count-before [seconds]
    local deadline=$((SECONDS + ${4:-20}))
    until (($(shown "$1" "$2") > $3)); do
        ((SECONDS > deadline)) && { fail "$1 never showed [$2] again"; screen "$1" | grep -v '^\s*$' | tail -5; return 1; }
        sleep 0.3
    done
}

send_after() {  # pane text pattern: send the text, then wait for a fresh pattern
    local before
    before=$(shown "$1" "$3")
    command_line "$1" "$2"
    wait_new "$1" "$3" "$before" "${4:-20}"
}

# Wait until a Python expression over the registry (`connections` by pane) holds.
wait_registry() {  # description expression [seconds]
    local deadline=$((SECONDS + ${3:-15}))
    until python3 - "$registry" "$2" << 'EOF'
import json, sys
try:
    registry = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    sys.exit(1)
by_pane = {}
for key, connection in registry.get("connections", {}).items():
    pane = (connection.get("tmux") or {}).get("pane")
    by_pane.setdefault(pane, []).append(dict(connection, key=key))
bound = {pane: next((c["thread"] for c in rows if (c.get("thread") or {}).get("id")), {})
         for pane, rows in by_pane.items()}
sys.exit(0 if eval(sys.argv[2], {"by_pane": by_pane, "bound": bound}) else 1)
EOF
    do
        ((SECONDS > deadline)) && { fail "$1"; return 1; }
        sleep 0.3
    done
}

thread_id() {  # name
    python3 - "$events" "$1" << 'EOF'
import json, sys
for line in open(sys.argv[1]):
    thread = json.loads(line).get("thread") or {}
    if thread.get("name") == sys.argv[2]:
        print(thread["id"])
        break
EOF
}

event_seen() {  # source thread-name
    python3 - "$events" "$1" "$2" << 'EOF'
import json, sys
for line in open(sys.argv[1]):
    event = json.loads(line)
    if event.get("source") == sys.argv[2] and (event.get("thread") or {}).get("name") == sys.argv[3]:
        sys.exit(0)
sys.exit(1)
EOF
}

command_line() {  # pane text: a slash command or a message, typed and sent
    t send-keys -t "$1" -l "$2"
    sleep 0.8
    t send-keys -t "$1" Enter
}

rename() {  # pane name
    send_after "$1" /rename "Name thread" || return 1
    t send-keys -t "$1" -l "$2"
    sleep 0.8
    t send-keys -t "$1" Enter
    wait_screen "$1" "· $2 ·"
}

# /resume lists only sessions with a user message; the turn fails without a model.
leave_a_message() {  # pane
    local before
    before=$(shown "$1" "interrupted")
    send_after "$1" "listener e2e message" "esc to interrupt" 30 || return 1
    t send-keys -t "$1" Escape
    wait_new "$1" "interrupted" "$before"
}

resume() {  # pane name
    send_after "$1" /resume "Resume a previous session" || return 1
    t send-keys -t "$1" -l "$2"
    sleep 1.5
    t send-keys -t "$1" Enter
    wait_screen "$1" "· $2 ·"
}

# The listener stops its capture helper on the way out; wait for that.
stop_listener() {
    local pid
    pid=$(cat "$sandbox/listener.pid" 2> /dev/null) || return 0
    kill "$pid" 2> /dev/null
    for _ in $(seq 50); do kill -0 "$pid" 2> /dev/null || return 0; sleep 0.2; done
}

start_listener() {
    setsid "$listener_bin" --control-socket "$control" --state-file "$registry" \
        --no-title-sync >> "$events" 2>> "$sandbox/listener.err" < /dev/null &
    echo $! > "$sandbox/listener.pid"
}

start_tui() {  # window-name [codex arguments...]
    local window=$1
    shift
    t new-window -d -t e2e -n "$window" -c "$sandbox/work" -e CODEX_HOME="$CODEX_HOME" "bash --norc --noprofile"
    t send-keys -t "e2e:$window" "codex --no-alt-screen $*" Enter
    wait_screen "e2e:$window" "Ask Codex to do anything" 30 || return 1
    # Whatever the TUI sends before the listener watches its connection is lost.
    local pane
    pane=$(t display-message -p -t "e2e:$window" '#{pane_id}')
    wait_registry "the listener should pick up the TUI in $pane" "'$pane' in by_pane"
}

# The app-server runs as an npm wrapper and the native binary under it; both are
# found by the sandbox they run in rather than by a pid that may be the wrapper's.
sandbox_app_servers() {
    local pid
    for pid in $(pgrep -f 'app-server --listen unix://'); do
        [[ "$(tr '\0' '\n' 2> /dev/null < "/proc/$pid/environ" | grep -m1 '^CODEX_HOME=')" == "CODEX_HOME=$CODEX_HOME" ]] &&
            echo "$pid"
    done
}

cleanup() {
    t kill-server 2> /dev/null
    stop_listener
    local servers
    servers=$(sandbox_app_servers)
    [[ -n "$servers" ]] && kill $servers 2> /dev/null
    for _ in $(seq 25); do [[ -z "$(sandbox_app_servers)" ]] && break; sleep 0.2; done
    # A sandbox path is too long for a socket, so Codex keeps the real one, and
    # its lock, under a name hashed from the path.
    local hashed
    hashed="${TMPDIR:-/tmp}/codex-daemon-$(id -u)/$(printf '%s' "$control" | sha256sum | cut -d' ' -f1)"
    rm -f -- "${hashed:?}" "${hashed:?}.lock"
    [[ -n "${KEEP_SANDBOX:-}" ]] && { echo "kept $sandbox"; return; }
    rm -rf -- "${sandbox:?}"
}
trap cleanup EXIT

printf 'check_for_update_on_startup = false\n[projects."%s/work"]\ntrust_level = "trusted"\n' \
    "$sandbox" > "$CODEX_HOME/config.toml"
printf 'sk-listener-e2e-not-a-key\n' | codex login --with-api-key > /dev/null 2>&1 ||
    { echo "FAIL: could not log the sandbox in"; exit 1; }
(cd "$sandbox/work" && exec setsid codex app-server --listen unix:// > "$sandbox/app-server.log" 2>&1 < /dev/null) &
for _ in $(seq 100); do [[ -S "$control" ]] && break; sleep 0.2; done
[[ -S "$control" ]] || { echo "FAIL: the sandbox app-server did not come up"; exit 1; }
start_listener
t new-session -d -s e2e -x 160 -y 45 -c "$sandbox/work" "bash --norc --noprofile"

echo "--- a rename reaches the registry through thread/name/updated"
start_tui one || exit 1
one=$(t display-message -p -t e2e:one '#{pane_id}')
rename e2e:one e2e-first
leave_a_message e2e:one
wait_registry "pane $one should be named e2e-first" "bound.get('$one', {}).get('name') == 'e2e-first'"
event_seen thread/name/updated e2e-first || fail "the name should come from the thread/name/updated announcement"

echo "--- /new starts a thread of its own"
command_line e2e:one /new
sleep 2
rename e2e:one e2e-second
leave_a_message e2e:one
wait_registry "pane $one should be on e2e-second after /new" "bound.get('$one', {}).get('name') == 'e2e-second'"

echo "--- /resume moves the pane once the old thread is let go"
resume e2e:one e2e-first
wait_registry "pane $one should be back on e2e-first" "bound.get('$one', {}).get('name') == 'e2e-first'"
event_seen thread/resume+unsubscribe e2e-first || fail "the switch should be the resume followed by the unsubscribe"

echo "--- the connection a /resume list opens is on no thread"
wait_registry "the last /resume list of pane $one should be gone" "len(by_pane.get('$one', [])) == 1"
send_after e2e:one /resume "Resume a previous session"
wait_registry "pane $one should show a second connection while the list is up" "len(by_pane.get('$one', [])) == 2" 10
wait_registry "only one connection of pane $one may carry a thread" \
    "[bool((c.get('thread') or {}).get('id')) for c in by_pane['$one']].count(True) == 1"
t send-keys -t e2e:one Escape

echo "--- a remembered switch outlives a listener restart and beats the resume argument"
wait_registry "the /resume list of pane $one should close" "len(by_pane.get('$one', [])) == 1"
start_tui two resume "$(thread_id e2e-second)" || exit 1
two=$(t display-message -p -t e2e:two '#{pane_id}')
wait_registry "pane $two should start on e2e-second" "bound.get('$two', {}).get('name') == 'e2e-second'"
resume e2e:two e2e-first
wait_registry "pane $two should be on e2e-first" "bound.get('$two', {}).get('name') == 'e2e-first'"
stop_listener
restarted_at=$(wc -l < "$events")
start_listener
wait_registry "after a restart pane $two should still be on e2e-first" \
    "bound.get('$two', {}).get('name') == 'e2e-first'" 20
tail -n +"$((restarted_at + 1))" "$events" | python3 -c '
import json, sys
pane, thread = sys.argv[1], sys.argv[2]
sys.exit(0 if any(
    event["event"] == "thread.bound" and event.get("source") == "registry:remembered"
    and (event.get("tmux") or {}).get("pane") == pane and event["thread"]["id"] == thread
    for event in map(json.loads, sys.stdin)) else 1)' "$two" "$(thread_id e2e-first)" ||
    fail "pane $two should have been restored from the remembered binding, not its resume argument"

if ((status == 0)); then
    echo PASS
else
    sed -n '1,20p' "$sandbox/listener.err"
    echo "FAILED (KEEP_SANDBOX=1 keeps the sandbox for a look)"
fi
exit "$status"
