# ssh-autoattach

Provides the `__tmux_ssh_*` zsh functions used for interactive SSH login.
Server and session names must match a short safe identifier, preventing pasted
commands or paths from becoming tmux socket names.
