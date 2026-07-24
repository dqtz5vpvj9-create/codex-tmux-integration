import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "agent_tmux_context.py"
SPEC = importlib.util.spec_from_file_location("agent_tmux_context_test", MODULE_PATH)
context = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(context)


def target(socket="/tmp/tmux/server", pane="%7", server_pid=123, pane_pid=456):
    return {
        "socket": socket,
        "pane": pane,
        "server_pid": server_pid,
        "pane_pid": pane_pid,
        "window_id": "@1",
    }


def test_direct_environment_is_live_probed(monkeypatch):
    probes = []

    def fake_probe(socket, pane, expected_server_pid=-1):
        probes.append((socket, pane, expected_server_pid))
        return target(socket, pane, expected_server_pid)

    monkeypatch.setattr(context, "_tmux_probe", fake_probe)
    monkeypatch.setattr(context, "_ancestor_codex_pids", lambda: [])
    monkeypatch.setattr(context, "_codex_pids", lambda: [])
    env = {"TMUX": "/tmp/tmux/server,123,0", "TMUX_PANE": "%7"}
    resolved = context.resolve_hook_target_details({}, env)
    assert (resolved["socket"], resolved["pane"]) == ("/tmp/tmux/server", "%7")
    assert probes == [("/tmp/tmux/server", "%7", 123)]


def test_invalid_direct_environment_is_not_trusted(monkeypatch):
    monkeypatch.setattr(context, "_tmux_probe", lambda *args, **kwargs: None)
    monkeypatch.setattr(context, "_codex_pids", lambda: [])
    env = {"TMUX": "/tmp/tmux/server,123,0", "TMUX_PANE": "%7"}
    assert context.resolve_hook_target({}, env) == ("", "")


def test_ancestor_process_identity_is_returned(monkeypatch):
    monkeypatch.setattr(context, "_tmux_probe", lambda *args, **kwargs: target())
    monkeypatch.setattr(context, "_ancestor_codex_pids", lambda: [999])
    monkeypatch.setattr(
        context,
        "_proc_environ",
        lambda pid: {"TMUX": "/tmp/tmux/server,123,0", "TMUX_PANE": "%7"},
    )
    monkeypatch.setattr(context, "process_start_time", lambda pid: 321)
    env = {"TMUX": "/tmp/tmux/server,123,0", "TMUX_PANE": "%7"}
    resolved = context.resolve_hook_target_details({}, env)
    assert resolved["agent_pid"] == 999
    assert resolved["agent_start_time"] == 321


def test_missing_identity_does_not_scan(monkeypatch):
    monkeypatch.setattr(context, "_direct_target", lambda env: None)
    monkeypatch.setattr(context, "_codex_pids", lambda: (_ for _ in ()).throw(AssertionError()))
    assert context.resolve_hook_target({}, {}) == ("", "")


def test_equal_score_in_different_panes_is_ambiguous(monkeypatch):
    monkeypatch.setattr(context, "_direct_target", lambda env: None)
    monkeypatch.setattr(context, "_codex_pids", lambda: [100, 200])
    candidates = {
        100: {**target("/tmp/a", "%1"), "agent_pid": 100, "agent_start_time": 1, "score": 300, "ambiguous": False},
        200: {**target("/tmp/b", "%2"), "agent_pid": 200, "agent_start_time": 2, "score": 300, "ambiguous": False},
    }
    monkeypatch.setattr(context, "_candidate", lambda pid, payload: candidates[pid])
    resolved = context.resolve_hook_target_details({"session_id": "session"}, {})
    assert resolved["ambiguous"] is True
    assert context.resolve_hook_target({"session_id": "session"}, {}) == ("", "")


def test_same_target_prefers_newer_process(monkeypatch):
    monkeypatch.setattr(context, "_direct_target", lambda env: None)
    monkeypatch.setattr(context, "_codex_pids", lambda: [100, 200])
    candidates = {
        100: {**target(), "agent_pid": 100, "agent_start_time": 10, "score": 300, "ambiguous": False},
        200: {**target(), "agent_pid": 200, "agent_start_time": 20, "score": 300, "ambiguous": False},
    }
    monkeypatch.setattr(context, "_candidate", lambda pid, payload: candidates[pid])
    resolved = context.resolve_hook_target_details({"session_id": "session"}, {})
    assert resolved["agent_pid"] == 200


def test_resume_last_is_not_treated_as_a_session(monkeypatch):
    monkeypatch.setattr(context, "_proc_cmdline", lambda pid: ["codex", "resume", "--last"])
    assert context._resume_session_id(1) == ""


def test_resume_skips_options_with_values(monkeypatch):
    monkeypatch.setattr(
        context,
        "_proc_cmdline",
        lambda pid: ["codex", "resume", "--model", "gpt", "--all", "session-name"],
    )
    assert context._resume_session_id(1) == "session-name"


def test_custom_codex_home_is_used():
    assert context._codex_home({"CODEX_HOME": "/tmp/custom"}) == Path("/tmp/custom")


def test_process_liveness_checks_start_time(monkeypatch):
    monkeypatch.setattr(context, "process_start_time", lambda pid: 42)
    assert context.process_is_alive(10, 42)
    assert not context.process_is_alive(10, 41)
