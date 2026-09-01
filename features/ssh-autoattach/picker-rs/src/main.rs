//! Session picker for SSH logins.
//!
//! Prints one decision line on stdout and does all of its drawing on /dev/tty,
//! so the calling shell can capture the decision with `$(...)`:
//!
//!   ATTACH\t<socket>\t<session>   attach to this session
//!   NEW                            caller should prompt for a new session
//!   SHELL                          caller should drop to a plain shell
//!
//! Exit 0 means the line above is valid.  Exit 2 means "not my job" (no tty, a
//! dumb terminal, no tmux, no sessions) and the caller should fall back to its
//! own menu.  Any other exit is a bug and the caller should fall back too.

use std::fs::File;
use std::io::{Read, Write};
use std::os::unix::fs::FileTypeExt;
use std::os::unix::io::AsRawFd;
use std::process::Command;
use std::time::{Duration, Instant};

// ---------------------------------------------------------------------------
// libc, declared rather than depended upon
// ---------------------------------------------------------------------------

#[repr(C)]
#[derive(Clone, Copy)]
struct Termios {
    c_iflag: u32,
    c_oflag: u32,
    c_cflag: u32,
    c_lflag: u32,
    c_line: u8,
    c_cc: [u8; 32],
    c_ispeed: u32,
    c_ospeed: u32,
}

#[repr(C)]
struct WinSize {
    row: u16,
    col: u16,
    _xpixel: u16,
    _ypixel: u16,
}

#[repr(C)]
struct PollFd {
    fd: i32,
    events: i16,
    revents: i16,
}

extern "C" {
    fn tcgetattr(fd: i32, termios_p: *mut Termios) -> i32;
    fn tcsetattr(fd: i32, optional_actions: i32, termios_p: *const Termios) -> i32;
    fn ioctl(fd: i32, request: u64, argp: *mut WinSize) -> i32;
    fn poll(fds: *mut PollFd, nfds: u64, timeout: i32) -> i32;
    fn getuid() -> u32;
}

const ICANON: u32 = 0x0002;
const ECHO: u32 = 0x0008;
const ISIG: u32 = 0x0001;
const IEXTEN: u32 = 0x8000;
const IXON: u32 = 0x0400;
const ICRNL: u32 = 0x0100;
const TCSANOW: i32 = 0;
const VTIME: usize = 5;
const VMIN: usize = 6;
const TIOCGWINSZ: u64 = 0x5413;
const POLLIN: i16 = 0x001;

/// Puts the terminal in raw mode and restores it on drop, including on panic.
struct RawMode {
    fd: i32,
    saved: Termios,
}

impl RawMode {
    fn enter(fd: i32) -> Option<RawMode> {
        unsafe {
            let mut t: Termios = std::mem::zeroed();
            if tcgetattr(fd, &mut t) != 0 {
                return None;
            }
            let saved = t;
            t.c_lflag &= !(ICANON | ECHO | ISIG | IEXTEN);
            t.c_iflag &= !(IXON | ICRNL);
            t.c_cc[VMIN] = 1;
            t.c_cc[VTIME] = 0;
            if tcsetattr(fd, TCSANOW, &t) != 0 {
                return None;
            }
            Some(RawMode { fd, saved })
        }
    }
}

impl Drop for RawMode {
    fn drop(&mut self) {
        unsafe {
            tcsetattr(self.fd, TCSANOW, &self.saved);
        }
    }
}

fn term_size(fd: i32) -> (usize, usize) {
    unsafe {
        let mut ws = WinSize { row: 0, col: 0, _xpixel: 0, _ypixel: 0 };
        if ioctl(fd, TIOCGWINSZ, &mut ws) == 0 && ws.col > 0 && ws.row > 0 {
            return (ws.col as usize, ws.row as usize);
        }
    }
    (80, 24)
}

/// Wait for readable input. `timeout_ms < 0` blocks. Returns true if readable.
fn wait_readable(fd: i32, timeout_ms: i32) -> bool {
    unsafe {
        let mut p = PollFd { fd, events: POLLIN, revents: 0 };
        poll(&mut p, 1, timeout_ms) > 0 && (p.revents & POLLIN) != 0
    }
}

// ---------------------------------------------------------------------------
// Display width
// ---------------------------------------------------------------------------

/// Columns a character occupies. Good enough for session names: CJK and the
/// common emoji blocks are wide, combining marks and controls are zero.
fn ch_width(c: char) -> usize {
    let u = c as u32;
    if u == 0 {
        return 0;
    }
    if u < 0x20 || (0x7f..0xa0).contains(&u) {
        return 0;
    }
    const ZERO: &[(u32, u32)] = &[
        (0x0300, 0x036f),
        (0x200b, 0x200f),
        (0xfe00, 0xfe0f),
        (0xfe20, 0xfe2f),
    ];
    for (lo, hi) in ZERO {
        if u >= *lo && u <= *hi {
            return 0;
        }
    }
    const WIDE: &[(u32, u32)] = &[
        (0x1100, 0x115f),
        (0x2e80, 0x303e),
        (0x3041, 0x33ff),
        (0x3400, 0x4dbf),
        (0x4e00, 0x9fff),
        (0xa000, 0xa4cf),
        (0xa960, 0xa97f),
        (0xac00, 0xd7a3),
        (0xf900, 0xfaff),
        (0xfe10, 0xfe19),
        (0xfe30, 0xfe6f),
        (0xff00, 0xff60),
        (0xffe0, 0xffe6),
        (0x1f300, 0x1f64f),
        (0x1f900, 0x1f9ff),
        (0x20000, 0x3fffd),
    ];
    for (lo, hi) in WIDE {
        if u >= *lo && u <= *hi {
            return 2;
        }
    }
    1
}

fn str_width(s: &str) -> usize {
    s.chars().map(ch_width).sum()
}

/// Truncate to `max` columns, marking the cut with an ellipsis.
fn truncate(s: &str, max: usize) -> String {
    if max == 0 {
        return String::new();
    }
    if str_width(s) <= max {
        return s.to_string();
    }
    let mut out = String::new();
    let mut w = 0;
    for c in s.chars() {
        let cw = ch_width(c);
        if w + cw > max.saturating_sub(1) {
            break;
        }
        out.push(c);
        w += cw;
    }
    out.push('…');
    out
}

fn pad(s: &str, width: usize) -> String {
    let w = str_width(s);
    if w >= width {
        s.to_string()
    } else {
        format!("{}{}", s, " ".repeat(width - w))
    }
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
struct Session {
    socket: String,
    name: String,
    windows: u32,
    attached: u32,
    activity: i64,
    /// The `@ssh_menu` user option, when the session sets one.
    tag: String,
    /// Whether any pane is running something a person interacts with.
    interactive: bool,
    /// Whether any pane is sitting at a bare shell prompt.
    has_shell: bool,
}

/// Commands that mean "a person works here", as opposed to a service or a
/// benchmark observer loop.  Classifying by what runs beats classifying by
/// session name: this fleet has agent sessions named after experiments.
const INTERACTIVE: &[&str] = &[
    "claude", "codex", "aider", "opencode", "gemini", "nvim", "vim", "vi", "emacs", "nano", "less",
    "man", "htop", "top", "btop", "ipython", "node", "git", "tig", "lazygit", "ranger", "yazi",
    "fzf", "gdb", "pdb", "tmux", "ssh",
];

/// A bare login shell counts as a place someone works.  Detaching leaves the
/// session unattached, and re-attaching to it is the whole point of this menu,
/// so attachment state must not gate visibility.  A service that leaves a
/// shell in its pane is indistinguishable here; pin it out with
/// `tmux set -t <session> @ssh_menu hide`.
const SHELLS: &[&str] = &["zsh", "bash", "sh", "fish", "dash", "ksh"];

fn is_interactive_cmd(cmd: &str) -> bool {
    INTERACTIVE.contains(&cmd)
}

fn is_shell_cmd(cmd: &str) -> bool {
    SHELLS.contains(&cmd)
}

const PANE_FORMAT: &str = "#{session_name}\t#{session_windows}\t#{session_attached}\t#{session_activity}\t#{@ssh_menu}\t#{pane_current_command}";

/// Fold `tmux list-panes -a` output into one entry per session.
fn parse_panes(socket: &str, out: &str) -> Vec<Session> {
    let mut sessions: Vec<Session> = Vec::new();
    for line in out.lines() {
        if line.is_empty() {
            continue;
        }
        let f: Vec<&str> = line.split('\t').collect();
        if f.len() < 6 {
            continue;
        }
        let name = f[0];
        if name.is_empty() {
            continue;
        }
        let cmd = f[5];
        match sessions.iter_mut().find(|s| s.name == name) {
            Some(s) => {
                s.interactive |= is_interactive_cmd(cmd);
                s.has_shell |= is_shell_cmd(cmd);
            }
            None => sessions.push(Session {
                socket: socket.to_string(),
                name: name.to_string(),
                windows: f[1].parse().unwrap_or(0),
                attached: f[2].parse().unwrap_or(0),
                activity: f[3].parse().unwrap_or(0),
                tag: f[4].to_string(),
                interactive: is_interactive_cmd(cmd),
                has_shell: is_shell_cmd(cmd),
            }),
        }
    }
    sessions
}

fn socket_dir() -> std::path::PathBuf {
    let base = std::env::var("TMUX_TMPDIR").unwrap_or_else(|_| "/tmp".to_string());
    std::path::PathBuf::from(base).join(format!("tmux-{}", unsafe { getuid() }))
}

fn collect() -> Vec<Session> {
    let mut all = Vec::new();
    let dir = match std::fs::read_dir(socket_dir()) {
        Ok(d) => d,
        Err(_) => return all,
    };
    let mut sockets: Vec<std::path::PathBuf> = dir
        .flatten()
        .filter(|e| {
            e.metadata()
                .map(|m| m.file_type().is_socket())
                .unwrap_or(false)
        })
        .map(|e| e.path())
        .collect();
    sockets.sort();
    for sock in sockets {
        let s = sock.to_string_lossy().to_string();
        // /tmp/tmux-<uid> accumulates sockets whose server is long gone.  A
        // connect() to a dead one fails at once, where `tmux -S` would pay a
        // process spawn per corpse: 30 stale sockets cost ~180 ms.
        if std::os::unix::net::UnixStream::connect(&sock).is_err() {
            continue;
        }
        if let Ok(out) = Command::new("tmux")
            .args(["-S", &s, "list-panes", "-a", "-F", PANE_FORMAT])
            .output()
        {
            if out.status.success() {
                all.extend(parse_panes(&s, &String::from_utf8_lossy(&out.stdout)));
            }
        }
    }
    all
}

/// `@ssh_menu` pins a session into or out of the default view; otherwise a
/// session shows when someone is working in it.
fn visible(s: &Session, show_all: bool) -> bool {
    match s.tag.as_str() {
        "hide" | "off" | "0" | "no" => false,
        "show" | "on" | "1" | "yes" => true,
        _ => show_all || s.interactive || s.has_shell,
    }
}

/// Most recently active first, so the countdown target is the session the
/// person was last in.
fn view(sessions: &[Session], show_all: bool) -> Vec<usize> {
    let mut idx: Vec<usize> = (0..sessions.len())
        .filter(|i| visible(&sessions[*i], show_all))
        .collect();
    idx.sort_by(|a, b| {
        sessions[*b]
            .activity
            .cmp(&sessions[*a].activity)
            .then_with(|| sessions[*a].name.cmp(&sessions[*b].name))
    });
    idx
}

fn ago(activity: i64, now: i64) -> String {
    let d = (now - activity).max(0);
    if d < 60 {
        format!("{d}s")
    } else if d < 3600 {
        format!("{}m", d / 60)
    } else if d < 86400 {
        format!("{}h", d / 3600)
    } else {
        format!("{}d", d / 86400)
    }
}

// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------

#[derive(Debug, PartialEq, Clone, Copy)]
enum Key {
    Up,
    Down,
    Left,
    Right,
    Enter,
    Digit(usize),
    Char(char),
    /// A press at a 1-based (row, column).
    Tap(usize, usize),
    ScrollUp,
    ScrollDown,
    Quit,
    Ignore,
}

/// Decode one key from the front of `buf`.
/// Returns the key and how many bytes it consumed, or None if `buf` holds only
/// part of an escape sequence.
fn decode(buf: &[u8]) -> Option<(Key, usize)> {
    if buf.is_empty() {
        return None;
    }
    match buf[0] {
        b'\r' | b'\n' => Some((Key::Enter, 1)),
        3 | 4 => Some((Key::Quit, 1)), // Ctrl-C, Ctrl-D
        0x1b => {
            if buf.len() < 2 {
                return None;
            }
            if buf[1] != b'[' {
                return Some((Key::Ignore, 2));
            }
            if buf.len() < 3 {
                return None;
            }
            if buf[2] == b'<' {
                // SGR mouse: ESC [ < btn ; col ; row (M|m)
                let end = buf.iter().position(|c| *c == b'M' || *c == b'm')?;
                let body = std::str::from_utf8(&buf[3..end]).unwrap_or("");
                let n: Vec<i64> = body.split(';').map(|p| p.parse().unwrap_or(-1)).collect();
                let consumed = end + 1;
                if n.len() != 3 {
                    return Some((Key::Ignore, consumed));
                }
                let (btn, col, row) = (n[0], n[1], n[2]);
                let press = buf[end] == b'M';
                let key = match btn {
                    64 => Key::ScrollUp,
                    65 => Key::ScrollDown,
                    0 if press && row > 0 && col > 0 => Key::Tap(row as usize, col as usize),
                    _ => Key::Ignore,
                };
                return Some((key, consumed));
            }
            match buf[2] {
                b'A' => Some((Key::Up, 3)),
                b'B' => Some((Key::Down, 3)),
                b'C' => Some((Key::Right, 3)),
                b'D' => Some((Key::Left, 3)),
                _ => Some((Key::Ignore, 3)),
            }
        }
        b'0'..=b'9' => Some((Key::Digit((buf[0] - b'0') as usize), 1)),
        c => {
            // Decode one UTF-8 char so a multibyte keypress is consumed whole.
            let len = if c < 0x80 {
                1
            } else if c >> 5 == 0b110 {
                2
            } else if c >> 4 == 0b1110 {
                3
            } else {
                4
            };
            if buf.len() < len {
                return None;
            }
            let ch = std::str::from_utf8(&buf[..len]).ok()?.chars().next()?;
            Some((Key::Char(ch), len))
        }
    }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Debug)]
enum Action {
    Pick(usize),
    ToggleAll,
    New,
    Shell,
}

/// A rectangle on screen that a tap can land in.  Cards claim their whole
/// width and both of their rows, so a thumb does not have to be precise.
#[derive(Clone, Copy, PartialEq, Debug)]
struct Hit {
    row: usize,
    col_start: usize,
    col_end: usize,
    action: Action,
}

struct Frame {
    text: String,
    hits: Vec<Hit>,
    per_page: usize,
}

fn hit_test(hits: &[Hit], row: usize, col: usize) -> Option<Action> {
    hits.iter()
        .find(|h| h.row == row && col >= h.col_start && col <= h.col_end)
        .map(|h| h.action)
}

const DIM: &str = "\x1b[2m";
const INV: &str = "\x1b[7m";
const OFF: &str = "\x1b[0m";

/// One framed row: `│ content… │`, padded to the inner width.
fn row(inner: &str, inner_w: usize, boxed: bool, invert: bool, dim: bool) -> String {
    let body = pad(&truncate(inner, inner_w), inner_w);
    let painted = if invert {
        format!("{INV}{body}{OFF}")
    } else if dim {
        format!("{DIM}{body}{OFF}")
    } else {
        body
    };
    if boxed {
        format!("│{painted}│\r\n")
    } else {
        format!("{painted}\r\n")
    }
}

fn rule(left: &str, right: &str, title: &str, inner_w: usize, boxed: bool) -> String {
    if !boxed {
        let t = truncate(title, inner_w);
        return format!("{DIM}{}{OFF}\r\n", pad(&t, inner_w));
    }
    let t = truncate(title, inner_w.saturating_sub(3));
    let used = str_width(&t) + 1;
    let dashes = inner_w.saturating_sub(used);
    format!(
        "{DIM}{left}─{t}{}{right}{OFF}\r\n",
        "─".repeat(dashes)
    )
}

#[allow(clippy::too_many_arguments)]
fn render(
    sessions: &[Session],
    idx: &[usize],
    sel: usize,
    scroll: usize,
    show_all: bool,
    hidden: usize,
    countdown: Option<f32>,
    timeout: f32,
    cols: usize,
    rows: usize,
    now: i64,
) -> Frame {
    let boxed = cols >= 26;
    let inner_w = if boxed { cols - 2 } else { cols };
    let col0 = if boxed { 2 } else { 1 }; // 1-based column of the inner area
    // top rule + separator + action bar + status + bottom rule
    let chrome = if boxed { 5 } else { 4 };
    let avail = rows.saturating_sub(chrome).max(1);
    let rows_per_card = if avail >= 4 && inner_w >= 20 { 2 } else { 1 };
    let per_page = (avail / rows_per_card).max(1);

    let mut out = String::from("\x1b[2J\x1b[H");
    let mut hits: Vec<Hit> = Vec::new();
    let mut screen_row = 1usize;

    let title = if show_all {
        format!(" tmux · 全部 {} 个会话 ", idx.len())
    } else {
        format!(" tmux · {} 个 agent 会话 ", idx.len())
    };
    out.push_str(&rule("╭", "╮", &title, inner_w, boxed));
    screen_row += 1;

    let shown = per_page.min(idx.len().saturating_sub(scroll));
    // Hug the content: a two-session menu should not draw ten blank rows.
    let list_rows = (shown * rows_per_card).max(rows_per_card);
    for k in 0..shown {
        let i = scroll + k;
        let s = &sessions[idx[i]];
        let selected = sel == i;
        let badge = if i < 9 {
            format!("{}", i + 1)
        } else {
            "·".to_string()
        };
        let head = format!(" {} {}  {}", if selected { "▸" } else { " " }, badge, s.name);
        out.push_str(&row(&head, inner_w, boxed, selected, false));
        hits.push(Hit { row: screen_row, col_start: 1, col_end: cols, action: Action::Pick(i) });
        screen_row += 1;

        if rows_per_card == 2 {
            let meta = format!(
                "      {} 窗口{} · {}前",
                s.windows,
                if s.attached > 0 { " · ● 已连接" } else { "" },
                ago(s.activity, now)
            );
            out.push_str(&row(&meta, inner_w, boxed, selected, !selected));
            hits.push(Hit { row: screen_row, col_start: 1, col_end: cols, action: Action::Pick(i) });
            screen_row += 1;
        }
    }
    for _ in shown * rows_per_card..list_rows {
        out.push_str(&row("", inner_w, boxed, false, false));
        screen_row += 1;
    }

    out.push_str(&rule("├", "┤", "", inner_w, boxed));
    screen_row += 1;

    // Action bar: three side-by-side buttons, each a wide tap target.
    // Three buttons must always be reachable, so the labels shrink rather
    // than the last button falling off the end of a phone screen.
    let label_sets: [[String; 3]; 4] = [
        [
            if show_all {
                " a 只看 agent ".into()
            } else if hidden > 0 {
                format!(" a 全部 +{hidden} ")
            } else {
                " a 全部 ".into()
            },
            " n 新建 ".into(),
            " s Shell ".into(),
        ],
        [
            if show_all { " a agent ".into() } else { " a 全部 ".into() },
            " n 新建 ".into(),
            " s 退出 ".into(),
        ],
        [
            if show_all { "a·少".into() } else { "a·全".into() },
            "n·新".into(),
            "s·退".into(),
        ],
        ["a".into(), "n".into(), "s".into()],
    ];
    let labels = label_sets
        .iter()
        .find(|set| {
            // Must match the draw loop below exactly: each button costs its
            // label, two brackets and one trailing space.
            set.iter().map(|l| str_width(l) + 3).sum::<usize>() <= inner_w
        })
        .unwrap_or(&label_sets[3]);
    let buttons = [
        (labels[0].clone(), Action::ToggleAll, idx.len()),
        (labels[1].clone(), Action::New, idx.len() + 1),
        (labels[2].clone(), Action::Shell, idx.len() + 2),
    ];
    let mut bar = String::new();
    let mut bar_plain_w = 0usize;
    for (label, action, sel_index) in buttons.iter() {
        let w = str_width(label) + 2; // brackets
        if bar_plain_w + w + 1 > inner_w {
            break;
        }
        let start_col = col0 + bar_plain_w + 1;
        let piece = format!("[{label}]");
        if sel == *sel_index {
            bar.push_str(&format!("{INV}{piece}{OFF}"));
        } else {
            bar.push_str(&format!("{DIM}{piece}{OFF}"));
        }
        bar.push(' ');
        hits.push(Hit {
            row: screen_row,
            col_start: start_col,
            col_end: start_col + w - 1,
            action: *action,
        });
        bar_plain_w += w + 1;
    }
    let bar_body = pad(&bar, 0);
    if boxed {
        out.push_str(&format!(
            "│{}{}│\r\n",
            bar_body,
            " ".repeat(inner_w.saturating_sub(bar_plain_w))
        ));
    } else {
        out.push_str(&format!("{bar_body}\r\n"));
    }
    screen_row += 1;

    // Status: a countdown bar, or the key hints.
    let status = match countdown {
        Some(remain) => {
            let name = &sessions[idx[0]].name;
            let text = format!(" {remain:.1}s → {name}");
            let text = truncate(&text, inner_w.saturating_sub(12));
            let bar_w = inner_w.saturating_sub(str_width(&text) + 2);
            let filled = if timeout > 0.0 {
                ((remain / timeout) * bar_w as f32).round().clamp(0.0, bar_w as f32) as usize
            } else {
                0
            };
            format!(
                "{text} {}{}",
                "▓".repeat(filled),
                "░".repeat(bar_w.saturating_sub(filled))
            )
        }
        None => {
            if inner_w >= 40 {
                " ⏎ 进入 · ↑↓ 选择 · 1-9 直达 · 轻触可选".to_string()
            } else {
                " ⏎ 进入 · ↑↓ · 1-9 · 轻触".to_string()
            }
        }
    };
    out.push_str(&row(&status, inner_w, boxed, false, true));
    screen_row += 1;

    if boxed {
        let more = if idx.len() > per_page {
            format!(" {}/{} ", scroll / per_page + 1, (idx.len() + per_page - 1) / per_page)
        } else {
            String::new()
        };
        out.push_str(&rule("╰", "╯", &more, inner_w, boxed));
    }
    let _ = screen_row;

    Frame { text: out, hits, per_page }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

fn timeout_secs() -> f32 {
    std::env::var("TMUX_SSH_MENU_TIMEOUT")
        .ok()
        .and_then(|v| v.parse::<f32>().ok())
        .filter(|v| *v >= 0.0)
        .unwrap_or(1.0)
}

fn now_unix() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn main() {
    if std::env::var("TERM").map(|t| t == "dumb").unwrap_or(false) {
        std::process::exit(2);
    }
    let sessions = collect();
    if sessions.is_empty() {
        std::process::exit(2); // caller creates its default session
    }

    let mut show_all = std::env::var("TMUX_SSH_MENU_ALL").map(|v| v == "1").unwrap_or(false);
    let mut idx = view(&sessions, show_all);
    if idx.is_empty() {
        // Nothing looks like an agent session; an empty menu would strand the
        // person, so list everything instead.
        show_all = true;
        idx = view(&sessions, show_all);
    }
    if idx.is_empty() {
        std::process::exit(2);
    }

    let tty = match File::options().read(true).write(true).open("/dev/tty") {
        Ok(f) => f,
        Err(_) => std::process::exit(2),
    };
    let fd = tty.as_raw_fd();
    let mut w = &tty;
    let mut r = &tty;

    let _raw = match RawMode::enter(fd) {
        Some(g) => g,
        None => std::process::exit(2),
    };
    let _ = w.write_all(b"\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h");
    let _ = w.flush();

    fn leave(w: &mut &File) {
        let _ = w.write_all(b"\x1b[?1006l\x1b[?1000l\x1b[?25h\x1b[?1049l");
        let _ = w.flush();
    }

    let timeout = timeout_secs();
    let deadline = Instant::now() + Duration::from_secs_f32(timeout);
    let mut counting = timeout > 0.0;
    let mut sel = 0usize;
    let mut scroll = 0usize;
    let mut buf: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 256];

    macro_rules! finish {
        ($($arg:tt)*) => {{
            leave(&mut w);
            println!($($arg)*);
            std::process::exit(0);
        }};
    }

    loop {
        let (cols, rows) = term_size(fd);
        let hidden = sessions.len() - idx.len();
        let remain = deadline.saturating_duration_since(Instant::now()).as_secs_f32();
        let countdown = if counting { Some(remain) } else { None };

        // Probe the layout so the selection can be kept on screen.
        let probe = render(
            &sessions, &idx, sel, scroll, show_all, hidden, countdown, timeout, cols, rows,
            now_unix(),
        );
        if sel < idx.len() {
            if sel < scroll {
                scroll = sel;
            } else if sel >= scroll + probe.per_page {
                scroll = sel + 1 - probe.per_page;
            }
        }
        let frame = render(
            &sessions, &idx, sel, scroll, show_all, hidden, countdown, timeout, cols, rows,
            now_unix(),
        );
        let _ = w.write_all(frame.text.as_bytes());
        let _ = w.flush();

        let wait_ms = if counting {
            (remain * 1000.0).max(0.0) as i32
        } else {
            -1
        };
        if !wait_readable(fd, wait_ms) {
            if counting {
                let s = &sessions[idx[0]];
                finish!("ATTACH\t{}\t{}", s.socket, s.name);
            }
            continue;
        }
        let n = match r.read(&mut chunk) {
            Ok(0) | Err(_) => {
                leave(&mut w);
                std::process::exit(2);
            }
            Ok(n) => n,
        };
        buf.extend_from_slice(&chunk[..n]);
        counting = false; // any input cancels the countdown

        while let Some((key, used)) = decode(&buf) {
            buf.drain(..used);
            let n_sessions = idx.len();
            let last = n_sessions + 2;
            let mut act: Option<Action> = None;
            match key {
                Key::Up | Key::Char('k') => sel = if sel == 0 { last } else { sel - 1 },
                Key::Down | Key::Char('j') => sel = if sel >= last { 0 } else { sel + 1 },
                Key::Left => {
                    if sel > n_sessions {
                        sel -= 1;
                    }
                }
                Key::Right => {
                    if sel >= n_sessions && sel < last {
                        sel += 1;
                    }
                }
                Key::ScrollUp => scroll = scroll.saturating_sub(1),
                Key::ScrollDown => {
                    if scroll + frame.per_page < n_sessions {
                        scroll += 1;
                    }
                }
                Key::Digit(d) => {
                    if d >= 1 && d <= n_sessions.min(9) {
                        act = Some(Action::Pick(d - 1));
                    }
                }
                Key::Tap(row, col) => act = hit_test(&frame.hits, row, col),
                Key::Enter => {
                    act = Some(if sel < n_sessions {
                        Action::Pick(sel)
                    } else if sel == n_sessions {
                        Action::ToggleAll
                    } else if sel == n_sessions + 1 {
                        Action::New
                    } else {
                        Action::Shell
                    })
                }
                Key::Char('a') | Key::Char('A') => act = Some(Action::ToggleAll),
                Key::Char('n') | Key::Char('N') => act = Some(Action::New),
                Key::Char('s') | Key::Char('S') | Key::Char('q') | Key::Char('Q') | Key::Quit => {
                    act = Some(Action::Shell)
                }
                Key::Char('g') => sel = 0,
                Key::Char('G') => sel = last,
                Key::Char(_) | Key::Ignore => {}
            }
            match act {
                Some(Action::Pick(i)) => {
                    let s = &sessions[idx[i]];
                    finish!("ATTACH\t{}\t{}", s.socket, s.name);
                }
                Some(Action::New) => finish!("NEW"),
                Some(Action::Shell) => finish!("SHELL"),
                Some(Action::ToggleAll) => {
                    let next = !show_all;
                    let candidate = view(&sessions, next);
                    // Filtering down to nothing would strand the person on an
                    // empty menu, so the toggle simply does not apply.
                    if !candidate.is_empty() {
                        show_all = next;
                        idx = candidate;
                        sel = 0;
                        scroll = 0;
                    }
                }
                None => {}
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(name: &str, activity: i64, tag: &str, interactive: bool) -> Session {
        Session {
            socket: "/tmp/x".into(),
            name: name.into(),
            windows: 1,
            attached: 0,
            activity,
            tag: tag.into(),
            interactive,
            has_shell: false,
        }
    }

    #[test]
    fn folds_panes_into_sessions() {
        let out = "agent\t2\t1\t100\t\tclaude\n\
                   agent\t2\t1\t100\t\tzsh\n\
                   choreo_t8\t1\t0\t50\t\twhile\n";
        let v = parse_panes("/tmp/s", out);
        assert_eq!(v.len(), 2);
        assert!(v[0].interactive);
        assert_eq!(v[0].windows, 2);
        assert!(!v[1].interactive);
    }

    #[test]
    fn an_experiment_named_session_running_claude_is_interactive() {
        // The 2026-08-31 fleet had exactly this: an agent living in a session
        // named after a benchmark.  Name-based filtering would hide it.
        let out = "choreo_pixel4a_posttask_freeze_t8\t2\t1\t9\t\tclaude\n";
        let v = parse_panes("/tmp/s", out);
        assert!(v[0].interactive);
        assert!(visible(&v[0], false));
    }

    #[test]
    fn observer_loops_are_hidden_by_default_and_shown_with_a() {
        let obs = s("choreo_power_flash", 10, "", false);
        assert!(!visible(&obs, false));
        assert!(visible(&obs, true));
    }

    #[test]
    fn ssh_menu_option_overrides_both_ways() {
        assert!(visible(&s("svc", 1, "show", false), false));
        assert!(!visible(&s("agent", 1, "hide", true), true));
    }

    #[test]
    fn view_is_most_recent_first() {
        let all = vec![
            s("old", 100, "", true),
            s("new", 300, "", true),
            s("mid", 200, "", true),
        ];
        let idx = view(&all, false);
        let names: Vec<&str> = idx.iter().map(|i| all[*i].name.as_str()).collect();
        assert_eq!(names, vec!["new", "mid", "old"]);
    }

    #[test]
    fn malformed_lines_are_skipped() {
        assert!(parse_panes("/tmp/s", "garbage\nalso\tgarbage\n").is_empty());
    }

    #[test]
    fn cjk_is_double_width() {
        assert_eq!(str_width("会话"), 4);
        assert_eq!(str_width("ab"), 2);
        assert_eq!(str_width("a会"), 3);
    }

    #[test]
    fn truncation_respects_columns() {
        assert_eq!(str_width(&truncate("会话名字很长", 5)), 5);
        assert_eq!(truncate("short", 10), "short");
        assert!(truncate("abcdefgh", 4).ends_with('…'));
        assert_eq!(str_width(&truncate("abcdefgh", 4)), 4);
    }

    #[test]
    fn decodes_arrows_enter_and_digits() {
        assert_eq!(decode(b"\x1b[A"), Some((Key::Up, 3)));
        assert_eq!(decode(b"\x1b[B"), Some((Key::Down, 3)));
        assert_eq!(decode(b"\r"), Some((Key::Enter, 1)));
        assert_eq!(decode(b"3"), Some((Key::Digit(3), 1)));
        assert_eq!(decode(b"a"), Some((Key::Char('a'), 1)));
        assert_eq!(decode(b"\x03"), Some((Key::Quit, 1)));
    }

    #[test]
    fn decodes_a_touch_as_a_row_press() {
        // A tap on row 4 arrives as an SGR press report.
        assert_eq!(decode(b"\x1b[<0;12;4M"), Some((Key::Tap(4, 12), 10)));
        // The matching release must be consumed without acting twice.
        assert_eq!(decode(b"\x1b[<0;12;4m"), Some((Key::Ignore, 10)));
        assert_eq!(decode(b"\x1b[<64;1;1M"), Some((Key::ScrollUp, 10)));
        assert_eq!(decode(b"\x1b[<65;1;1M"), Some((Key::ScrollDown, 10)));
    }

    #[test]
    fn partial_escape_sequences_wait_for_more_bytes() {
        assert_eq!(decode(b"\x1b"), None);
        assert_eq!(decode(b"\x1b["), None);
        assert_eq!(decode(b"\x1b[<0;12"), None);
    }

    #[test]
    fn multibyte_keys_are_consumed_whole() {
        assert_eq!(decode("会".as_bytes()), Some((Key::Char('会'), 3)));
    }

    #[test]
    fn narrow_render_never_exceeds_the_terminal_width() {
        let all = vec![s("一个名字很长的会话名称测试", 10, "", true), s("agent", 20, "", true)];
        let idx = view(&all, false);
        for cols in [20usize, 26, 30, 46, 80] {
            for rows in [8usize, 14, 24] {
                let f = render(&all, &idx, 0, 0, false, 3, None, 1.0, cols, rows, 100);
                for line in f.text.split("\r\n") {
                    let plain = strip_ansi(line);
                    assert!(
                        str_width(&plain) <= cols,
                        "cols={cols} rows={rows} width={} line={plain:?}",
                        str_width(&plain)
                    );
                }
            }
        }
    }

    #[test]
    fn countdown_names_the_top_session_and_draws_a_bar() {
        let all = vec![s("older", 10, "", true), s("newest", 99, "", true)];
        let idx = view(&all, false);
        let f = render(&all, &idx, 0, 0, false, 0, Some(0.5), 1.0, 80, 14, 100);
        let p = strip_ansi(&f.text);
        assert!(p.contains("newest"), "{p}");
        assert!(p.contains('▓') && p.contains('░'), "{p}");
    }

    #[test]
    fn a_card_claims_both_of_its_rows_across_the_full_width() {
        let all = vec![s("alpha", 30, "", true), s("beta", 20, "", true)];
        let idx = view(&all, false);
        let f = render(&all, &idx, 0, 0, false, 0, None, 1.0, 40, 20, 100);
        // rows 2 and 3 are the first card; a tap anywhere on either picks it
        assert_eq!(hit_test(&f.hits, 2, 1), Some(Action::Pick(0)));
        assert_eq!(hit_test(&f.hits, 3, 40), Some(Action::Pick(0)));
        assert_eq!(hit_test(&f.hits, 4, 20), Some(Action::Pick(1)));
    }

    #[test]
    fn the_action_bar_maps_taps_by_column() {
        let all = vec![s("alpha", 30, "", true)];
        let idx = view(&all, false);
        let f = render(&all, &idx, 0, 0, false, 2, None, 1.0, 60, 20, 100);
        let bar: Vec<&Hit> = f
            .hits
            .iter()
            .filter(|h| !matches!(h.action, Action::Pick(_)))
            .collect();
        assert_eq!(bar.len(), 3, "three buttons expected");
        assert_eq!(bar[0].action, Action::ToggleAll);
        assert_eq!(bar[1].action, Action::New);
        assert_eq!(bar[2].action, Action::Shell);
        // the buttons share one row and do not overlap
        assert!(bar.iter().all(|h| h.row == bar[0].row));
        assert!(bar[0].col_end < bar[1].col_start);
        assert!(bar[1].col_end < bar[2].col_start);
        // tapping inside the middle button hits New, not its neighbours
        let mid = (bar[1].col_start + bar[1].col_end) / 2;
        assert_eq!(hit_test(&f.hits, bar[1].row, mid), Some(Action::New));
    }

    #[test]
    fn all_three_buttons_fit_at_every_width() {
        let all = vec![s("alpha", 30, "", true)];
        let idx = view(&all, false);
        for cols in [20usize, 24, 26, 30, 36, 46, 60, 100] {
            let f = render(&all, &idx, 0, 0, false, 7, None, 1.0, cols, 16, 100);
            let n = f
                .hits
                .iter()
                .filter(|h| !matches!(h.action, Action::Pick(_)))
                .count();
            assert_eq!(n, 3, "cols={cols} lost a button");
        }
    }

    #[test]
    fn the_list_hugs_its_content() {
        let all = vec![s("alpha", 30, "", true), s("beta", 20, "", true)];
        let idx = view(&all, false);
        let f = render(&all, &idx, 0, 0, false, 0, None, 1.0, 40, 30, 100);
        // 1 title + 2 cards x 2 rows + separator + buttons + status + bottom
        assert_eq!(f.text.matches("\r\n").count(), 9, "{}", f.text);
    }

    #[test]
    fn tap_targets_are_at_least_two_rows_tall_when_there_is_room() {
        let all = vec![s("alpha", 30, "", true)];
        let idx = view(&all, false);
        let f = render(&all, &idx, 0, 0, false, 0, None, 1.0, 40, 20, 100);
        let card_rows: Vec<usize> = f
            .hits
            .iter()
            .filter(|h| h.action == Action::Pick(0))
            .map(|h| h.row)
            .collect();
        assert_eq!(card_rows.len(), 2, "a card should own two rows");
    }

    #[test]
    fn a_short_screen_falls_back_to_one_row_cards() {
        let all: Vec<Session> = (0..6).map(|i| s(&format!("s{i}"), i, "", true)).collect();
        let idx = view(&all, false);
        let f = render(&all, &idx, 0, 0, false, 0, None, 1.0, 40, 8, 100);
        let card_rows: Vec<usize> = f
            .hits
            .iter()
            .filter(|h| matches!(h.action, Action::Pick(_)))
            .map(|h| h.row)
            .collect();
        assert_eq!(card_rows.len(), f.per_page, "one row per visible card");
    }

    #[test]
    fn a_detached_shell_session_stays_listed() {
        // Detaching is the normal state of a session you come back to.
        let out = "agent\t1\t0\t5\t\tzsh\n";
        let v = parse_panes("/tmp/s", out);
        assert!(v[0].has_shell);
        assert!(visible(&v[0], false));
        // A service pane is pinned out explicitly, not guessed at.
        let out = "mcp\t1\t0\t5\thide\tzsh\n";
        let v = parse_panes("/tmp/s", out);
        assert!(!visible(&v[0], false));
    }

    fn strip_ansi(s: &str) -> String {
        let mut out = String::new();
        let mut it = s.chars().peekable();
        while let Some(c) = it.next() {
            if c == '\x1b' {
                for c2 in it.by_ref() {
                    if c2.is_ascii_alphabetic() {
                        break;
                    }
                }
            } else {
                out.push(c);
            }
        }
        out
    }
}
