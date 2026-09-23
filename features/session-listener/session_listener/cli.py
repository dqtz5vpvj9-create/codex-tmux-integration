"""Controller and command-line interface for the session listener."""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import errno
import json
import os
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from .model import (
    CAPTURE_SCHEMA,
    DEFAULT_CONTROL_SOCKET,
    SCHEMA,
    SS_REFRESH_SECONDS,
    ListenerError,
    discover,
    process_start_ticks,
    utc_now,
)
from .protocol import ProtocolState, ThreadNameResolver, connection_event


TRUSTED_HELPER = Path("/usr/libexec/codex-session-listener/current/codex-session-capture")
DEFAULT_TITLE_SYNC = Path.home() / ".local/bin/codex-tmux-title-sync"


class TitleSyncConsumer:
    """Trigger the non-resident tmux applier after metadata changes.

    A failed run is reported and left at that: the next change runs it again,
    whereas ending the listener would empty the registry every consumer reads.
    """

    def __init__(self, command: Path, timeout: float = 3.0):
        self.command = command
        self.timeout = timeout

    def __call__(self, state_file: Path) -> None:
        try:
            completed = subprocess.run(
                [
                    str(self.command),
                    "--registry-sync",
                    "--registry-file",
                    str(state_file),
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"codex-session-listener: tmux title sync failed: {exc}", file=sys.stderr)
            return
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit {completed.returncode}"
            print(f"codex-session-listener: tmux title sync failed: {detail}", file=sys.stderr)


class RegistryOutput:
    """Mirror public events to an atomic, metadata-only current-state file."""

    def __init__(
        self,
        stream: TextIO,
        state_file: Path | None,
        on_change: Callable[[Path], None] | None = None,
    ):
        self.stream = stream
        self.state_file = state_file
        self.on_change = on_change
        self.connections: dict[str, dict[str, Any]] = {}
        self.processes: dict[str, dict[str, Any]] = {}
        self.remembered = self._load_remembered()
        if state_file is not None:
            self._persist()

    def write(self, value: str) -> int:
        written = self.stream.write(value)
        for line in value.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                self._consume(event)
        return written

    def flush(self) -> None:
        self.stream.flush()

    def reset(self) -> None:
        self.connections.clear()
        self.processes.clear()
        if self.state_file is not None:
            self._persist()

    def _consume(self, event: dict[str, Any]) -> None:
        if event.get("schema") != SCHEMA:
            return
        connection_id = event.get("connection_id")
        kind = event.get("event")
        process = event.get("process")
        if kind in {"process.started", "process.exited"} and isinstance(process, dict):
            if kind == "process.started":
                self.processes[str(process.get("pid"))] = process
            else:
                self.processes.pop(str(process.get("pid")), None)
            if self.state_file is not None:
                # Titles come from connections; the applier finds processes itself.
                self._persist(notify=False)
            return
        if not isinstance(connection_id, str) or not isinstance(kind, str):
            return
        if kind == "connection.closed":
            self.connections.pop(connection_id, None)
        else:
            current = self.connections.setdefault(connection_id, {})
            for key in ("server", "client", "tmux"):
                if key in event:
                    current[key] = event[key]
            if kind == "thread.unbound":
                current["thread"] = {"id": None, "name": None}
                current.pop("pending_name", None)
            elif kind == "thread.name.requested":
                thread = event.get("thread")
                if isinstance(thread, dict):
                    current["pending_name"] = thread.get("name")
            elif isinstance(event.get("thread"), dict):
                current["thread"] = event["thread"]
                if kind in {"thread.name.updated", "thread.name.resolved"}:
                    current.pop("pending_name", None)
            current["last_event"] = kind
            current["observed_at"] = event.get("observed_at")
        if self.state_file is not None:
            self._persist()

    @property
    def bindings_file(self) -> Path | None:
        if self.state_file is None:
            return None
        return self.state_file.with_name(self.state_file.name + ".bindings")

    def _load_remembered(self) -> dict[str, str]:
        """Thread ids the previous listener had bound, by connection key.

        The registry is cleared whenever the listener stops, and a connection
        that is idle afterwards sends nothing a new listener could learn its
        thread from. A connection key covers both process generations and both
        socket inodes, so a remembered id can only reattach to the very
        connection it was observed on.
        """

        if self.bindings_file is None:
            return {}
        try:
            saved = json.loads(self.bindings_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        bindings = saved.get("bindings") if isinstance(saved, dict) else None
        if not isinstance(bindings, dict):
            return {}
        return {key: value for key, value in bindings.items() if isinstance(value, str)}

    def _persist_bindings(self) -> None:
        assert self.bindings_file is not None
        bindings = {
            key: connection["thread"]["id"]
            for key, connection in self.connections.items()
            if isinstance((connection.get("thread") or {}).get("id"), str)
        }
        temporary = self.bindings_file.with_name(self.bindings_file.name + ".tmp")
        temporary.write_text(
            json.dumps({"schema": "codex.session-bindings.v1", "bindings": bindings}),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.bindings_file)
        # What the next listen() in this process starts from, should the helper
        # ask for a restart; the copy read at startup is hours out of date by then.
        self.remembered = bindings

    def _persist(self, notify: bool = True) -> None:
        assert self.state_file is not None
        self.state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # An empty registry means the listener is starting or stopping, which is
        # exactly when the remembered bindings must survive.
        if self.connections:
            self._persist_bindings()
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.state_file.name}.", dir=self.state_file.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "schema": "codex.session-registry.v1",
                        "observed_at": utc_now(),
                        "connections": self.connections,
                        "processes": self.processes,
                    },
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_file)
            if notify and self.on_change is not None:
                self.on_change(self.state_file)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def encode_event(stream: TextIO, event: dict[str, Any]) -> None:
    stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def opening_events(
    state: ProtocolState, remembered: str | None = None
) -> list[dict[str, Any]]:
    """Publish the thread a new listener starts a connection on, named or not.

    ``remembered`` is the thread a previous listener had bound to this same
    connection. The connection key covers the process generation and its
    ``resume UUID`` argument, so whatever was remembered was seen after that
    argument was given, and wins over it: the user may have used /resume since.
    """

    events = [connection_event("connection.opened", state.connection)]
    if remembered is not None:
        events.append(state._event("thread.bound", remembered, None, "registry:remembered"))
    elif state.connection.thread_hint is not None:
        events.append(
            state._event(
                "thread.bound",
                state.connection.thread_hint,
                None,
                "process:resume",
            )
        )
    return events


def process_event(record: dict[str, Any]) -> dict[str, Any] | None:
    """Publish an agent process the helper saw start or exit.

    The registry lists the agents that started while the listener ran; an exit
    is published even for an older one, so a consumer watching the file learns
    of it without polling.
    """

    pid = record.get("pid")
    name = {"exec": "process.started", "exit": "process.exited"}.get(record.get("event"))
    if not isinstance(pid, int) or name is None:
        return None
    return {
        "schema": SCHEMA,
        "observed_at": utc_now(),
        "event": name,
        "process": {
            "pid": pid,
            "start_ticks": process_start_ticks(pid),
            "comm": record.get("comm"),
        },
    }


def helper_command(
    helper: Path,
    control_socket: Path,
) -> list[str]:
    helper = validate_privileged_helper(helper)
    command = [str(helper), "--control-socket", str(control_socket)]
    if os.geteuid() == 0:
        return command
    sudo = Path("/usr/bin/sudo")
    if not sudo.is_file():
        raise ListenerError("sudo is required to load the trusted BPF helper")
    return [str(sudo), "-n", "--", *command]


def validate_privileged_helper(helper: Path) -> Path:
    """Require an immutable, root-owned helper bundle before using sudo."""

    requested = helper.absolute()
    try:
        resolved = helper.resolve(strict=True)
    except OSError as exc:
        raise ListenerError(
            "trusted capture helper is not installed; run "
            "install-codex-session-listener-helper first"
        ) from exc
    bundle = resolved.parent
    required = [
        resolved,
        bundle / "bpf" / "codex_session_capture.bpf.o",
    ]
    ancestors: list[Path] = []
    current = bundle
    while current != current.parent:
        ancestors.append(current)
        current = current.parent
    paths = [
        requested,
        *required,
        bundle / "bpf",
        *ancestors,
    ]
    for path in paths:
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ListenerError(f"trusted helper bundle is incomplete: {path}") from exc
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ListenerError(
                f"trusted helper path must be root-owned and not group/world writable: {path}"
            )
        if path in required and not stat.S_ISREG(info.st_mode):
            raise ListenerError(f"trusted helper component is not a regular file: {path}")
        if path == requested and not (
            stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
        ):
            raise ListenerError(f"trusted helper entry is not a file or symlink: {path}")
    return requested


def diagnose(control_socket: Path, output: TextIO) -> int:
    current = discover(control_socket)
    value = {
        "schema": SCHEMA,
        "observed_at": utc_now(),
        "event": "diagnostic.snapshot",
        "control_socket": str(control_socket),
        "server": {
            "pid": current.server_pid,
            "start_ticks": current.server_start_ticks,
            "listener_fd": current.listener_fd,
            "listener_inode": current.listener_inode,
        },
        "connections": [
            {
                "connection_id": connection.key,
                "server_fd": connection.server_fd,
                "client_pid": connection.client_pid,
                "client_start_ticks": connection.client_start_ticks,
                "client_fd": connection.client_fd,
                "tmux": (
                    {
                        "socket": connection.tmux.socket_path,
                        "server_pid": connection.tmux.server_pid,
                        "server_start_ticks": connection.tmux.server_start_ticks,
                        "pane": connection.tmux.pane_id,
                        "pane_pid": connection.tmux.pane_pid,
                        "pane_start_ticks": connection.tmux.pane_start_ticks,
                    }
                    if connection.tmux
                    else None
                ),
            }
            for connection in sorted(
                current.connections.values(), key=lambda item: item.server_fd
            )
        ],
    }
    encode_event(output, value)
    return 0


def listen(
    control_socket: Path,
    output: TextIO,
    helper: Path = TRUSTED_HELPER,
    remembered: Mapping[str, str] | None = None,
) -> int:
    command = helper_command(helper, control_socket)
    initial = discover(control_socket)
    remembered = remembered or {}
    states = {
        fd: ProtocolState(
            connection,
            thread_id=remembered.get(connection.key) or connection.thread_hint,
        )
        for fd, connection in initial.connections.items()
    }
    for state in states.values():
        for event in opening_events(state, remembered.get(state.connection.key)):
            encode_event(output, event)

    helper = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    if helper.stdout is None or helper.stderr is None:
        raise ListenerError("failed to create capture helper pipes")

    resolver = ThreadNameResolver(control_socket)
    resolver_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="codex-session-name"
    )
    resolutions: dict[concurrent.futures.Future[str | None], str] = {}
    known_names: dict[str, str] = {}
    resolution_attempts: dict[str, int] = {}
    retry_at: dict[str, float] = {}
    last_discovery = initial
    pipe_buffers = {
        helper.stdout.fileno(): bytearray(),
        helper.stderr.fileno(): bytearray(),
    }

    def reconcile_topology() -> bool:
        nonlocal last_discovery
        try:
            current = discover(control_socket)
        except ListenerError:
            return True
        if (current.server_pid, current.server_start_ticks) != (
            last_discovery.server_pid,
            last_discovery.server_start_ticks,
        ):
            return False
        removed = set(states) - set(current.connections)
        added = set(current.connections) - set(states)
        replaced = {
            fd
            for fd in set(states) & set(current.connections)
            if states[fd].connection.key != current.connections[fd].key
        }
        for fd in sorted(removed | replaced):
            encode_event(
                output, connection_event("connection.closed", states[fd].connection)
            )
            states.pop(fd, None)
        for fd in sorted(added | replaced):
            # A connection the first discovery missed, or saw without its tmux
            # pane, can still be one a previous listener had bound.
            connection = current.connections[fd]
            generation = (connection.client_pid, connection.client_start_ticks)
            if connection.thread_hint is not None and any(
                (other.connection.client_pid, other.connection.client_start_ticks) == generation
                for other in states.values()
            ):
                # The resume argument names the thread the TUI started on, which
                # its first connection carries. A later one, such as the one its
                # /resume list opens, is on no thread at all.
                connection = dataclasses.replace(connection, thread_hint=None)
            state = ProtocolState(
                connection,
                thread_id=remembered.get(connection.key) or connection.thread_hint,
            )
            states[fd] = state
            for event in opening_events(state, remembered.get(connection.key)):
                encode_event(output, event)
        last_discovery = current
        return True

    def process_capture_record(record: dict[str, Any]) -> bool:
        if record.get("schema") != CAPTURE_SCHEMA:
            return True
        if record.get("kind") == "topology":
            return reconcile_topology()
        if record.get("kind") == "process":
            event = process_event(record)
            if event is not None:
                encode_event(output, event)
            return True
        if record.get("kind") != "protocol":
            return True
        fd = record.get("fd")
        direction = record.get("direction")
        if not isinstance(fd, int) or direction not in {
            "client_to_server",
            "server_to_client",
        }:
            return True
        state = states.get(fd)
        message = record.get("message")
        if state is None or not isinstance(message, dict):
            return True
        before = (state.thread_id, state.thread_name)
        for semantic_event in state.consume(direction, message):
            encode_event(output, semantic_event)
            thread = semantic_event.get("thread")
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            thread_name = thread.get("name") if isinstance(thread, dict) else None
            if (
                semantic_event.get("event") != "thread.name.requested"
                and isinstance(thread_id, str)
                and isinstance(thread_name, str)
            ):
                known_names[thread_id] = thread_name
        if state.thread_id == before[0] and before[1] is not None and state.thread_name is None:
            # A confirmed rename dropped the name so that it is read back; the
            # old one cached here must not stand in for that read.
            known_names.pop(state.thread_id, None)
        return True

    def schedule_name_resolutions() -> None:
        now = time.monotonic()
        pending = set(resolutions.values())
        for state in states.values():
            thread_id = state.thread_id
            if thread_id is None or state.thread_name is not None:
                continue
            known = known_names.get(thread_id)
            if known is not None:
                state.thread_name = known
                encode_event(
                    output,
                    state._event(
                        "thread.name.resolved", thread_id, known, "thread/read-cache"
                    ),
                )
                continue
            if thread_id in pending or retry_at.get(thread_id, 0.0) > now:
                continue
            future = resolver_pool.submit(resolver.resolve, thread_id)
            resolutions[future] = thread_id
            pending.add(thread_id)

    try:
        while True:
            schedule_name_resolutions()
            ready, _, _ = select.select(
                [helper.stdout, helper.stderr], [], [], SS_REFRESH_SECONDS
            )
            for stream in ready:
                descriptor = stream.fileno()
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError as exc:
                    if exc.errno == errno.EINTR:
                        continue
                    raise
                if not chunk:
                    continue
                if stream is helper.stderr:
                    sys.stderr.write(chunk.decode("utf-8", errors="replace"))
                    sys.stderr.flush()
                    continue
                buffer = pipe_buffers[descriptor]
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw_line, _, remainder = buffer.partition(b"\n")
                    buffer[:] = remainder
                    try:
                        record = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    if not process_capture_record(record):
                        return 75

            for future in list(resolutions):
                if not future.done():
                    continue
                thread_id = resolutions.pop(future)
                try:
                    name = future.result()
                except Exception:
                    name = None
                if thread_id in known_names:
                    resolution_attempts.pop(thread_id, None)
                    retry_at.pop(thread_id, None)
                    continue
                if name is None:
                    attempts = resolution_attempts.get(thread_id, 0) + 1
                    resolution_attempts[thread_id] = attempts
                    retry_at[thread_id] = time.monotonic() + min(30.0, 2.0**attempts)
                    continue
                known_names[thread_id] = name
                resolution_attempts.pop(thread_id, None)
                retry_at.pop(thread_id, None)
                for state in states.values():
                    if state.thread_id != thread_id or state.thread_name is not None:
                        continue
                    state.thread_name = name
                    encode_event(
                        output,
                        state._event(
                            "thread.name.resolved", thread_id, name, "thread/read"
                        ),
                    )

            return_code = helper.poll()
            if return_code is not None:
                stderr = helper.stderr.read().decode("utf-8", errors="replace").strip()
                if return_code == 75:
                    return 75
                if return_code != 0:
                    raise ListenerError(
                        f"capture helper exited with {return_code}: {stderr}"
                    )
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        resolver_pool.shutdown(wait=False, cancel_futures=True)
        if helper.poll() is None:
            helper.terminate()
            try:
                helper.wait(timeout=3)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                helper.kill()
                try:
                    helper.wait(timeout=3)
                except KeyboardInterrupt:
                    pass


def default_control_socket() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / DEFAULT_CONTROL_SOCKET


def default_state_file() -> Path:
    return Path.home() / ".local/state/codex-tmux-integration/sessions.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Observe Codex app-server thread bindings as privacy-filtered JSONL."
    )
    parser.add_argument(
        "--control-socket",
        type=Path,
        default=default_control_socket(),
        help="Codex app-server Unix control socket",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=default_state_file(),
        help="atomic current-session registry (metadata only)",
    )
    parser.add_argument(
        "--no-state-file",
        action="store_true",
        help="disable the current-session registry file",
    )
    parser.add_argument(
        "--title-sync-command",
        type=Path,
        default=DEFAULT_TITLE_SYNC,
        help="one-shot tmux title applier",
    )
    parser.add_argument(
        "--no-title-sync",
        action="store_true",
        help="maintain the registry without updating tmux titles",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="print one connection snapshot without loading BPF",
    )
    parser.add_argument(
        "--helper",
        type=Path,
        default=TRUSTED_HELPER,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output: RegistryOutput | None = None
    try:
        if args.diagnose:
            return diagnose(args.control_socket, sys.stdout)

        def stop(_signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        state_file = None if args.no_state_file else args.state_file
        if not args.no_title_sync and state_file is None:
            raise ListenerError("tmux title sync requires the current-session registry")
        consumer = (
            None
            if args.no_title_sync
            else TitleSyncConsumer(args.title_sync_command)
        )
        output = RegistryOutput(sys.stdout, state_file, consumer)
        retry_delay = 0.5
        last_wait_error: str | None = None
        while True:
            try:
                result = listen(
                    args.control_socket, output, args.helper, output.remembered
                )
            except ListenerError as exc:
                message = str(exc)
                if not message.startswith("no app-server is listening on "):
                    raise
                if message != last_wait_error:
                    print(
                        f"codex-session-listener: waiting for app-server: {message}",
                        file=sys.stderr,
                    )
                    last_wait_error = message
                time.sleep(retry_delay)
                retry_delay = min(10.0, retry_delay * 2)
                continue
            if result != 75:
                return result
            output.reset()
            last_wait_error = None
            retry_delay = 0.5
            time.sleep(0.5)
    except ListenerError as exc:
        print(f"codex-session-listener: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if output is not None:
            output.reset()
