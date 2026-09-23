"""WebSocket decoding and privacy-filtered Codex session semantics."""

from __future__ import annotations

import base64
import dataclasses
import json
import os
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


@dataclasses.dataclass
class ProtocolState:
    connection: Connection
    pending: dict[str, tuple[str, str | None, str | None]] = dataclasses.field(
        default_factory=dict
    )
    thread_id: str | None = None
    thread_name: str | None = None
    ephemeral_threads: set[str] = dataclasses.field(default_factory=set)
    # A thread the client resumed while it was still on another one. The Codex
    # TUI does that both to switch (/resume: resume the new thread, then
    # unsubscribe the old one) and merely to look at a thread (/agent opening a
    # sub-agent, refreshing a snapshot before replay), which leaves the pane's
    # own thread subscribed. So the pane only moves once the client lets go of
    # some other thread. That is the thread it was really on, which after a
    # listener restart need not be the one remembered here.
    switching_to: tuple[str, str | None] | None = None

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
        }:
            self.pending[str(request_id)] = (method, thread_id, name)
        events: list[dict[str, Any]] = []
        if method in {"turn/start", "turn/steer", "thread/shellCommand"} and thread_id:
            self.switching_to = None
            events.extend(self._bind(thread_id, None, method))
        if method == "thread/resume" and thread_id:
            events.extend(self._resume(thread_id, None, method))
        if method == "thread/unsubscribe" and thread_id:
            # Acted on as sent: the TUI moves on without waiting to see whether
            # it worked, and so does the pane.
            events.extend(self._unsubscribe(thread_id))
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
        if pending is None:
            return []
        pending_method, pending_thread, pending_name = pending
        if "error" in message:
            # A /resume that failed never goes on to unsubscribe anything.
            if (
                pending_method == "thread/resume"
                and self.switching_to is not None
                and self.switching_to[0] == pending_thread
            ):
                self.switching_to = None
            return []
        result = message.get("result")
        thread_id = nested_string(
            result, ("thread", "id"), ("threadId",), ("id",)
        ) or pending_thread
        name = nested_string(result, ("thread", "name"), ("name",)) or pending_name
        if pending_method in {"thread/start", "thread/resume", "thread/fork"} and thread_id:
            thread = result.get("thread") if isinstance(result, dict) else None
            if isinstance(thread, dict) and thread.get("ephemeral") is True:
                self.ephemeral_threads.add(thread_id)
                return []
            if pending_method == "thread/resume":
                return self._resume(thread_id, name, f"{pending_method}:response")
            self.switching_to = None
            return self._bind(thread_id, name, f"{pending_method}:response")
        if pending_method == "thread/name/set" and pending_thread == self.thread_id:
            # The rename went through. thread/name/updated follows with the name as
            # stored (trimmed), unless this client opted out of it at initialize, so
            # drop the old name and let the controller's non-subscribing thread/read
            # resolver fetch the committed value.
            if self.thread_name != pending_name:
                self.thread_name = None
            return []
        return []

    def _unsubscribe(self, thread_id: str) -> list[dict[str, Any]]:
        if self.switching_to is not None:
            target, name = self.switching_to
            self.switching_to = None
            if target != thread_id:
                return self._bind(target, name, "thread/resume+unsubscribe")
        if thread_id != self.thread_id:
            return []
        old_thread, old_name = self.thread_id, self.thread_name
        self.thread_id = None
        self.thread_name = None
        return [self._event("thread.unbound", old_thread, old_name, "thread/unsubscribe")]

    def _resume(self, thread_id: str, name: str | None, reason: str) -> list[dict[str, Any]]:
        if self.thread_id is None or self.thread_id == thread_id:
            return self._bind(thread_id, name, reason)
        if thread_id not in self.ephemeral_threads:
            self.switching_to = (thread_id, name)
        return []

    def _bind(self, thread_id: str, name: str | None, reason: str) -> list[dict[str, Any]]:
        if thread_id in self.ephemeral_threads:
            return []
        changed = thread_id != self.thread_id
        name_changed = name is not None and name != self.thread_name
        self.thread_id = thread_id
        if name is not None or changed:
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
