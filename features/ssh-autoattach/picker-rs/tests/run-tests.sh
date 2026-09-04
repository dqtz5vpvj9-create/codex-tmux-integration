#!/usr/bin/env bash
# Every layer: the units, the emulator that judges the frames, and the frames.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== cargo test =="
cargo test --quiet --manifest-path "$here/../Cargo.toml"

echo
echo "== emulator self-tests =="
python3 "$here/test_vt.py"

echo
echo "== rendered-screen tests =="
cargo build --release --quiet --manifest-path "$here/../Cargo.toml"
python3 "$here/test_picker.py"
