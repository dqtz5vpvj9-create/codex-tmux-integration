#!/bin/bash
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
socket_label="codex-tmux-test-$$"
socket_path="${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)/$socket_label"

cleanup() {
  tmux -L "$socket_label" kill-server 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

tmux -L "$socket_label" -f "$repo_root/features/title-sync/tmux/title-sync.conf" \
  new-session -d -s integration
tmux -S "$socket_path" source-file \
  "$repo_root/features/window-attention/tmux/clear-attention.conf"
tmux -S "$socket_path" source-file \
  "$repo_root/features/pane-logging/tmux/pane-logging.conf"

tmux -S "$socket_path" show-hooks -g pane-focus-in | grep -F '[50]' >/dev/null
tmux -S "$socket_path" show-hooks -g after-select-window | grep -F '[60]' >/dev/null
tmux -S "$socket_path" show-hooks -g after-new-window | grep -F '[70]' >/dev/null

before=$(tmux -S "$socket_path" list-panes -a -F '#{pane_id}' | wc -l)
tmux -S "$socket_path" split-window -d
after=$(tmux -S "$socket_path" list-panes -a -F '#{pane_id}' | wc -l)
test "$before" -eq 1
test "$after" -eq 2

new_pane=$(tmux -S "$socket_path" list-panes -a -F '#{pane_id}' | tail -n 1)
pipe_enabled=0
for _attempt in 1 2 3 4 5 6 7 8 9 10; do
  if [ "$(tmux -S "$socket_path" display-message -p -t "$new_pane" '#{pane_pipe}')" = 1 ]; then
    pipe_enabled=1
    break
  fi
  sleep 0.1
done
test "$pipe_enabled" -eq 1

printf 'isolated tmux integration passed: %s\n' "$socket_path"
