"""Shared retention and locking helpers for tmux pane logs.

Pane logs are cache objects owned by a tmux pane/server lifetime. Garbage
collection removes a complete pane log group (the current file and all of its
rotations) once the owner is no longer active. Active groups are protected as
units, so pruning never silently removes another live sink's history.

All writers and the pruner use the same lock in the log root. The sink keeps
its normal per-pane rotation behavior; lifecycle-aware pruning is deliberately
performed by ``tmux-autolog`` after tmux events rather than by an individual
sink that cannot see the other active panes.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
import re
import time
from pathlib import Path
from typing import Iterable, Iterator


DEFAULT_MAX_TOTAL_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
LOG_NAME_RE = re.compile(r"(?P<base>.+\.log)(?:\.\d+)?$")
LOCK_NAME_RE = re.compile(r"\.(?P<base>.+\.log)\.lock$")


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _nonnegative_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def max_total_bytes() -> int:
    return _positive_int("TMUX_PANE_LOG_MAX_TOTAL_BYTES", DEFAULT_MAX_TOTAL_BYTES)


def max_age_seconds() -> int:
    return _nonnegative_int("TMUX_PANE_LOG_MAX_AGE_SECONDS", DEFAULT_MAX_AGE_SECONDS)


def log_root_for_path(path: Path) -> Path:
    configured = os.environ.get("TMUX_PANE_LOGDIR")
    if configured:
        return Path(configured).expanduser()
    return path.expanduser().parent


def is_log_file(path: Path) -> bool:
    return path.is_file() and LOG_NAME_RE.fullmatch(path.name) is not None


def log_group(path: Path) -> Path:
    """Return the current-log path that owns ``path`` and its rotations."""

    match = LOG_NAME_RE.fullmatch(path.name)
    if match is None:
        return path
    return path.with_name(match.group("base"))


def iter_log_files(root: Path) -> Iterator[Path]:
    try:
        entries = root.rglob("*")
    except OSError:
        return
    for path in entries:
        if is_log_file(path):
            yield path


@contextmanager
def retention_lock(root: Path) -> Iterator[None]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    lock_path = root / ".retention.lock"
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _resolved_paths(paths: Iterable[Path]) -> set[Path]:
    return {path.expanduser().resolve(strict=False) for path in paths}


def _stat_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _delete(path: Path) -> int:
    size = _stat_size(path)
    try:
        path.unlink()
    except OSError:
        return 0
    return size


def _delete_group(entries: list[tuple[Path, int, float]]) -> int:
    base = log_group(entries[0][0])
    removed = sum(_delete(path) for path, _, _ in entries)
    removed += _delete(base.with_name(f".{base.name}.lock"))
    return removed


def total_log_bytes(root: Path) -> int:
    return sum(_stat_size(path) for path in iter_log_files(root))


def _cleanup_unowned_artifacts(root: Path, protected_groups: set[Path]) -> int:
    removed = 0
    protected_dirs = {path.parent for path in protected_groups}
    try:
        entries = list(root.rglob("*"))
    except OSError:
        entries = []
    for path in entries:
        if path.is_file():
            lock_match = LOCK_NAME_RE.fullmatch(path.name)
            if lock_match is not None:
                group = path.with_name(lock_match.group("base"))
                if group.parent.resolve(strict=False) not in protected_dirs:
                    removed += _delete(path)
        elif path.is_dir() and path.name == ".runtime":
            if path.parent.resolve(strict=False) in protected_dirs:
                continue
            try:
                runtime_files = [child for child in path.iterdir() if child.is_file()]
            except OSError:
                runtime_files = []
            for child in runtime_files:
                removed += _delete(child)
    return removed


def _prune_locked(
    root: Path,
    *,
    protected: set[Path],
    total_limit: int,
    age_limit: int,
    now: float,
    collect_unprotected: bool,
) -> int:
    files = list(iter_log_files(root))
    groups: dict[Path, list[tuple[Path, int, float]]] = {}
    for path in files:
        try:
            stat_result = path.stat()
        except OSError:
            continue
        base = log_group(path)
        groups.setdefault(base, []).append(
            (path, stat_result.st_size, stat_result.st_mtime)
        )

    deleted = 0
    protected_groups = {
        log_group(path).resolve(strict=False) for path in protected
    }
    group_info = []
    for base, entries in groups.items():
        group_info.append(
            (
                base,
                entries,
                sum(size for _, size, _ in entries),
                max(mtime for _, _, mtime in entries),
            )
        )
    total = sum(size for _, _, size, _ in group_info)

    # Lifecycle collection removes complete, unowned groups immediately. The
    # caller has already resolved active pane ownership before taking the
    # retention lock, and the lock serializes this operation with all writes.
    for base, entries, size, _ in sorted(
        group_info, key=lambda item: (item[3], str(item[0]))
    ):
        if base.resolve(strict=False) in protected_groups:
            continue
        if collect_unprotected:
            removed = _delete_group(entries)
            deleted += removed
            total -= removed
            continue
        latest = max(mtime for _, _, mtime in entries)
        if age_limit and now - latest >= age_limit:
            removed = _delete_group(entries)
            deleted += removed
            total -= removed

    if total > total_limit:
        remaining = [
            (base, entries, size, latest)
            for base, entries, size, latest in group_info
            if any(path.exists() for path, _, _ in entries)
            and base.resolve(strict=False) not in protected_groups
        ]
        for base, entries, _, _ in sorted(
            remaining,
            key=lambda item: (item[3], str(item[0])),
        ):
            if total <= total_limit:
                break
            removed = _delete_group(entries)
            deleted += removed
            total -= removed

    deleted += _cleanup_unowned_artifacts(root, protected_groups)

    # Remove only empty directories that the logger created. This keeps old
    # server lifetimes from leaving a large directory tree behind, while never
    # recursively deleting anything that is not empty.
    try:
        directories = [path for path in root.rglob("*") if path.is_dir()]
    except OSError:
        directories = []
    for directory in sorted(
        directories,
        key=lambda path: (len(path.parts), str(path)),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    return deleted


def prune(
    root: Path,
    *,
    protected: Iterable[Path] = (),
    total_limit: int | None = None,
    age_limit: int | None = None,
    now: float | None = None,
    collect_unprotected: bool = False,
) -> int:
    """Collect unowned log groups and enforce the total byte limit.

    Returns the number of bytes successfully removed. Errors are deliberately
    contained so a retention failure cannot stop terminal logging.
    """

    root = root.expanduser()
    total_limit = max_total_bytes() if total_limit is None else total_limit
    age_limit = max_age_seconds() if age_limit is None else age_limit
    now = time.time() if now is None else now
    try:
        with retention_lock(root):
            return _prune_locked(
                root,
                protected=_resolved_paths(protected),
                total_limit=total_limit,
                age_limit=age_limit,
                now=now,
                collect_unprotected=collect_unprotected,
            )
    except OSError:
        return 0
