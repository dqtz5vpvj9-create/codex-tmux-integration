import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODEX = ROOT / "adapters/androidtools/codex_hook_wrapper.sh"
CLAUDE = ROOT / "adapters/androidtools/claude_hook_wrapper.sh"


def mock_queue(tmp_path):
    path = tmp_path / "queue.py"
    path.write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    return path


def wrapper_env(tmp_path, queue):
    env = os.environ.copy()
    env.update(
        {
            "ANDROIDTOOLS_NOTIFY_PYTHON": sys.executable,
            "ANDROIDTOOLS_NOTIFY_QUEUE": str(queue),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        }
    )
    return env


def test_codex_wrapper_uses_canonical_hook_name(tmp_path):
    queue = mock_queue(tmp_path)
    result = subprocess.run(
        [str(CODEX), "legacy-name"],
        input=json.dumps({"hook_event_name": "Stop"}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=wrapper_env(tmp_path, queue),
        check=False,
    )
    assert result.returncode == 0
    log = (tmp_path / "cache/codex-tmux-integration/codex_hook.log").read_text()
    args = json.loads(log)
    assert args[args.index("--hook") + 1] == "Stop"
    assert args[args.index("--client") + 1] == "codex"


def test_claude_wrapper_sets_client(tmp_path):
    queue = mock_queue(tmp_path)
    result = subprocess.run(
        [str(CLAUDE), "notification"],
        input=json.dumps({"message": "ready"}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=wrapper_env(tmp_path, queue),
        check=False,
    )
    assert result.returncode == 0
    log = (tmp_path / "cache/codex-tmux-integration/claude_hook.log").read_text()
    args = json.loads(log)
    assert args[args.index("--hook") + 1] == "notification"
    assert args[args.index("--client") + 1] == "claude"


def test_missing_backend_is_non_blocking(tmp_path):
    env = wrapper_env(tmp_path, tmp_path / "missing.py")
    result = subprocess.run(
        [str(CODEX), "Stop"],
        input="{}",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    log = (tmp_path / "cache/codex-tmux-integration/codex_hook.log").read_text()
    assert "notification backend unavailable" in log


def test_private_local_config_supplies_backend(tmp_path):
    queue = mock_queue(tmp_path)
    config_root = tmp_path / "config"
    config = config_root / "codex-tmux-integration/notifications.env"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"ANDROIDTOOLS_NOTIFY_QUEUE='{queue}'\n"
        f"ANDROIDTOOLS_NOTIFY_PYTHON='{sys.executable}'\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    env = os.environ.copy()
    env.pop("ANDROIDTOOLS_NOTIFY_QUEUE", None)
    env.pop("ANDROIDTOOLS_NOTIFY_PYTHON", None)
    env.update(
        {
            "XDG_CONFIG_HOME": str(config_root),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        }
    )
    result = subprocess.run(
        [str(CODEX), "Stop"],
        input=json.dumps({"hook_event_name": "Stop"}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    log = (tmp_path / "cache/codex-tmux-integration/codex_hook.log").read_text()
    args = json.loads(log)
    assert args[args.index("--client") + 1] == "codex"


def test_claude_wrapper_uses_canonical_hook_name(tmp_path):
    queue = mock_queue(tmp_path)
    result = subprocess.run(
        [str(CLAUDE), "stop"],
        input=json.dumps({"hook_event_name": "Stop", "session_id": "s1"}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=wrapper_env(tmp_path, queue),
        check=False,
    )
    assert result.returncode == 0
    log = (tmp_path / "cache/codex-tmux-integration/claude_hook.log").read_text()
    args = json.loads(log)
    assert args[args.index("--hook") + 1] == "Stop"
    raw = tmp_path / "cache/codex-tmux-integration/claude_hook_raw_stdin.json"
    assert json.loads(raw.read_text())["session_id"] == "s1"


def test_claude_wrapper_skips_subagent_stop_event(tmp_path):
    queue = mock_queue(tmp_path)
    result = subprocess.run(
        [str(CLAUDE), "SubagentStop"],
        input=json.dumps(
            {"hook_event_name": "SubagentStop", "session_id": "s1"}
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=wrapper_env(tmp_path, queue),
        check=False,
    )
    assert result.returncode == 0
    log = (tmp_path / "cache/codex-tmux-integration/claude_hook.log").read_text()
    assert "skipped: sub-agent event" in log


def test_claude_wrapper_skips_subagent_transcript_path(tmp_path):
    queue = mock_queue(tmp_path)
    proj = tmp_path / ".claude/projects/-fake-project/parent-session/subagents"
    proj.mkdir(parents=True)
    transcript = proj / "agent-abc123.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "isSidechain": True}) + "\n",
        encoding="utf-8",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "s1",
        "transcript_path": str(transcript),
    }
    result = subprocess.run(
        [str(CLAUDE), "stop"],
        input=json.dumps(payload),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=wrapper_env(tmp_path, queue),
        check=False,
    )
    assert result.returncode == 0
    log = (tmp_path / "cache/codex-tmux-integration/claude_hook.log").read_text()
    assert "skipped: sub-agent event" in log


def test_claude_wrapper_skips_sidechain_transcript_tip(tmp_path):
    queue = mock_queue(tmp_path)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "isSidechain": False}) + "\n"
        + json.dumps({"type": "assistant", "isSidechain": True}) + "\n",
        encoding="utf-8",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "s1",
        "transcript_path": str(transcript),
    }
    result = subprocess.run(
        [str(CLAUDE), "stop"],
        input=json.dumps(payload),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=wrapper_env(tmp_path, queue),
        check=False,
    )
    assert result.returncode == 0
    log = (tmp_path / "cache/codex-tmux-integration/claude_hook.log").read_text()
    assert "skipped: sub-agent event" in log


def test_claude_wrapper_forwards_main_agent_stop(tmp_path):
    queue = mock_queue(tmp_path)
    transcript = tmp_path / "main-transcript.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "isSidechain": False}) + "\n"
        + json.dumps({"type": "assistant", "isSidechain": False}) + "\n",
        encoding="utf-8",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "s1",
        "transcript_path": str(transcript),
    }
    result = subprocess.run(
        [str(CLAUDE), "stop"],
        input=json.dumps(payload),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=wrapper_env(tmp_path, queue),
        check=False,
    )
    assert result.returncode == 0
    log = (tmp_path / "cache/codex-tmux-integration/claude_hook.log").read_text()
    args = json.loads(log)
    assert args[args.index("--hook") + 1] == "Stop"
