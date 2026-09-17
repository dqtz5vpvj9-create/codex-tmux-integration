# tmux-context

Shared hook routing used by `title-sync`, `window-attention` and `model-switch`.
It prefers direct `TMUX` and `TMUX_PANE`; if an agent hook subprocess lacks
those variables, it recovers the inherited tmux environment from the hook's
nearest agent ancestor, and falls back to correlating session or transcript
identity with live agent processes.

Both Codex and Claude Code are recognized, and each contributes the identity
material it actually publishes:

| | Codex | Claude Code |
| --- | --- | --- |
| Process | `comm == codex`, excluding `codex app-server` | `comm == claude`, excluding subcommands that own no pane (`mcp`, `update`, …) |
| Session binding | the transcript descriptor the process holds open under `<CODEX_HOME>/sessions` | `sessionId` in `<CLAUDE_CONFIG_DIR>/sessions/<pid>.json`, verified against the recorded process start time |
| Transcript | open file descriptor | `<CLAUDE_CONFIG_DIR>/projects/*/<sessionId>.jsonl` |
| Resume argument | `codex resume <id>` | `claude --resume <id>` |

Scores are shared across both agents — transcript match, session match, then a
resume argument — so equal-scoring matches in different panes stay ambiguous and
are never acted on. Resolved targets carry an `agent_kind`, which lets callers
apply agent-specific behavior without repeating process detection.
