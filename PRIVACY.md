# Privacy

This repository contains implementation and synthetic test fixtures only.

Do not commit:

- Codex or Claude configuration copied from a user account;
- hook payloads, transcripts, session identifiers, or pane logs;
- tmux socket names or live process inventories;
- notification backend paths or credentials;
- generated installer state or backups;
- personal names, email addresses, home-directory paths, hostnames, or IP
  addresses.

The installer stores machine-specific notification settings in
`~/.config/codex-tmux-integration/notifications.env` with mode `0600`. That
file is intentionally excluded from Git.

Before publishing a change, scan both tracked files and commit metadata for
credentials and personal identifiers, then run the full test suite.
