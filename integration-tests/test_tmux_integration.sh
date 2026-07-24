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
export TMUX_TMPDIR="$work/tmux"
mkdir -p "$HOME/.codex" "$HOME/.claude" "$HOME/.local/bin" "$TMUX_TMPDIR"
printf '{}\n' > "$HOME/.codex/hooks.json"
printf '{}\n' > "$HOME/.claude/settings.json"
printf 'set -g mouse on\n' > "$HOME/.tmux.conf"
printf 'alias ll="ls -l"\n' > "$HOME/.zshrc"
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

tmux -S "$socket_path" select-pane -t "$pane_agent"
wait_for_value "tmux -S '$socket_path' display-message -p -t '$window' '#{window_name}'" 'reliable title'
end_payload='{"hook_event_name":"SessionEnd","session_id":"11111111-1111-1111-1111-111111111111","transcript_path":null,"cwd":"/tmp","reason":"other"}'
printf '%s' "$end_payload" | \
  TMUX="$socket_path,$server_pid,0" TMUX_PANE="$pane_agent" \
  "$HOME/.local/bin/codex-tmux-title-sync"
wait_for_value "tmux -S '$socket_path' display-message -p -t '$window' '#{window_name}'" shell

# Attention restores the pre-existing local window style.
tmux -S "$socket_path" set-window-option -t "$window" window-status-style 'fg=blue'
notify_payload='{"hook_event_name":"Stop","session_id":"11111111-1111-1111-1111-111111111111"}'
printf '%s' "$notify_payload" | \
  TMUX="$socket_path,$server_pid,0" TMUX_PANE="$pane_agent" \
  "$HOME/.local/bin/agent-tmux-notify"
wait_for_value "tmux -S '$socket_path' show-window-options -v -t '$window' window-status-style" 'fg=black,bg=#d7af00,bold'
"$HOME/.local/bin/agent-tmux-notify" --clear-window --socket "$socket_path" --window "$window"
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
