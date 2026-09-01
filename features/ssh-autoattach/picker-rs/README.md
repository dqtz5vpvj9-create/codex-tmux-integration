# tmux-ssh-picker

The menu an SSH login lands on. Replaces the whiptail dialog in
`shell/tmux-ssh.zsh`, which stays in place as the fallback.

## What it does differently

**Lists sessions people work in, not the whole fleet.** A session shows when
any of its panes runs something interactive — an agent, an editor, a shell.
Benchmark observers and services stay out of the way behind `a`. The test that
matters: this fleet runs agents inside sessions *named* after experiments
(`choreo_pixel4a_posttask_freeze_t8_c1_diagnostic_20260831` hosts two `claude`
panes), so filtering by name would hide exactly the sessions you want.

A session can pin itself either way:

    tmux set -t <session> @ssh_menu hide    # never list it
    tmux set -t <session> @ssh_menu show    # always list it

**Attaches on its own after a countdown.** With no input it takes the most
recently active session, so the common case costs one SSH round trip and no
decisions. Any key or tap cancels the timer. `TMUX_SSH_MENU_TIMEOUT` sets the
delay in seconds; `0` disables it.

**Fits a phone and takes taps.** The layout adapts from 80 columns down to 20,
dropping the frame and shrinking the button labels rather than losing a button.
Each session owns two full-width rows, so a thumb does not have to be precise.
Mouse reporting is SGR (`1000h`/`1006h`), which phone SSH clients send on tap.

## Keys

    ⏎        attach to the highlighted session
    ↑↓ jk    move, wrapping through the buttons
    ←→       move along the button bar
    1-9      attach to that session directly
    a        toggle experiment and service sessions
    n        new session
    s q ^C   plain shell
    tap      attach to the session tapped, or press the button tapped

## Contract with the shell

One line on stdout, everything else on `/dev/tty`:

    ATTACH\t<socket>\t<session>
    NEW
    SHELL

Exit 0 means that line is good. **Exit 2 means "not my job"** — no tty, a dumb
terminal, no sessions — and the caller falls back to its own menu. So does any
other exit, and so does a decision line the caller will not parse. A broken
picker degrades the login; it cannot block it.

## Build

    ./build.sh              # installs to ~/.local/bin/tmux-ssh-picker
    cargo test              # 21 unit tests

No dependencies, deliberately: this runs on every SSH login. The few libc calls
are declared in `src/main.rs`, so a bare `rustc src/main.rs` also works.

## Latency

Time from exec to the first frame on the real server: **10 ms**, of which the
`tmux list-panes` spawn is most. It was 187 ms before the picker learned to
skip dead sockets — `/tmp/tmux-<uid>/` had accumulated 30 of them, and asking
`tmux -S` about a corpse costs a process spawn each. If you ever need less than
this, the remaining win is not a faster language but not spawning tmux at all:
keep a `tmux -C` subscriber warm and read its cache.
