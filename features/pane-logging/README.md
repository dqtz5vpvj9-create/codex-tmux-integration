# pane-logging

Enables bounded `pipe-pane` logging on panes without an existing user pipe. A
marker records the server lifetime, log file, and live sink PID. Cleanup closes
the pipe only while that recorded sink still owns it.

The live sink starts before history capture. It buffers new output until the
snapshot is written, then appends buffered data, avoiding the setup gap between
`capture-pane` and `pipe-pane`. File locking serializes live and snapshot writes,
and every active and rotated file obeys the configured byte limit.

Log directories include the tmux server PID as well as the socket identity, so
a server restarted at the same socket does not append to a previous lifetime.
`tlog` discovers both default sockets and custom sockets registered by the tmux
configuration.
