"""Privileged, metadata-only eBPF capture helper."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import json
import os
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path
from typing import Any

from .model import (
    CAPTURE_SCHEMA,
    MAX_CAPTURE_BYTES,
    SS_REFRESH_SECONDS,
    Discovery,
    ListenerError,
    discover,
    process_descends_from,
    process_fd_socket_inode,
    process_start_ticks,
)
from .protocol import (
    WebSocketDecoder,
    decode_json_message,
    sanitize_protocol_message,
    sanitize_protocol_prefix,
    websocket_payload_prefix,
)


class CaptureEvent(ctypes.Structure):
    _fields_ = [
        ("timestamp_ns", ctypes.c_uint64),
        ("drop_generation", ctypes.c_uint64),
        ("pid", ctypes.c_uint32),
        ("tid", ctypes.c_uint32),
        ("fd", ctypes.c_int32),
        ("direction", ctypes.c_uint8),
        ("original_len", ctypes.c_uint32),
        ("captured_len", ctypes.c_uint32),
        ("data", ctypes.c_ubyte * MAX_CAPTURE_BYTES),
    ]


class LibbpfCaptureBackend:
    """Load a precompiled BPF object without retaining Clang in memory."""

    _SAMPLE_CALLBACK = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t
    )

    def __init__(self, object_path: Path, callback: Any, gap_callback: Any):
        library_name = ctypes.util.find_library("bpf") or "libbpf.so.1"
        self.lib = ctypes.CDLL(library_name, use_errno=True)
        self._configure_prototypes()
        self.obj = self.lib.bpf_object__open_file(os.fsencode(object_path), None)
        self._check_pointer(self.obj, "open BPF object")
        self.links: list[int] = []
        self.ring_buffer: int | None = None
        self.current_fds: set[int] = set()
        self.last_drop_generation: int | None = None
        try:
            result = self.lib.bpf_object__load(self.obj)
            if result != 0:
                raise ListenerError(f"load BPF object failed: {os.strerror(-result)}")
            previous = None
            while True:
                program = self.lib.bpf_object__next_program(self.obj, previous)
                if not program:
                    break
                link = self.lib.bpf_program__attach(program)
                self._check_pointer(link, "attach BPF tracepoint")
                self.links.append(link)
                previous = program
            self.watched_map_fd = self.lib.bpf_object__find_map_fd_by_name(
                self.obj, b"watched_fds"
            )
            events_fd = self.lib.bpf_object__find_map_fd_by_name(self.obj, b"events")
            if self.watched_map_fd < 0 or events_fd < 0:
                raise ListenerError("required BPF maps are missing")

            def on_sample(_context: Any, data: Any, size: int) -> int:
                if size < ctypes.sizeof(CaptureEvent):
                    return 0
                event = ctypes.cast(data, ctypes.POINTER(CaptureEvent)).contents
                generation = int(event.drop_generation)
                if self.last_drop_generation is None:
                    if generation:
                        gap_callback(generation)
                elif generation != self.last_drop_generation:
                    gap_callback(generation - self.last_drop_generation)
                self.last_drop_generation = generation
                callback(event)
                return 0

            self._sample_callback = self._SAMPLE_CALLBACK(on_sample)
            self.ring_buffer = self.lib.ring_buffer__new(
                events_fd,
                self._sample_callback,
                None,
                None,
            )
            self._check_pointer(self.ring_buffer, "create BPF ring buffer")
        except Exception:
            self.close()
            raise

    def _configure_prototypes(self) -> None:
        lib = self.lib
        lib.bpf_object__open_file.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        lib.bpf_object__open_file.restype = ctypes.c_void_p
        lib.libbpf_get_error.argtypes = [ctypes.c_void_p]
        lib.libbpf_get_error.restype = ctypes.c_long
        lib.bpf_object__load.argtypes = [ctypes.c_void_p]
        lib.bpf_object__load.restype = ctypes.c_int
        lib.bpf_object__next_program.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.bpf_object__next_program.restype = ctypes.c_void_p
        lib.bpf_program__attach.argtypes = [ctypes.c_void_p]
        lib.bpf_program__attach.restype = ctypes.c_void_p
        lib.bpf_link__destroy.argtypes = [ctypes.c_void_p]
        lib.bpf_link__destroy.restype = ctypes.c_int
        lib.bpf_object__find_map_fd_by_name.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.bpf_object__find_map_fd_by_name.restype = ctypes.c_int
        lib.bpf_map_update_elem.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint64,
        ]
        lib.bpf_map_update_elem.restype = ctypes.c_int
        lib.bpf_map_delete_elem.argtypes = [ctypes.c_int, ctypes.c_void_p]
        lib.bpf_map_delete_elem.restype = ctypes.c_int
        lib.ring_buffer__new.argtypes = [
            ctypes.c_int,
            self._SAMPLE_CALLBACK,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        lib.ring_buffer__new.restype = ctypes.c_void_p
        lib.ring_buffer__poll.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.ring_buffer__poll.restype = ctypes.c_int
        lib.ring_buffer__free.argtypes = [ctypes.c_void_p]
        lib.bpf_object__close.argtypes = [ctypes.c_void_p]

    def _check_pointer(self, pointer: Any, action: str) -> None:
        error = self.lib.libbpf_get_error(pointer)
        if error:
            raise ListenerError(f"{action} failed: {os.strerror(-error)}")

    def update(self, discovery: Discovery) -> None:
        wanted = {
            (discovery.server_pid << 32) | connection.server_fd
            for connection in discovery.connections.values()
        }
        for value in self.current_fds - wanted:
            key = ctypes.c_uint64(value)
            result = self.lib.bpf_map_delete_elem(
                self.watched_map_fd, ctypes.byref(key)
            )
            if result not in (0, -errno.ENOENT):
                error = -result if result < 0 else ctypes.get_errno()
                raise ListenerError(
                    f"delete watched BPF fd failed: {os.strerror(error)}"
                )
        for value in wanted - self.current_fds:
            key = ctypes.c_uint64(value)
            leaf = ctypes.c_uint8(1)
            result = self.lib.bpf_map_update_elem(
                self.watched_map_fd, ctypes.byref(key), ctypes.byref(leaf), 0
            )
            if result != 0:
                error = -result if result < 0 else ctypes.get_errno()
                raise ListenerError(f"update watched BPF fd failed: {os.strerror(error)}")
        self.current_fds = wanted

    def poll(self, timeout_ms: int) -> None:
        result = self.lib.ring_buffer__poll(self.ring_buffer, timeout_ms)
        if result < 0 and result != -errno.EINTR:
            raise ListenerError(f"poll BPF buffer failed: {os.strerror(-result)}")

    def close(self) -> None:
        if getattr(self, "ring_buffer", None):
            self.lib.ring_buffer__free(self.ring_buffer)
            self.ring_buffer = None
        for link in reversed(getattr(self, "links", [])):
            self.lib.bpf_link__destroy(link)
        self.links = []
        if getattr(self, "obj", None):
            self.lib.bpf_object__close(self.obj)
            self.obj = None


def build_bpf_object() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    clang_path = Path("/usr/bin/clang")
    clang = str(clang_path) if clang_path.is_file() else None
    source = Path(__file__).resolve().parents[1] / "bpf" / "codex_session_capture.bpf.c"
    if clang is None:
        raise ListenerError("clang is required to build the BPF capture program")
    if not source.is_file():
        raise ListenerError(f"BPF source is missing: {source}")
    if ctypes.util.find_library("bpf") is None:
        raise ListenerError("libbpf is required by the capture helper")
    temporary = tempfile.TemporaryDirectory(prefix="codex-session-listener-")
    output = Path(temporary.name) / "codex_session_capture.bpf.o"
    command = [clang, "-target", "bpf", "-O2", "-g"]
    multiarch = sysconfig.get_config_var("MULTIARCH")
    if isinstance(multiarch, str) and Path("/usr/include", multiarch).is_dir():
        command.append(f"-I/usr/include/{multiarch}")
    command.extend(["-c", str(source), "-o", str(output)])
    completed = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if completed.returncode != 0:
        temporary.cleanup()
        detail = completed.stderr.strip().splitlines()
        summary = detail[-1] if detail else "unknown compiler error"
        raise ListenerError(f"BPF compilation failed: {summary}")
    return temporary, output


def drop_sudo_privileges() -> bool:
    """Drop the helper back to the invoking user after BPF attachment."""

    uid_value = os.environ.get("SUDO_UID")
    gid_value = os.environ.get("SUDO_GID")
    if os.geteuid() != 0:
        return False
    if uid_value is None or gid_value is None:
        raise ListenerError(
            "capture helper requires SUDO_UID and SUDO_GID for mandatory privilege drop"
        )
    try:
        uid = int(uid_value)
        gid = int(gid_value)
    except ValueError as exc:
        raise ListenerError("invalid SUDO_UID or SUDO_GID") from exc
    if uid <= 0 or gid <= 0:
        raise ListenerError("refusing to retain root as the runtime listener user")
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    return True


def installed_bpf_object() -> Path:
    return Path(__file__).resolve().parents[1] / "bpf" / "codex_session_capture.bpf.o"


def capture_helper(control_socket: Path) -> int:
    if os.geteuid() != 0:
        raise ListenerError("capture helper must run as root")
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_refresh = 0.0
    active_identity: tuple[int, int] | None = None
    topology_signature: tuple[tuple[Any, ...], ...] | None = None
    missing_refreshes = 0
    decoders: dict[tuple[int, str], WebSocketDecoder] = {}
    last_threads: dict[int, str] = {}
    live_connections: dict[int, Any] = {}
    backend: LibbpfCaptureBackend

    def refresh() -> None:
        nonlocal active_identity, topology_signature, missing_refreshes, live_connections
        try:
            current = discover(control_socket)
        except ListenerError:
            missing_refreshes += 1
            if active_identity is not None and missing_refreshes >= 3:
                raise SystemExit(75)
            return
        missing_refreshes = 0
        identity = (current.server_pid, current.server_start_ticks)
        if active_identity is not None and identity != active_identity:
            raise SystemExit(75)
        active_identity = identity
        backend.update(current)
        live_connections = dict(current.connections)
        signature = tuple(
            sorted(
                (
                    connection.server_fd,
                    connection.server_inode,
                    connection.client_pid,
                    connection.client_start_ticks,
                    connection.client_fd,
                    connection.client_inode,
                    connection.tmux.server_pid if connection.tmux else 0,
                    connection.tmux.server_start_ticks if connection.tmux else 0,
                    connection.tmux.pane_id if connection.tmux else "",
                    connection.tmux.pane_pid if connection.tmux else 0,
                    connection.tmux.pane_start_ticks if connection.tmux else 0,
                )
                for connection in current.connections.values()
            )
        )
        if signature == topology_signature:
            return
        previous_fds = (
            {item[0] for item in topology_signature}
            if topology_signature is not None
            else set()
        )
        current_fds = {item[0] for item in signature}
        for fd in previous_fds - current_fds:
            decoders.pop((fd, "client_to_server"), None)
            decoders.pop((fd, "server_to_client"), None)
            last_threads.pop(fd, None)
        topology_signature = signature
        _write_record(
            {
                "schema": CAPTURE_SCHEMA,
                "kind": "topology",
                "server_pid": current.server_pid,
                "server_start_ticks": current.server_start_ticks,
                "signature": signature,
            }
        )

    def on_event(event: CaptureEvent) -> None:
        nonlocal stopping
        captured = bytes(event.data[: event.captured_len])
        fd = int(event.fd)
        connection = live_connections.get(fd)
        if (
            int(event.pid) != (active_identity[0] if active_identity else -1)
            or connection is None
            or process_fd_socket_inode(int(event.pid), fd) != connection.server_inode
            or process_start_ticks(connection.client_pid)
            != connection.client_start_ticks
        ):
            decoders.pop((fd, "client_to_server"), None)
            decoders.pop((fd, "server_to_client"), None)
            last_threads.pop(fd, None)
            return
        direction = "server_to_client" if event.direction == 1 else "client_to_server"
        decoder = decoders.setdefault(
            (fd, direction),
            WebSocketDecoder(expect_masked=direction == "client_to_server"),
        )
        messages: list[dict[str, Any]] = []
        omitted = int(event.original_len) - int(event.captured_len)
        if omitted:
            prefix = websocket_payload_prefix(
                captured, masked=direction == "client_to_server"
            )
            if prefix is not None:
                sanitized, latest = sanitize_protocol_prefix(
                    prefix, last_thread_id=last_threads.get(fd)
                )
                if latest is not None:
                    last_threads[fd] = latest
                if sanitized is not None:
                    messages.append(sanitized)
            decoder.feed(captured, omitted)
        else:
            for payload in decoder.feed(captured):
                message = decode_json_message(payload)
                if message is None:
                    continue
                sanitized, latest = sanitize_protocol_message(
                    message, last_thread_id=last_threads.get(fd)
                )
                if latest is not None:
                    last_threads[fd] = latest
                if sanitized is not None:
                    messages.append(sanitized)
        for message in messages:
            tmux = connection.tmux
            if tmux is not None and (
                process_start_ticks(tmux.server_pid) != tmux.server_start_ticks
                or process_start_ticks(tmux.pane_pid) != tmux.pane_start_ticks
                or not process_descends_from(connection.client_pid, tmux.pane_pid)
            ):
                continue
            try:
                _write_record(
                    {
                        "schema": CAPTURE_SCHEMA,
                        "kind": "protocol",
                        "pid": int(event.pid),
                        "fd": fd,
                        "direction": direction,
                        "message": message,
                    }
                )
            except BrokenPipeError:
                stopping = True
                return

    def on_gap(count: int) -> None:
        decoders.clear()
        last_threads.clear()
        print(
            f"codex-session-listener: discarded stream state after {count} lost events",
            file=sys.stderr,
        )

    bpf_object = installed_bpf_object()
    if not bpf_object.is_file():
        raise ListenerError(f"installed BPF object is missing: {bpf_object}")
    backend = LibbpfCaptureBackend(bpf_object, on_event, on_gap)
    drop_sudo_privileges()
    try:
        while not stopping:
            now = time.monotonic()
            if now - last_refresh >= SS_REFRESH_SECONDS:
                refresh()
                last_refresh = now
            try:
                backend.poll(100)
            except KeyboardInterrupt:
                break
        return 0
    finally:
        backend.close()


def _write_record(record: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(record, separators=(",", ":")) + "\n")
    sys.stdout.flush()
