import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL = REPO_ROOT / "installer" / "install"
UNINSTALL = REPO_ROOT / "installer" / "uninstall"


def prepare_home(home):
    (home / ".codex").mkdir()
    (home / ".claude").mkdir()
    (home / ".local/bin").mkdir(parents=True)
    (home / ".tmux.conf").write_text(
        "set -g mouse on\n"
        "set-hook -g 'pane-focus-in[50]' 'run-shell codex-tmux-title-sync'\n",
        encoding="utf-8",
    )
    (home / ".zshrc").write_text("alias ll='ls -l'\n", encoding="utf-8")
    (home / ".codex/hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/home/example/.codex/xbrd/quota.sh",
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (home / ".claude/settings.json").write_text(
        json.dumps({"env": {"SECRET": "keep"}, "hooks": {}}),
        encoding="utf-8",
    )


def run(*args):
    return subprocess.run(
        [str(value) for value in args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def test_install_is_idempotent_and_preserves_unrelated_data(tmp_path):
    prepare_home(tmp_path)
    features = "title-sync,window-attention,pane-logging,ssh-autoattach"
    first = run(
        INSTALL,
        "--home",
        tmp_path,
        "--repo-root",
        REPO_ROOT,
        "--features",
        features,
    )
    assert first.returncode == 0, first.stderr
    assert (tmp_path / ".local/bin/codex-tmux-title-sync").is_symlink()
    assert (tmp_path / ".local/bin/agent-tmux-notify").is_symlink()
    assert (tmp_path / ".local/bin/agent_tmux_context.py").is_symlink()
    assert "# >>> codex-tmux-integration >>>" in (
        tmp_path / ".tmux.conf"
    ).read_text()
    codex = json.loads((tmp_path / ".codex/hooks.json").read_text())
    claude = json.loads((tmp_path / ".claude/settings.json").read_text())
    assert claude["env"]["SECRET"] == "keep"
    assert any(
        hook["command"] == "/home/example/.codex/xbrd/quota.sh"
        for group in codex["hooks"]["Stop"]
        for hook in group["hooks"]
    )

    second = run(
        INSTALL,
        "--home",
        tmp_path,
        "--repo-root",
        REPO_ROOT,
        "--features",
        features,
    )
    assert second.returncode == 0, second.stderr
    assert "already up to date" in second.stdout


def test_single_feature_uninstall_keeps_other_features(tmp_path):
    prepare_home(tmp_path)
    first = run(
        INSTALL,
        "--home",
        tmp_path,
        "--repo-root",
        REPO_ROOT,
        "--features",
        "title-sync,window-attention",
    )
    assert first.returncode == 0, first.stderr
    removed = run(
        UNINSTALL,
        "--home",
        tmp_path,
        "--features",
        "title-sync",
    )
    assert removed.returncode == 0, removed.stderr
    assert not (tmp_path / ".local/bin/codex-tmux-title-sync").exists()
    assert (tmp_path / ".local/bin/agent-tmux-notify").is_symlink()
    hooks = json.loads((tmp_path / ".codex/hooks.json").read_text())["hooks"]
    commands = [
        hook["command"]
        for groups in hooks.values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert all("codex-tmux-title-sync" not in command for command in commands)
    assert any("agent-tmux-notify" in command for command in commands)


def test_full_uninstall_removes_only_managed_content(tmp_path):
    prepare_home(tmp_path)
    installed = run(
        INSTALL,
        "--home",
        tmp_path,
        "--repo-root",
        REPO_ROOT,
        "--features",
        "title-sync,window-attention,pane-logging,ssh-autoattach",
    )
    assert installed.returncode == 0, installed.stderr
    removed = run(UNINSTALL, "--home", tmp_path)
    assert removed.returncode == 0, removed.stderr
    assert not (tmp_path / ".local/bin/codex-tmux-title-sync").exists()
    assert not (tmp_path / ".local/bin/agent_tmux_context.py").exists()
    assert "# >>> codex-tmux-integration >>>" not in (
        tmp_path / ".tmux.conf"
    ).read_text()
    assert "set -g mouse on" in (tmp_path / ".tmux.conf").read_text()
    assert "alias ll='ls -l'" in (tmp_path / ".zshrc").read_text()
    codex = json.loads((tmp_path / ".codex/hooks.json").read_text())
    claude = json.loads((tmp_path / ".claude/settings.json").read_text())
    assert claude["env"]["SECRET"] == "keep"
    assert any(
        hook["command"] == "/home/example/.codex/xbrd/quota.sh"
        for group in codex["hooks"]["Stop"]
        for hook in group["hooks"]
    )


def test_notification_backend_is_stored_in_private_local_config(tmp_path):
    prepare_home(tmp_path)
    queue = tmp_path / "backend/notification_queue.py"
    python = tmp_path / "runtime/python3"
    queue.parent.mkdir()
    python.parent.mkdir()
    queue.write_text("# queue fixture\n", encoding="utf-8")
    python.write_text("# python fixture\n", encoding="utf-8")

    installed = run(
        INSTALL,
        "--home",
        tmp_path,
        "--repo-root",
        REPO_ROOT,
        "--features",
        "external-notifications",
        "--notification-queue",
        queue,
        "--notification-python",
        python,
    )
    assert installed.returncode == 0, installed.stderr
    config = tmp_path / ".config/codex-tmux-integration/notifications.env"
    assert config.stat().st_mode & 0o777 == 0o600
    text = config.read_text()
    assert str(queue.resolve()) in text
    assert str(python.resolve()) in text
    state = json.loads(
        (tmp_path / ".local/state/codex-tmux-integration/state.json").read_text()
    )
    assert str(queue.resolve()) not in json.dumps(state)
