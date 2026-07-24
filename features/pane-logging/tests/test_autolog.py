import importlib.machinery
import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "tmux-autolog"
LOADER = importlib.machinery.SourceFileLoader("tmux_autolog_test", str(MODULE_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
autolog = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(autolog)


def test_server_key_changes_when_server_restarts():
    assert autolog.server_key("/tmp/server", 1) != autolog.server_key("/tmp/server", 2)


def test_existing_user_pipe_is_preserved(monkeypatch):
    monkeypatch.setattr(autolog, "server_pid", lambda socket: 1)
    monkeypatch.setattr(
        autolog,
        "pane_info",
        lambda socket, pane: {"pane": pane, "pipe": True, "window": "shell"},
    )
    monkeypatch.setattr(autolog, "marker", lambda socket, pane: None)
    monkeypatch.setattr(
        autolog,
        "tmux_result",
        lambda *args: (_ for _ in ()).throw(AssertionError()),
    )
    assert not autolog.enable_on("/tmp/server", "%1")


def test_owned_pipe_is_not_replaced(monkeypatch):
    monkeypatch.setattr(autolog, "server_pid", lambda socket: 1)
    monkeypatch.setattr(
        autolog,
        "pane_info",
        lambda socket, pane: {"pane": pane, "pipe": True, "window": "shell"},
    )
    monkeypatch.setattr(autolog, "marker", lambda socket, pane: {"version": 2})
    assert autolog.enable_on("/tmp/server", "%1")


def test_cleanup_preserves_replaced_user_pipe(monkeypatch, tmp_path):
    pid_file = tmp_path / "sink.pid"
    logfile = tmp_path / "pane.log"
    monkeypatch.setattr(
        autolog,
        "pane_info",
        lambda socket, pane: {"pane": pane, "pipe": True, "window": "shell"},
    )
    monkeypatch.setattr(
        autolog,
        "marker",
        lambda socket, pane: {"pid_file": str(pid_file), "logfile": str(logfile)},
    )
    calls = []
    monkeypatch.setattr(autolog, "tmux_result", lambda socket, *args: calls.append(args))
    monkeypatch.setattr(autolog, "unset_marker", lambda socket, pane: calls.append(("unset", pane)))
    assert autolog.close_owned_pipe("/tmp/server", "%1")
    assert not any(call and call[0] == "pipe-pane" for call in calls)
    assert ("unset", "%1") in calls


def test_migrate_replaces_only_verified_legacy_sink(monkeypatch, tmp_path):
    monkeypatch.setattr(autolog, "LOGDIR", tmp_path)
    monkeypatch.setattr(autolog, "list_panes", lambda socket: ["%1", "%2"])
    monkeypatch.setattr(
        autolog,
        "pane_info",
        lambda socket, pane: {"pane": pane, "pipe": True, "window": "shell"},
    )
    monkeypatch.setattr(autolog, "marker", lambda socket, pane: None)
    legacy_one = tmp_path / autolog.legacy_server_key("/tmp/server") / "1.log"
    monkeypatch.setattr(
        autolog,
        "_find_sink_pid",
        lambda path: 100 if path == str(legacy_one) else -1,
    )
    calls = []
    monkeypatch.setattr(autolog, "tmux_result", lambda socket, *args: calls.append(args))
    monkeypatch.setattr(autolog, "enable_on", lambda socket, pane: calls.append(("enable", pane)) or True)
    assert autolog.migrate("/tmp/server") == 1
    assert ("pipe-pane", "-t", "%1") in calls
    assert ("enable", "%1") in calls
    assert not any(call == ("pipe-pane", "-t", "%2") for call in calls)
