#!/bin/bash
set -euo pipefail

command -v tmux >/dev/null 2>&1 || {
  printf 'SKIP: tmux is unavailable\n'
  exit 0
}

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work=$(mktemp -d)
export HOME="$work/home"
export XDG_CACHE_HOME="$HOME/.cache"
export XDG_STATE_HOME="$HOME/.local/state"
export CODEX_HOME="$HOME/.codex"
export CLAUDE_CONFIG_DIR="$HOME/.claude"
export TMUX_TMPDIR="$work/tmux"
# The test may itself be launched from tmux.  Never let the installer reconcile
# that real server; all targets below are passed with an explicit socket.
unset TMUX TMUX_PANE
mkdir -p "$HOME/.codex" "$HOME/.claude" "$HOME/.local/bin" "$TMUX_TMPDIR"
printf '{}\n' > "$HOME/.codex/hooks.json"
printf '{}\n' > "$HOME/.claude/settings.json"
printf 'set -g mouse on\n' > "$HOME/.tmux.conf"
printf 'alias ll="ls -l"\n' > "$HOME/.zshrc"
printf '#!/bin/sh\nsleep 120\n' > "$HOME/.local/bin/codex"
chmod +x "$HOME/.local/bin/codex"
cp "$HOME/.local/bin/codex" "$HOME/.local/bin/claude"
export PATH="$HOME/.local/bin:$PATH"

socket_label="codex-tmux-test-$$"
socket_path="$TMUX_TMPDIR/tmux-$(id -u)/$socket_label"

cleanup() {
  tmux -L "$socket_label" kill-server 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT HUP INT TERM

wait_for_value() {
  local command=$1 expected=$2 value=""
  for _attempt in {1..100}; do
    value=$(eval "$command" 2>/dev/null || true)
    [[ "$value" == "$expected" ]] && return 0
    sleep 0.03
  done
  printf 'expected %q, received %q from %s\n' "$expected" "$value" "$command" >&2
  return 1
}

"$repo_root/installer/install" \
  --home "$HOME" \
  --repo-root "$repo_root" \
  --features title-sync,window-attention,pane-logging

# A user-owned fixed index remains intact while managed hooks use appended indexes.
tmux -L "$socket_label" -f "$HOME/.config/codex-tmux-integration/tmux.conf" \
  new-session -d -s integration -n shell 'sleep 120'
tmux -S "$socket_path" set-hook -g 'after-select-window[50]' 'display-message user-hook'
tmux -S "$socket_path" source-file "$HOME/.config/codex-tmux-integration/tmux.conf"
tmux -S "$socket_path" show-hooks -g after-select-window | grep -F 'after-select-window[50] display-message user-hook' >/dev/null
[[ $(tmux -S "$socket_path" show-hooks -g after-select-window | grep -c 'CODEX_TMUX_HOOK=title-sync') -eq 1 ]]
[[ $(tmux -S "$socket_path" show-hooks -g after-select-window | grep -c 'CODEX_TMUX_HOOK=window-attention') -eq 1 ]]

for agent_config in "$HOME/.codex/hooks.json" "$HOME/.claude/settings.json"; do
  grep -F 'CODEX_TMUX_INTEGRATION=title-sync' "$agent_config" >/dev/null
  grep -F 'CODEX_TMUX_INTEGRATION=window-attention' "$agent_config" >/dev/null
done

window=$(tmux -S "$socket_path" display-message -p -t integration:0 '#{window_id}')
pane_agent=$(tmux -S "$socket_path" display-message -p -t integration:0.0 '#{pane_id}')
pane_shell=$(tmux -S "$socket_path" split-window -d -t "$window" -P -F '#{pane_id}' 'sleep 120')
server_pid=$(tmux -S "$socket_path" display-message -p '#{pid}')

tmux -S "$socket_path" select-pane -t "$pane_agent"
tmux -S "$socket_path" rename-window -t "$window" shell
tmux -S "$socket_path" set-window-option -t "$window" automatic-rename off
printf '%s\n' '{"id":"11111111-1111-1111-1111-111111111111","thread_name":"reliable title","updated_at":"2026-01-01T00:00:00Z"}' > "$CODEX_HOME/session_index.jsonl"

hook_payload='{"hook_event_name":"SessionStart","session_id":"11111111-1111-1111-1111-111111111111","transcript_path":null,"cwd":"/tmp","model":"test","permission_mode":"default","source":"startup"}'
printf '%s' "$hook_payload" | \
  TMUX="$socket_path,$server_pid,0" TMUX_PANE="$pane_agent" \
  "$HOME/.local/bin/codex-tmux-title-sync"
wait_for_value "tmux -S '$socket_path' display-message -p -t '$window' '#{window_name}'" 'reliable title'

# Fast focus changes end with the shell pane and must restore the original name.
for _iteration in {1..20}; do
  tmux -S "$socket_path" select-pane -t "$pane_shell"
  tmux -S "$socket_path" select-pane -t "$pane_agent"
done
tmux -S "$socket_path" select-pane -t "$pane_shell"
wait_for_value "tmux -S '$socket_path' display-message -p -t '$window' '#{window_name}'" shell

# A blank Codex CLI has no SessionStart payload yet. zsh preexec must still
# rename the window before the stub process starts, without a pane focus change.
startup_window=$(tmux -S "$socket_path" new-window -d -n launch-shell -P -F '#{window_id}' 'exec zsh -f')
startup_pane=$(tmux -S "$socket_path" display-message -p -t "$startup_window" '#{pane_id}')
tmux -S "$socket_path" set-window-option -t "$startup_window" automatic-rename off
tmux -S "$socket_path" send-keys -t "$startup_pane" \
  "source '$HOME/.config/codex-tmux-integration/shell.zsh'" Enter
sleep 0.1
tmux -S "$socket_path" send-keys -t "$startup_pane" codex Enter
wait_for_value "tmux -S '$socket_path' display-message -p -t '$startup_window' '#{window_name}'" codex
# The preexec hook changes the title immediately before exec.  Wait until the
# stub is actually the foreground process so Ctrl-C cannot race that handoff.
wait_for_value "tmux -S '$socket_path' display-message -p -t '$startup_pane' '#{pane_current_command}'" sh
tmux -S "$socket_path" send-keys -t "$startup_pane" C-c
wait_for_value "tmux -S '$socket_path' display-message -p -t '$startup_window' '#{window_name}'" launch-shell

# Codex hook subprocesses may omit TMUX and TMUX_PANE. Keep the hook beneath a
# real process named "codex" and verify parent-process routing without either
# variable or a resume argument.
fake_codex="$work/codex"
ancestor_launcher="$work/ancestor-launcher.sh"
cp /bin/sh "$fake_codex"
printf '%s\n' \
  '#!/bin/sh' \
  'sleep 0.1' \
  "printf '%s' '$hook_payload' | env -u TMUX -u TMUX_PANE '$HOME/.local/bin/codex-tmux-title-sync'" \
  'sleep 120' \
  > "$ancestor_launcher"
ancestor_window=$(tmux -S "$socket_path" new-window -d -n ancestor-shell -P -F '#{window_id}' \
  "$fake_codex $ancestor_launcher")
wait_for_value "tmux -S '$socket_path' display-message -p -t '$ancestor_window' '#{window_name}'" 'reliable title'

# Claude Code closes its transcript between writes, so a hook is bound to a pane
# through the session state file Claude keeps for its own PID. The hook below
# carries neither TMUX nor TMUX_PANE and is routed by its "claude" ancestor.
claude_session='44444444-4444-4444-4444-444444444444'
claude_projects="$HOME/.claude/projects/-work"
mkdir -p "$claude_projects" "$HOME/.claude/sessions"
claude_transcript="$claude_projects/$claude_session.jsonl"
printf '%s\n' \
  '{"type":"user","message":{"content":"hi"}}' \
  "{\"type\":\"ai-title\",\"aiTitle\":\"claude title\",\"sessionId\":\"$claude_session\"}" \
  > "$claude_transcript"
claude_payload="{\"hook_event_name\":\"Stop\",\"session_id\":\"$claude_session\",\"transcript_path\":\"$claude_transcript\",\"cwd\":\"$work\"}"

fake_claude="$work/claude"
claude_launcher="$work/claude-launcher.sh"
cp /bin/sh "$fake_claude"
cat > "$claude_launcher" <<CLAUDE_LAUNCHER
#!/bin/sh
start=\$(sed 's/^.*) //' "/proc/\$\$/stat" | awk '{print \$20}')
printf '{"pid":%s,"sessionId":"%s","procStart":"%s","name":"%s","nameSource":"derived"}\n' \\
  "\$\$" '$claude_session' "\$start" 'work-1' > '$HOME/.claude/sessions/'"\$\$"'.json'
sleep 0.1
printf '%s' '$claude_payload' | env -u TMUX -u TMUX_PANE '$HOME/.local/bin/codex-tmux-title-sync'
sleep 120
CLAUDE_LAUNCHER
chmod +x "$claude_launcher"

claude_window=$(tmux -S "$socket_path" new-window -d -n claude-shell -P -F '#{window_id}' \
  "$fake_claude $claude_launcher")
claude_pane=$(tmux -S "$socket_path" display-message -p -t "$claude_window" '#{pane_id}')
tmux -S "$socket_path" set-window-option -t "$claude_window" automatic-rename off
# A second pane keeps the window alive after the agent pane is closed below.
tmux -S "$socket_path" split-window -d -t "$claude_window" 'sleep 120' >/dev/null
wait_for_value "tmux -S '$socket_path' display-message -p -t '$claude_window' '#{window_name}'" 'claude title'

# A newer chat title reaches the window without capturing the Claude pane.
"$HOME/.local/bin/codex-tmux-title-sync" \
  --socket "$socket_path" --pane "$claude_pane" --ensure-watcher </dev/null
printf '%s\n' \
  "{\"type\":\"ai-title\",\"aiTitle\":\"claude renamed\",\"sessionId\":\"$claude_session\"}" \
  >> "$claude_transcript"
wait_for_value "tmux -S '$socket_path' display-message -p -t '$claude_window' '#{window_name}'" 'claude renamed'

# Claude reports "waiting for the user" through Notification rather than a
# permission event, and the same notifier highlights the window.
claude_notify="{\"hook_event_name\":\"Notification\",\"session_id\":\"$claude_session\",\"transcript_path\":\"$claude_transcript\",\"message\":\"needs input\"}"
printf '%s' "$claude_notify" | \
  TMUX="$socket_path,$server_pid,0" TMUX_PANE="$claude_pane" \
  "$HOME/.local/bin/agent-tmux-notify"
wait_for_value "tmux -S '$socket_path' show-window-options -v -t '$claude_window' window-status-style" 'fg=black,bg=#d7af00,bold'
tmux -S "$socket_path" select-window -t "$claude_window"
wait_for_value "tmux -S '$socket_path' show-window-options -v -t '$claude_window' window-status-style" ''

# Closing the Claude pane releases the window name it owned.
tmux -S "$socket_path" kill-pane -t "$claude_pane"
wait_for_value "tmux -S '$socket_path' display-message -p -t '$claude_window' '#{window_name}'" claude-shell

# Once a pane is bound to a session UUID, later renames come from the session
# index. This fake Codex pane prints no TUI status line, so the update cannot
# succeed through capture-pane.
printf '%s\n' \
  '{"id":"22222222-2222-2222-2222-222222222222","thread_name":"index initial","updated_at":"2026-01-01T00:00:00Z"}' \
  >> "$CODEX_HOME/session_index.jsonl"
index_window=$(tmux -S "$socket_path" new-window -d -n index-shell -P -F '#{window_id}' \
  "$fake_codex -c 'sleep 120; :'")
index_pane=$(tmux -S "$socket_path" display-message -p -t "$index_window" '#{pane_id}')
index_payload='{"hook_event_name":"SessionStart","session_id":"22222222-2222-2222-2222-222222222222","transcript_path":null,"cwd":"/tmp","model":"test","permission_mode":"default","source":"startup"}'
printf '%s' "$index_payload" | \
  TMUX="$socket_path,$server_pid,0" TMUX_PANE="$index_pane" \
  "$HOME/.local/bin/codex-tmux-title-sync"
wait_for_value "tmux -S '$socket_path' display-message -p -t '$index_window' '#{window_name}'" 'index initial'
"$HOME/.local/bin/codex-tmux-title-sync" \
  --socket "$socket_path" --pane "$index_pane" --ensure-watcher </dev/null
printf '%s\n' \
  '{"id":"22222222-2222-2222-2222-222222222222","thread_name":"index renamed","updated_at":"2026-01-01T00:01:00Z"}' \
  >> "$CODEX_HOME/session_index.jsonl"
wait_for_value "tmux -S '$socket_path' display-message -p -t '$index_window' '#{window_name}'" 'index renamed'
tmux -S "$socket_path" send-keys -t "$index_pane" C-c

# The eBPF listener registry is the authoritative source in the new runtime.
# Applying it must rename and restore without starting a per-pane watcher.
registry_window=$(tmux -S "$socket_path" new-window -d -n registry-shell -P -F '#{window_id}' 'sleep 120')
registry_pane=$(tmux -S "$socket_path" display-message -p -t "$registry_window" '#{pane_id}')
registry_pid=$(tmux -S "$socket_path" display-message -p -t "$registry_pane" '#{pane_pid}')
# Parse after the final ')' because Linux permits spaces in the comm field.
registry_start=$(sed 's/^.*) //' "/proc/$registry_pid/stat" | awk '{print $20}')
registry_path="$HOME/.local/state/codex-tmux-integration/sessions.json"
mkdir -p "$(dirname "$registry_path")"
printf '{"schema":"codex.session-registry.v1","connections":{"c":{"client":{"pid":%s,"start_ticks":%s},"tmux":{"socket":"%s","server_pid":%s,"pane":"%s","pane_pid":%s},"thread":{"id":"33333333-3333-3333-3333-333333333333","name":"registry title"}}}}\n' \
  "$registry_pid" "$registry_start" "$socket_path" "$server_pid" "$registry_pane" "$registry_pid" \
  > "$registry_path"
chmod 600 "$registry_path"
"$HOME/.local/bin/codex-tmux-title-sync" --registry-sync --registry-file "$registry_path" </dev/null
wait_for_value "tmux -S '$socket_path' display-message -p -t '$registry_window' '#{window_name}'" 'registry title'
[[ -z $(find "$XDG_CACHE_HOME/codex-tmux-integration/title-sync" -path '*/watchers/*.json' -exec grep -l "\"pane\": \"$registry_pane\"" {} + 2>/dev/null || true) ]]
printf '{"schema":"codex.session-registry.v1","connections":{}}\n' > "$registry_path"
chmod 600 "$registry_path"
"$HOME/.local/bin/codex-tmux-title-sync" --registry-sync --registry-file "$registry_path" </dev/null
wait_for_value "tmux -S '$socket_path' display-message -p -t '$registry_window' '#{window_name}'" registry-shell
rm -f "$registry_path"

tmux -S "$socket_path" select-pane -t "$pane_agent"
wait_for_value "tmux -S '$socket_path' display-message -p -t '$window' '#{window_name}'" 'reliable title'
end_payload='{"hook_event_name":"SessionEnd","session_id":"11111111-1111-1111-1111-111111111111","transcript_path":null,"cwd":"/tmp","reason":"other"}'
printf '%s' "$end_payload" | \
  TMUX="$socket_path,$server_pid,0" TMUX_PANE="$pane_agent" \
  "$HOME/.local/bin/codex-tmux-title-sync"
wait_for_value "tmux -S '$socket_path' display-message -p -t '$window' '#{window_name}'" shell

# Attention repairs missing clear hooks before it applies a style, then restores
# the pre-existing local value as soon as the highlighted window is selected.
tmux -S "$socket_path" set-window-option -t "$window" window-status-style 'fg=blue'
tmux -S "$socket_path" select-window -t "$ancestor_window"
"$HOME/.local/bin/codex-tmux-hook-manager" remove window-attention --socket "$socket_path"
[[ -z $("$HOME/.local/bin/codex-tmux-hook-manager" \
  list-owned window-attention --socket "$socket_path") ]]
notify_payload='{"hook_event_name":"Stop","session_id":"11111111-1111-1111-1111-111111111111"}'
printf '%s' "$notify_payload" | \
  TMUX="$socket_path,$server_pid,0" TMUX_PANE="$pane_agent" \
  "$HOME/.local/bin/agent-tmux-notify"
wait_for_value "tmux -S '$socket_path' show-window-options -v -t '$window' window-status-style" 'fg=black,bg=#d7af00,bold'
[[ $("$HOME/.local/bin/codex-tmux-hook-manager" \
  list-owned window-attention --socket "$socket_path" | wc -l) -eq 4 ]]
tmux -S "$socket_path" select-window -t "$window"
wait_for_value "tmux -S '$socket_path' show-window-options -v -t '$window' window-status-style" 'fg=blue'

# Managed logging is enabled and marked. A user pipe is preserved.
"$HOME/.local/bin/tmux-autolog" --socket "$socket_path" sweep
wait_for_value "tmux -S '$socket_path' display-message -p -t '$pane_agent' '#{pane_pipe}'" 1
[[ -n $(tmux -S "$socket_path" show-options -pqv -t "$pane_agent" @codex_tmux_pipe) ]]
"$HOME/.local/bin/tmux-autolog" --socket "$socket_path" cleanup
wait_for_value "tmux -S '$socket_path' display-message -p -t '$pane_agent' '#{pane_pipe}'" 0
user_log="$work/user-pipe.log"
tmux -S "$socket_path" pipe-pane -t "$pane_shell" "cat >> '$user_log'"
"$HOME/.local/bin/tmux-autolog" --socket "$socket_path" pane "$pane_shell"
wait_for_value "tmux -S '$socket_path' display-message -p -t '$pane_shell' '#{pane_pipe}'" 1
[[ -z $(tmux -S "$socket_path" show-options -pqv -t "$pane_shell" @codex_tmux_pipe) ]]
"$HOME/.local/bin/tmux-autolog" --socket "$socket_path" pane "$pane_agent"

CODEX_TMUX_HOME="$HOME" "$repo_root/installer/doctor" | tee "$work/doctor.log"
grep -F 'SUMMARY errors=0' "$work/doctor.log" >/dev/null

"$repo_root/installer/uninstall" --home "$HOME"
! tmux -S "$socket_path" show-hooks -g after-select-window | grep -F 'CODEX_TMUX_HOOK=' >/dev/null
wait_for_value "tmux -S '$socket_path' display-message -p -t '$pane_agent' '#{pane_pipe}'" 0
wait_for_value "tmux -S '$socket_path' display-message -p -t '$pane_shell' '#{pane_pipe}'" 1
tmux -S "$socket_path" show-hooks -g after-select-window | grep -F 'after-select-window[50] display-message user-hook' >/dev/null

printf 'isolated tmux integration passed: %s\n' "$socket_path"
