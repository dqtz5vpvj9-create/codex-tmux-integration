"""A terminal emulator good enough to judge a TUI by.

The picker is a full-screen program whose bugs are geometric: a border in the
wrong column, a cell painted the wrong colour, content written past the right
margin, a frame left behind after a resize. None of that is visible in a byte
stream, so the tests keep a screen and inspect it.

Implements the subset xterm-compatible terminals agree on: CUP/CUU/CUD/CUF/CUB,
ED, EL, SGR, DECSET/DECRST for the alternate buffer, cursor visibility and
mouse reporting, DSR replies, autowrap with the deferred-wrap rule, and
scrolling. East Asian width is configurable, because that is the axis the
picker has to get right.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field, replace


def char_width(ch: str, ambiguous: int = 1) -> int:
    """Cells a character occupies. `ambiguous` is what the font does with the
    East Asian Ambiguous class -- 1 on a plain terminal, 2 with a CJK font."""
    if ch in ("\r", "\n"):
        return 0
    cp = ord(ch)
    if cp == 0 or cp < 32 or 0x7F <= cp < 0xA0:
        return 0
    if unicodedata.combining(ch):
        return 0
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("W", "F"):
        return 2
    if eaw == "A":
        return ambiguous
    return 1


@dataclass
class Cell:
    char: str = " "
    fg: int | None = None
    bg: int | None = None
    inverse: bool = False
    bold: bool = False
    dim: bool = False
    #: second half of a wide character; carries no glyph of its own
    trailing: bool = False


@dataclass
class Pen:
    fg: int | None = None
    bg: int | None = None
    inverse: bool = False
    bold: bool = False
    dim: bool = False

    def reset(self) -> None:
        self.fg = self.bg = None
        self.inverse = self.bold = self.dim = False


@dataclass
class Buffer:
    cols: int
    rows: int
    grid: list[list[Cell]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.grid:
            self.grid = [[Cell() for _ in range(self.cols)] for _ in range(self.rows)]

    def resize(self, cols: int, rows: int) -> None:
        """Match xterm: keep what fits at the top-left, pad the rest."""
        grid = []
        for y in range(rows):
            row = []
            for x in range(cols):
                row.append(self.grid[y][x] if y < self.rows and x < self.cols else Cell())
            grid.append(row)
        self.cols, self.rows, self.grid = cols, rows, grid


class Terminal:
    def __init__(self, cols: int = 80, rows: int = 24, ambiguous: int = 1) -> None:
        self.cols, self.rows = cols, rows
        self.ambiguous = ambiguous
        self.main = Buffer(cols, rows)
        self.alt = Buffer(cols, rows)
        self.buf = self.main
        self.in_alt = False
        self.x = self.y = 0
        self.pen = Pen()
        self.pending_wrap = False
        self.cursor_visible = True
        self.mouse_modes: set[int] = set()
        self.responses = bytearray()
        #: characters the program tried to place outside the screen
        self.overflow = 0
        self._pending = ""

    # -- geometry ---------------------------------------------------------
    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.main.resize(cols, rows)
        self.alt.resize(cols, rows)
        self.x = min(self.x, cols - 1)
        self.y = min(self.y, rows - 1)
        self.pending_wrap = False

    # -- input ------------------------------------------------------------
    def feed(self, data: str | bytes) -> None:
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        text = self._pending + data
        self._pending = ""
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "\x1b":
                consumed = self._escape(text, i)
                if consumed is None:      # incomplete, wait for more bytes
                    self._pending = text[i:]
                    return
                i += consumed
                continue
            if ch == "\r":
                self.x = 0
                self.pending_wrap = False
            elif ch == "\n":
                self._newline()
            elif ch == "\b":
                self.x = max(0, self.x - 1)
                self.pending_wrap = False
            elif ch == "\t":
                self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
            else:
                self._put(ch)
            i += 1

    # -- escape handling --------------------------------------------------
    def _escape(self, text: str, i: int) -> int | None:
        if i + 1 >= len(text):
            return None
        nxt = text[i + 1]
        if nxt != "[":
            if nxt in "()":            # charset designation, needs one more byte
                return None if i + 2 >= len(text) else 3
            return 2                    # ignore other two-byte escapes
        j = i + 2
        while j < len(text) and not ("@" <= text[j] <= "~"):
            j += 1
        if j >= len(text):
            return None
        body, final = text[i + 2 : j], text[j]
        self._csi(body, final)
        return j - i + 1

    def _csi(self, body: str, final: str) -> None:
        private = body.startswith("?")
        raw = body[1:] if private else body
        params = [int(p) if p.isdigit() else 0 for p in raw.split(";")] if raw else []

        def p(idx: int, default: int = 1) -> int:
            return params[idx] if idx < len(params) and params[idx] else default

        if private:
            self._decset(params, final)
        elif final == "H" or final == "f":
            self.y = min(self.rows - 1, max(0, p(0) - 1))
            self.x = min(self.cols - 1, max(0, p(1) - 1))
            self.pending_wrap = False
        elif final == "A":
            self.y = max(0, self.y - p(0))
        elif final == "B":
            self.y = min(self.rows - 1, self.y + p(0))
        elif final == "C":
            self.x = min(self.cols - 1, self.x + p(0))
        elif final == "D":
            self.x = max(0, self.x - p(0))
        elif final == "G":
            self.x = min(self.cols - 1, max(0, p(0) - 1))
        elif final == "J":
            self._erase_display(p(0, 0))
        elif final == "K":
            self._erase_line(p(0, 0))
        elif final == "m":
            self._sgr(params or [0])
        elif final == "n" and p(0, 0) == 6:
            self.responses += f"\x1b[{self.y + 1};{self.x + 1}R".encode()

    def _decset(self, params: list[int], final: str) -> None:
        on = final == "h"
        for mode in params:
            if mode == 1049:
                if on and not self.in_alt:
                    self.in_alt = True
                    self.buf = self.alt
                    self._erase_display(2)
                    self.x = self.y = 0
                elif not on and self.in_alt:
                    self.in_alt = False
                    self.buf = self.main
            elif mode == 25:
                self.cursor_visible = on
            elif mode in (1000, 1002, 1003, 1006, 1015):
                self.mouse_modes.discard(mode) if not on else self.mouse_modes.add(mode)

    def _sgr(self, params: list[int]) -> None:
        i = 0
        while i < len(params):
            v = params[i]
            if v == 0:
                self.pen.reset()
            elif v == 1:
                self.pen.bold = True
            elif v == 2:
                self.pen.dim = True
            elif v == 7:
                self.pen.inverse = True
            elif v == 22:
                self.pen.bold = self.pen.dim = False
            elif v == 27:
                self.pen.inverse = False
            elif 30 <= v <= 37:
                self.pen.fg = v - 30
            elif v == 39:
                self.pen.fg = None
            elif 40 <= v <= 47:
                self.pen.bg = v - 40
            elif v == 49:
                self.pen.bg = None
            elif 90 <= v <= 97:
                self.pen.fg = v - 90 + 8
            elif 100 <= v <= 107:
                self.pen.bg = v - 100 + 8
            elif v in (38, 48):        # extended colour, skip its arguments
                if i + 1 < len(params) and params[i + 1] == 5:
                    i += 2
                elif i + 1 < len(params) and params[i + 1] == 2:
                    i += 4
            i += 1

    # -- drawing ----------------------------------------------------------
    def _blank(self) -> Cell:
        """Erasing paints the current background, which is how a full-screen
        colour fill works."""
        return Cell(bg=self.pen.bg)

    def _erase_display(self, mode: int) -> None:
        if mode == 2 or mode == 3:
            for row in self.buf.grid:
                for x in range(self.cols):
                    row[x] = self._blank()
        elif mode == 0:
            self._erase_line(0)
            for y in range(self.y + 1, self.rows):
                for x in range(self.cols):
                    self.buf.grid[y][x] = self._blank()
        elif mode == 1:
            self._erase_line(1)
            for y in range(0, self.y):
                for x in range(self.cols):
                    self.buf.grid[y][x] = self._blank()

    def _erase_line(self, mode: int) -> None:
        row = self.buf.grid[self.y]
        rng = range(self.x, self.cols) if mode == 0 else (
            range(0, self.x + 1) if mode == 1 else range(0, self.cols))
        for x in rng:
            row[x] = self._blank()

    def _newline(self) -> None:
        self.pending_wrap = False
        if self.y + 1 < self.rows:
            self.y += 1
        else:
            self.buf.grid.pop(0)
            self.buf.grid.append([self._blank() for _ in range(self.cols)])

    def _put(self, ch: str) -> None:
        w = char_width(ch, self.ambiguous)
        if w == 0:
            return
        if self.pending_wrap:
            self.x = 0
            self._newline()
            self.pending_wrap = False
        if self.x + w > self.cols:
            # xterm never splits a wide glyph: it wraps to the next line.
            self.overflow += 1
            self.x = 0
            self._newline()
        cell = Cell(char=ch, fg=self.pen.fg, bg=self.pen.bg, inverse=self.pen.inverse,
                    bold=self.pen.bold, dim=self.pen.dim)
        self.buf.grid[self.y][self.x] = cell
        if w == 2:
            self.buf.grid[self.y][self.x + 1] = replace(cell, char="", trailing=True)
        self.x += w
        if self.x >= self.cols:
            self.x = self.cols - 1
            self.pending_wrap = True

    # -- inspection -------------------------------------------------------
    def row_text(self, y: int) -> str:
        out = []
        for cell in self.buf.grid[y]:
            if cell.trailing:
                continue
            out.append(cell.char if cell.char else " ")
        return "".join(out)

    def text(self) -> list[str]:
        return [self.row_text(y) for y in range(self.rows)]

    def render(self) -> str:
        return "\n".join(line.rstrip() for line in self.text())

    def cell(self, row1: int, col1: int) -> Cell:
        """1-based, the way escape sequences and hit tests talk about it."""
        return self.buf.grid[row1 - 1][col1 - 1]

    def bg_map(self) -> list[str]:
        """One character per cell: the background colour index, or '.'."""
        return [
            "".join("." if c.bg is None else format(c.bg, "x") for c in row)
            for row in self.buf.grid
        ]

    def take_responses(self) -> bytes:
        out = bytes(self.responses)
        self.responses.clear()
        return out
