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
