import contextlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "codex-tmux-title-sync"
LOADER = importlib.machinery.SourceFileLoader("codex_tmux_title_test", str(MODULE_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
title = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(title)


@pytest.fixture(autouse=True)
def isolate_listener_registry(monkeypatch, tmp_path):
    """Unit tests must not inherit the host's deployed listener state."""

    registry = tmp_path / "sessions.json"
    monkeypatch.setattr(title, "SESSION_REGISTRY_PATH", registry)
    monkeypatch.setattr(
        title,
        "session_registry_active",
        lambda path=registry: title.read_session_registry(path) is not None,
    )


def pane_context(window="@1", active=True, pane_pid=10, server_pid=1):
    return {
        "server_pid": server_pid,
        "pane_pid": pane_pid,
        "window_id": window,
        "pane_active": active,
        "pane_dead": False,
    }


def test_shorten_and_clean():
    assert title.shorten("hello\x1b[31m world") == "hello world"
    assert title.shorten("abcdefgh", 7) == "abcd..."


def test_parser_tolerates_empty_window_from_stale_pane_exit_hook():
    args = title.build_parser().parse_args(
        [
            "--socket",
            "/tmp/server",
            "--pane",
            "%21",
            "--window",
            "--pane-exited",
        ]
    )
    assert args.window is None
    assert args.pane_exited is True


def test_codex_status_reads_unicode_rename_from_same_pane():
    raw = """
• Session renamed to Prompt验收. To resume this session run codex resume, then select Prompt验收 (019fa849-2879-7f61-87ca-997c7118cbc0)

› Implement {feature}

  gpt-5.6-sol max · /android/androidtools/AutoDroid · Context 31% left · 258K window · Prompt验收 · Main [default]
"""
    assert title.codex_status_from_capture(raw) == (
        "Prompt验收",
        "019fa849-2879-7f61-87ca-997c7118cbc0",
    )


def test_codex_status_reads_unnamed_session_id():
    session_id = "019fa849-2879-7f61-87ca-997c7118cbc0"
    raw = f"gpt-5.6-sol max · 258K window · {session_id} · Main [default]"
    assert title.codex_status_from_capture(raw) == (session_id, session_id)


def test_codex_0147_status_and_wrapped_rename_override_footerless_ui():
    session_id = "019fe0ec-8948-7b30-b66b-d438a0b3fe08"
    raw = f"""
│  Session:              {session_id}                      │

• Session renamed to viewtree. To resume this session run codex resume, then select
viewtree ({session_id})

  gpt-5.6-luna max fast · /android/androidtools/AutoDroid · weekly 100% left
"""
    assert title.codex_status_from_capture(raw) == ("viewtree", session_id)


def test_codex_0147_status_box_supplies_session_without_rename():
    session_id = "019fe0ec-8948-7b30-b66b-d438a0b3fe08"
    raw = f"│  Session:              {session_id}                      │"
    assert title.codex_status_from_capture(raw) == (session_id, session_id)


def test_same_pane_status_updates_cached_title_and_window(monkeypatch, tmp_path):
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(title, "pane_context", lambda pane, socket=None: pane_context())
    monkeypatch.setattr(title, "current_server_pid", lambda socket: 1)
    monkeypatch.setattr(title, "process_is_alive", lambda pid, start: True)
    reconciled = []
    monkeypatch.setattr(
        title,
        "reconcile_window",
        lambda socket, window: reconciled.append((socket, window)) or 1,
    )
    assert (
        title.apply_pane_observation(
            "/tmp/server",
            "%1",
            "Prompt验收",
            "019fa849-2879-7f61-87ca-997c7118cbc0",
            agent_pid=99,
            agent_start_time=42,
        )
        == 1
    )
    state = title.cached_pane_state("/tmp/server", "%1")
    assert state is not None
    assert state["title"] == "Prompt验收"
    assert state["session_id"] == "019fa849-2879-7f61-87ca-997c7118cbc0"
    assert state["agent_pid"] == 99
    assert state["provisional"] is False
    assert reconciled == [("/tmp/server", "@1")]


def test_same_status_repairs_generic_window_reset_but_preserves_manual_name(
    monkeypatch,
):
    state = {
        "title": "Prompt验收",
        "session_id": "019fa849-2879-7f61-87ca-997c7118cbc0",
        "agent_pid": 99,
        "agent_start_time": 42,
        "provisional": False,
    }
    monkeypatch.setattr(title, "cached_pane_state", lambda socket, pane: state)
    names = iter(["codex", "manual"])
    monkeypatch.setattr(title, "window_name", lambda window, socket: next(names))
    reconciled = []
    monkeypatch.setattr(
        title,
        "reconcile_window",
        lambda socket, window: reconciled.append((socket, window)) or 1,
    )

    arguments = (
        "/tmp/server",
        "%1",
        "Prompt验收",
        "019fa849-2879-7f61-87ca-997c7118cbc0",
    )
    keywords = {"agent_pid": 99, "agent_start_time": 42, "window": "@1"}
    assert title.apply_pane_observation(*arguments, **keywords) == 1
    assert title.apply_pane_observation(*arguments, **keywords) == 0
    assert reconciled == [("/tmp/server", "@1")]


def test_bound_session_reads_renamed_title_from_index_without_tui_capture(
    monkeypatch,
):
    session_id = "019fa849-2879-7f61-87ca-997c7118cbc0"
    monkeypatch.setattr(
        title,
        "cached_pane_state",
        lambda socket, pane: {
            "title": "old title",
            "session_id": session_id,
            "agent_pid": 99,
            "agent_start_time": 42,
            "provisional": False,
        },
    )
    monkeypatch.setattr(
        title,
        "thread_name_for_session",
        lambda value: "Prompt验收" if value == session_id else "",
    )
    monkeypatch.setattr(
        title,
        "pane_codex_status",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("a bound session must not scrape the TUI")
        ),
    )

    assert title.watcher_observation(
        "/tmp/server",
        "%1",
        agent_pid=99,
        agent_start_time=42,
    ) == ("Prompt验收", session_id)


def test_index_change_rechecks_and_rebinds_switched_session(monkeypatch):
    old_session = "019fdb18-d536-7760-bb99-8d541963ab6c"
    new_session = "019fe0ec-8948-7b30-b66b-d438a0b3fe08"
    monkeypatch.setattr(
        title,
        "cached_pane_state",
        lambda socket, pane: {
            "title": "codex-019fdb18",
            "session_id": old_session,
            "agent_pid": 99,
            "agent_start_time": 42,
            "provisional": False,
        },
    )
    monkeypatch.setattr(
        title,
        "thread_name_for_session",
        lambda value: "old title" if value == old_session else "viewtree",
    )
    captures = []
    monkeypatch.setattr(
        title,
        "pane_codex_status",
        lambda socket, pane: captures.append((socket, pane))
        or ("viewtree", new_session),
    )

    assert title.watcher_observation(
        "/tmp/server",
        "%1",
        agent_pid=99,
        agent_start_time=42,
    ) == ("old title", old_session)
    assert captures == []
    assert title.watcher_observation(
        "/tmp/server",
        "%1",
        agent_pid=99,
        agent_start_time=42,
        verify_bound_session=True,
    ) == ("viewtree", new_session)
    assert captures == [("/tmp/server", "%1")]


def test_other_process_binding_falls_back_to_same_pane_capture(monkeypatch):
    monkeypatch.setattr(
        title,
        "cached_pane_state",
        lambda socket, pane: {
            "title": "stale",
            "session_id": "019fa849-2879-7f61-87ca-997c7118cbc0",
            "agent_pid": 98,
            "agent_start_time": 41,
            "provisional": False,
        },
    )
    captured = ("current", "019fad30-7dbc-7123-95c0-fe6177ebde5d")
    monkeypatch.setattr(title, "pane_codex_status", lambda socket, pane: captured)
    assert title.watcher_observation(
        "/tmp/server",
        "%1",
        agent_pid=99,
        agent_start_time=42,
    ) == captured


def test_ensure_watcher_spawns_detached_helper(monkeypatch, tmp_path):
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(title, "pane_context", lambda pane, socket=None: pane_context())
    launched = []
    monkeypatch.setattr(
        title.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append((command, kwargs)),
    )

    assert title.ensure_pane_watcher("/tmp/server", "%1")
    command, kwargs = launched[0]
    assert command[-5:] == ["--socket", "/tmp/server", "--pane", "%1", "--watch-pane"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is title.subprocess.DEVNULL


def test_ensure_watcher_replaces_older_registered_version(monkeypatch, tmp_path):
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(title, "pane_context", lambda pane, socket=None: pane_context())
    registration = title.watcher_state_path("/tmp/server", 1, "%1")
    title.atomic_write_json(
        registration,
        {"version": title.WATCHER_VERSION - 1, "pid": 123, "start_time": 7},
    )
    alive = iter([True, False])
    monkeypatch.setattr(
        title, "process_is_alive", lambda pid, start: next(alive)
    )
    signals = []
    monkeypatch.setattr(
        title.os, "kill", lambda pid, sig: signals.append((pid, sig))
    )
    launched = []
    monkeypatch.setattr(
        title.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append((command, kwargs)),
    )

    assert title.ensure_pane_watcher("/tmp/server", "%1")
    assert signals == [(123, title.signal.SIGTERM)]
    assert len(launched) == 1


def test_watcher_survives_transient_tmux_failure_and_exits_cache_only(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(title, "WATCH_INTERVAL_SECONDS", 0)
    contexts = iter(
        [
            pane_context(),
            None,
            pane_context(),
            pane_context(),
        ]
    )
    monkeypatch.setattr(
        title, "pane_context", lambda pane, socket=None: next(contexts)
    )
    monkeypatch.setattr(title, "process_start_time", lambda pid: 7)
    monkeypatch.setattr(title, "current_server_pid", lambda socket: 1)
    monkeypatch.setattr(
        title,
        "_newest_agent_process",
        lambda socket, pane: {
            "agent_kind": title.AGENT_CLAUDE,
            "agent_pid": 99,
            "agent_start_time": 42,
            "transcripts": [],
        },
    )
    alive = iter([True, False])
    monkeypatch.setattr(
        title, "process_is_alive", lambda pid, start: next(alive)
    )
    monkeypatch.setattr(
        title,
        "claude_observation",
        lambda socket, pane, **kwargs: (
            "Prompt验收",
            "019fa849-2879-7f61-87ca-997c7118cbc0",
        ),
    )
    observations = []
    monkeypatch.setattr(
        title,
        "apply_pane_observation",
        lambda *args, **kwargs: observations.append((args, kwargs)) or 1,
    )
    title.atomic_write_json(
        title.pane_state_path("/tmp/server", 1, "%1"),
        {"agent_pid": 99, "agent_start_time": 42},
    )
    cleared = []
    monkeypatch.setattr(
        title,
        "clear_pane_state",
        lambda socket, pane: cleared.append((socket, pane)) or "@1",
    )
    reconciled = []
    monkeypatch.setattr(
        title,
        "reconcile_window_from_cache",
        lambda socket, window: reconciled.append((socket, window)) or 1,
    )
    monkeypatch.setattr(
        title,
        "reconcile_window",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("watcher exit must not rediscover a generic title")
        ),
    )

    assert title.watch_pane("/tmp/server", "%1") == 0
    assert len(observations) == 1
    assert cleared == [("/tmp/server", "%1")]
    assert reconciled == [("/tmp/server", "@1")]


def test_codex_watcher_exits_without_observing_the_tui(monkeypatch, tmp_path):
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(title, "pane_context", lambda pane, socket=None: pane_context())
    monkeypatch.setattr(title, "process_start_time", lambda pid: 7)
    monkeypatch.setattr(
        title,
        "_newest_agent_process",
        lambda socket, pane: {
            "agent_kind": title.AGENT_CODEX,
            "agent_pid": 99,
            "agent_start_time": 42,
        },
    )
    monkeypatch.setattr(
        title,
        "pane_codex_status",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("Codex titles must come from the listener registry")
        ),
    )

    assert title.watch_pane("/tmp/server", "%1") == 0


def test_discovery_ignores_codex_processes(monkeypatch):
    monkeypatch.setattr(
        title,
        "agent_processes_for_target",
        lambda socket, pane: [{"agent_kind": title.AGENT_CODEX, "agent_pid": 99}],
    )
    assert title.discover_pane_state("/tmp/server", "%1") is None


def test_window_automatic_rename_reads_effective_value(monkeypatch):
    monkeypatch.setattr(title, "tmux", lambda *args, **kwargs: "0")
    assert title.window_automatic_rename("@1", "/tmp/server") is False
    monkeypatch.setattr(title, "tmux", lambda *args, **kwargs: "1")
    assert title.window_automatic_rename("@1", "/tmp/server") is True


def test_server_key_includes_server_lifetime():
    first = title.server_key("/tmp/default", 10)
    second = title.server_key("/tmp/default", 11)
    assert first != second
    assert "-10-" in first
    assert "-11-" in second


def test_thread_name_reads_latest_entry_from_end(monkeypatch, tmp_path):
    index = tmp_path / "session_index.jsonl"
    index.write_text(
        "\n".join(
            [
                json.dumps({"id": "one", "thread_name": "old"}),
                "invalid",
                json.dumps({"id": "two", "thread_name": "other"}),
                json.dumps({"id": "one", "thread_name": "new"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(title, "SESSION_INDEX_PATH", index)
    assert title.thread_name_for_session("one") == "new"


def write_registry(path, connections):
    path.write_text(
        json.dumps(
            {
                "schema": title.SESSION_REGISTRY_SCHEMA,
                "observed_at": "now",
                "connections": connections,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_session_registry_requires_private_owned_metadata(tmp_path):
    registry = tmp_path / "sessions.json"
    write_registry(registry, {})
    assert title.read_session_registry(registry)["connections"] == {}
    registry.chmod(0o622)
    assert title.read_session_registry(registry) is None


def test_registry_sync_maps_confirmed_name_to_live_tmux_pane(monkeypatch, tmp_path):
    registry = tmp_path / "sessions.json"
    write_registry(
        registry,
        {
            "connection": {
                "client": {"pid": 20, "start_ticks": 200},
                "tmux": {
                    "socket": "/tmp/tmux/default",
                    "server_pid": 30,
                    "pane": "%4",
                    "pane_pid": 21,
                },
                "thread": {"id": "019fe555-abcd", "name": "viewtree"},
            }
        },
    )
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(title, "current_server_pid", lambda _socket: 30)
    monkeypatch.setattr(
        title,
        "pane_context",
        lambda _pane, socket=None: pane_context(
            window="@7", active=True, pane_pid=21, server_pid=30
        ),
    )
    monkeypatch.setattr(title, "process_is_alive", lambda pid, start: (pid, start) == (20, 200))
    cached = []
    monkeypatch.setattr(
        title,
        "cache_pane_title",
        lambda *args, **kwargs: cached.append((args, kwargs)) or True,
    )
    reconciled = []
    monkeypatch.setattr(
        title,
        "reconcile_window_from_cache",
        lambda socket, window: reconciled.append((socket, window)) or 1,
    )
    assert title.sync_session_registry(registry) == 1
    assert cached[0][0][:4] == (
        "/tmp/tmux/default",
        "%4",
        "viewtree",
        "019fe555-abcd",
    )
    assert cached[0][1]["source"] == title.SESSION_REGISTRY_SOURCE
    assert reconciled == [("/tmp/tmux/default", "@7")]


def test_registry_sync_reconciles_both_windows_after_pane_moves(monkeypatch, tmp_path):
    registry = tmp_path / "sessions.json"
    write_registry(
        registry,
        {
            "connection": {
                "client": {"pid": 20, "start_ticks": 200},
                "tmux": {
                    "socket": "/tmp/tmux/default",
                    "server_pid": 30,
                    "pane": "%4",
                    "pane_pid": 21,
                },
                "thread": {"id": "019fe555-abcd", "name": "viewtree"},
            }
        },
    )
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path / "state")
    title.atomic_write_json(
        title.pane_state_path("/tmp/tmux/default", 30, "%4"),
        {
            "socket": "/tmp/tmux/default",
            "server_pid": 30,
            "pane": "%4",
            "pane_pid": 21,
            "window_id": "@6",
            "source": title.SESSION_REGISTRY_SOURCE,
        },
    )
    monkeypatch.setattr(title, "current_server_pid", lambda _socket: 30)
    monkeypatch.setattr(
        title,
        "pane_context",
        lambda _pane, socket=None: pane_context(
            window="@7", active=True, pane_pid=21, server_pid=30
        ),
    )
    monkeypatch.setattr(title, "process_is_alive", lambda pid, start: (pid, start) == (20, 200))
    reconciled = []
    monkeypatch.setattr(
        title,
        "reconcile_window_from_cache",
        lambda socket, window: reconciled.append((socket, window)) or 1,
    )

    assert title.sync_session_registry(
        registry, window_filter=("/tmp/tmux/default", "@7")
    ) == 2
    assert reconciled == [
        ("/tmp/tmux/default", "@6"),
        ("/tmp/tmux/default", "@7"),
    ]


def test_registry_sync_removes_only_listener_owned_stale_state(monkeypatch, tmp_path):
    registry = tmp_path / "sessions.json"
    write_registry(registry, {})
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path / "state")
    stale = title.pane_state_path("/tmp/tmux/default", 30, "%4")
    title.atomic_write_json(
        stale,
        {
            "socket": "/tmp/tmux/default",
            "server_pid": 30,
            "pane": "%4",
            "window_id": "@7",
            "source": title.SESSION_REGISTRY_SOURCE,
        },
    )
    legacy = title.pane_state_path("/tmp/tmux/default", 30, "%5")
    title.atomic_write_json(
        legacy,
        {
            "socket": "/tmp/tmux/default",
            "server_pid": 30,
            "pane": "%5",
            "window_id": "@8",
        },
    )
    reconciled = []
    monkeypatch.setattr(
        title,
        "reconcile_window_from_cache",
        lambda socket, window: reconciled.append((socket, window)) or 1,
    )
    assert title.sync_session_registry(registry) == 1
    assert not stale.exists()
    assert legacy.exists()
    assert reconciled == [("/tmp/tmux/default", "@7")]


def test_registry_mode_disables_per_pane_watcher(monkeypatch):
    monkeypatch.setattr(title, "session_registry_active", lambda: True)
    assert title.ensure_pane_watcher("/tmp/tmux/default", "%4") is False


def test_sync_all_reconciles_windows_and_backfills_watchers(monkeypatch):
    monkeypatch.setattr(
        title,
        "tmux",
        lambda *args, **kwargs: "@1\n@2" if args[0] == "list-windows" else "",
    )
    reconciled = []
    monkeypatch.setattr(
        title,
        "reconcile_window",
        lambda socket, window: reconciled.append((socket, window)) or 1,
    )
    watched = []
    monkeypatch.setattr(
        title,
        "ensure_window_watcher",
        lambda socket, window: watched.append((socket, window)) or True,
    )

    assert title.sync_all("/tmp/server") == 2
    assert reconciled == [
        ("/tmp/server", "@1"),
        ("/tmp/server", "@2"),
    ]
    assert watched == reconciled


def test_cache_is_expired_when_agent_process_exits(monkeypatch, tmp_path):
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(title, "pane_context", lambda pane, socket=None: pane_context())
    monkeypatch.setattr(title, "process_is_alive", lambda pid, start: False)
    assert title.cache_pane_title(
        "/tmp/server", "%1", "task", "session", agent_pid=99, agent_start_time=42
    )
    assert title.cached_pane_state("/tmp/server", "%1") is None


def test_starting_title_is_provisional_and_shell_return_clears_it(monkeypatch, tmp_path):
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(title, "pane_context", lambda pane, socket=None: pane_context())
    monkeypatch.setattr(title, "current_server_pid", lambda socket: 1)
    monkeypatch.setattr(title, "process_is_alive", lambda pid, start: True)
    reconciled = []
    monkeypatch.setattr(
        title,
        "reconcile_window",
        lambda socket, window: reconciled.append((socket, window)) or 1,
    )
    monkeypatch.setattr(
        title,
        "reconcile_window_from_cache",
        lambda socket, window: reconciled.append((socket, window)) or 1,
    )

    assert title.mark_pane_starting("/tmp/server", "%1") == 1
    state = title.cached_pane_state("/tmp/server", "%1")
    assert state is not None
    assert state["title"] == "codex"
    assert state["provisional"] is True

    assert title.finish_pane_starting("/tmp/server", "%1") == 1
    assert title.cached_pane_state("/tmp/server", "%1") is None
    assert reconciled == [
        ("/tmp/server", "@1"),
        ("/tmp/server", "@1"),
    ]


def test_shell_return_does_not_clear_lifecycle_owned_title(monkeypatch):
    monkeypatch.setattr(title, "pane_context", lambda pane, socket=None: pane_context())
    monkeypatch.setattr(
        title,
        "cached_pane_state",
        lambda socket, pane: {"title": "real task", "provisional": False},
    )
    monkeypatch.setattr(
        title,
        "clear_pane_state",
        lambda *args: (_ for _ in ()).throw(AssertionError("must preserve real state")),
    )
    assert title.finish_pane_starting("/tmp/server", "%1") == 0


def test_hook_title_uses_latest_session_name(monkeypatch):
    monkeypatch.setattr(title, "thread_name_for_session", lambda value: "My task")
    assert title.title_for_hook({"session_id": "abc-def"}) == ("My task", "abc-def")


def test_resume_last_never_becomes_a_title():
    process = {"transcripts": [], "resume": ""}
    assert title.derive_process_title(process) == ("codex", "")


def test_reconcile_restores_original_name_for_shell_pane(monkeypatch, tmp_path):
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(title, "current_server_pid", lambda socket: 1)
    monkeypatch.setattr(title, "window_lock", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(title, "window_active_pane", lambda window, socket: "%2")
    monkeypatch.setattr(title, "cached_pane_state", lambda socket, pane: None)
    monkeypatch.setattr(title, "discover_pane_state", lambda socket, pane: None)

    state_path = title.window_state_path("/tmp/server", 1, "@1")
    title.atomic_write_json(
        state_path,
        {
            "version": 2,
            "socket": "/tmp/server",
            "server_pid": 1,
            "window_id": "@1",
            "original_name": "shell",
            "original_automatic_rename": True,
            "last_applied": "agent task",
        },
    )
    names = ["agent task"]
    automatic = []
    monkeypatch.setattr(title, "window_name", lambda window, socket: names[-1])
    monkeypatch.setattr(
        title,
        "rename_window",
        lambda window, value, socket: names.append(value) or True,
    )
    monkeypatch.setattr(
        title,
        "set_window_automatic_rename",
        lambda window, enabled, socket: automatic.append(enabled) or True,
    )

    assert title.reconcile_window("/tmp/server", "@1") == 1
    assert names[-1] == "shell"
    assert automatic[-1] is True
    assert not state_path.exists()


def test_reconcile_rechecks_active_pane_inside_serialized_operation(monkeypatch, tmp_path):
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(title, "current_server_pid", lambda socket: 1)
    monkeypatch.setattr(title, "window_lock", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(title, "window_active_pane", lambda window, socket: "%new")
    monkeypatch.setattr(
        title,
        "cached_pane_state",
        lambda socket, pane: {"title": "new title"} if pane == "%new" else {"title": "old"},
    )
    applied = []
    monkeypatch.setattr(
        title,
        "_apply_window_state",
        lambda socket, server_pid, window, pane, value: applied.append((pane, value)) or True,
    )
    assert title.reconcile_window("/tmp/server", "@1") == 1
    assert applied == [("%new", "new title")]


def test_manual_window_rename_is_preserved_on_restore(monkeypatch, tmp_path):
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path)
    path = title.window_state_path("/tmp/server", 1, "@1")
    title.atomic_write_json(
        path,
        {
            "version": 2,
            "socket": "/tmp/server",
            "server_pid": 1,
            "window_id": "@1",
            "original_name": "shell",
            "original_automatic_rename": True,
            "last_applied": "agent task",
        },
    )
    monkeypatch.setattr(title, "window_name", lambda window, socket: "manual")
    monkeypatch.setattr(
        title,
        "rename_window",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    monkeypatch.setattr(
        title,
        "set_window_automatic_rename",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    assert title._restore_window_state("/tmp/server", 1, "@1")
    assert not path.exists()


def test_session_end_clears_pane_and_reconciles(monkeypatch):
    monkeypatch.setattr(
        title,
        "resolve_hook_target_details",
        lambda payload, env: {
            "socket": "/tmp/server",
            "pane": "%1",
            "ambiguous": False,
            "agent_kind": title.AGENT_CLAUDE,
        },
    )
    monkeypatch.setattr(title, "pane_context", lambda pane, socket=None: pane_context())
    cleared = []
    monkeypatch.setattr(
        title,
        "clear_pane_state",
        lambda socket, pane: cleared.append((socket, pane)) or "@1",
    )
    reconciled = []
    monkeypatch.setattr(
        title,
        "reconcile_window",
        lambda socket, window: reconciled.append((socket, window)) or 1,
    )
    assert title.sync_hook({"hook_event_name": "SessionEnd", "session_id": "s"}) == 1
    assert cleared == [("/tmp/server", "%1")]
    assert reconciled == [("/tmp/server", "@1")]


def write_claude_transcript(path, titles):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "user", "message": {"content": "hello"}})]
    for value in titles:
        lines.append(json.dumps({"type": "ai-title", "aiTitle": value}))
        lines.append(json.dumps({"type": "assistant", "message": {"content": "ok"}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_claude_transcript_title_reads_the_newest_entry(tmp_path):
    transcript = write_claude_transcript(
        tmp_path / "s1.jsonl", ["first title", "仓库 Claude 支持"]
    )
    assert title.claude_transcript_title(transcript) == "仓库 Claude 支持"


def test_claude_title_prefers_a_user_chosen_session_name(tmp_path):
    transcript = write_claude_transcript(tmp_path / "s1.jsonl", ["ai title"])
    record = {"name": "release work", "nameSource": "user"}
    assert title.claude_session_title("s1", transcript, record) == "release work"


def test_claude_title_falls_back_from_ai_title_to_derived_name(tmp_path):
    empty = tmp_path / "s1.jsonl"
    empty.write_text("", encoding="utf-8")
    record = {"name": "codex-tmux-integration-5f", "nameSource": "derived"}
    assert (
        title.claude_session_title("s1", empty, record) == "codex-tmux-integration-5f"
    )


def test_claude_title_falls_back_to_a_session_prefix():
    assert (
        title.claude_session_title("f8906817-a839-49e1-8e7e-5682ff1d31fc")
        == "claude-f8906817"
    )


def test_claude_hook_title_comes_from_the_payload_transcript(tmp_path, monkeypatch):
    transcript = write_claude_transcript(tmp_path / "s1.jsonl", ["仓库 Claude 支持"])
    monkeypatch.setattr(
        title,
        "thread_name_for_session",
        lambda session_id: (_ for _ in ()).throw(
            AssertionError("a Claude hook must not read the Codex session index")
        ),
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "s1",
        "transcript_path": str(transcript),
    }
    assert title.title_for_hook(payload, title.AGENT_CLAUDE) == (
        "仓库 Claude 支持",
        "s1",
    )


def test_hook_agent_kind_prefers_the_resolved_process(monkeypatch, tmp_path):
    monkeypatch.setattr(title, "CLAUDE_PROJECTS_ROOT", tmp_path / "claude/projects")
    target = {"agent_kind": title.AGENT_CLAUDE}
    assert title.hook_agent_kind({}, target) == title.AGENT_CLAUDE


def test_hook_agent_kind_falls_back_to_the_transcript_location(monkeypatch, tmp_path):
    claude_projects = tmp_path / "claude/projects"
    codex_home = tmp_path / "codex"
    monkeypatch.setattr(title, "CLAUDE_PROJECTS_ROOT", claude_projects)
    monkeypatch.setattr(title, "CODEX_HOME", codex_home)

    claude_payload = {"transcript_path": str(claude_projects / "-repo/s1.jsonl")}
    codex_payload = {"transcript_path": str(codex_home / "sessions/2026/s1.jsonl")}
    assert title.hook_agent_kind(claude_payload, {}) == title.AGENT_CLAUDE
    assert title.hook_agent_kind(codex_payload, {}) == title.AGENT_CODEX
    assert title.hook_agent_kind({}, {}) == title.AGENT_CODEX


def test_derive_process_title_uses_claude_session_state(tmp_path):
    transcript = write_claude_transcript(tmp_path / "s1.jsonl", ["repo cleanup"])
    process = {
        "agent_kind": title.AGENT_CLAUDE,
        "session": {"sessionId": "s1", "name": "repo-5f", "nameSource": "derived"},
        "transcripts": [str(transcript)],
        "resume": "",
    }
    assert title.derive_process_title(process) == ("repo cleanup", "s1")


def test_claude_state_stamp_tracks_session_file_and_transcript(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(title, "CLAUDE_SESSIONS_ROOT", sessions)
    record = sessions / "99.json"
    record.write_text("{}", encoding="utf-8")
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text("", encoding="utf-8")

    first = title.agent_state_stamp(title.AGENT_CLAUDE, 99, str(transcript))
    record.write_text('{"pid": 99}', encoding="utf-8")
    assert title.agent_state_stamp(title.AGENT_CLAUDE, 99, str(transcript)) != first


def test_watcher_reads_claude_state_instead_of_capturing_the_pane(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(title, "pane_context", lambda pane, socket=None: pane_context())
    monkeypatch.setattr(title, "process_start_time", lambda pid: 7)
    monkeypatch.setattr(title, "current_server_pid", lambda socket: 1)
    monkeypatch.setattr(
        title,
        "_newest_agent_process",
        lambda socket, pane: {
            "agent_pid": 99,
            "agent_start_time": 42,
            "agent_kind": title.AGENT_CLAUDE,
            "transcripts": ["/tmp/s1.jsonl"],
        },
    )
    alive = iter([True, False])
    monkeypatch.setattr(title, "process_is_alive", lambda pid, start: next(alive))
    monkeypatch.setattr(
        title,
        "pane_codex_status",
        lambda socket, pane: (_ for _ in ()).throw(
            AssertionError("Claude panes must not be captured")
        ),
    )
    monkeypatch.setattr(
        title, "claude_observation", lambda pid, transcript: ("repo cleanup", "s1")
    )
    observations = []
    monkeypatch.setattr(
        title,
        "apply_pane_observation",
        lambda *args, **kwargs: observations.append((args, kwargs)) or 1,
    )
    monkeypatch.setattr(title, "clear_pane_state", lambda socket, pane: "@1")
    monkeypatch.setattr(title, "reconcile_window_from_cache", lambda socket, window: 1)

    assert title.watch_pane("/tmp/server", "%1") == 0
    assert len(observations) == 1
    assert observations[0][0][2:] == ("repo cleanup", "s1")
    assert observations[0][1]["agent_kind"] == title.AGENT_CLAUDE
