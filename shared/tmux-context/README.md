# tmux-context

Shared hook routing used by `title-sync` and `window-attention`. It prefers
direct `TMUX` and `TMUX_PANE`; if a Codex hook subprocess lacks those
variables, it correlates session or transcript identity with live Codex
processes and recovers the inherited tmux environment.
