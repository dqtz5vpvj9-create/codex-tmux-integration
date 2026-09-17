"""Read-only diagnosis for one Codex app-server thread."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .model import (
    DEFAULT_CONTROL_SOCKET,
    Connection,
    Discovery,
    ListenerError,
    codex_client_kind,
    discover,
    process_start_ticks,
)
from .protocol import AppServerReader


DEFAULT_STATE_FILE = Path.home() / ".local/state/codex-tmux-integration/sessions.json"


def default_control_socket() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return codex_home.expanduser() / DEFAULT_CONTROL_SOCKET


def load_registry(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"connections": {}}
    except (OSError, json.JSONDecodeError) as exc:
        raise ListenerError(f"could not read listener registry {path}: {exc}") from exc
    connections = value.get("connections") if isinstance(value, dict) else None
    return value if isinstance(connections, dict) else {"connections": {}}


def tmux_pane_details(tmux: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(tmux, dict):
        return None
    socket_path = tmux.get("socket")
    pane = tmux.get("pane")
    if not isinstance(socket_path, str) or not isinstance(pane, str):
        return None
    try:
        completed = subprocess.run(
            [
                "tmux",
                "-S",
                socket_path,
                "display-message",
                "-p",
                "-t",
                pane,
                "#{session_name}\t#{window_index}\t#{window_id}\t"
                "#{window_name}\t#{pane_active}",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    fields = completed.stdout.rstrip("\n").split("\t")
    if completed.returncode != 0 or len(fields) != 5:
        return None
    return {
        "session": fields[0],
        "index": fields[1],
        "id": fields[2],
        "name": fields[3],
        "pane_active": fields[4] == "1",
    }


def live_registry_clients(
    registry: dict[str, Any], thread_id: str, discovery: Discovery | None = None
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    live_identities = (
        {
            (
                item.client_pid,
                item.client_start_ticks,
                item.client_fd,
                item.client_kind,
            )
            for item in discovery.connections.values()
        }
        if discovery is not None
        else None
    )
    for connection_id, entry in registry.get("connections", {}).items():
        if not isinstance(entry, dict):
            continue
        thread = entry.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            continue
        client = entry.get("client")
        if not isinstance(client, dict):
            continue
        pid = client.get("pid")
        recorded_start = client.get("start_ticks")
        if not isinstance(pid, int) or not isinstance(recorded_start, int):
            continue
        if process_start_ticks(pid) != recorded_start:
            continue
        actual_kind = codex_client_kind(pid)
        recorded_kind = client.get("kind")
        if actual_kind is None:
            continue
        if isinstance(recorded_kind, str) and recorded_kind != actual_kind:
            continue
        identity = (pid, recorded_start, client.get("fd"), actual_kind)
        if live_identities is not None and identity not in live_identities:
            continue
        tmux = entry.get("tmux") if isinstance(entry.get("tmux"), dict) else None
        match = {
            "connection_id": connection_id,
            "kind": actual_kind,
            "pid": pid,
            "tmux": tmux,
            "observed_at": entry.get("observed_at"),
        }
        window = tmux_pane_details(tmux)
        if window is not None:
            match["window"] = window
        matches.append(match)
    return sorted(matches, key=lambda item: (item["kind"], item["pid"]))


def rollout_writer_fds(server_pid: int, rollout_path: str | None) -> list[int]:
    if not rollout_path:
        return []
    wanted = os.path.realpath(rollout_path)
    directory = Path(f"/proc/{server_pid}/fd")
    try:
        descriptors = list(directory.iterdir())
    except (FileNotFoundError, PermissionError):
        return []
    result: list[int] = []
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if target.endswith(" (deleted)"):
            target = target[: -len(" (deleted)")]
        if os.path.realpath(target) == wanted:
            try:
                result.append(int(descriptor.name))
            except ValueError:
                pass
    return sorted(result)


def connection_summary(connection: Connection) -> dict[str, Any]:
    return {
        "kind": connection.client_kind,
        "pid": connection.client_pid,
        "tmux": (
            {"socket": connection.tmux.socket_path, "pane": connection.tmux.pane_id}
            if connection.tmux
            else None
        ),
    }


def build_report(
    thread: dict[str, Any],
    discovery: Discovery,
    registry: dict[str, Any],
) -> dict[str, Any]:
    thread_id = str(thread.get("id") or thread.get("sessionId") or "")
    clients = live_registry_clients(registry, thread_id, discovery)
    registry_connections = registry.get("connections", {})
    unbound_clients = [
        connection_summary(connection)
        for connection in discovery.connections.values()
        if not (
            isinstance(registry_connections.get(connection.key), dict)
            and isinstance(registry_connections[connection.key].get("thread"), dict)
            and registry_connections[connection.key]["thread"].get("id")
        )
    ]
    status = thread.get("status")
    status_type = status.get("type") if isinstance(status, dict) else "unknown"
    path = thread.get("path") if isinstance(thread.get("path"), str) else None
    writer_fds = rollout_writer_fds(discovery.server_pid, path)

    if clients:
        conclusion = "observed_client"
    elif status_type == "active":
        conclusion = "active_without_observed_client"
    elif writer_fds:
        conclusion = "loaded_without_observed_client"
    else:
        conclusion = "not_loaded"

    return {
        "thread": {
            "id": thread_id,
            "name": thread.get("name"),
            "status": status_type,
            "cwd": thread.get("cwd"),
            "model_provider": thread.get("modelProvider"),
            "source": thread.get("source"),
            "path": path,
            "recency_at": thread.get("recencyAt"),
        },
        "app_server": {
            "pid": discovery.server_pid,
            "writer_fds": writer_fds,
        },
        "clients": clients,
        "unbound_live_clients": unbound_clients,
        "conclusion": conclusion,
    }


def local_time(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )


def client_label(client: dict[str, Any]) -> str:
    if client["kind"] == "remote_proxy":
        return "远程客户端（app-server proxy，通常是 Codex Desktop）"
    tmux = client.get("tmux")
    if isinstance(tmux, dict):
        pane = tmux.get("pane", "?")
        window = client.get("window")
        if isinstance(window, dict):
            activity = "当前活动 pane" if window.get("pane_active") else "非活动 pane"
            return (
                f"终端 TUI（tmux {pane}；窗口 {window.get('session', '?')}:"
                f"{window.get('index', '?')} {window.get('id', '?')}；{activity}；"
                f"窗口标题 {window.get('name', '?')}）"
            )
        return f"终端 TUI（tmux {pane}）"
    return "终端 TUI"


def render_human(report: dict[str, Any]) -> str:
    thread = report["thread"]
    server = report["app_server"]
    lines = [
        f"会话：{thread['name'] or '未命名'} ({thread['id']})",
        f"状态：{thread['status']}",
        f"目录：{thread['cwd'] or '未知'}",
        f"模型提供方：{thread['model_provider'] or '未知'}",
    ]
    recent = local_time(thread.get("recency_at"))
    if recent:
        lines.append(f"最近活动：{recent}")
    writer_fds = server["writer_fds"]
    if writer_fds:
        joined = ",".join(str(item) for item in writer_fds)
        lines.append(f"写入者：中央 app-server PID {server['pid']}（FD {joined}）")
    else:
        lines.append(f"写入者：未发现 app-server PID {server['pid']} 持有会话文件")

    lines.append("当前客户端：")
    if report["clients"]:
        for client in report["clients"]:
            lines.append(f"  - {client_label(client)}，PID {client['pid']}")
    else:
        lines.append("  - 监听器尚未观察到与该会话绑定的客户端")

    conclusion = report["conclusion"]
    if conclusion == "observed_client":
        detail = "已确认会话当前由上列客户端连接。"
    elif conclusion == "active_without_observed_client":
        detail = "会话仍在执行，但当前客户端身份尚未被监听器观察到。"
    elif conclusion == "loaded_without_observed_client":
        detail = "会话已加载且写入器仍存在；可能处于空闲保留期，或客户端连接早于监听器记录。"
    else:
        detail = "会话当前未加载，未发现活动写入器。"
    lines.append(f"判断：{detail}")

    remote_unbound = [
        item for item in report["unbound_live_clients"] if item["kind"] == "remote_proxy"
    ]
    if not report["clients"] and remote_unbound:
        pids = ", ".join(str(item["pid"]) for item in remote_unbound)
        lines.append(
            f"补充：当前存在尚未绑定到具体会话的远程 proxy：PID {pids}；不能据此猜测它们占用本会话。"
        )
    return "\n".join(lines)


def compact_client_label(report: dict[str, Any]) -> str:
    labels: list[str] = []
    for client in report["clients"]:
        if client["kind"] == "remote_proxy":
            label = "remote"
        else:
            tmux = client.get("tmux")
            pane = tmux.get("pane") if isinstance(tmux, dict) else None
            label = f"tmux:{pane}" if pane else "TUI"
        if label not in labels:
            labels.append(label)
    return ",".join(labels) if labels else "-"


def render_list(reports: list[dict[str, Any]]) -> str:
    if not reports:
        return "当前没有已加载的 Codex 会话。"
    status_order = {"active": 0, "idle": 1, "notLoaded": 2}
    ordered = sorted(
        reports,
        key=lambda report: (
            status_order.get(report["thread"]["status"], 9),
            -(report["thread"].get("recency_at") or 0),
        ),
    )
    lines = [f"当前已加载 {len(ordered)} 个 Codex 会话："]
    for index, report in enumerate(ordered, start=1):
        thread = report["thread"]
        lines.extend(
            [
                f"\n{index}. [{thread['status']}] {thread.get('name') or '（未命名）'}",
                f"   客户端：{compact_client_label(report)}  目录：{thread.get('cwd') or '-'}",
                f"   ID：{thread['id']}",
            ]
        )
    lines.append("\n指定会话 ID 可查看完整诊断。")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读诊断一个 Codex 会话当前由谁连接、是否仍有写入器。"
    )
    parser.add_argument("thread_id", nargs="?", help="Codex 会话 UUID；省略时列出当前已加载会话")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--control-socket", type=Path, default=default_control_socket())
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        reader = AppServerReader(args.control_socket)
        discovery = discover(args.control_socket)
        registry = load_registry(args.state_file)
        if args.thread_id:
            report = build_report(
                reader.read_thread(args.thread_id), discovery, registry
            )
            output: dict[str, Any] | list[dict[str, Any]] = report
        else:
            reports = [
                build_report(reader.read_thread(thread_id), discovery, registry)
                for thread_id in reader.list_loaded_thread_ids()
            ]
            output = reports
    except ListenerError as exc:
        print(f"诊断失败：{exc}")
        return 2
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    elif args.thread_id:
        print(render_human(report))
    else:
        print(render_list(reports))
    return 0
