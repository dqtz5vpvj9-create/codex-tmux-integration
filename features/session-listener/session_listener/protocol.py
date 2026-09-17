"""WebSocket decoding and privacy-filtered Codex session semantics."""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import re
import socket
import struct
from pathlib import Path
from typing import Any

from .model import Connection, ListenerError, MAX_MESSAGE_BYTES, SCHEMA, utc_now


class WebSocketDecoder:
    """Incremental WebSocket text-frame decoder with bounded buffering."""

    def __init__(self, *, expect_masked: bool, max_message: int = MAX_MESSAGE_BYTES):
        self.expect_masked = expect_masked
        self.max_message = max_message
        self.buffer = bytearray()
        self.handshake_complete = False
        self.fragment_opcode: int | None = None
        self.fragment_data = bytearray()
        self.desynchronized = False

    def feed(self, data: bytes, omitted: int = 0) -> list[bytes]:
        if self.desynchronized:
            return self._attempt_resync(data)
        self.buffer.extend(data)
        messages = self._drain()
        if omitted:
            self.buffer.clear()
            self.fragment_opcode = None
            self.fragment_data.clear()
            self.desynchronized = True
        return messages

    def _attempt_resync(self, data: bytes) -> list[bytes]:
        self.buffer = bytearray(data)
        self.handshake_complete = True
        self.desynchronized = False
        messages = self._drain()
        if self.buffer:
            self.buffer.clear()
            self.desynchronized = True
        return messages

    def _consume_handshake(self) -> bool:
        if self.handshake_complete:
            return True
        if self.buffer.startswith((b"GET ", b"HTTP/1.1 ")):
            boundary = self.buffer.find(b"\r\n\r\n")
            if boundary < 0:
                return False
            del self.buffer[: boundary + 4]
        self.handshake_complete = True
        return True

    def _drain(self) -> list[bytes]:
        messages: list[bytes] = []
        if not self._consume_handshake():
            return messages
        while True:
            parsed = self._next_frame()
            if parsed is None:
                break
            fin, opcode, payload = parsed
            if opcode in (0x8, 0x9, 0xA):
                continue
            if opcode in (0x1, 0x2):
                self.fragment_opcode = opcode
                self.fragment_data = bytearray(payload)
            elif opcode == 0x0 and self.fragment_opcode is not None:
                self.fragment_data.extend(payload)
            else:
                self.fragment_opcode = None
                self.fragment_data.clear()
                continue
            if len(self.fragment_data) > self.max_message:
                self.fragment_opcode = None
                self.fragment_data.clear()
                continue
            if fin:
                if self.fragment_opcode == 0x1:
                    messages.append(bytes(self.fragment_data))
                self.fragment_opcode = None
                self.fragment_data.clear()
        return messages

    def _next_frame(self) -> tuple[bool, int, bytes] | None:
        if len(self.buffer) < 2:
            return None
        first, second = self.buffer[0], self.buffer[1]
        if first & 0x70:
            self._desync()
            return None
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        if opcode not in (0x0, 0x1, 0x2, 0x8, 0x9, 0xA):
            self._desync()
            return None
        masked = bool(second & 0x80)
        if masked != self.expect_masked:
            self._desync()
            return None
        length = second & 0x7F
        offset = 2
        if length == 126:
            if len(self.buffer) < 4:
                return None
            length = struct.unpack("!H", self.buffer[2:4])[0]
            offset = 4
        elif length == 127:
            if len(self.buffer) < 10:
                return None
            length = struct.unpack("!Q", self.buffer[2:10])[0]
            offset = 10
        if masked:
            if len(self.buffer) < offset + 4:
                return None
            mask = bytes(self.buffer[offset : offset + 4])
            offset += 4
        else:
            mask = b""
        frame_end = offset + length
        if len(self.buffer) < frame_end:
            return None
        payload = bytes(self.buffer[offset:frame_end])
        del self.buffer[:frame_end]
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return fin, opcode, payload

    def _desync(self) -> None:
        self.desynchronized = True
        self.buffer.clear()


def websocket_frame(payload: bytes, *, masked: bool, opcode: int = 0x1) -> bytes:
    first = 0x80 | opcode
    length = len(payload)
    mask_bit = 0x80 if masked else 0
    if length < 126:
        header = bytes((first, mask_bit | length))
    elif length < 65536:
        header = bytes((first, mask_bit | 126)) + struct.pack("!H", length)
    else:
        header = bytes((first, mask_bit | 127)) + struct.pack("!Q", length)
    if not masked:
        return header + payload
    mask = os.urandom(4)
    encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return header + mask + encoded


class AppServerReader:
    """Read thread metadata without resuming or subscribing to the thread."""

    def __init__(self, control_socket: Path, timeout: float = 3.0):
        self.control_socket = control_socket
        self.timeout = timeout

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(self.timeout)
        try:
            client.connect(str(self.control_socket))
            self._upgrade(client)
            decoder = WebSocketDecoder(expect_masked=False)
            self._send(
                client,
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {
                            "name": "codex_session_reader",
                            "title": "Codex Session Reader",
                            "version": "1",
                        },
                        "capabilities": {
                            "optOutNotificationMethods": [
                                "thread/started",
                                "item/agentMessage/delta",
                            ]
                        },
                    },
                },
            )
            self._wait_for_response(client, decoder, 1)
            self._send(client, {"method": "initialized", "params": {}})
            self._send(
                client,
                {
                    "method": method,
                    "id": 2,
                    "params": params,
                },
            )
            response = self._wait_for_response(client, decoder, 2)
            if "error" in response:
                error = response.get("error")
                message = nested_string(error, ("message",)) or f"{method} failed"
                raise ListenerError(message)
            result = response.get("result")
            if not isinstance(result, dict):
                raise ListenerError(f"{method} returned no result")
            return result
        except (OSError, TimeoutError) as exc:
            raise ListenerError(f"could not read app-server thread: {exc}") from exc
        finally:
            client.close()

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        result = self._request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise ListenerError("thread/read returned no thread metadata")
        return thread

    def list_loaded_thread_ids(self) -> list[str]:
        result = self._request("thread/loaded/list", {})
        data = result.get("data")
        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise ListenerError("thread/loaded/list returned invalid data")
        return data

    def _upgrade(self, client: socket.socket) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        client.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = client.recv(4096)
            if not chunk:
                raise ListenerError("app-server closed during WebSocket upgrade")
            response.extend(chunk)
            if len(response) > 65536:
                raise ListenerError("oversized WebSocket upgrade response")
        status = bytes(response).split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise ListenerError(f"WebSocket upgrade failed: {status!r}")

    @staticmethod
    def _send(client: socket.socket, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode()
        client.sendall(websocket_frame(payload, masked=True))

    @staticmethod
    def _wait_for_response(
        client: socket.socket, decoder: WebSocketDecoder, request_id: int
    ) -> dict[str, Any]:
        while True:
            chunk = client.recv(65536)
            if not chunk:
                raise ListenerError("app-server closed before responding")
            for payload in decoder.feed(chunk):
                message = decode_json_message(payload)
                if message is not None and message.get("id") == request_id:
                    return message


class ThreadNameResolver(AppServerReader):
    """Resolve a name with non-subscribing ``thread/read``."""

    def resolve(self, thread_id: str) -> str | None:
        try:
            thread = self.read_thread(thread_id)
        except ListenerError:
            return None
        name = thread.get("name")
        return name if isinstance(name, str) else None


def decode_json_message(payload: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


_METHOD_RE = re.compile(rb'"method"\s*:\s*"([^"\\]+)"')
_THREAD_RE = re.compile(rb'"(?:threadId|thread_id)"\s*:\s*"([\w-]{8,128})"')
_THREAD_OBJECT_RE = re.compile(
    rb'"thread"\s*:\s*\{[^{}]{0,512}?"id"\s*:\s*"([\w-]{8,128})"'
)
_NAME_RE = re.compile(rb'"name"\s*:\s*"((?:[^"\\]|\\.){0,512})"')
_ID_RE = re.compile(rb'"id"\s*:\s*("(?:[^"\\]|\\.)*"|-?\d+)')

_SESSION_METHODS = {
    "thread/start",
    "thread/resume",
    "thread/fork",
    "thread/name/set",
    "thread/unsubscribe",
    "turn/start",
    "turn/steer",
    "thread/shellCommand",
    "thread/name/updated",
    "thread/started",
    "thread/tokenUsage/updated",
    "turn/started",
}


def websocket_payload_prefix(data: bytes, *, masked: bool) -> bytes | None:
    if data.startswith((b"GET ", b"HTTP/1.1 ")) or len(data) < 2:
        return None
    first, second = data[0], data[1]
    if first & 0x70 or (first & 0x0F) not in (0x1, 0x2):
        return None
    if bool(second & 0x80) != masked:
        return None
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(data) < 4:
            return None
        offset = 4
    elif length == 127:
        if len(data) < 10:
            return None
        offset = 10
    if masked:
        if len(data) < offset + 4:
            return None
        mask = data[offset : offset + 4]
        offset += 4
        return bytes(value ^ mask[index % 4] for index, value in enumerate(data[offset:]))
    return data[offset:]


def nested_string(value: Any, *paths: tuple[str, ...]) -> str | None:
    for path in paths:
        current = value
        for key in path:
            if not isinstance(current, dict) or key not in current:
                break
            current = current[key]
        else:
            if isinstance(current, str) and current:
                return current
    return None


def sanitize_protocol_message(
    message: dict[str, Any], *, last_thread_id: str | None
) -> tuple[dict[str, Any] | None, str | None]:
    method = message.get("method")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    thread_id = nested_string(
        params, ("threadId",), ("thread_id",), ("thread", "id")
    )
    name = nested_string(params, ("name",), ("thread", "name"))
    interesting = method in _SESSION_METHODS
    if isinstance(method, str) and interesting:
        sanitized: dict[str, Any] = {"method": method, "params": {}}
        if "id" in message:
            sanitized["id"] = message["id"]
        if thread_id:
            sanitized["params"]["threadId"] = thread_id
        if name is not None and method in {"thread/name/set", "thread/name/updated"}:
            sanitized["params"]["name"] = name
        return sanitized, thread_id or last_thread_id
    if "id" in message and ("result" in message or "error" in message):
        result = message.get("result")
        result_thread = nested_string(result, ("thread", "id"), ("threadId",))
        result_name = nested_string(result, ("thread", "name"))
        if result_thread:
            sanitized = {
                "id": message["id"],
                "result": {"thread": {"id": result_thread}},
            }
            if result_name is not None:
                sanitized["result"]["thread"]["name"] = result_name
            return sanitized, result_thread
        if "error" in message:
            return {"id": message["id"], "error": {}}, last_thread_id
    return None, last_thread_id


def sanitize_protocol_prefix(
    payload: bytes, *, last_thread_id: str | None
) -> tuple[dict[str, Any] | None, str | None]:
    method_match = _METHOD_RE.search(payload[:1024])
    try:
        method = method_match.group(1).decode("ascii") if method_match else None
    except UnicodeDecodeError:
        return None, last_thread_id
    thread_match = _THREAD_RE.search(payload[:2048]) or _THREAD_OBJECT_RE.search(
        payload[:2048]
    )
    thread_id = thread_match.group(1).decode("ascii") if thread_match else None
    if method is None:
        return None, last_thread_id
    interesting = method in _SESSION_METHODS
    if not interesting:
        return None, last_thread_id
    sanitized: dict[str, Any] = {"method": method, "params": {}}
    request_id = _ID_RE.search(payload[:512])
    if request_id:
        try:
            sanitized["id"] = json.loads(request_id.group(1))
        except json.JSONDecodeError:
            pass
    if thread_id:
        sanitized["params"]["threadId"] = thread_id
    if method in {"thread/name/set", "thread/name/updated"}:
        name_match = _NAME_RE.search(payload[:2048])
        if name_match:
            try:
                sanitized["params"]["name"] = json.loads(
                    b'"' + name_match.group(1) + b'"'
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
    return sanitized, thread_id or last_thread_id


@dataclasses.dataclass
class ProtocolState:
    connection: Connection
    pending: dict[str, tuple[str, str | None, str | None]] = dataclasses.field(
        default_factory=dict
    )
    thread_id: str | None = None
    thread_name: str | None = None

    def consume(self, direction: str, message: dict[str, Any]) -> list[dict[str, Any]]:
        return (
            self._consume_request(message)
            if direction == "client_to_server"
            else self._consume_server(message)
        )

    def _consume_request(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        method = message.get("method")
        if not isinstance(method, str):
            return []
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        thread_id = nested_string(
            params, ("threadId",), ("thread_id",), ("thread", "id")
        )
        name = nested_string(params, ("name",), ("thread", "name"))
        request_id = message.get("id")
        if request_id is not None and method in {
            "thread/start",
            "thread/resume",
            "thread/fork",
            "thread/name/set",
            "thread/unsubscribe",
        }:
            self.pending[str(request_id)] = (method, thread_id, name)
        events: list[dict[str, Any]] = []
        if method in {
            "thread/resume",
            "turn/start",
            "turn/steer",
            "thread/shellCommand",
        } and thread_id:
            events.extend(self._bind(thread_id, None, method))
        if method == "thread/name/set" and thread_id and name is not None:
            if self.thread_id is None:
                events.extend(self._bind(thread_id, None, method))
            if self.thread_id == thread_id:
                events.append(
                    self._event("thread.name.requested", thread_id, name, method)
                )
        return events

    def _consume_server(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        method = message.get("method")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if isinstance(method, str):
            thread_id = nested_string(
                params, ("threadId",), ("thread_id",), ("thread", "id")
            )
            name = nested_string(params, ("name",), ("thread", "name"))
            if method == "thread/name/updated" and thread_id and name is not None:
                if self.thread_id != thread_id:
                    return []
                self.thread_name = name
                return [self._event("thread.name.updated", thread_id, name, method)]
            if thread_id == self.thread_id and name is not None:
                return self._bind(thread_id, name, method)
            return []
        response_id = message.get("id")
        if response_id is None:
            return []
        pending = self.pending.pop(str(response_id), None)
        if pending is None or "error" in message:
            return []
        pending_method, pending_thread, pending_name = pending
        result = message.get("result")
        thread_id = nested_string(
            result, ("thread", "id"), ("threadId",), ("id",)
        ) or pending_thread
        name = nested_string(result, ("thread", "name"), ("name",)) or pending_name
        if pending_method in {"thread/start", "thread/resume", "thread/fork"} and thread_id:
            return self._bind(thread_id, name, f"{pending_method}:response")
        if pending_method == "thread/name/set" and pending_thread == self.thread_id:
            # A successful response confirms that app-server processed the rename,
            # but some clients do not receive a thread/name/updated notification on
            # this connection. Invalidate the old cached name so the controller's
            # non-subscribing thread/read resolver fetches the committed value.
            if self.thread_name != pending_name:
                self.thread_name = None
            return []
        if pending_method == "thread/unsubscribe" and pending_thread == self.thread_id:
            old_thread = self.thread_id
            old_name = self.thread_name
            self.thread_id = None
            self.thread_name = None
            return [
                self._event(
                    "thread.unbound",
                    old_thread,
                    old_name,
                    "thread/unsubscribe:response",
                )
            ]
        return []

    def _bind(self, thread_id: str, name: str | None, reason: str) -> list[dict[str, Any]]:
        changed = thread_id != self.thread_id
        name_changed = name is not None and name != self.thread_name
        self.thread_id = thread_id
        if name is not None:
            self.thread_name = name
        if not changed and not name_changed:
            return []
        return [self._event("thread.bound", thread_id, self.thread_name, reason)]

    def _event(
        self, event: str, thread_id: str | None, thread_name: str | None, reason: str
    ) -> dict[str, Any]:
        connection = self.connection
        tmux = connection.tmux
        return {
            "schema": SCHEMA,
            "observed_at": utc_now(),
            "event": event,
            "connection_id": connection.key,
            "source": reason,
            "server": {
                "pid": connection.server_pid,
                "start_ticks": connection.server_start_ticks,
                "fd": connection.server_fd,
            },
            "client": {
                "pid": connection.client_pid,
                "start_ticks": connection.client_start_ticks,
                "fd": connection.client_fd,
                "kind": connection.client_kind,
            },
            "tmux": (
                {
                    "socket": tmux.socket_path,
                    "server_pid": tmux.server_pid,
                    "server_start_ticks": tmux.server_start_ticks,
                    "pane": tmux.pane_id,
                    "pane_pid": tmux.pane_pid,
                    "pane_start_ticks": tmux.pane_start_ticks,
                }
                if tmux
                else None
            ),
            "thread": {"id": thread_id, "name": thread_name},
        }


def connection_event(event: str, connection: Connection) -> dict[str, Any]:
    return ProtocolState(connection)._event(event, None, None, "socket")
