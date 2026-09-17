# title-sync

Keeps a tmux window named after the Codex or Claude Code session that owns its
active pane, and restores the previous name when that ownership ends.

The zsh launch hook applies a provisional title — `codex` or `claude`, matching
the command that was typed — before the CLI draws its UI. This is intentionally
separate from session metadata: a blank new CLI does not create a Codex thread
until its first prompt. For Codex, `session-listener` replaces the provisional
title from its verified registry. Claude lifecycle data performs the equivalent
transition for Claude Code.

The shell records provisional-title ownership before invoking the helper, so an
immediate `Ctrl-C` cannot strand the window at the launcher name.

## Where a title comes from

| Agent | Title source, in order |
| --- | --- |
| Codex | The confirmed thread name in the `session-listener` registry, then `codex-<id prefix>`. |
| Claude Code | A session name the user chose, the newest `ai-title` record in the session transcript, the name Claude derived for the session, then `claude-<id prefix>`. |

Claude Code records `sessionId`, `cwd` and its own process start time in
`~/.claude/sessions/<pid>.json`, and does not hold its transcript open. That
state file is therefore what binds a Claude hook or pane to a session, in the
same role that an open transcript descriptor plays for Codex. `CLAUDE_CONFIG_DIR`
and `CODEX_HOME` are both honored.

## Live updates

For Codex, `session-listener` owns identity and name observation. It writes
`~/.local/state/codex-tmux-integration/sessions.json` and invokes this feature
in one-shot registry mode after metadata changes. The title path does not scan
Codex processes, capture the TUI, or poll `session_index.jsonl`.

For Claude, the observer never captures the pane. It stats the session state
file and transcript, and re-reads them only when one of them changes, so a
rename or a new chat title reaches the window title without parsing a UI.

Install-time reconciliation and later focus hooks backfill the Claude observer.
Transient tmux query failures do not terminate it. Registry reconciliation also
repairs a generic `codex` reset without treating it as a user-selected name.

## Ownership

Window reconciliation is serialized per `socket + server PID + window` and
reads the active pane after acquiring that lock, so delayed focus hooks cannot
overwrite a newer selection.

Before applying an agent title, the feature records the current window name and
`automatic-rename` behavior. It restores them when the active pane has no live
agent owner, when `SessionEnd` runs, or during uninstall. A manual rename made
while the feature is active is preserved.

The Codex focus path uses only listener-owned cache state. Process scanning is
limited to Claude panes that predate hook installation.

Set `CODEX_TMUX_TITLE_LAUNCHERS` to a space-separated list when a local wrapper
other than `codex` or `claude` should trigger the provisional title, or to
narrow the default to one agent. Set `CODEX_TMUX_START_TITLE` to replace that
provisional text for every launcher.
