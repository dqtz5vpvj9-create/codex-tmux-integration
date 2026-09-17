import importlib.util
import json
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
    monkeypatch.setattr(context, "_ancestor_agent_pids", lambda: [])
    monkeypatch.setattr(context, "_agent_pids", lambda: [])
    env = {"TMUX": "/tmp/tmux/server,123,0", "TMUX_PANE": "%7"}
    resolved = context.resolve_hook_target_details({}, env)
    assert (resolved["socket"], resolved["pane"]) == ("/tmp/tmux/server", "%7")
    assert probes == [("/tmp/tmux/server", "%7", 123)]


def test_invalid_direct_environment_is_not_trusted(monkeypatch):
    monkeypatch.setattr(context, "_tmux_probe", lambda *args, **kwargs: None)
    monkeypatch.setattr(context, "_agent_pids", lambda: [])
    env = {"TMUX": "/tmp/tmux/server,123,0", "TMUX_PANE": "%7"}
    assert context.resolve_hook_target({}, env) == ("", "")


def test_ancestor_process_identity_is_returned(monkeypatch):
    monkeypatch.setattr(context, "_tmux_probe", lambda *args, **kwargs: target())
    monkeypatch.setattr(context, "_ancestor_agent_pids", lambda: [(999, "codex")])
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
    assert resolved["agent_kind"] == "codex"


def test_ancestor_routes_hook_when_hook_environment_omits_tmux(monkeypatch):
    monkeypatch.setattr(context, "_direct_target", lambda env: None)
    monkeypatch.setattr(context, "_ancestor_agent_pids", lambda: [(999, "claude")])
    monkeypatch.setattr(
        context,
        "_proc_environ",
        lambda pid: {"TMUX": "/tmp/tmux/server,123,0", "TMUX_PANE": "%7"},
    )
    monkeypatch.setattr(
        context,
        "_tmux_probe",
        lambda socket, pane, expected_server_pid=-1: target(
            socket, pane, expected_server_pid
        ),
    )
    monkeypatch.setattr(context, "process_start_time", lambda pid: 321)
    monkeypatch.setattr(
        context,
        "_agent_pids",
        lambda: (_ for _ in ()).throw(AssertionError("global scan should not run")),
    )

    resolved = context.resolve_hook_target_details(
        {"session_id": "new-session", "transcript_path": None},
        {},
    )

    assert (resolved["socket"], resolved["pane"]) == ("/tmp/tmux/server", "%7")
    assert resolved["agent_pid"] == 999
    assert resolved["agent_start_time"] == 321
    assert resolved["score"] == 600
    assert resolved["agent_kind"] == "claude"


def test_missing_identity_does_not_scan(monkeypatch):
    monkeypatch.setattr(context, "_direct_target", lambda env: None)
    monkeypatch.setattr(context, "_ancestor_agent_pids", lambda: [])
    monkeypatch.setattr(context, "_agent_pids", lambda: (_ for _ in ()).throw(AssertionError()))
    assert context.resolve_hook_target({}, {}) == ("", "")


def test_equal_score_in_different_panes_is_ambiguous(monkeypatch):
    monkeypatch.setattr(context, "_direct_target", lambda env: None)
    monkeypatch.setattr(context, "_ancestor_agent_pids", lambda: [])
    monkeypatch.setattr(context, "_agent_pids", lambda: [(100, "codex"), (200, "claude")])
    candidates = {
        100: {**target("/tmp/a", "%1"), "agent_pid": 100, "agent_start_time": 1, "score": 300, "ambiguous": False},
        200: {**target("/tmp/b", "%2"), "agent_pid": 200, "agent_start_time": 2, "score": 300, "ambiguous": False},
    }
    monkeypatch.setattr(context, "_candidate", lambda pid, kind, payload: candidates[pid])
    resolved = context.resolve_hook_target_details({"session_id": "session"}, {})
    assert resolved["ambiguous"] is True
    assert context.resolve_hook_target({"session_id": "session"}, {}) == ("", "")


def test_same_target_prefers_newer_process(monkeypatch):
    monkeypatch.setattr(context, "_direct_target", lambda env: None)
    monkeypatch.setattr(context, "_ancestor_agent_pids", lambda: [])
    monkeypatch.setattr(context, "_agent_pids", lambda: [(100, "codex"), (200, "codex")])
    candidates = {
        100: {**target(), "agent_pid": 100, "agent_start_time": 10, "score": 300, "ambiguous": False},
        200: {**target(), "agent_pid": 200, "agent_start_time": 20, "score": 300, "ambiguous": False},
    }
    monkeypatch.setattr(context, "_candidate", lambda pid, kind, payload: candidates[pid])
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


def test_custom_claude_config_dir_is_used():
    assert context._claude_home({"CLAUDE_CONFIG_DIR": "/tmp/claude"}) == Path("/tmp/claude")


def test_claude_process_is_recognized(monkeypatch):
    monkeypatch.setattr(context, "_proc_comm", lambda pid: "claude")
    monkeypatch.setattr(
        context,
        "_proc_cmdline",
        lambda pid: ["claude", "--dangerously-skip-permissions", "--resume", "abc"],
    )
    assert context.agent_kind_for_pid(1) == "claude"


def test_claude_subcommands_that_own_no_pane_are_ignored(monkeypatch):
    monkeypatch.setattr(context, "_proc_comm", lambda pid: "claude")
    monkeypatch.setattr(context, "_proc_cmdline", lambda pid: ["claude", "mcp", "serve"])
    assert context.agent_kind_for_pid(1) == ""


def test_codex_app_server_is_not_an_agent_pane(monkeypatch):
    monkeypatch.setattr(context, "_proc_comm", lambda pid: "codex")
    monkeypatch.setattr(context, "_proc_cmdline", lambda pid: ["codex", "app-server"])
    assert context.agent_kind_for_pid(1) == ""


def test_claude_resume_flag_supplies_a_session_id(monkeypatch):
    monkeypatch.setattr(
        context, "_proc_cmdline", lambda pid: ["claude", "--resume", "session-1"]
    )
    assert context.resume_session_id(1, "claude") == "session-1"
    monkeypatch.setattr(
        context, "_proc_cmdline", lambda pid: ["claude", "--resume=session-2"]
    )
    assert context.resume_session_id(1, "claude") == "session-2"


def claude_home(tmp_path, pid, record):
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{pid}.json").write_text(json.dumps(record), encoding="utf-8")
    return {"CLAUDE_CONFIG_DIR": str(tmp_path)}


def test_claude_session_record_rejects_a_reused_pid(monkeypatch, tmp_path):
    env = claude_home(tmp_path, 500, {"pid": 500, "sessionId": "s1", "procStart": "77"})
    monkeypatch.setattr(context, "process_start_time", lambda pid: 88)
    assert context.claude_session_record(500, env) == {}
    monkeypatch.setattr(context, "process_start_time", lambda pid: 77)
    assert context.claude_session_record(500, env)["sessionId"] == "s1"


def test_claude_session_record_rejects_a_foreign_pid(tmp_path):
    env = claude_home(tmp_path, 500, {"pid": 501, "sessionId": "s1"})
    assert context.claude_session_record(500, env) == {}


def test_claude_transcript_match_outranks_a_session_id_match(monkeypatch, tmp_path):
    env = claude_home(tmp_path, 500, {"pid": 500, "sessionId": "s1"})
    project = tmp_path / "projects" / "-home-user-repo"
    project.mkdir(parents=True)
    transcript = project / "s1.jsonl"
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(context, "process_start_time", lambda pid: 1)

    assert (
        context._score(500, "claude", {"transcript_path": str(transcript)}, env) == 400
    )
    assert context._score(500, "claude", {"session_id": "s1"}, env) == 300
    assert context._score(500, "claude", {"session_id": "other"}, env) == 0


def test_claude_processes_report_their_transcript_and_kind(monkeypatch, tmp_path):
    env = claude_home(tmp_path, 500, {"pid": 500, "sessionId": "s1"})
    project = tmp_path / "projects" / "-home-user-repo"
    project.mkdir(parents=True)
    (project / "s1.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(context, "_agent_pids", lambda: [(500, "claude")])
    monkeypatch.setattr(
        context,
        "_proc_environ",
        lambda pid: {**env, "TMUX": "/tmp/tmux/server,123,0", "TMUX_PANE": "%7"},
    )
    monkeypatch.setattr(
        context,
        "_tmux_probe",
        lambda socket, pane, expected_server_pid=-1: target(socket, pane, expected_server_pid),
    )
    monkeypatch.setattr(context, "process_start_time", lambda pid: 1)
    monkeypatch.setattr(context, "_proc_cmdline", lambda pid: ["claude"])

    processes = context.agent_processes_for_target("/tmp/tmux/server", "%7")
    assert len(processes) == 1
    assert processes[0]["agent_kind"] == "claude"
    assert processes[0]["session"]["sessionId"] == "s1"
    assert processes[0]["transcripts"] == [str(project / "s1.jsonl")]
