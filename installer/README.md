# installer

`install` discovers feature manifests, creates backups, links binaries,
generates aggregate config, and merges hook fragments into both agents'
configuration. `uninstall` removes a selected feature or all features. `doctor`
checks the installed state and loads the generated tmux config on an isolated
socket.

Hook fragments are merged per agent: Codex fragments into `hooks.json` below
`CODEX_HOME`, Claude fragments into `settings.json` below `CLAUDE_CONFIG_DIR`.
Both variables are honored only when the run targets the real home directory, so
an explicit `--home` used by tests or by an isolated install stays contained.
Installing a feature for an agent that is not present on the machine is
harmless: the merged hooks are never invoked.

`doctor` reports, per agent, whether every enabled feature's lifecycle hooks are
present in that agent's configuration, and whether the agent CLI is on `PATH`.

Pass `--notification-queue` and optionally `--notification-python` to create a
machine-local `notifications.env` with mode `0600`. The repository does not
provide or record a host-specific notification path.

Dry-run output includes only managed hook sections; unrelated Claude settings
and credentials are never rendered.
