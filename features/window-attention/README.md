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

## Pane marker

A window can hold several agents, so the notifier also records which pane
raised the event. It sets the pane option `@agent_attention` to
`input:<unix time>` when the agent waits on the user and to `done:<unix time>`
when a turn finished. Anything that lists agents per pane can read it in the
same `list-panes -F '#{@agent_attention}'` call it already makes, and tmux drops
it together with the pane.

The marker follows what the user has actually seen. It is set unless a client
is looking at the window and the pane is on screen, so a pane hidden behind a
zoomed neighbour is marked even though its window is not highlighted. A focus
hook clears the marker from every pane of an unzoomed window, and from the
active pane only when the window is zoomed.

Every change signals the tmux channel `agent-attention`. A consumer that wants
to react without polling blocks in `tmux wait-for agent-attention`.

## Clearing

`switch-client` changes a client's session, window and pane without running
`select-window` or `select-pane`; `choose-tree` and pickers built on it work
that way. The clear hooks therefore include `session-window-changed` and
`client-session-changed` next to `after-select-window`, `after-select-pane`,
`client-attached` and `client-focus-in`.

Set `AGENT_TMUX_NOTIFY_STYLE` to replace the default highlight style.
