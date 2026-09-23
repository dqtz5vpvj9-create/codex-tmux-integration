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


def test_hook_repairs_clear_hooks_before_marking(monkeypatch):
    calls = []
    monkeypatch.setattr(
        notify,
        "resolve_hook_target_details",
        lambda payload, env: {
            "ambiguous": False,
            "socket": "/tmp/server",
            "pane": "%1",
        },
    )
    monkeypatch.setattr(notify, "window_for_pane", lambda socket, pane: "@1")
    monkeypatch.setattr(
        notify,
        "ensure_clear_hooks",
        lambda socket: calls.append(("ensure", socket)) or True,
    )
    monkeypatch.setattr(
        notify,
        "mark_window",
        lambda socket, window, style, pane, kind: calls.append(
            ("mark", socket, window, style, pane, kind)
        )
        or True,
    )
    assert notify.mark_from_hook({"hook_event_name": "Stop"})
    assert calls == [
        ("ensure", "/tmp/server"),
        ("mark", "/tmp/server", "@1", notify.DEFAULT_STYLE, "%1", "done"),
    ]


def test_hook_fails_closed_when_clear_hooks_cannot_be_repaired(monkeypatch):
    monkeypatch.setattr(
        notify,
        "resolve_hook_target_details",
        lambda payload, env: {
            "ambiguous": False,
            "socket": "/tmp/server",
            "pane": "%1",
        },
    )
    monkeypatch.setattr(notify, "window_for_pane", lambda socket, pane: "@1")
    monkeypatch.setattr(notify, "ensure_clear_hooks", lambda socket: False)
    monkeypatch.setattr(
        notify,
        "mark_window",
        lambda *args: (_ for _ in ()).throw(AssertionError("unexpected highlight")),
    )
    assert not notify.mark_from_hook({"hook_event_name": "Stop"})


def test_waiting_events_are_told_apart_from_finished_turns():
    assert notify.attention_kind({"hook_event_name": "Stop"}) == "done"
    assert notify.attention_kind({"hook_event_name": "Notification"}) == "input"
    assert notify.attention_kind({"hook_event_name": "PermissionRequest"}) == "input"


def _mark_with(monkeypatch, tmp_path, viewers, in_view):
    marked, styled = [], []
    monkeypatch.setattr(notify, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(notify, "current_server_pid", lambda socket: 1)
    monkeypatch.setattr(notify, "window_lock", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(notify, "active_clients", lambda socket, window: viewers)
    monkeypatch.setattr(notify, "pane_in_view", lambda socket, pane: in_view)
    monkeypatch.setattr(notify, "current_style", lambda socket, window: ("default", False))
    monkeypatch.setattr(
        notify, "set_pane_attention", lambda socket, pane, kind: marked.append((pane, kind))
    )
    monkeypatch.setattr(
        notify, "set_style", lambda socket, window, value: styled.append(value) or True
    )
    result = notify.mark_window("/tmp/server", "@1", "attention", "%7", "input")
    return result, marked, styled


def test_unwatched_pane_is_marked_along_with_its_window(monkeypatch, tmp_path):
    assert _mark_with(monkeypatch, tmp_path, viewers=0, in_view=True) == (
        True,
        [("%7", "input")],
        ["attention"],
    )


def test_pane_on_screen_is_not_marked(monkeypatch, tmp_path):
    assert _mark_with(monkeypatch, tmp_path, viewers=1, in_view=True) == (False, [], [])


def test_pane_hidden_by_zoom_is_marked_without_a_window_highlight(monkeypatch, tmp_path):
    assert _mark_with(monkeypatch, tmp_path, viewers=1, in_view=False) == (
        False,
        [("%7", "input")],
        [],
    )


def _clear_with(monkeypatch, listing):
    unset = []
    monkeypatch.setattr(notify, "tmux", lambda *args: listing)
    monkeypatch.setattr(
        notify,
        "tmux_result",
        lambda socket, *args: unset.append(args[4]) if args[0] == "set-option" else None,
    )
    notify.clear_pane_attention("/tmp/server", "@1")
    return unset


def test_focus_clears_every_marked_pane_of_an_unzoomed_window(monkeypatch):
    listing = "%1 1 0 \n%2 0 0 done:10\n%3 0 0 input:11"
    assert _clear_with(monkeypatch, listing) == ["%2", "%3"]


def test_focus_on_a_zoomed_window_clears_only_the_pane_in_view(monkeypatch):
    listing = "%1 1 1 done:9\n%2 0 1 input:11"
    assert _clear_with(monkeypatch, listing) == ["%1"]
