from __future__ import annotations

import dataclasses
import ctypes
import io
import json
import os
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


FEATURE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FEATURE_ROOT))

import session_listener as listener  # noqa: E402
from session_listener import model  # noqa: E402
from session_listener import capture, cli, diagnose  # noqa: E402


def make_websocket_frame(payload: bytes, *, masked: bool, fin: bool = True, opcode: int = 1) -> bytes:
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    if length < 126:
        header = bytes([first, (0x80 if masked else 0) | length])
    elif length < 65536:
        header = bytes([first, (0x80 if masked else 0) | 126]) + struct.pack("!H", length)
    else:
        header = bytes([first, (0x80 if masked else 0) | 127]) + struct.pack("!Q", length)
    if not masked:
        return header + payload
    mask = b"\x11\x22\x33\x44"
    encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return header + mask + encoded


def connection() -> object:
    return listener.Connection(
        server_pid=10,
        server_start_ticks=100,
        server_fd=7,
        server_inode=70,
        client_pid=20,
        client_start_ticks=200,
        client_fd=8,
        client_inode=80,
        tmux=listener.TmuxIdentity(
            "/tmp/tmux-1000/default", "%4", 30, 300, 21, 210
        ),
    )


def test_parse_ss_line_extracts_endpoint() -> None:
    line = (
        "u_str ESTAB 0 0 /home/me/.codex/app-server-control/"
        "app-server-control.sock 100 * 200 users:((\"codex\",pid=31,fd=9))"
    )
    assert listener.parse_ss_line(line) == [
        listener.SocketEndpoint("ESTAB", "/home/me/.codex/app-server-control/app-server-control.sock", 100, 200, 31, 9)
    ]


def test_parse_tmux_environment_requires_absolute_socket_and_pane() -> None:
    assert listener.parse_tmux_environment(
        {"TMUX": "/tmp/tmux-1000/default,123,0", "TMUX_PANE": "%8"}
    ) == listener.TmuxCandidate("/tmp/tmux-1000/default", "%8", 123)
    assert listener.parse_tmux_environment({"TMUX": "relative,1,0", "TMUX_PANE": "%8"}) is None
    assert listener.parse_tmux_environment({"TMUX": "/tmp/x,1,0"}) is None


def test_process_generation_parser_handles_process_names() -> None:
    assert model.process_start_ticks(os.getpid()) > 0
    assert listener.process_descends_from(os.getpid(), os.getpid())


def test_tmux_inventory_records_server_and_pane_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        model.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="30\t%4\t21\n", stderr=""
        ),
    )
    monkeypatch.setattr(model, "process_start_ticks", lambda pid: pid * 10)
    assert model.tmux_inventory("/tmp/tmux/default")["%4"] == listener.TmuxIdentity(
        "/tmp/tmux/default", "%4", 30, 300, 21, 210
    )


@pytest.mark.parametrize("masked", [False, True])
def test_websocket_decoder_handles_split_frames(masked: bool) -> None:
    payload = json.dumps({"method": "thread/name/updated", "params": {"threadId": "thr", "name": "viewtree"}}).encode()
    frame = make_websocket_frame(payload, masked=masked)
    decoder = listener.WebSocketDecoder(expect_masked=masked)
    assert decoder.feed(frame[:3]) == []
    assert decoder.feed(frame[3:11]) == []
    assert decoder.feed(frame[11:]) == [payload]


def test_websocket_decoder_handles_http_upgrade_and_multiple_frames() -> None:
    one = b'{"method":"thread/started"}'
    two = b'{"method":"thread/name/updated"}'
    decoder = listener.WebSocketDecoder(expect_masked=False)
    data = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n"
    data += make_websocket_frame(one, masked=False) + make_websocket_frame(two, masked=False)
    assert decoder.feed(data) == [one, two]


def test_websocket_decoder_unmasks_client_frame() -> None:
    payload = b'{"method":"thread/resume","id":4}'
    decoder = listener.WebSocketDecoder(expect_masked=True)
    assert decoder.feed(make_websocket_frame(payload, masked=True)) == [payload]


def test_capture_gap_fails_closed_and_resynchronizes() -> None:
    decoder = listener.WebSocketDecoder(expect_masked=False)
    first = make_websocket_frame(b'{"ignored":true}', masked=False)
    assert decoder.feed(first[:4], omitted=len(first) - 4) == []
    valid = b'{"method":"thread/started"}'
    assert decoder.feed(make_websocket_frame(valid, masked=False)) == [valid]


def test_production_websocket_encoder_round_trips() -> None:
    payload = b'{"method":"thread/read"}'
    decoder = listener.WebSocketDecoder(expect_masked=True)
    assert decoder.feed(listener.websocket_frame(payload, masked=True)) == [payload]


def test_protocol_state_correlates_start_response() -> None:
    state = listener.ProtocolState(connection())
    request = {"method": "thread/start", "id": 7, "params": {"cwd": "/tmp"}}
    assert state.consume("client_to_server", request) == []
    events = state.consume(
        "server_to_client",
        {"id": 7, "result": {"thread": {"id": "thr-new", "name": "hello"}}},
    )
    assert [(item["event"], item["thread"]) for item in events] == [
        ("thread.bound", {"id": "thr-new", "name": "hello"})
    ]
    assert events[0]["tmux"] == {
        "socket": "/tmp/tmux-1000/default",
        "server_pid": 30,
        "server_start_ticks": 300,
        "pane": "%4",
        "pane_pid": 21,
        "pane_start_ticks": 210,
    }


def test_protocol_state_binds_resume_before_response() -> None:
    state = listener.ProtocolState(connection())
    events = state.consume(
        "client_to_server",
        {"method": "thread/resume", "id": 8, "params": {"threadId": "thr-old"}},
    )
    assert events[0]["event"] == "thread.bound"
    assert events[0]["thread"] == {"id": "thr-old", "name": None}


def test_protocol_state_discards_turn_content() -> None:
    state = listener.ProtocolState(connection())
    events = state.consume(
        "client_to_server",
        {
            "method": "turn/start",
            "id": 9,
            "params": {"threadId": "thr", "input": [{"text": "secret"}]},
        },
    )
    serialized = json.dumps(events)
    assert "secret" not in serialized
    assert events[0]["thread"]["id"] == "thr"


def test_protocol_state_does_not_bind_from_content_notification() -> None:
    state = listener.ProtocolState(connection())
    events = state.consume(
        "server_to_client",
        {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thr", "delta": "private answer text"},
        },
    )
    assert events == []
    assert "private answer text" not in json.dumps(events)


def test_protocol_state_does_not_bootstrap_from_broadcast_token_usage() -> None:
    state = listener.ProtocolState(connection())
    events = state.consume(
        "server_to_client",
        {
            "method": "thread/tokenUsage/updated",
            "params": {"threadId": "thr"},
        },
    )
    assert events == []
    assert state.thread_id is None


def test_protocol_state_does_not_bootstrap_from_broadcast_name_update() -> None:
    state = listener.ProtocolState(connection())
    events = state.consume(
        "server_to_client",
        {
            "method": "thread/name/updated",
            "params": {"threadId": "thread-other", "name": "wrong"},
        },
    )
    assert events == []
    assert state.thread_id is None
    assert state.thread_name is None


def test_name_request_and_confirmation_are_distinct() -> None:
    state = listener.ProtocolState(connection())
    requested = state.consume(
        "client_to_server",
        {
            "method": "thread/name/set",
            "id": 12,
            "params": {"threadId": "thr", "name": "viewtree"},
        },
    )
    confirmed = state.consume(
        "server_to_client",
        {
            "method": "thread/name/updated",
            "params": {"threadId": "thr", "name": "viewtree"},
        },
    )
    assert [event["event"] for event in requested] == [
        "thread.bound",
        "thread.name.requested",
    ]
    assert confirmed[0]["event"] == "thread.name.updated"


def test_successful_name_set_response_invalidates_old_name_for_readback() -> None:
    state = listener.ProtocolState(connection())
    state.thread_id = "thread-active"
    state.thread_name = "old name"
    requested = state.consume(
        "client_to_server",
        {
            "method": "thread/name/set",
            "id": 12,
            "params": {"threadId": "thread-active", "name": "new name"},
        },
    )
    assert requested[0]["event"] == "thread.name.requested"
    assert state.thread_name == "old name"
    assert state.consume("server_to_client", {"id": 12, "result": {}}) == []
    assert state.thread_name is None


def test_name_notification_before_response_keeps_confirmed_name() -> None:
    state = listener.ProtocolState(connection())
    state.thread_id = "thread-active"
    state.thread_name = "old name"
    state.consume(
        "client_to_server",
        {
            "method": "thread/name/set",
            "id": 12,
            "params": {"threadId": "thread-active", "name": "new name"},
        },
    )
    state.consume(
        "server_to_client",
        {
            "method": "thread/name/updated",
            "params": {"threadId": "thread-active", "name": "new name"},
        },
    )
    state.consume("server_to_client", {"id": 12, "result": {}})
    assert state.thread_name == "new name"


def test_connection_key_changes_with_process_generation() -> None:
    first = connection()
    second = dataclasses.replace(first, client_start_ticks=201)
    assert first.key != second.key


def test_connection_key_changes_with_socket_inode() -> None:
    first = connection()
    second = dataclasses.replace(first, server_inode=71, client_inode=81)
    assert first.key != second.key


def test_background_notification_cannot_replace_active_thread() -> None:
    state = listener.ProtocolState(connection())
    state.consume(
        "client_to_server",
        {"method": "turn/start", "id": 1, "params": {"threadId": "thread-active"}},
    )
    events = state.consume(
        "server_to_client",
        {
            "method": "thread/tokenUsage/updated",
            "params": {"threadId": "thread-background"},
        },
    )
    assert events == []
    assert state.thread_id == "thread-active"


def test_successful_unsubscribe_clears_active_thread() -> None:
    state = listener.ProtocolState(connection())
    state.consume(
        "client_to_server",
        {"method": "turn/start", "id": 1, "params": {"threadId": "thread-active"}},
    )
    state.consume(
        "client_to_server",
        {
            "method": "thread/unsubscribe",
            "id": 2,
            "params": {"threadId": "thread-active"},
        },
    )
    events = state.consume("server_to_client", {"id": 2, "result": {}})
    assert events[0]["event"] == "thread.unbound"
    assert state.thread_id is None


def test_codex_tui_filter_excludes_app_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model, "read_process_command", lambda _pid: ["/bin/codex", "resume", "thr"])
    assert listener.is_codex_tui_process(10)
    monkeypatch.setattr(model, "read_process_command", lambda _pid: ["/bin/codex", "app-server", "proxy"])
    assert not listener.is_codex_tui_process(10)
    assert listener.is_codex_app_server_proxy_process(10)
    assert listener.codex_client_kind(10) == "remote_proxy"

    monkeypatch.setattr(
        model,
        "read_process_command",
        lambda _pid: ["/bin/codex", "app-server", "--listen", "unix://"],
    )
    assert listener.codex_client_kind(10) is None


def test_resume_process_supplies_only_explicit_thread_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "019fef92-949e-77a1-a9e2-2c54da07cceb"
    monkeypatch.setattr(
        model,
        "read_process_command",
        lambda _pid: ["/bin/codex", "resume", "-m", "gpt", thread_id],
    )
    assert listener.resumed_thread_id(10) == thread_id
    monkeypatch.setattr(
        model,
        "read_process_command",
        lambda _pid: ["/bin/codex", "fork", thread_id],
    )
    assert listener.resumed_thread_id(10) is None
    monkeypatch.setattr(model, "read_process_command", lambda _pid: ["/bin/codex"])
    assert listener.resumed_thread_id(10) is None


def test_app_server_filter_requires_native_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model,
        "read_process_command",
        lambda _pid: ["/bin/codex", "-c", "x=y", "app-server", "--listen", "unix://"],
    )
    assert listener.is_codex_app_server_process(10)
    monkeypatch.setattr(
        model, "read_process_command", lambda _pid: ["python3", "fake_server.py"]
    )
    assert not listener.is_codex_app_server_process(10)
    monkeypatch.setattr(model, "read_process_command", lambda _pid: ["python3", "listener.py"])
    assert not listener.is_codex_tui_process(10)


def test_sanitizer_drops_content_notification_entirely() -> None:
    sanitized, latest = listener.sanitize_protocol_message(
        {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-12345678", "delta": "private"},
        },
        last_thread_id=None,
    )
    assert latest is None
    assert sanitized is None


def test_sanitizer_ignores_repeated_content_notification() -> None:
    sanitized, latest = listener.sanitize_protocol_message(
        {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-12345678", "delta": "private"},
        },
        last_thread_id="thread-12345678",
    )
    assert sanitized is None
    assert latest == "thread-12345678"


def test_sanitizer_keeps_active_thread_token_usage() -> None:
    sanitized, latest = listener.sanitize_protocol_message(
        {
            "method": "thread/tokenUsage/updated",
            "params": {"threadId": "thread-12345678", "tokenUsage": {"secret": 1}},
        },
        last_thread_id=None,
    )
    assert latest == "thread-12345678"
    assert sanitized == {
        "method": "thread/tokenUsage/updated",
        "params": {"threadId": "thread-12345678"},
    }


def test_partial_masked_frame_extracts_thread_before_large_content() -> None:
    payload = (
        b'{"method":"turn/start","id":9,"params":'
        b'{"threadId":"thread-12345678","input":"' + b"x" * 8000
    )
    frame = make_websocket_frame(payload, masked=True)
    prefix = listener.websocket_payload_prefix(frame[:4096], masked=True)
    assert prefix is not None
    sanitized, latest = listener.sanitize_protocol_prefix(prefix, last_thread_id=None)
    assert latest == "thread-12345678"
    assert sanitized == {
        "method": "turn/start",
        "id": 9,
        "params": {"threadId": "thread-12345678"},
    }


def test_registry_file_is_atomic_private_metadata(tmp_path: Path) -> None:
    state_file = tmp_path / "state/sessions.json"
    stream = io.StringIO()
    registry = cli.RegistryOutput(stream, state_file)
    event = listener.connection_event("connection.opened", connection())
    cli.encode_event(registry, event)
    value = json.loads(state_file.read_text())
    assert value["connections"][connection().key]["tmux"]["pane"] == "%4"
    assert state_file.stat().st_mode & 0o777 == 0o600
    assert "secret" not in state_file.read_text()


def test_protocol_event_records_client_kind() -> None:
    remote = dataclasses.replace(connection(), tmux=None, client_kind="remote_proxy")
    event = listener.connection_event("connection.opened", remote)
    assert event["client"]["kind"] == "remote_proxy"
    assert event["tmux"] is None


def test_opening_events_publish_explicit_resume_hint_without_name() -> None:
    thread_id = "019ff05a-6556-7a90-b2d9-23a4351217f7"
    resumed = dataclasses.replace(connection(), thread_hint=thread_id)
    state = listener.ProtocolState(resumed, thread_id=thread_id)

    events = cli.opening_events(state)

    assert [event["event"] for event in events] == [
        "connection.opened",
        "thread.bound",
    ]
    assert events[1]["thread"] == {"id": thread_id, "name": None}
    assert events[1]["source"] == "process:resume"


def test_diagnose_reports_verified_remote_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(diagnose, "process_start_ticks", lambda pid: 200 if pid == 20 else None)
    monkeypatch.setattr(diagnose, "codex_client_kind", lambda pid: "remote_proxy" if pid == 20 else None)
    monkeypatch.setattr(diagnose, "rollout_writer_fds", lambda _pid, _path: [104])
    remote = dataclasses.replace(connection(), tmux=None, client_kind="remote_proxy")
    discovery = listener.Discovery(10, 100, 6, 60, {7: remote})
    registry = {
        "connections": {
            remote.key: {
                "client": {
                    "pid": 20,
                    "start_ticks": 200,
                    "fd": 8,
                    "kind": "remote_proxy",
                },
                "tmux": None,
                "thread": {"id": "thread-12345678", "name": "CPU analysis"},
                "observed_at": "2026-08-11T08:00:00Z",
            }
        }
    }
    report = diagnose.build_report(
        {
            "id": "thread-12345678",
            "name": "CPU analysis",
            "status": {"type": "idle"},
            "cwd": "/android/project",
            "modelProvider": "opencode",
            "path": str(tmp_path / "rollout.jsonl"),
        },
        discovery,
        registry,
    )
    assert report["conclusion"] == "observed_client"
    assert report["clients"] == [
        {
            "connection_id": remote.key,
            "kind": "remote_proxy",
            "pid": 20,
            "tmux": None,
            "observed_at": "2026-08-11T08:00:00Z",
        }
    ]
    rendered = diagnose.render_human(report)
    assert "远程客户端" in rendered
    assert "PID 20" in rendered
    assert "中央 app-server PID 10（FD 104）" in rendered


def test_diagnose_does_not_guess_from_unbound_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(diagnose, "rollout_writer_fds", lambda _pid, _path: [22])
    remote = dataclasses.replace(connection(), tmux=None, client_kind="remote_proxy")
    discovery = listener.Discovery(10, 100, 6, 60, {7: remote})
    report = diagnose.build_report(
        {
            "id": "thread-12345678",
            "status": {"type": "idle"},
            "path": str(tmp_path / "rollout.jsonl"),
        },
        discovery,
        {"connections": {}},
    )
    assert report["conclusion"] == "loaded_without_observed_client"
    assert report["clients"] == []
    assert report["unbound_live_clients"][0]["kind"] == "remote_proxy"
    assert "不能据此猜测" in diagnose.render_human(report)


def test_diagnose_does_not_call_proxy_bound_to_another_thread_unbound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(diagnose, "process_start_ticks", lambda _pid: 200)
    monkeypatch.setattr(diagnose, "codex_client_kind", lambda _pid: "remote_proxy")
    monkeypatch.setattr(diagnose, "rollout_writer_fds", lambda _pid, _path: [22])
    remote = dataclasses.replace(connection(), tmux=None, client_kind="remote_proxy")
    discovery = listener.Discovery(10, 100, 6, 60, {7: remote})
    registry = {
        "connections": {
            remote.key: {
                "client": {
                    "pid": 20,
                    "start_ticks": 200,
                    "fd": 8,
                    "kind": "remote_proxy",
                },
                "thread": {"id": "some-other-thread"},
            }
        }
    }
    report = diagnose.build_report(
        {
            "id": "thread-12345678",
            "status": {"type": "idle"},
            "path": str(tmp_path / "rollout.jsonl"),
        },
        discovery,
        registry,
    )
    assert report["clients"] == []
    assert report["unbound_live_clients"] == []


def test_diagnose_no_argument_selects_loaded_session_list() -> None:
    assert diagnose.parse_args([]).thread_id is None
    assert diagnose.parse_args(["thread-12345678"]).thread_id == "thread-12345678"


def test_diagnose_reports_tmux_window_and_active_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        diagnose.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="agent\t3\t@47\tCodex EBFP监视器\t0\n",
        ),
    )
    details = diagnose.tmux_pane_details(
        {"socket": "/tmp/tmux/default", "pane": "%151"}
    )
    assert details == {
        "session": "agent",
        "index": "3",
        "id": "@47",
        "name": "Codex EBFP监视器",
        "pane_active": False,
    }
    label = diagnose.client_label(
        {"kind": "tui", "tmux": {"pane": "%151"}, "window": details}
    )
    assert "非活动 pane" in label
    assert "窗口 agent:3 @47" in label


def test_diagnose_loaded_list_is_active_first() -> None:
    def report(thread_id: str, status: str, name: str) -> dict:
        return {
            "thread": {
                "id": thread_id,
                "name": name,
                "status": status,
                "cwd": f"/work/{name}",
                "recency_at": 1,
            },
            "clients": [],
        }

    rendered = diagnose.render_list(
        [report("idle-id", "idle", "idle"), report("active-id", "active", "active")]
    )
    assert rendered.index("active-id") < rendered.index("idle-id")
    assert "当前已加载 2 个 Codex 会话" in rendered


def test_registry_does_not_confirm_requested_name(tmp_path: Path) -> None:
    state_file = tmp_path / "sessions.json"
    registry = cli.RegistryOutput(io.StringIO(), state_file)
    state = listener.ProtocolState(connection())
    for event in state.consume(
        "client_to_server",
        {
            "method": "thread/name/set",
            "id": 1,
            "params": {"threadId": "thread-active", "name": "requested"},
        },
    ):
        cli.encode_event(registry, event)
    current = json.loads(state_file.read_text())["connections"][connection().key]
    assert current["thread"] == {"id": "thread-active", "name": None}
    assert current["pending_name"] == "requested"


def test_registry_notifies_title_consumer_after_private_atomic_write(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "sessions.json"
    calls: list[tuple[Path, int]] = []

    def consume(path: Path) -> None:
        calls.append((path, path.stat().st_mode & 0o777))

    registry = cli.RegistryOutput(io.StringIO(), state_file, consume)
    cli.encode_event(registry, listener.connection_event("connection.opened", connection()))
    assert calls
    assert calls[-1] == (state_file, 0o600)


def test_title_sync_consumer_uses_registry_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "sessions.json"
    state_file.write_text("{}\n")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command)
        or SimpleNamespace(returncode=0, stderr=""),
    )
    cli.TitleSyncConsumer(Path("/tmp/title-sync"))(state_file)
    assert calls == [
        [
            "/tmp/title-sync",
            "--registry-sync",
            "--registry-file",
            str(state_file),
        ]
    ]


def test_untrusted_user_writable_helper_is_rejected(tmp_path: Path) -> None:
    helper = tmp_path / "codex_session_listener.py"
    helper.write_text("pass\n")
    with pytest.raises(listener.ListenerError):
        cli.validate_privileged_helper(helper)


def test_bpf_object_compiles() -> None:
    temporary, object_path = capture.build_bpf_object()
    try:
        assert object_path.stat().st_size > 0
    finally:
        temporary.cleanup()


def test_capture_event_layout_matches_bpf_structure() -> None:
    assert ctypes.sizeof(capture.CaptureEvent) == 4136


def test_controller_waits_through_app_server_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes: list[object] = [
        listener.ListenerError("no app-server is listening on /tmp/control.sock"),
        75,
        0,
    ]

    def fake_listen(*_args: object, **_kwargs: object) -> int:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return int(outcome)

    monkeypatch.setattr(cli, "listen", fake_listen)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    assert cli.main(["--no-state-file", "--no-title-sync"]) == 0
    assert outcomes == []


def test_ephemeral_side_thread_does_not_replace_pane_thread() -> None:
    state = listener.ProtocolState(connection())
    state.consume(
        "client_to_server",
        {"method": "turn/start", "id": 1, "params": {"threadId": "thread-main"}},
    )
    sanitized, latest = listener.sanitize_protocol_message(
        {
            "id": 2,
            "result": {
                "thread": {"id": "thread-title", "ephemeral": True, "preview": "x"}
            },
        },
        last_thread_id="thread-main",
    )
    assert latest == "thread-main"
    assert sanitized == {
        "id": 2,
        "result": {"thread": {"id": "thread-title", "ephemeral": True}},
    }
    state.consume(
        "client_to_server",
        {"method": "thread/start", "id": 2, "params": {"cwd": "/tmp"}},
    )
    assert state.consume("server_to_client", sanitized) == []
    events = state.consume(
        "client_to_server",
        {"method": "turn/start", "id": 3, "params": {"threadId": "thread-title"}},
    )
    assert events == []
    assert state.thread_id == "thread-main"


def test_switching_threads_drops_the_previous_name() -> None:
    state = listener.ProtocolState(connection())
    state.consume(
        "client_to_server",
        {"method": "thread/start", "id": 1, "params": {"cwd": "/tmp"}},
    )
    state.consume(
        "server_to_client",
        {"id": 1, "result": {"thread": {"id": "thread-one", "name": "first"}}},
    )
    events = state.consume(
        "client_to_server",
        {"method": "thread/resume", "id": 2, "params": {"threadId": "thread-two"}},
    )
    assert events[0]["thread"] == {"id": "thread-two", "name": None}
