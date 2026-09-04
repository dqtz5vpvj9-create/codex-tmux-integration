"""Run the picker on a pty and watch it through the emulator."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import sys
import termios
import time

sys.path.insert(0, os.path.dirname(__file__))
from vt import Terminal

BIN = os.environ.get(
    "PICKER_BIN",
    os.path.join(os.path.dirname(__file__), "..", "target", "release", "tmux-ssh-picker"),
)


class Run:
    def __init__(self, term: Terminal, decision: str, exited: bool, raw: bytes):
        self.term = term
        self.decision = decision
        self.exited = exited
        self.raw = raw

    @property
    def screen(self) -> str:
        return self.term.render()


def run_picker(
    cols: int = 80,
    rows: int = 24,
    *,
    ambiguous: int = 1,
    answer_dsr: bool = True,
    keys: list[tuple[float, bytes]] | None = None,
    resize_at: list[tuple[float, int, int]] | None = None,
    timeout: str = "1.0",
    wait: float = 3.0,
    tmux_tmpdir: str | None = None,
    cache: str | None = "/mnt/cache/data-cache/harness-cache",
    env: dict[str, str] | None = None,
) -> Run:
    """Drive the picker. `keys` and `resize_at` are (seconds_from_start, ...)."""
    keys = list(keys or [])
    resize_at = list(resize_at or [])
    if cache:
        shutil.rmtree(cache, ignore_errors=True)
    out_path = "/mnt/cache/data-cache/harness-decision.txt"
    if os.path.exists(out_path):
        os.unlink(out_path)

    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.environ["TMUX_SSH_MENU_TIMEOUT"] = timeout
        if tmux_tmpdir:
            os.environ["TMUX_TMPDIR"] = tmux_tmpdir
        if cache:
            os.environ["XDG_CACHE_HOME"] = cache
        for k, v in (env or {}).items():
            os.environ[k] = v
        f = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.dup2(f, 1)
        os.execv(BIN, [BIN])
        os._exit(127)

    def set_size(c: int, r: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", r, c, 0, 0))
        os.kill(pid, signal.SIGWINCH)

    set_size(cols, rows)
    term = Terminal(cols, rows, ambiguous=ambiguous)
    raw = bytearray()
    start = time.time()
    answered = False
    exited = False
    while time.time() - start < wait:
        now = time.time() - start
        while keys and now >= keys[0][0]:
            os.write(fd, keys.pop(0)[1])
        while resize_at and now >= resize_at[0][0]:
            _, c, r = resize_at.pop(0)
            term.resize(c, r)
            set_size(c, r)
        ready, _, _ = select.select([fd], [], [], 0.02)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            raw += chunk
            term.feed(chunk)
            reply = term.take_responses()
            if reply and answer_dsr and not answered:
                answered = True
                os.write(fd, reply)
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done:
            exited = True
            break
    if not exited:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    os.close(fd)
    decision = ""
    if os.path.exists(out_path):
        decision = open(out_path).read().strip()
    return Run(term, decision, exited, bytes(raw))


def dialog_rows(term: Terminal) -> list[int]:
    """1-based rows that carry a dialog border."""
    rows = []
    for y in range(term.rows):
        line = term.row_text(y)
        if any(ch in line for ch in "│┌┐└┘├┤|+"):
            rows.append(y + 1)
    return rows


def right_border_columns(term: Terminal) -> list[int]:
    """1-based column of the rightmost border character on each dialog row."""
    cols = []
    for y in range(term.rows):
        hits = [x + 1 for x, c in enumerate(term.buf.grid[y]) if c.char in "│┐┘┤|+"]
        if hits:
            cols.append(max(hits))
    return cols


def locate(term: Terminal, needle: str) -> tuple[int, int] | None:
    """1-based (row, col) of `needle` on screen.

    Not a string index: a row holding wide glyphs has more columns than
    characters, and a tap is addressed by column. Walking the cells is the
    only way to get the two to agree.
    """
    for y in range(term.rows):
        cols, chars = [], []
        for x, cell in enumerate(term.buf.grid[y]):
            if cell.trailing:
                continue
            cols.append(x + 1)
            chars.append(cell.char if cell.char else " ")
        line = "".join(chars)
        i = line.find(needle)
        if i >= 0:
            return y + 1, cols[i]
    return None


def selected_rows(term: Terminal) -> list[int]:
    """1-based rows carrying the current-entry highlight."""
    return [
        y + 1
        for y in range(term.rows)
        if sum(1 for c in term.buf.grid[y] if c.bg == 1) > 3
    ]
