import os
import subprocess
from pathlib import Path


SINK = Path(__file__).resolve().parents[1] / "bin" / "tmux-log-sink"


def run_sink(path, payload, **env_values):
    env = os.environ.copy()
    env.update(env_values)
    return subprocess.run(
        [str(SINK), str(path)],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )


def test_small_write_is_flushed(tmp_path):
    output = tmp_path / "pane.log"
    result = run_sink(output, b"x")
    assert result.returncode == 0
    assert output.read_bytes() == b"x"
    assert output.stat().st_mode & 0o777 == 0o600


def test_rotation_is_bounded(tmp_path):
    output = tmp_path / "pane.log"
    assert run_sink(
        output,
        b"12345",
        TMUX_PANE_LOG_MAX_BYTES="5",
        TMUX_PANE_LOG_BACKUPS="2",
    ).returncode == 0
    assert run_sink(
        output,
        b"67890",
        TMUX_PANE_LOG_MAX_BYTES="5",
        TMUX_PANE_LOG_BACKUPS="2",
    ).returncode == 0
    assert output.read_bytes() == b"67890"
    assert (tmp_path / "pane.log.1").read_bytes() == b"12345"
