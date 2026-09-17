# Architecture

The repository is divided by user-visible behavior. Each feature may own a
manifest, commands, lifecycle hook fragments, tmux or shell configuration,
tests, and documentation.

## Agents

Codex and Claude Code share tmux ownership and restoration logic, but publish
session identity differently. Codex bindings come from the eBPF-backed
`session-listener` registry. Claude reports `agent_kind` through the common
resolver and publishes its identity through its session state and transcript.

Nothing is assumed about an agent being installed. Hook fragments are merged
into both `~/.codex/hooks.json` and `~/.claude/settings.json`; an absent CLI
simply never invokes them.

## Verified hook routing

A pane identity includes:

```text
tmux socket path + tmux server PID + pane ID + pane PID
```

Hook subprocesses first use inherited `TMUX` and `TMUX_PANE`, followed by a live
tmux probe. When those variables are absent, the resolver walks the hook's own
ancestors: the nearest Codex or Claude process is authoritative because it is
the process that launched the hook.

Session correlation remains as a fallback for detached hook runners. Codex is
correlated through the transcript descriptor it holds open below `CODEX_HOME`.
Claude Code closes its transcript between writes and instead maintains
`<CLAUDE_CONFIG_DIR>/sessions/<pid>.json`, so that file — validated against the
process start time it records — plays the same role. A `resume` argument is the
weakest accepted signal for both. Equal-scoring matches in different panes are
rejected as ambiguous.

Process-backed cache entries also include the process start time, preventing a
reused PID from validating stale state.

## Runtime ownership

Tmux hooks are array entries. A shared hook manager appends entries containing a
feature marker and removes only entries carrying the same marker. No fixed
array index is reserved.

Window title and attention features maintain reversible state per tmux server
lifetime. Pane logging combines a pane marker with a verified sink process.
Install and uninstall apply or remove this runtime state on every live socket in
the private registry.

## Observing a rename

`session-listener` observes Codex app-server metadata and invokes `title-sync`
in one-shot registry mode when a binding or confirmed name changes. Codex panes
are not captured or polled. Claude still uses one pane-bound observer that reads
its session state file and transcript only when either file changes; it also
never captures the pane.

## Configuration ownership

The repository owns one marked source block in each applicable dotfile. JSON
hooks carry an environment assignment identifying the owning feature. Legacy
unmarked commands are migrated only when their complete executable path equals
the installation bin directory; an unrelated command with the same basename is
preserved.
