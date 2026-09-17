# codex-tmux-integration

Feature-oriented integration for running Codex and Claude Code inside one or
more tmux servers. Each directory under `features/` owns its command, hook
fragment, tmux or shell configuration, tests, and documentation.

## Features

| Feature | Responsibility |
| --- | --- |
| `title-sync` | Rename a window immediately when zsh launches an agent, upgrade it from authoritative session data, and restore the previous tmux name when ownership ends. |
| `session-listener` | Observe Codex app-server session metadata with eBPF and publish verified thread-to-tmux bindings for `title-sync`. |
| `window-attention` | Highlight inactive windows when an agent stops or needs input, then restore the previous style. |
| `pane-logging` | Capture pane logs and garbage-collect them with the pane/server lifetime; browse them with `tlog`. |
| `model-switch` | Switch the active agent's model — and, for Codex, its reasoning effort — with presets or a popup. |
| `ssh-autoattach` | Select a server and session on interactive SSH login. |
| `external-notifications` | Optionally forward hooks to a user-configured notification backend. |

The shared `tmux-context` component resolves a hook to a verified
`socket path + server PID + pane ID`. Equal-scoring matches in different panes
are treated as ambiguous and do not modify tmux state.

## Supported agents

Codex and Claude Code are first-class and can run side by side in the same tmux
server. Every feature that touches an agent is routed through one resolver that
records which agent owns a pane, so agent-specific behavior never requires
repeating process detection.

| | Codex | Claude Code |
| --- | --- | --- |
| Hook configuration | `~/.codex/hooks.json` (`CODEX_HOME`) | `~/.claude/settings.json` (`CLAUDE_CONFIG_DIR`) |
| Title events | `session-listener` registry changes; no title hooks | `SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd` |
| Attention events | `Stop`, `PermissionRequest` | `Stop`, `Notification` |
| Window title | verified listener registry, including Codex's generated or explicit thread name | the session name, or the newest chat title recorded in the transcript |
| Live rename tracking | app-server metadata observed by `session-listener` | the session state file and transcript, without capturing the pane |
| Model switch | `/model` two-stage menu, model × reasoning effort | `/model <alias>` |

`pane-logging`, `ssh-autoattach` and the tmux runtime are agent-neutral and
apply to every pane.

Features are selectable, so a Codex-only or Claude-only machine installs only
what it needs. Hooks for an agent that is not installed are inert: the fragments
are merged into that agent's configuration file and never run.

## Runtime ownership

The tmux features use explicit ownership markers instead of fixed hook indexes.
Reloading configuration removes and recreates only entries marked for the same
feature, leaving hooks installed by the user or another plugin unchanged.

`title-sync` stores the original window name and `automatic-rename` behavior.
It serializes updates per window, reads the active pane inside that serialized
operation, and restores the previous state when the user selects a non-agent
pane, the listener removes a Codex binding, or a Claude `SessionEnd` hook runs.

For a blank new CLI, the zsh `preexec` integration first applies a provisional
`codex` or `claude` title, because neither agent has session metadata before its
first prompt. The Codex listener registry or Claude lifecycle hooks replace it
with the session name or identifier without requiring a pane focus change.

`window-attention` preserves an existing `window-status-style`. `pane-logging`
marks its pipe ownership and closes a pipe during cleanup only while the
recorded sink process still owns it.

Every tmux server that loads the generated configuration is recorded in a
private local registry. This allows install, uninstall, `doctor`, and `tlog` to
handle custom `tmux -S` socket paths as well as default sockets.

## Install

```bash
./installer/install --dry-run
./installer/install
./installer/doctor
```

Select a subset with:

```bash
./installer/install --features title-sync,window-attention,pane-logging
```

Installing `session-listener` adds its listener, the independent read-only
`codex-session-diagnose SESSION_ID` command, and explicit setup commands. It
does not install the privileged helper or start the user service; those remain
separate, deliberate setup steps documented under `features/session-listener`.

The installer:

1. backs up every file it will change under
   `~/.local/state/codex-tmux-integration/backups/`;
2. links feature commands into `~/.local/bin`;
3. generates tmux and zsh aggregate files under
   `~/.config/codex-tmux-integration`;
4. adds one managed source block to `.tmux.conf` or `.zshrc` when required;
5. merges marker-owned hook commands into both agents' configuration while
   preserving unrelated settings;
6. reconciles already-running registered tmux servers.

Reinstalling with a smaller feature set removes links and live runtime state
that belonged to features no longer selected.

## Uninstall

```bash
./installer/uninstall --features external-notifications
./installer/uninstall
```

Uninstall restores managed window names and styles, removes marker-owned tmux
hooks, closes verified project-owned logging pipes, and preserves unrelated
configuration.

## Optional notification backend

`external-notifications` has no host-specific backend by default. Configure a
local queue without committing its path:

```bash
./installer/install \
  --notification-queue /path/to/notification_queue.py \
  --notification-python /path/to/python3
```

The values are stored with mode `0600` under
`~/.config/codex-tmux-integration/notifications.env`. Environment variables
`ANDROIDTOOLS_NOTIFY_QUEUE` and `ANDROIDTOOLS_NOTIFY_PYTHON` take precedence.
Missing notification infrastructure does not disable the tmux features.

Do not commit generated hook payloads, pane logs, user configuration, state
manifests, transcripts, or notification backend settings. See `PRIVACY.md`.

## Validation

```bash
python3 -m pytest -q
./integration-tests/test_tmux_integration.sh
./installer/doctor
```

`doctor` reports, per agent, whether every enabled feature's lifecycle hooks are
present in that agent's configuration, and whether each agent CLI is on `PATH`.

GitHub Actions runs the Python suite, shell syntax checks, and isolated tmux
integration tests on each pull request.
