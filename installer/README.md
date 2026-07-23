# installer

`install` discovers feature manifests, creates backups, links binaries,
generates aggregate config, and merges hook fragments. `uninstall` removes a
selected feature or all features. `doctor` checks the installed state and
loads the generated tmux config on an isolated socket.

Pass `--notification-queue` and optionally `--notification-python` to create a
machine-local `notifications.env` with mode `0600`. The repository does not
provide or record a host-specific notification path.

Dry-run output includes only managed hook sections; unrelated Claude settings
and credentials are never rendered.
