# Model switch

Switch the active Codex or Claude Code TUI without restarting or resuming its
conversation. The helper drives each CLI's own `/model` command, so the agent
retains responsibility for validating the selection and for updating its normal
configuration state.

The target pane is identified before any key is sent: the helper walks the
pane's process tree and refuses to type into a pane that is not running one of
the two agents.

## Bindings

Each binding resolves to the vocabulary of whichever agent owns the pane.

| Key | Codex | Claude Code |
| --- | --- | --- |
| `prefix + 1` | `gpt-5.6-sol`, Max | `opus` |
| `prefix + 2` | `gpt-5.6-sol`, Medium | `sonnet` |
| `prefix + 3` | `gpt-5.6-terra`, Extra high | `fable` |
| `prefix + 4` | `gpt-5.6-luna`, Max | `haiku` |
| `prefix + g` | Popup grid: sol/terra/luna × Low/Medium/Extra high/Max | Popup list of the configured aliases |

The four numeric bindings intentionally replace tmux's default window bindings.
Use the switcher while the agent is idle at its input prompt. It never sends an
interrupt to a running turn.

Set `CODEX_TMUX_CLAUDE_MODELS` to a space- or comma-separated list to change the
Claude aliases offered in the popup and accepted by `apply --model`. Aliases are
typed into a live pane, so only `[A-Za-z0-9._[]-]` names are accepted.

## Selection mechanics

Codex exposes a two-stage menu, so the helper parses the visible rows and sends
the minimum number of navigation keys. Claude accepts the model as an argument,
so the helper submits `/model <alias>` directly and confirms that the pane
advanced past the composed command. Claude echoes a submitted slash command into
its transcript, so completion is never inferred from the command text
disappearing.

## Latency

There are no fixed inter-menu sleeps. The helper polls at 2 ms initially, backs
off to 5 ms after 50 ms and 10 ms after 250 ms, and sends each navigation stage
as one tmux command. The completion message reports total time and active
non-sleep time so latency regressions are visible.

## Isolated manual test

Run `integration-tests/model_switch_sandbox.sh`. It creates a private tmux
socket, installer home and Codex home, copies authentication into the temporary
Codex home with mode 0600, and removes the directory after detaching. It does
not source configuration into an existing tmux server.
