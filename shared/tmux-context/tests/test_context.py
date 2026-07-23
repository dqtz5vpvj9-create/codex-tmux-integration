import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "agent_tmux_context.py"
SPEC = importlib.util.spec_from_file_location("agent_tmux_context_test", MODULE_PATH)
context = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(context)


def test_direct_environment_wins():
    env = {"TMUX": "/tmp/tmux-1000/server,123,0", "TMUX_PANE": "%7"}
    assert context.resolve_hook_target({}, env) == ("/tmp/tmux-1000/server", "%7")


def test_missing_identity_does_not_scan(monkeypatch):
    monkeypatch.setattr(context, "_codex_pids", lambda: (_ for _ in ()).throw(AssertionError()))
    assert context.resolve_hook_target({}, {}) == ("", "")


def test_session_fallback_selects_matching_server(monkeypatch):
    monkeypatch.setattr(context, "_codex_pids", lambda: iter((100, 200)))
    monkeypatch.setattr(
        context,
        "_proc_environ",
        lambda pid: {
            "TMUX": f"/tmp/tmux-1000/server-{pid},1,0",
            "TMUX_PANE": "%0",
        },
    )
    monkeypatch.setattr(context, "_open_transcripts", lambda pid: [])
    monkeypatch.setattr(
        context,
        "_resume_session_id",
        lambda pid: "wanted-session" if pid == 200 else "other-session",
    )
    assert context.resolve_hook_target({"session_id": "wanted-session"}, {}) == (
        "/tmp/tmux-1000/server-200",
        "%0",
    )
