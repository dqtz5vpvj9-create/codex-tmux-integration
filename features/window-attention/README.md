# window-attention

Highlights the owning tmux window when Codex or Claude stops or asks for
attention. The active-client check and style update share a per-window lock, so
a delayed notification cannot re-highlight a window after the user focuses it.

The previous `window-status-style` and its local/inherited status are recorded.
Focus hooks and uninstall restore that state only while the project-applied
style remains current; a user style change is preserved.
