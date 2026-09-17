import os
import subprocess
from pathlib import Path


SHELL_FRAGMENT = Path(__file__).resolve().parents[1] / "shell" / "tmux-ssh.zsh"
RECONNECT_ID = "0123456789abcdef0123456789abcdef"


def run_zsh(script: str, *args: str, env=None):
    return subprocess.run(
        ["zsh", "-fc", script, "test", str(SHELL_FRAGMENT), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        env=env,
    )


def test_remember_target_uses_private_runtime_file(tmp_path):
    result = run_zsh(
        'source "$1"; '
        'TMUX_SSH_RECONNECT_ID="$2"; XDG_RUNTIME_DIR="$3"; '
        '__tmux_ssh_remember_target /tmp/tmux-1002/default work; '
        'cat "$3/codex-tmux-integration/ssh-reconnect/$2"',
        RECONNECT_ID,
        str(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["/tmp/tmux-1002/default", "work"]
    reconnect_dir = tmp_path / "codex-tmux-integration" / "ssh-reconnect"
    assert oct(reconnect_dir.stat().st_mode & 0o777) == "0o700"
    mapping = reconnect_dir / RECONNECT_ID
    assert oct(mapping.stat().st_mode & 0o777) == "0o600"


def test_invalid_reconnect_id_is_ignored(tmp_path):
    result = run_zsh(
        'source "$1"; '
        'TMUX_SSH_RECONNECT_ID="../../bad"; XDG_RUNTIME_DIR="$2"; '
        '__tmux_ssh_remember_target /tmp/tmux-1002/default work; '
        '[[ ! -e "$2/codex-tmux-integration" ]]',
        str(tmp_path),
    )

    assert result.returncode == 0, result.stderr


def test_reconnect_attaches_to_live_mapped_session(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "tmux.log"
    fake_tmux = bin_dir / "tmux"
    fake_tmux.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$TMUX_TEST_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["TMUX_TEST_LOG"] = str(log)

    result = run_zsh(
        'source "$1"; '
        'TMUX_SSH_RECONNECT_ID="$2"; XDG_RUNTIME_DIR="$3"; '
        '__tmux_ssh_remember_target /tmp/tmux-1002/default work; '
        '__tmux_ssh_try_reconnect',
        RECONNECT_ID,
        str(tmp_path),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "tmux: reconnecting default:work\n"
    assert log.read_text(encoding="utf-8").splitlines() == [
        "-S /tmp/tmux-1002/default has-session -t work",
        "-S /tmp/tmux-1002/default attach-session -t work",
    ]


def test_missing_session_removes_mapping_and_falls_back(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_tmux = bin_dir / "tmux"
    fake_tmux.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_tmux.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    result = run_zsh(
        'source "$1"; '
        'TMUX_SSH_RECONNECT_ID="$2"; XDG_RUNTIME_DIR="$3"; '
        '__tmux_ssh_remember_target /tmp/tmux-1002/default gone; '
        '__tmux_ssh_try_reconnect || print menu; '
        '[[ ! -e "$3/codex-tmux-integration/ssh-reconnect/$2" ]]',
        RECONNECT_ID,
        str(tmp_path),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "menu\n"
