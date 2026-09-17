import sys
from pathlib import Path


BIN_ROOT = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN_ROOT))

from pane_log_retention import prune  # noqa: E402


def test_lifecycle_collection_removes_an_entire_unowned_pane_group(tmp_path):
    old_root = tmp_path / "old-server"
    old_root.mkdir()
    old_log = old_root / "1.log"
    old_rotation = old_root / "1.log.1"
    old_log.write_bytes(b"old-current\n")
    old_rotation.write_bytes(b"old-rotation\n")
    (old_root / ".1.log.lock").touch()
    (old_root / ".runtime").mkdir()
    (old_root / ".runtime" / "stale.release").touch()

    active_root = tmp_path / "live-server"
    active_root.mkdir()
    active_log = active_root / "2.log"
    active_rotation = active_root / "2.log.1"
    active_log.write_bytes(b"live-current\n")
    active_rotation.write_bytes(b"live-rotation\n")

    removed = prune(
        tmp_path,
        protected=[active_log],
        collect_unprotected=True,
    )

    assert removed == len(b"old-current\n") + len(b"old-rotation\n")
    assert not old_log.exists()
    assert not old_rotation.exists()
    assert not (old_root / ".1.log.lock").exists()
    assert not (old_root / ".runtime" / "stale.release").exists()
    assert active_log.read_bytes() == b"live-current\n"
    assert active_rotation.read_bytes() == b"live-rotation\n"


def test_total_budget_evicts_whole_old_groups_not_partial_rotations(tmp_path):
    old_root = tmp_path / "old-server"
    old_root.mkdir()
    old_log = old_root / "1.log"
    old_rotation = old_root / "1.log.1"
    old_log.write_bytes(b"old-current\n")
    old_rotation.write_bytes(b"old-rotation\n")

    active_root = tmp_path / "live-server"
    active_root.mkdir()
    active_log = active_root / "2.log"
    active_rotation = active_root / "2.log.1"
    active_log.write_bytes(b"live-current\n")
    active_rotation.write_bytes(b"live-rotation\n")

    prune(
        tmp_path,
        protected=[active_log],
        total_limit=1,
        age_limit=0,
    )

    assert not old_log.exists()
    assert not old_rotation.exists()
    assert active_log.exists()
    assert active_rotation.exists()
