#!/usr/bin/env python3
"""Discover and register tmux sockets used by codex-tmux-integration."""

from __future__ import annotations

import fcntl
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

TMUX_TIMEOUT_SECONDS = 0.8


def state_root(home: Path | None = None) -> Path:
    actual_home = Path.home().expanduser().resolve()
    if home is None:
        home = actual_home
        honor_xdg = True
    else:
        home = home.expanduser().resolve()
        honor_xdg = home == actual_home
    configured = os.environ.get("XDG_STATE_HOME") if honor_xdg else None
    if configured:
        return Path(configured).expanduser() / "codex-tmux-integration"
    return home / ".local" / "state" / "codex-tmux-integration"


def registry_path(home: Path | None = None) -> Path:
    return state_root(home) / "tmux-sockets"


def socket_from_tmux_env(value: str | None = None) -> str:
    raw = os.environ.get("TMUX", "") if value is None else value
    return raw.rsplit(",", 2)[0].strip() if raw else ""


def server_pid(socket: str) -> int:
    if not socket:
        return -1
    try:
        result = subprocess.run(
            ["tmux", "-S", socket, "display-message", "-p", "#{pid}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=TMUX_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1
    if result.returncode != 0:
        return -1
    try:
        return int(result.stdout.strip())
    except ValueError:
        return -1


def _read_registry(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        socket, separator, pid_text = line.partition("\t")
        socket = socket.strip()
        if not socket:
            continue
        try:
            pid = int(pid_text) if separator else -1
        except ValueError:
            pid = -1
        result[socket] = pid
    return result


def register_socket(socket: str, pid: int | None = None, home: Path | None = None) -> None:
    socket = str(socket or "").strip()
    if not socket:
        return
    path = registry_path(home)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        entries = _read_registry(path)
        entries[socket] = server_pid(socket) if pid is None else pid
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                for registered_socket, registered_pid in sorted(entries.items()):
                    stream.write(f"{registered_socket}\t{registered_pid}\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def registered_sockets(home: Path | None = None) -> list[str]:
    return sorted(_read_registry(registry_path(home)))


def default_socket_candidates() -> Iterable[str]:
    base = Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}"
    try:
        entries = list(base.iterdir())
    except OSError:
        return []
    return [str(path) for path in entries if path.is_socket()]


def discover_sockets(home: Path | None = None) -> list[str]:
    sockets = set(default_socket_candidates())
    sockets.update(registered_sockets(home))
    current = socket_from_tmux_env()
    if current:
        sockets.add(current)
    configured = os.environ.get("CODEX_TMUX_SOCKETS", "")
    if configured:
        sockets.update(value for value in configured.split(os.pathsep) if value)
    return sorted(sockets)


def live_sockets(home: Path | None = None) -> list[str]:
    return [socket for socket in discover_sockets(home) if server_pid(socket) > 0]
