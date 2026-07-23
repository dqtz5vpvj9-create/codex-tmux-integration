# Migration

This repository intentionally contains no machine-specific migration snapshot.
Capture the following information locally before cutover and keep it outside
Git:

- tmux socket paths and server count;
- session, window, and pane identities;
- current `pane_pipe` state;
- hashes and permissions of files that the installer will replace;
- a permissions-restricted backup of Codex, Claude, tmux, and shell config.

## Cutover

1. Run `installer/install --dry-run`.
2. Run the repository tests on an isolated tmux socket.
3. Install the selected features.
4. Reload each live socket with the updated `.tmux.conf`.
5. Run `tmux-autolog --socket <path> sweep`.
6. Compare server, session, window, and pane identities against the private
   baseline.
7. Run `installer/doctor`.

Never commit local backups, session identifiers, transcript paths, socket
names, notification payloads, or pane logs.
