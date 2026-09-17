# window-attention

Highlights the owning tmux window when Codex or Claude Code stops or asks for
attention. The active-client check and style update share a per-window lock, so
a delayed notification cannot re-highlight a window after the user focuses it.

| Event | Codex | Claude Code |
| --- | --- | --- |
| Turn finished | `Stop` | `Stop` |
| Waiting on the user | `PermissionRequest` (Bash) | `Notification` |

Both agents route through the same shared resolver, so a highlight is applied
only after the hook has been tied to one verified socket and pane.

The previous `window-status-style` and its local/inherited status are recorded.
Focus hooks and uninstall restore that state only while the project-applied
style remains current; a user style change is preserved.

Before applying a highlight, the notifier verifies that all focus-clear hooks
are installed on the same tmux socket. Hook replacement is add-before-remove,
so a reload failure keeps the previous clear path; if no clear path can be
confirmed, the notifier leaves the window unchanged.

Set `AGENT_TMUX_NOTIFY_STYLE` to replace the default highlight style.
