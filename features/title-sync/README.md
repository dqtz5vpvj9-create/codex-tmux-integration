# title-sync

Consumes Codex lifecycle hook JSON, resolves one verified tmux socket and pane,
and stores the title with the Codex process identity. Window reconciliation is
serialized per `socket + server PID + window` and reads the active pane after
acquiring that lock, so delayed focus hooks cannot overwrite a newer selection.

Before applying an agent title, the feature records the current window name and
`automatic-rename` behavior. It restores them when the active pane has no live
Codex owner, when `SessionEnd` runs, or during uninstall. A manual rename made
while the feature is active is preserved.

The focus path uses cached lifecycle state. Process scanning is limited to a
compatibility fallback for panes that predate hook installation.
