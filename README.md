# codex-tmux-integration

Feature-oriented integration for running Codex and Claude Code inside one or
more tmux servers. Each directory under `features/` owns its command, hook
fragment, tmux or shell configuration, tests, and documentation.

## Features

| Feature | Responsibility |
| --- | --- |
| `title-sync` | Rename a window from the active Codex session and restore the previous tmux name when ownership ends. |
| `window-attention` | Highlight inactive windows when an agent stops or needs input, then restore the previous style. |
| `pane-logging` | Capture bounded logs per server lifetime and pane; browse them with `tlog`. |
| `ssh-autoattach` | Select a server and session on interactive SSH login. |
| `external-notifications` | Optionally forward hooks to a user-configured notification backend. |

The shared `tmux-context` component resolves a hook to a verified
`socket path + server PID + pane ID`. Equal-scoring matches in different panes
are treated as ambiguous and do not modify tmux state.

## Runtime ownership

The tmux features use explicit ownership markers instead of fixed hook indexes.
Reloading configuration removes and recreates only entries marked for the same
feature, leaving hooks installed by the user or another plugin unchanged.

`title-sync` stores the original window name and `automatic-rename` behavior.
It serializes updates per window, reads the active pane inside that serialized
operation, and restores the previous state when the user selects a non-Codex
pane, the Codex process exits, or a `SessionEnd` hook runs.

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

The installer:

1. backs up every file it will change under
   `~/.local/state/codex-tmux-integration/backups/`;
2. links feature commands into `~/.local/bin`;
3. generates tmux and zsh aggregate files under
   `~/.config/codex-tmux-integration`;
4. adds one managed source block to `.tmux.conf` or `.zshrc` when required;
5. merges marker-owned hook commands while preserving unrelated settings;
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

GitHub Actions runs the Python suite, shell syntax checks, and isolated tmux
integration tests on each pull request.
