import importlib.machinery
import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "codex-tmux-title-sync"
LOADER = importlib.machinery.SourceFileLoader("codex_tmux_title_test", str(MODULE_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
title = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(title)


def test_shorten_and_clean():
    assert title.shorten("hello\x1b[31m world") == "hello world"
    assert title.shorten("abcdefgh", 7) == "abcd..."


def test_server_key_separates_same_named_sockets():
    first = title.server_key("/tmp/a/default")
    second = title.server_key("/tmp/b/default")
    assert first != second
    assert first.startswith("default-")
    assert second.startswith("default-")


def test_cache_is_per_server_and_pane(monkeypatch, tmp_path):
    monkeypatch.setattr(title, "TITLE_CACHE_ROOT", tmp_path)
    monkeypatch.setattr(
        title,
        "pane_context",
        lambda pane, socket=None: {
            "server_pid": 1 if socket.endswith("one") else 2,
            "pane_pid": 10 if socket.endswith("one") else 20,
            "window_id": "@1",
            "pane_active": True,
        },
    )
    assert title.cache_pane_title("/tmp/one", "%0", "one")
    assert title.cache_pane_title("/tmp/two", "%0", "two")
    assert title.cached_pane_title("/tmp/one", "%0") == "one"
    assert title.cached_pane_title("/tmp/two", "%0") == "two"


def test_hook_title_uses_session_name(monkeypatch):
    monkeypatch.setattr(title, "thread_name_for_session", lambda value: "My task")
    assert title.title_for_hook({"session_id": "abc-def"}) == ("My task", "abc-def")
