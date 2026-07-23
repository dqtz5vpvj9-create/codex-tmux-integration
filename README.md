# codex-tmux-integration

Feature-oriented integration for running Codex and Claude Code inside one or
more tmux servers. Each directory under `features/` owns its command, hook
fragment, tmux or shell configuration, tests, and documentation.

## Features

| Feature | Responsibility |
| --- | --- |
| `title-sync` | Rename the owning tmux window from the Codex session title. |
| `window-attention` | Highlight inactive windows when an agent stops or needs input. |
| `pane-logging` | Capture bounded logs per server and pane; browse them with `tlog`. |
| `ssh-autoattach` | Safely select a server and session on interactive SSH login. |
| `external-notifications` | Optionally forward hooks to AndroidTools notifications. |

The shared `tmux-context` component resolves a hook to the correct
`socket_path + pane_id`. A pane id such as `%0` is not globally unique.

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
4. adds one managed source block to `.tmux.conf` and `.zshrc`;
5. merges only feature-owned hook commands into Codex and Claude JSON.

It preserves unrelated hooks and never prints unrelated Claude settings.

## Uninstall

```bash
./installer/uninstall --features external-notifications
./installer/uninstall
```

Uninstall removes only links and hook/config entries owned by this repository.

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
manifests, transcripts, or notification backend settings.

## Validation

```bash
python3 -m pytest -q
./integration-tests/test_tmux_integration.sh
./installer/doctor
```
