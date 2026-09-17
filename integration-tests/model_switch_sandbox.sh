#!/bin/bash
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
sandbox_root=$(mktemp -d)
sandbox_home="$sandbox_root/home"
sandbox_codex="$sandbox_root/codex-home"
sandbox_tmux="$sandbox_root/tmux"
socket_label="codex-model-switch-$$"
source_codex="${CODEX_HOME:-$HOME/.codex}"

cleanup() {
  TMUX_TMPDIR="$sandbox_tmux" tmux -L "$socket_label" kill-server >/dev/null 2>&1 || true
  if [[ -d "$sandbox_root" && "$sandbox_root" == /tmp/* ]]; then
    rm -rf -- "$sandbox_root"
  fi
}
trap cleanup EXIT HUP INT TERM

mkdir -m 700 -p "$sandbox_home/.local/bin" "$sandbox_codex" "$sandbox_tmux"
if [[ -f "$source_codex/auth.json" ]]; then
  cp -p "$source_codex/auth.json" "$sandbox_codex/auth.json"
fi
if [[ -d "$source_codex/accounts" ]]; then
  cp -a "$source_codex/accounts" "$sandbox_codex/accounts"
fi
for source in "$source_codex/config.toml" "$source_codex/models_cache.json"; do
  [[ -f "$source" ]] && cp -p "$source" "$sandbox_codex/"
done
chmod -R go-rwx "$sandbox_codex"

HOME="$sandbox_home" CODEX_HOME="$sandbox_codex" TMUX_TMPDIR="$sandbox_tmux" \
CODEX_TMUX_SOCKETS="$sandbox_root/unregistered-socket" \
  "$repo_root/installer/install" \
  --home "$sandbox_home" \
  --repo-root "$repo_root" \
  --features model-switch

PATH="$sandbox_home/.local/bin:$PATH" \
HOME="$sandbox_home" CODEX_HOME="$sandbox_codex" TMUX_TMPDIR="$sandbox_tmux" \
  tmux -L "$socket_label" \
  -f "$sandbox_home/.config/codex-tmux-integration/tmux.conf" \
  new-session -d -s model-switch \
  "env HOME='$sandbox_home' CODEX_HOME='$sandbox_codex' PATH='$sandbox_home/.local/bin:$PATH' codex --no-alt-screen"

printf '%s\n' \
  'Isolated Codex model-switch test:' \
  '  prefix+1  gpt-5.6-sol max' \
  '  prefix+2  gpt-5.6-sol medium' \
  '  prefix+3  gpt-5.6-terra xhigh' \
  '  prefix+4  gpt-5.6-luna max' \
  '  prefix+g  popup matrix' \
  '' \
  'Detach with prefix+d; the temporary socket and Codex home are then removed.'

env -u TMUX TMUX_TMPDIR="$sandbox_tmux" tmux -L "$socket_label" attach -t model-switch
