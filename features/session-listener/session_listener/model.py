"""Process, socket, and tmux identity discovery."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import os
import re
import subprocess
from pathlib import Path

SCHEMA = "codex.session-listener.v1"
CAPTURE_SCHEMA = "codex.session-capture.v1"
DEFAULT_CONTROL_SOCKET = "app-server-control/app-server-control.sock"
MAX_CAPTURE_BYTES = 4096
MAX_MESSAGE_BYTES = 1024 * 1024
SS_REFRESH_SECONDS = 2.0
THREAD_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class ListenerError(RuntimeError):
    """A user-facing listener failure."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def process_start_ticks(pid: int) -> int | None:
    """Return Linux /proc start ticks, guarding against PID reuse.

    Every reader here takes any OSError as "no such process": one that exits
    between open() and read() fails the read with ESRCH, not ENOENT.
    """

    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        suffix = value[value.rfind(")") + 2 :].split()
        return int(suffix[19])
    except (OSError, IndexError, ValueError):
        return None


def process_parent_pid(pid: int) -> int | None:
    """Read PPID without being confused by spaces in ``comm``."""

    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        suffix = value[value.rfind(")") + 2 :].split()
        return int(suffix[1])
    except (OSError, IndexError, ValueError):
        return None


def process_descends_from(pid: int, ancestor_pid: int) -> bool:
    current = pid
    seen: set[int] = set()
    for _ in range(128):
        if current == ancestor_pid:
            return True
        if current <= 1 or current in seen:
            return False
        seen.add(current)
        parent = process_parent_pid(current)
        if parent is None:
            return False
        current = parent
    return False


def process_fd_socket_inode(pid: int, fd: int) -> int | None:
    """Return the live socket inode behind one process descriptor."""

    try:
        target = os.readlink(f"/proc/{pid}/fd/{fd}")
    except (FileNotFoundError, PermissionError, OSError):
        return None
    match = re.fullmatch(r"socket:\[(\d+)\]", target)
    return int(match.group(1)) if match else None


def read_process_environment(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        result[key.decode(errors="replace")] = value.decode(errors="replace")
    return result


def read_process_command(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [item.decode(errors="replace") for item in raw.split(b"\0") if item]


def codex_client_kind(pid: int) -> str | None:
    """Classify trusted native clients of the shared app-server socket."""

    command = read_process_command(pid)
    if not command or Path(command[0]).name != "codex":
        return None
    arguments = command[1:]
    if "app-server-control" in arguments:
        return None
    try:
        app_server_index = arguments.index("app-server")
    except ValueError:
        return "tui"
    if arguments[app_server_index + 1 : app_server_index + 2] == ["proxy"]:
        return "remote_proxy"
    return None


def is_codex_tui_process(pid: int) -> bool:
    return codex_client_kind(pid) == "tui"


def is_codex_app_server_proxy_process(pid: int) -> bool:
    return codex_client_kind(pid) == "remote_proxy"


def resumed_thread_id(pid: int) -> str | None:
    """Return only an explicit ``codex resume ... UUID`` identity hint."""

    command = read_process_command(pid)
    try:
        resume_index = command.index("resume")
    except ValueError:
        return None
    for argument in command[resume_index + 1 :]:
        if THREAD_ID_RE.fullmatch(argument):
            return argument.lower()
    return None


def is_codex_app_server_process(pid: int) -> bool:
    command = read_process_command(pid)
    return bool(
        command
        and Path(command[0]).name == "codex"
        and "app-server" in command[1:]
    )


@dataclasses.dataclass(frozen=True)
class TmuxCandidate:
    socket_path: str
    pane_id: str
    advertised_server_pid: int


def parse_tmux_environment(environment: dict[str, str]) -> TmuxCandidate | None:
    pane = environment.get("TMUX_PANE", "")
    tmux = environment.get("TMUX", "")
    if not pane.startswith("%") or not tmux:
        return None
    fields = tmux.rsplit(",", 2)
    socket_path = fields[0]
    if not socket_path.startswith("/"):
        return None
    try:
        server_pid = int(fields[1])
    except (IndexError, ValueError):
        return None
    if server_pid <= 1:
        return None
    return TmuxCandidate(socket_path, pane, server_pid)


@dataclasses.dataclass(frozen=True)
class SocketEndpoint:
    state: str
    path: str | None
    local_inode: int
    peer_inode: int
    pid: int
    fd: int


_PROCESS_RE = re.compile(r"pid=(?P<pid>\d+),fd=(?P<fd>\d+)")


def parse_ss_line(line: str) -> list[SocketEndpoint]:
    fields = line.split()
    if len(fields) < 8 or not fields[0].startswith("u_"):
        return []
    try:
        local_inode = int(fields[5])
        peer_inode = int(fields[7])
    except ValueError:
        return []
    path = None if fields[4] == "*" else fields[4]
    return [
        SocketEndpoint(
            state=fields[1],
            path=path,
            local_inode=local_inode,
            peer_inode=peer_inode,
            pid=int(match.group("pid")),
            fd=int(match.group("fd")),
        )
        for match in _PROCESS_RE.finditer(line)
    ]


def socket_endpoints() -> list[SocketEndpoint]:
    try:
        completed = subprocess.run(
            ["/usr/bin/ss", "-xapnH"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2,
        )
    except FileNotFoundError as exc:
        raise ListenerError("ss is required for socket ownership discovery") from exc
    except subprocess.TimeoutExpired as exc:
        raise ListenerError("ss timed out during socket ownership discovery") from exc
    if completed.returncode != 0:
        raise ListenerError(f"ss failed: {completed.stderr.strip()}")
    return [
        endpoint
        for line in completed.stdout.splitlines()
        for endpoint in parse_ss_line(line)
    ]


@dataclasses.dataclass(frozen=True)
class TmuxIdentity:
    socket_path: str
    pane_id: str
    server_pid: int
    server_start_ticks: int
    pane_pid: int
    pane_start_ticks: int


def tmux_inventory(socket_path: str) -> dict[str, TmuxIdentity]:
    """Ask the addressed tmux server for its real generation and panes."""

    try:
        completed = subprocess.run(
            [
                "/usr/bin/tmux",
                "-S",
                socket_path,
                "list-panes",
                "-a",
                "-F",
                "#{pid}\t#{pane_id}\t#{pane_pid}",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}
    result: dict[str, TmuxIdentity] = {}
    for line in completed.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        try:
            server_pid = int(fields[0])
            pane_pid = int(fields[2])
        except ValueError:
            continue
        server_start = process_start_ticks(server_pid)
        pane_start = process_start_ticks(pane_pid)
        if server_start is None or pane_start is None:
            continue
        result[fields[1]] = TmuxIdentity(
            socket_path=socket_path,
            pane_id=fields[1],
            server_pid=server_pid,
            server_start_ticks=server_start,
            pane_pid=pane_pid,
            pane_start_ticks=pane_start,
        )
    return result


@dataclasses.dataclass(frozen=True)
class Connection:
    server_pid: int
    server_start_ticks: int
    server_fd: int
    server_inode: int
    client_pid: int
    client_start_ticks: int
    client_fd: int
    client_inode: int
    tmux: TmuxIdentity | None
    client_kind: str = "tui"
    thread_hint: str | None = None

    @property
    def key(self) -> str:
        material = (
            f"{self.server_pid}:{self.server_start_ticks}:{self.server_fd}:"
            f"{self.server_inode}:{self.client_pid}:{self.client_start_ticks}:"
            f"{self.client_fd}:{self.client_inode}:"
            f"{self.client_kind}:"
            f"{self.thread_hint or ''}:"
            f"{self.tmux.server_pid if self.tmux else 0}:"
            f"{self.tmux.server_start_ticks if self.tmux else 0}:"
            f"{self.tmux.pane_id if self.tmux else ''}"
        )
        return hashlib.sha256(material.encode()).hexdigest()[:20]


@dataclasses.dataclass(frozen=True)
class Discovery:
    server_pid: int
    server_start_ticks: int
    listener_fd: int
    listener_inode: int
    connections: dict[int, Connection]


def discover(control_socket: Path) -> Discovery:
    target = str(control_socket.resolve(strict=False))
    endpoints = socket_endpoints()
    listeners = [
        endpoint
        for endpoint in endpoints
        if endpoint.state == "LISTEN" and endpoint.path == target
    ]
    identities = {(item.pid, item.fd, item.local_inode) for item in listeners}
    if not identities:
        raise ListenerError(f"no app-server is listening on {target}")
    if len(identities) != 1:
        raise ListenerError(f"multiple app-servers own {target}: {sorted(identities)}")
    server_pid, listener_fd, listener_inode = next(iter(identities))
    if not is_codex_app_server_process(server_pid):
        raise ListenerError(
            f"control socket listener {server_pid} is not a native Codex app-server"
        )
    server_start = process_start_ticks(server_pid)
    if server_start is None:
        raise ListenerError(f"app-server process {server_pid} disappeared")

    reverse: dict[tuple[int, int], list[SocketEndpoint]] = {}
    for endpoint in endpoints:
        reverse.setdefault((endpoint.local_inode, endpoint.peer_inode), []).append(
            endpoint
        )

    pending_connections: list[
        tuple[SocketEndpoint, int, int, int, str, TmuxCandidate | None]
    ] = []
    server_sides = [
        item
        for item in endpoints
        if item.pid == server_pid
        and item.path == target
        and item.state == "ESTAB"
        and item.fd != listener_fd
    ]
    for server_side in server_sides:
        peers = [
            item
            for item in reverse.get(
                (server_side.peer_inode, server_side.local_inode), []
            )
            if item.pid != server_pid
        ]
        peer_identities = {(item.pid, item.fd) for item in peers}
        if len(peer_identities) != 1:
            continue
        client_pid, client_fd = next(iter(peer_identities))
        client_kind = codex_client_kind(client_pid)
        if client_kind is None:
            continue
        client_start = process_start_ticks(client_pid)
        if client_start is None:
            continue
        tmux_candidate = (
            parse_tmux_environment(read_process_environment(client_pid))
            if client_kind == "tui"
            else None
        )
        pending_connections.append(
            (
                server_side,
                client_pid,
                client_fd,
                client_start,
                client_kind,
                tmux_candidate,
            )
        )

    inventories = {
        candidate.socket_path: tmux_inventory(candidate.socket_path)
        for candidate in {
            item[5] for item in pending_connections if item[5] is not None
        }
    }
    connections: dict[int, Connection] = {}
    for (
        server_side,
        client_pid,
        client_fd,
        client_start,
        client_kind,
        candidate,
    ) in pending_connections:
        tmux_identity = None
        if candidate is not None:
            identity = inventories.get(candidate.socket_path, {}).get(candidate.pane_id)
            if (
                identity is not None
                and identity.server_pid == candidate.advertised_server_pid
                and process_descends_from(client_pid, identity.pane_pid)
            ):
                tmux_identity = identity
        connections[server_side.fd] = Connection(
            server_pid=server_pid,
            server_start_ticks=server_start,
            server_fd=server_side.fd,
            server_inode=server_side.local_inode,
            client_pid=client_pid,
            client_start_ticks=client_start,
            client_fd=client_fd,
            client_inode=server_side.peer_inode,
            tmux=tmux_identity,
            client_kind=client_kind,
            thread_hint=(resumed_thread_id(client_pid) if client_kind == "tui" else None),
        )
    return Discovery(
        server_pid=server_pid,
        server_start_ticks=server_start,
        listener_fd=listener_fd,
        listener_inode=listener_inode,
        connections=connections,
    )
