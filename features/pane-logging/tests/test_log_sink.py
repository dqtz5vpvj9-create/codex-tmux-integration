import os
import subprocess
import time
from pathlib import Path


SINK = Path(__file__).resolve().parents[1] / "bin" / "tmux-log-sink"


def run_sink(path, payload, *options, **env_values):
    env = os.environ.copy()
    env.update(env_values)
    return subprocess.run(
        [str(SINK), *options, str(path)],
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


def test_first_chunk_is_strictly_bounded(tmp_path):
    output = tmp_path / "pane.log"
    payload = b"abcdefghijkl"
    result = run_sink(
        output,
        payload,
        TMUX_PANE_LOG_MAX_BYTES="5",
        TMUX_PANE_LOG_BACKUPS="3",
    )
    assert result.returncode == 0
    assert output.stat().st_size <= 5
    assert all(path.stat().st_size <= 5 for path in tmp_path.glob("pane.log.*") if path.suffix != ".lock")
    assert (tmp_path / "pane.log.2").read_bytes() + (tmp_path / "pane.log.1").read_bytes() + output.read_bytes() == payload


def test_rotation_remains_bounded_across_processes(tmp_path):
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


def test_live_data_is_buffered_until_snapshot_release(tmp_path):
    output = tmp_path / "pane.log"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    pid_file = tmp_path / "pid"
    process = subprocess.Popen(
        [
            str(SINK),
            "--live",
            "--ready",
            str(ready),
            "--release",
            str(release),
            "--pid-file",
            str(pid_file),
            str(output),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    deadline = time.monotonic() + 2
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()
    process.stdin.write(b"live\n")
    process.stdin.flush()
    time.sleep(0.05)
    assert not output.exists()
    assert run_sink(output, b"snapshot\n", "--snapshot").returncode == 0
    release.touch()
    process.stdin.close()
    assert process.wait(timeout=2) == 0
    assert output.read_bytes() == b"snapshot\nlive\n"
