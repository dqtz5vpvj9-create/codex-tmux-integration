"""Self-tests for the emulator, against behaviour xterm is specified to have.

A test tool that has not itself been tested proves nothing about what it
measures, and several of these rules (deferred wrap, wide glyphs never being
split, erase painting the current background) are exactly the ones a naive
emulator gets wrong -- and getting them wrong is what made an earlier version
of these tests report a torn frame as clean.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from vt import Terminal, char_width

fails = []


def check(name, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}{'' if cond else '  ' + str(detail)}")
    if not cond:
        fails.append(name)


print("坐标与文本")
t = Terminal(20, 5)
t.feed("\x1b[3;5Hxy")
check("CUP is 1-based", t.cell(3, 5).char == "x" and t.cell(3, 6).char == "y")
check("cursor advances", (t.y, t.x) == (2, 6))

print("宽字符")
t = Terminal(10, 3)
t.feed("\x1b[1;1H中a")
check("wide glyph takes two cells", t.cell(1, 1).char == "中" and t.cell(1, 2).trailing)
check("next glyph lands after it", t.cell(1, 3).char == "a")
check("row_text collapses the trailing cell", t.row_text(0).startswith("中a"))

print("自动换行（xterm 的延迟换行规则）")
t = Terminal(4, 3)
t.feed("\x1b[1;1Habcd")
check("filling the row does not wrap yet", t.y == 0 and t.row_text(0) == "abcd")
t.feed("e")
check("the next glyph wraps", t.y == 1 and t.row_text(1).startswith("e"))

print("宽字符不会被劈开")
t = Terminal(5, 3)
t.feed("\x1b[1;5H中")
check("a wide glyph at the last column wraps whole", t.cell(2, 1).char == "中")
check("and is counted as an overflow", t.overflow == 1)

print("擦除会填当前背景色（整屏蓝底就是这么来的）")
t = Terminal(4, 2)
t.feed("\x1b[44m\x1b[2J")
check("erase paints the pen's background", all(c.bg == 4 for row in t.buf.grid for c in row))
t.feed("\x1b[0m\x1b[2J")
check("and resets when the pen does", all(c.bg is None for row in t.buf.grid for c in row))

print("SGR 属性逐格记录")
t = Terminal(10, 2)
t.feed("\x1b[41;97mX\x1b[0mY")
check("bg recorded", t.cell(1, 1).bg == 1)
check("bright fg recorded", t.cell(1, 1).fg == 15)
check("reset applies to the next cell", t.cell(1, 2).bg is None)
t.feed("\x1b[7mZ")
check("inverse recorded", t.cell(1, 3).inverse)

print("备用屏幕")
t = Terminal(8, 2)
t.feed("main")
t.feed("\x1b[?1049h")
check("alt starts blank", t.render().strip() == "")
t.feed("alt")
t.feed("\x1b[?1049l")
check("main buffer survives", t.row_text(0).startswith("main"))

print("光标位置查询")
t = Terminal(20, 6)
t.feed("\x1b[4;7H\x1b[6n")
check("DSR reports where the cursor is", t.take_responses() == b"\x1b[4;7R")
t = Terminal(20, 6)
t.feed("\x1b[1;1H─\x1b[6n")
check("ambiguous=1 leaves the cursor at column 2", t.take_responses() == b"\x1b[1;2R")
t = Terminal(20, 6, ambiguous=2)
t.feed("\x1b[1;1H─\x1b[6n")
check("ambiguous=2 leaves it at column 3", t.take_responses() == b"\x1b[1;3R")

print("滚动")
t = Terminal(4, 2)
t.feed("a\r\nb\r\nc")
check("bottom line scrolls the screen up", t.row_text(0).startswith("b") and t.row_text(1).startswith("c"))

print("尺寸变化")
t = Terminal(10, 3)
t.feed("\x1b[1;1Hhello")
t.resize(6, 2)
check("top-left content survives a resize", t.row_text(0) == "hello ")
check("geometry updated", (t.cols, t.rows) == (6, 2))

print("分块喂入（转义序列被网络切开）")
t = Terminal(10, 2)
for piece in [b"\x1b", b"[2;", b"3H", "Z".encode()]:
    t.feed(piece)
check("a split escape sequence still lands", t.cell(2, 3).char == "Z")

print("宽度表")
check("CJK is wide", char_width("会") == 2)
check("ASCII is narrow", char_width("a") == 1)
check("box drawing is ambiguous", char_width("│", 1) == 1 and char_width("│", 2) == 2)
check("combining marks are zero", char_width("́") == 0)

print()
if fails:
    print("FAILURES:", fails); sys.exit(1)
print("模拟器自测全部通过")
