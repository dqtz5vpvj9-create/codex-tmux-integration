"""Build steps for the privileged capture helper.

The helper itself is the Rust crate in ``helper-rs``. Both of its inputs are
built here as the calling user; the installer then copies them into a
root-owned bundle, so root never runs anything from this source tree.
"""

from __future__ import annotations

import ctypes.util
import shutil
import subprocess
import sysconfig
import tempfile
from pathlib import Path

from .model import ListenerError

FEATURE_ROOT = Path(__file__).resolve().parents[1]
HELPER_CRATE = FEATURE_ROOT / "helper-rs"
HELPER_NAME = "codex-session-capture"


def build_bpf_object() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    clang_path = Path("/usr/bin/clang")
    clang = str(clang_path) if clang_path.is_file() else None
    source = FEATURE_ROOT / "bpf" / "codex_session_capture.bpf.c"
    if clang is None:
        raise ListenerError("clang is required to build the BPF capture program")
    if not source.is_file():
        raise ListenerError(f"BPF source is missing: {source}")
    if ctypes.util.find_library("bpf") is None:
        raise ListenerError("libbpf is required by the capture helper")
    temporary = tempfile.TemporaryDirectory(prefix="codex-session-listener-")
    output = Path(temporary.name) / "codex_session_capture.bpf.o"
    command = [clang, "-target", "bpf", "-O2", "-g"]
    multiarch = sysconfig.get_config_var("MULTIARCH")
    if isinstance(multiarch, str) and Path("/usr/include", multiarch).is_dir():
        command.append(f"-I/usr/include/{multiarch}")
    command.extend(["-c", str(source), "-o", str(output)])
    completed = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if completed.returncode != 0:
        temporary.cleanup()
        detail = completed.stderr.strip().splitlines()
        summary = detail[-1] if detail else "unknown compiler error"
        raise ListenerError(f"BPF compilation failed: {summary}")
    return temporary, output


def build_capture_helper() -> Path:
    """Compile the helper. The crate has no dependencies, so this never goes online."""

    cargo = shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo")
    if not Path(cargo).is_file():
        raise ListenerError("cargo is required to build the capture helper")
    completed = subprocess.run(
        [cargo, "build", "--release", "--locked", "--offline"],
        cwd=HELPER_CRATE,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise ListenerError(f"capture helper build failed: {completed.stderr.strip()}")
    return HELPER_CRATE / "target" / "release" / HELPER_NAME
