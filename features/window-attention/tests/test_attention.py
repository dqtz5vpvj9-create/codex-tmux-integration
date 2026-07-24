import contextlib
import importlib.machinery
import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "agent-tmux-notify"
LOADER = importlib.machinery.SourceFileLoader("agent_tmux_notify_test", str(MODULE_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
notify = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(notify)


def test_current_style_prefers_exact_local_value(monkeypatch):
    class Result:
        returncode = 0
        stdout = "fg=black,bg=#d7af00,bold\n"

    monkeypatch.setattr(notify, "tmux_result", lambda *args: Result())
    monkeypatch.setattr(
        notify,
        "tmux",
        lambda *args: "bold,fg=black,bg=#d7af00",
    )
    assert notify.current_style("/tmp/server", "@1") == (
        "fg=black,bg=#d7af00,bold",
        True,
    )


def test_current_style_reads_inherited_global_value(monkeypatch):
    class Result:
        def __init__(self, stdout):
            self.returncode = 0
            self.stdout = stdout

    results = iter((Result(""), Result("default\n")))
    monkeypatch.setattr(notify, "tmux_result", lambda *args: next(results))
    assert notify.current_style("/tmp/server", "@1") == ("default", False)


def test_active_window_is_not_highlighted(monkeypatch, tmp_path):
    monkeypatch.setattr(notify, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(notify, "current_server_pid", lambda socket: 1)
    monkeypatch.setattr(notify, "window_lock", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(notify, "active_clients", lambda socket, window: 1)
    monkeypatch.setattr(notify, "_restore_locked", lambda *args: False)
    monkeypatch.setattr(
        notify,
        "set_style",
        lambda *args: (_ for _ in ()).throw(AssertionError()),
    )
    assert not notify.mark_window("/tmp/server", "@1", "style")


def test_inactive_window_saves_and_restores_existing_style(monkeypatch, tmp_path):
    monkeypatch.setattr(notify, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(notify, "current_server_pid", lambda socket: 1)
    monkeypatch.setattr(notify, "window_lock", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(notify, "active_clients", lambda socket, window: 0)
    style = ["fg=blue"]
    monkeypatch.setattr(notify, "current_style", lambda socket, window: (style[-1], True))
    monkeypatch.setattr(
        notify,
        "set_style",
        lambda socket, window, value: style.append(value) or True,
    )
    monkeypatch.setattr(
        notify,
        "unset_style",
        lambda *args: (_ for _ in ()).throw(AssertionError()),
    )
    assert notify.mark_window("/tmp/server", "@1", "attention")
    assert style[-1] == "attention"
    assert notify.clear_window("/tmp/server", "@1")
    assert style[-1] == "fg=blue"


def test_inherited_style_is_unset_on_restore(monkeypatch, tmp_path):
    monkeypatch.setattr(notify, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(notify, "current_server_pid", lambda socket: 1)
    monkeypatch.setattr(notify, "window_lock", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(notify, "active_clients", lambda socket, window: 0)
    style = ["default"]
    monkeypatch.setattr(notify, "current_style", lambda socket, window: (style[-1], False))
    monkeypatch.setattr(
        notify,
        "set_style",
        lambda socket, window, value: style.append(value) or True,
    )
    unset = []
    monkeypatch.setattr(notify, "unset_style", lambda socket, window: unset.append(window) or True)
    assert notify.mark_window("/tmp/server", "@1", "attention")
    assert notify.clear_window("/tmp/server", "@1")
    assert unset == ["@1"]


def test_user_style_change_is_preserved(monkeypatch, tmp_path):
    monkeypatch.setattr(notify, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(notify, "current_server_pid", lambda socket: 1)
    monkeypatch.setattr(notify, "window_lock", lambda *args: contextlib.nullcontext())
    path = notify.state_path("/tmp/server", 1, "@1")
    notify.atomic_write_json(
        path,
        {
            "socket": "/tmp/server",
            "server_pid": 1,
            "window_id": "@1",
            "original_style": "old",
            "original_explicit": True,
            "last_applied": "attention",
        },
    )
    monkeypatch.setattr(notify, "current_style", lambda socket, window: ("manual", True))
    monkeypatch.setattr(
        notify,
        "set_style",
        lambda *args: (_ for _ in ()).throw(AssertionError()),
    )
    monkeypatch.setattr(
        notify,
        "unset_style",
        lambda *args: (_ for _ in ()).throw(AssertionError()),
    )
    assert notify.clear_window("/tmp/server", "@1")
    assert not path.exists()


def test_mark_rechecks_focus_under_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(notify, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(notify, "current_server_pid", lambda socket: 1)
    entered = []

    @contextlib.contextmanager
    def lock(*args):
        entered.append(True)
        yield

    monkeypatch.setattr(notify, "window_lock", lock)
    monkeypatch.setattr(notify, "active_clients", lambda socket, window: 1)
    monkeypatch.setattr(notify, "_restore_locked", lambda *args: False)
    assert not notify.mark_window("/tmp/server", "@1", "attention")
    assert entered == [True]


def test_ambiguous_hook_target_is_ignored(monkeypatch):
    monkeypatch.setattr(
        notify,
        "resolve_hook_target_details",
        lambda payload, env: {"ambiguous": True, "socket": "", "pane": ""},
    )
    assert not notify.mark_from_hook({"session_id": "x"})
