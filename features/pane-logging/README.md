# pane-logging

Enables `pipe-pane` on every pane, writes immediately through
`tmux-log-sink`, rotates bounded logs, and namespaces files by tmux socket
identity plus pane id. `tlog` lists or opens logs across all local servers.
