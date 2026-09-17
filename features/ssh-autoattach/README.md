# ssh-autoattach

Provides the `__tmux_ssh_*` zsh functions used for interactive SSH login.
Server and session names must match a short safe identifier, preventing pasted
commands or paths from becoming tmux socket names.

When `TMUX_SSH_RECONNECT_ID` is a 32-character hexadecimal identifier, the
selected socket and session are stored in the user's runtime directory. A later
SSH login carrying the same identifier attaches to that session directly. If
the session no longer exists, the stale mapping is removed and the normal menu
is shown.

The reconnect identifier is deliberately supplied by the SSH client rather
than inferred from an address, PID, or terminal name. A local wrapper can retry
only when OpenSSH exits with status 255, while a normal tmux detach or shell exit
ends the wrapper.
