# Codex session listener

`codex-session-listener` observes the local connection between Codex terminal
clients and the shared app-server. It does not replace the app-server socket,
wrap the `codex` command, inject a library, or modify a Codex process.

The command writes newline-delimited JSON events describing only:

- app-server and client process generations;
- a verified tmux socket and pane, when the client inherited them;
- the active thread ID and user-facing thread name;
- the protocol method that established or changed that metadata.

It deliberately never emits user input, assistant output, tool data, approval
contents, or raw app-server frames.

## Agent processes

The same helper traces `sched_process_exec` and `sched_process_exit`, filtered
in the kernel to the `claude` and `codex` executables; nothing about any other
process leaves it. The registry lists, under `processes`, every agent that
started while the listener ran, and it is rewritten when any agent exits, so a
consumer watching the file learns of both without scanning `/proc`. A Codex TUI
that runs without the app-server opens no connection, and this is the only
event its start produces.

The section stays empty until the helper bundle that contains these tracepoints
has been installed with `install-codex-session-listener-helper`.

## Restarts

The registry is cleared whenever the listener stops, and a connection that sits
idle afterwards sends nothing a new listener could learn its thread from. The
bound thread ids are therefore also kept in `sessions.json.bindings`, keyed by
the connection key. That key covers both process generations and both socket
inodes, so a remembered id can only reattach to the very connection it was
observed on; its name is then read back through `thread/read` as usual. A
`resume` argument still wins.

When a captured binding contains an ID but no name, the unprivileged controller
uses app-server's read-only `thread/read` method with `includeTurns: false`.
That method neither resumes nor subscribes to the thread. The returned summary
is used only for its `thread.name` field and is then discarded.

The transport and method names follow the
[Codex App Server protocol documentation](https://learn.chatgpt.com/docs/app-server).

## Inspect connections without privileges

```bash
./features/session-listener/bin/codex-session-listener --diagnose
```

This confirms which live app-server connections can be mapped to tmux panes.
It does not load BPF or read protocol bytes.

## Listen

The Linux kernel requires elevated BPF privileges on most distributions. The
normal controller remains unprivileged. First install a content-addressed,
root-owned helper bundle explicitly:

```bash
install-codex-session-listener-helper
```

The installer compiles the BPF object and the capture helper as the calling
user, then copies a fixed bundle to `/usr/libexec/codex-session-listener`. The
listener refuses helpers that are not root-owned, contain missing components,
or are writable by group/other users. Root never executes anything, and never
loads a BPF object, from the user-writable source tree. The installer also
validates and installs a `sudoers.d` rule restricted to that fixed helper, the
current user, and the selected control-socket path; it does not grant a general
interpreter or BPF command.

The helper is the Rust crate in `helper-rs`, so installing it needs `cargo` in
addition to `clang` and the system's libbpf. The crate has no dependencies: it
declares the few libc calls it uses, opens libbpf by its soname at run time,
and builds with `--locked --offline`. Its sources are copied into the bundle
next to the binary, so what root runs can be read where it is installed.

Then run the listener:

```bash
./features/session-listener/bin/codex-session-listener
```

The helper watches only file descriptors that `ss` verifies belong to the
configured app-server control socket. It strips each decoded JSON-RPC message
inside the privileged helper and sends only session metadata through the
private pipe. Raw byte segments never enter the public event process and are
discarded immediately.

`ss -p` reads the descriptor table of every process, which on a busy machine
costs tens of milliseconds of CPU per call. The helper therefore checks every
two seconds only whether the set of sockets bound to the control path in
`/proc/net/unix` has changed, and runs the full verification when it has, when
the last one failed or could not reach a tmux server, and every thirty seconds
regardless. Between verifications every captured event is still checked against
the verified socket inode and the client's process generation, as before.

The fixed helper needs root only while loading and attaching BPF. When launched
through `sudo`, it drops supplementary groups, GID, and UID back to the invoking
user before socket discovery, protocol decoding, and event processing.

The helper loads only the BPF object stored in its own root-owned bundle.
Compilation or loading errors stop the listener explicitly; there is no
hidden, high-memory fallback backend.

For continuous operation, install the opt-in user service after the helper and
normal feature commands are installed:

```bash
install-codex-session-listener-service
```

This is a separate action: the ordinary repository installer never starts it.
The listener waits through app-server downtime and reconnects after a process
generation change.

Use a non-default Codex home or explicit endpoint with:

```bash
CODEX_HOME=/path/to/codex-home codex-session-listener
codex-session-listener --control-socket /path/to/control.sock
```

## Event schema

Every line is one `codex.session-listener.v1` object. Important events are:

- `connection.opened` and `connection.closed`;
- `thread.bound` after an authoritative start, resume, fork, turn request, or
  active-thread notification. A connection that is already on a thread does
  not move when it merely resumes another one: the Codex TUI also resumes a
  thread just to show it (`/agent` opening a sub-agent, refreshing a snapshot
  before replay), and only a real `/resume` goes on to unsubscribe the old
  thread. So such a resume waits until the client unsubscribes some other
  thread (source `thread/resume+unsubscribe`), and a turn on either thread
  settles it. The switch happens on the unsubscribe request, since the TUI
  does not wait on the answer either;
- `thread.name.requested` for the client request;
- `thread.name.updated` after the app-server confirms the new name. The
  announcement carries it as `threadName`; a client that opted out of it still
  gets the bare answer to its request, after which the name is read back.
- `thread.name.resolved` after a read-only lookup fills an existing name.

`connection_id` includes both process start times and file descriptors, so PID
or FD reuse cannot silently inherit a previous binding. Socket inodes and the
tmux server/pane process generations are also included and revalidated.

The same metadata is written atomically with mode `0600` to
`~/.local/state/codex-tmux-integration/sessions.json`. Consumers can read this
current-state registry without scraping stdout. Use `--no-state-file` to
disable it or `--state-file PATH` to choose another location.

Native terminal clients and `codex app-server proxy` connections are recorded
separately. Proxy connections have client kind `remote_proxy` and no tmux
binding; this makes remote clients such as Codex Desktop visible without
pretending that every proxy belongs to a particular desktop application.

## Diagnose one session

Use the independent, read-only diagnostic command when a session cannot be
resumed:

```bash
codex-session-diagnose
codex-session-diagnose 019f0000-0000-7000-8000-000000000001
```

With no argument it lists all threads currently loaded by app-server. Passing
a thread ID prints the detailed diagnosis for that thread.

It combines non-subscribing `thread/read`, the live app-server process and
rollout file descriptor, and verified listener registry entries. It reports a
remote proxy as the owner only after the listener has observed that connection
carrying the requested thread ID. Unbound proxies are listed separately and
are never guessed to own the thread. Pass `--json` for structured output.

By default the listener invokes `codex-tmux-title-sync` in one-shot registry
mode after metadata changes. The applier preserves manual window names and
exits immediately; it does not create a per-pane polling process. A failed
run is reported on stderr and retried with the next change; agent process
events do not trigger it, since titles come from connections. Use
`--no-title-sync` when only the general metadata registry is wanted.

## End-to-end check

`integration-tests/session_listener_e2e.sh` drives real Codex TUIs against a
second listener: a sandbox `CODEX_HOME` with its own app-server, a private
tmux server, and the installed capture helper through sudo. It covers a rename,
`/new`, a `/resume` switch, the extra connection a `/resume` list opens, and a
listener restart. The sandbox signs in with a made-up API key, so nothing
reaches the model and the real ChatGPT tokens are never copied. It needs
passwordless sudo and an installed helper, so CI only checks its syntax.
`KEEP_SANDBOX=1` keeps the sandbox for a look after a failure.

## Boundaries

- A 64-bit Linux host with BPF, `ss`, `clang`, `libbpf`, and `sudo` is required.
- Existing WebSocket connections are observed from the next decodable frame;
  the listener never forces a reconnect.
- The Unix WebSocket transport is an app-server protocol surface. Unknown or
  malformed frames are ignored and never fall back to screen or transcript
  inference.
- App-server WebSocket transports are experimental. Protocol drift fails
  closed: unrecognized methods never become session bindings.
- The listener captures scalar and vectored socket I/O through `read`, `write`,
  `recvfrom`, `sendto`, `readv`, `writev`, `recvmsg`, and `sendmsg`.
- Send buffers are accepted only after the syscall reports the exact number of
  bytes written. Kernel events use one globally ordered ring; a dropped or
  deliberately omitted segment invalidates the decoder before later bytes are
  considered.
- Startup diagnostics report no session metadata until a relevant JSON-RPC
  message appears; it never fills a gap with screen or transcript inference.
