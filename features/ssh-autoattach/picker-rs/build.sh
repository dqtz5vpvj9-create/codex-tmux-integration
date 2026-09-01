#!/usr/bin/env bash
# Build the SSH session picker and install it where tmux-ssh.zsh looks for it.
# The shell hook falls back to its whiptail menu whenever the binary is absent,
# so a failed build degrades the login rather than breaking it.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="${1:-$HOME/.local/bin/tmux-ssh-picker}"

cargo build --release --manifest-path "$here/Cargo.toml"
mkdir -p "$(dirname "$dest")"
install -m 0755 "$here/target/release/tmux-ssh-picker" "$dest"
echo "installed $dest"
