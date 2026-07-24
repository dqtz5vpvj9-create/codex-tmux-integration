import contextlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "codex-tmux-title-sync"
LOADER = importlib.machinery.SourceFileLoader("codex_tmux_title_test", str(MODULE_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
title = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(title)


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


def test_cache_is_expired_when_agent_process_exits(monkeypatch, tmp_path):
    monkeypatch.setattr(title, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(title, "pane_context", lambda pane, socket=None: pane_context())
    monkeypatch.setattr(title, "process_is_alive", lambda pid, start: False)
    assert title.cache_pane_title(
        "/tmp/server", "%1", "task", "session", agent_pid=99, agent_start_time=42
    )
    assert title.cached_pane_state("/tmp/server", "%1") is None


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
