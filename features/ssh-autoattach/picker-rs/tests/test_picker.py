"""Judge the picker's rendering through the emulator.

Everything here is a screen assertion: where borders land, what colour a cell
is, what a tap at a pixel row hits. Byte-level checks let a torn frame pass.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from harness import run_picker, right_border_columns, locate, selected_rows

import atexit
import subprocess

TMUX = os.environ.get("PICKER_TEST_TMUX", "/mnt/cache/data-cache/picker-test-tmux")
SOCKET = "pickertest"
fails = []


def tmux(*args, check=False):
    env = dict(os.environ, TMUX_TMPDIR=TMUX)
    return subprocess.run(["tmux", "-L", SOCKET, *args], env=env,
                          capture_output=True, text=True, check=check)


def make_fixture():
    """A server of our own, so the suite never touches the real sessions."""
    os.makedirs(TMUX, exist_ok=True)
    tmux("kill-server")
    tmux("new-session", "-d", "-s", "agent", "zsh")
    tmux("new-session", "-d", "-s", "choreo_bench_long_name_20260903", "tail", "-f", "/dev/null")
    atexit.register(lambda: tmux("kill-server"))
    panes = tmux("list-panes", "-a", "-F", "#{session_name} #{pane_current_command}").stdout
    if "agent zsh" not in panes:
        print("fixture failed to start:", panes)
        sys.exit(2)


make_fixture()


def check(name, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}{'' if cond else '  ' + str(detail)}")
    if not cond:
        fails.append(name)


def go(**kw):
    kw.setdefault("tmux_tmpdir", TMUX)
    return run_picker(**kw)


def find_row(term, needle):
    for y in range(term.rows):
        if needle in term.row_text(y):
            return y + 1
    return None


print("几何：任何尺寸下框都要闭合，且不越界")
for amb in (1, 2):
    for cols, rows in [(20, 10), (24, 12), (30, 14), (42, 18), (60, 20), (80, 24), (120, 40)]:
        r = go(cols=cols, rows=rows, ambiguous=amb, timeout="9", wait=1.2)
        borders = sorted(set(right_border_columns(r.term)))
        check(
            f"ambiguous={amb} {cols}x{rows} 右边框对齐",
            len(borders) == 1 and borders[0] <= cols and r.term.overflow == 0,
            f"borders={borders} overflow={r.term.overflow}",
        )

print("尺寸变化：必须按新尺寸重画")
r = go(cols=80, rows=24, timeout="9", wait=2.0, resize_at=[(0.4, 42, 18)])
borders = sorted(set(right_border_columns(r.term)))
check("缩窄后重画", len(borders) == 1 and borders[0] <= 42, f"borders={borders}")
r = go(cols=40, rows=20, timeout="9", wait=2.2, resize_at=[(0.4, 100, 30)])
borders = sorted(set(right_border_columns(r.term)))
check("放宽后重画", len(borders) == 1 and 40 < borders[0] <= 100, f"borders={borders}")
r = go(cols=42, rows=22, timeout="9", wait=2.4, resize_at=[(0.4, 42, 11), (1.0, 42, 22)])
borders = sorted(set(right_border_columns(r.term)))
check("键盘弹出再收起", len(borders) == 1 and r.term.overflow == 0, f"borders={borders}")

print("配色：newt 的蓝底浮窗与红底当前项")
r = go(cols=60, rows=20, timeout="9", wait=1.2)
bg = r.term.bg_map()
check("整屏蓝底", bg[0][0] == "4", f"角落背景={bg[0][0]}")
sel = selected_rows(r.term)
check("当前项整行红底", len(sel) == 2, sel)
if sel:
    c = r.term.cell(sel[0], 10)
    check("当前项是白字", c.fg == 15, (c.bg, c.fg))
where = locate(r.term, "9.0s")
check("浮窗内是灰白底", where is not None and r.term.cell(where[0], where[1]).bg == 7, where)
check("窗口外是蓝底", r.term.cell(1, 1).bg == 4)
check("有投影", any(c.bg == 0 for row in r.term.buf.grid for c in row))

print("字符集：由终端的回答决定")
r = go(cols=50, rows=18, ambiguous=1, timeout="9", wait=1.2)
check("窄终端用 Unicode 框", "┌" in r.screen, r.screen.splitlines()[:2])
r = go(cols=50, rows=18, ambiguous=2, timeout="9", wait=1.2)
check("CJK 双宽终端降级 ASCII", "+" in r.screen and "┌" not in r.screen, r.screen.splitlines()[:2])
r = go(cols=50, rows=18, answer_dsr=False, timeout="9", wait=1.6)
check("终端不回答时取安全值", "┌" not in r.screen, r.screen.splitlines()[:2])

print("行为：倒计时、按键、点按")
r = go(cols=60, rows=20, timeout="0.5", wait=2.5)
check("无输入则自动进入", r.decision.startswith("ATTACH\t"), r.decision)
r = go(cols=60, rows=20, timeout="0.5", wait=2.5, keys=[(0.1, b"\x1b[B")])
check("按键取消倒计时", r.decision == "", r.decision)
r = go(cols=60, rows=20, timeout="9", wait=2.0, keys=[(0.3, b"1")])
check("数字直达", r.decision.startswith("ATTACH\t"), r.decision)

probe = go(cols=60, rows=20, timeout="9", wait=1.2)
card = selected_rows(probe.term)
r = go(cols=60, rows=20, timeout="9", wait=2.0,
       keys=[(0.4, f"\x1b[<0;10;{card[1]}M".encode()), (0.45, f"\x1b[<0;10;{card[1]}m".encode())])
check(f"轻触卡片第二行（第 {card[1]} 行）", r.decision.startswith("ATTACH\t"), r.decision)

btn = locate(probe.term, "Shell")
r = go(cols=60, rows=20, timeout="9", wait=2.0,
       keys=[(0.4, f"\x1b[<0;{btn[1]};{btn[0]}M".encode()), (0.45, f"\x1b[<0;{btn[1]};{btn[0]}m".encode())])
check(f"轻触 Shell 按钮（{btn[0]} 行 {btn[1]} 列）", r.decision == "SHELL", r.decision)

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("全部通过")
