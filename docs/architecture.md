# Architecture

The repository is divided by user-visible behavior. Each feature may own a
manifest, commands, lifecycle hook fragments, tmux or shell configuration,
tests, and documentation.

## Verified hook routing

A pane identity includes:

```text
tmux socket path + tmux server PID + pane ID + pane PID
```

Hook subprocesses first use inherited `TMUX` and `TMUX_PANE`, followed by a live
tmux probe. When those variables are absent, the resolver correlates the hook
session or normalized transcript path with live Codex processes, supports a
custom `CODEX_HOME`, excludes app-server processes, and rejects equal-scoring
matches in different panes.

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

## Configuration ownership

The repository owns one marked source block in each applicable dotfile. JSON
hooks carry an environment assignment identifying the owning feature. Legacy
unmarked commands are migrated only when their complete executable path equals
the installation bin directory; an unrelated command with the same basename is
preserved.
