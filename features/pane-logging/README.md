# pane-logging

Enables bounded `pipe-pane` logging on panes without an existing user pipe. A
marker records the server lifetime, log file, and live sink PID. Cleanup closes
the pipe only while that recorded sink still owns it.

The live sink starts before history capture. It buffers new output until the
snapshot is written, then appends buffered data, avoiding the setup gap between
`capture-pane` and `pipe-pane`. File locking serializes live and snapshot writes,
and every active and rotated file keeps the original 32 MiB plus three-copy
rotation behavior. The log root has a 128 MiB global cache budget.

`tmux-autolog` performs lifecycle garbage collection after pane and session
events. It verifies the marker and sink PID for every active pane, protects
those complete log groups, and removes the current file and all rotations for
panes or server lifetimes that no longer have an owner. This keeps cleanup
from truncating a live pane or deleting another live sink's history. If the
active managed groups themselves exceed the global budget, it disables logging
for the oldest managed panes and then removes their complete groups; this
does not alter the terminal pane or a user-owned `pipe-pane`.

Log directories include the tmux server PID as well as the socket identity, so
a server restarted at the same socket does not append to a previous lifetime.
Pane-exit and session-close hooks trigger retention cleanup, while `tlog`
discovers both default sockets and custom sockets registered by the tmux
configuration.
