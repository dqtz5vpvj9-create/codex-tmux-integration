import importlib.machinery
import importlib.util
import io
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "agent-tmux-notify"
LOADER = importlib.machinery.SourceFileLoader("agent_tmux_notify_test", str(MODULE_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
notify = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(notify)


def test_active_window_is_not_highlighted(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "resolve_hook_target", lambda payload, env: ("/tmp/s", "%1"))

    def fake_tmux(socket, *args):
        calls.append(args)
        return "@1" if "#{window_id}" in args else "1"

    monkeypatch.setattr(notify, "tmux", fake_tmux)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id":"x"}'))
    assert notify.main() == 0
    assert not any("set-window-option" in call for call in calls)


def test_inactive_window_is_highlighted(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "resolve_hook_target", lambda payload, env: ("/tmp/s", "%1"))

    def fake_tmux(socket, *args):
        calls.append(args)
        return "@1" if "#{window_id}" in args else "0"

    monkeypatch.setattr(notify, "tmux", fake_tmux)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id":"x"}'))
    assert notify.main() == 0
    assert any("set-window-option" in call for call in calls)
