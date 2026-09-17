# Migration

This repository intentionally contains no machine-specific migration snapshot.
Keep inventories, backups, socket paths, session identifiers, transcript paths,
and notification configuration outside Git.

## Upgrade to runtime state schema 2

The installer records schema version 2 in its private state manifest. During the
first upgrade from an earlier installation it performs narrowly scoped runtime
migration on registered live tmux servers:

- removes the historical hook indexes only when their command matches the old
  feature implementation;
- releases global window options previously set by title synchronization;
- clears the historical default attention style where it is still present;
- removes the legacy title cache;
- installs marker-owned hooks and reconciles current windows and panes.

A user hook at the same historical index remains in place when its command does
not match the old feature command.

## Adding Claude Code to an existing install

No state migration is required. Re-run `installer/install` with the same feature
selection: it merges the Claude fragments into `~/.claude/settings.json`
alongside the existing Codex hooks, leaving unrelated settings and hooks in that
file untouched. Existing Codex behavior is unchanged.

A Claude session that was already running when the hooks were installed has no
`SessionStart` left to fire. Its window is picked up by the next focus hook, or
immediately by:

```bash
codex-tmux-title-sync --socket "$(tmux display-message -p '#{socket_path}')" --all
```

`installer/doctor` reports per agent whether every enabled feature's lifecycle
hooks are present, so a partially migrated configuration is visible without
reading the JSON by hand.

Set `CLAUDE_CONFIG_DIR` before installing when Claude Code does not use
`~/.claude`; it is honored the same way `CODEX_HOME` already is.

## Cutover

1. Run `installer/install --dry-run`.
2. Run the Python and isolated tmux tests.
3. Install the selected features.
4. Run `installer/doctor`.
5. Compare the local server, session, window, pane, and pipe inventory with the
   private baseline.

Install and uninstall now reconcile live sockets found in the private socket
registry, including custom `tmux -S` paths. A server that has never loaded the
generated configuration cannot appear in that registry and should be sourced
once before migration.
