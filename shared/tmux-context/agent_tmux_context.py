#!/usr/bin/env python3
"""Resolve an agent hook to one unambiguous tmux server and pane."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

TMUX_TIMEOUT_SECONDS = 0.8
AGENT_CODEX = "codex"
AGENT_CLAUDE = "claude"
AGENT_KINDS = (AGENT_CODEX, AGENT_CLAUDE)
EMPTY_TARGET: dict[str, Any] = {
    "socket": "",
    "pane": "",
    "server_pid": -1,
    "pane_pid": -1,
    "window_id": "",
    "agent_kind": "",
    "agent_pid": -1,
    "agent_start_time": -1,
    "score": 0,
    "ambiguous": False,
}

# The ``claude`` executable also serves subcommands that never own a pane.
CLAUDE_NON_SESSION_COMMANDS = frozenset(
    {
        "config",
        "doctor",
        "install",
        "mcp",
        "migrate-installer",
        "plugin",
        "setup-token",
        "update",
    }
)
CLAUDE_RESUME_FLAGS = ("--resume", "-r")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any, default: int = -1) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _tmux_identity(env: Mapping[str, str]) -> tuple[str, int]:
    raw = _clean(env.get("TMUX"))
    if not raw:
        return "", -1
    parts = raw.rsplit(",", 2)
    socket = parts[0]
    server_pid = _as_int(parts[1], -1) if len(parts) > 1 else -1
    return socket, server_pid


def _run_tmux(socket: str, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["tmux", "-S", socket, *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=TMUX_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _tmux_probe(socket: str, pane: str, expected_server_pid: int = -1) -> dict[str, Any] | None:
    if not socket or not pane:
        return None
    result = _run_tmux(
        socket,
        "display-message",
        "-p",
        "-t",
        pane,
        "#{pid}\t#{pane_id}\t#{pane_pid}\t#{window_id}",
    )
    if result is None or result.returncode != 0:
        return None
    parts = result.stdout.strip().split("\t")
    if len(parts) != 4 or parts[1] != pane:
        return None
    server_pid = _as_int(parts[0])
    if expected_server_pid > 0 and server_pid != expected_server_pid:
        return None
    return {
        "socket": socket,
        "pane": pane,
        "server_pid": server_pid,
        "pane_pid": _as_int(parts[2]),
        "window_id": _clean(parts[3]),
    }


def _proc_environ(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        result[key.decode(errors="ignore")] = value.decode(errors="ignore")
    return result


def _proc_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="ignore") for part in raw.split(b"\0") if part]


def _proc_comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(errors="ignore").strip()
    except OSError:
        return ""


def _proc_stat_fields(pid: int) -> tuple[int, int]:
    """Return ``(ppid, start_time_ticks)`` from Linux procfs."""

    try:
        raw = Path(f"/proc/{pid}/stat").read_text(errors="replace")
    except OSError:
        return -1, -1
    close = raw.rfind(")")
    if close < 0:
        return -1, -1
    fields = raw[close + 2 :].split()
    if len(fields) < 20:
        return -1, -1
    return _as_int(fields[1]), _as_int(fields[19])


def process_start_time(pid: int) -> int:
    return _proc_stat_fields(pid)[1]


def process_is_alive(pid: int, start_time: int = -1) -> bool:
    if pid <= 0:
        return False
    actual = process_start_time(pid)
    return actual > 0 and (start_time <= 0 or actual == start_time)


def _is_codex_process(pid: int) -> bool:
    if _proc_comm(pid) != AGENT_CODEX:
        return False
    parts = _proc_cmdline(pid)
    return not any(part == "app-server" for part in parts[1:])


def _claude_subcommand(parts: list[str]) -> str:
    """Return the first positional argument of a ``claude`` command line."""

    index = 1
    while index < len(parts):
        value = parts[index]
        if value == "--":
            index += 1
            break
        if value.startswith("-"):
            index += 1
            continue
        break
    return parts[index] if index < len(parts) else ""


def _is_claude_process(pid: int) -> bool:
    if _proc_comm(pid) != AGENT_CLAUDE:
        return False
    return _claude_subcommand(_proc_cmdline(pid)) not in CLAUDE_NON_SESSION_COMMANDS


def agent_kind_for_pid(pid: int) -> str:
    """Return ``codex``, ``claude`` or an empty string for one process."""

    if _is_codex_process(pid):
        return AGENT_CODEX
    if _is_claude_process(pid):
        return AGENT_CLAUDE
    return ""


def _ancestor_agent_pids(start_pid: int | None = None) -> list[tuple[int, str]]:
    pid = os.getppid() if start_pid is None else start_pid
    result: list[tuple[int, str]] = []
    seen: set[int] = set()
    for _ in range(64):
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        kind = agent_kind_for_pid(pid)
        if kind:
            result.append((pid, kind))
        ppid, _ = _proc_stat_fields(pid)
        pid = ppid
    return result


def _agent_pids() -> list[tuple[int, str]]:
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return []
    result: list[tuple[int, str]] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        kind = agent_kind_for_pid(pid)
        if kind:
            result.append((pid, kind))
    return sorted(result)


def _codex_home(process_env: Mapping[str, str]) -> Path:
    configured = _clean(process_env.get("CODEX_HOME"))
    if configured:
        return Path(configured).expanduser()
    home = _clean(process_env.get("HOME"))
    return Path(home).expanduser() / ".codex" if home else Path.home() / ".codex"


def _claude_home(process_env: Mapping[str, str]) -> Path:
    configured = _clean(process_env.get("CLAUDE_CONFIG_DIR"))
    if configured:
        return Path(configured).expanduser()
    home = _clean(process_env.get("HOME"))
    return Path(home).expanduser() / ".claude" if home else Path.home() / ".claude"


def _normalized_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _open_transcripts(pid: int, process_env: Mapping[str, str] | None = None) -> list[str]:
    env = _proc_environ(pid) if process_env is None else process_env
    sessions_root = _normalized_path(_codex_home(env) / "sessions")
    try:
        descriptors = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return []
    paths: list[tuple[float, str]] = []
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
            path = _normalized_path(target)
            if path.suffix == ".jsonl" and _is_under(path, sessions_root):
                paths.append((path.stat().st_mtime, str(path)))
        except OSError:
            continue
    return [path for _, path in sorted(paths, reverse=True)]


def claude_session_record(
    pid: int, process_env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Read Claude Code's per-process session file and verify it owns ``pid``.

    Claude Code does not keep its transcript open, so process correlation uses
    the state file it maintains under ``<claude home>/sessions/<pid>.json``. A
    recorded process start time rejects a file left behind by a reused PID.
    """

    env = _proc_environ(pid) if process_env is None else process_env
    path = _claude_home(env) / "sessions" / f"{pid}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(record, dict) or _as_int(record.get("pid")) != pid:
        return {}
    recorded_start = _as_int(record.get("procStart"))
    if recorded_start > 0 and process_start_time(pid) != recorded_start:
        return {}
    return record


def claude_transcripts_for_session(
    session_id: str, process_env: Mapping[str, str] | None = None
) -> list[str]:
    """Return transcript paths Claude Code records for one session id."""

    session_id = _clean(session_id)
    if not session_id or "/" in session_id or session_id.startswith("."):
        return []
    env = os.environ if process_env is None else process_env
    root = _normalized_path(_claude_home(env) / "projects")
    try:
        return sorted(str(path) for path in root.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return []


def _claude_transcripts(pid: int, process_env: Mapping[str, str]) -> list[str]:
    record = claude_session_record(pid, process_env)
    return claude_transcripts_for_session(record.get("sessionId"), process_env)


def _transcript_session_id(path: str) -> str:
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
            for _ in range(16):
                line = stream.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "session_meta":
                    return _clean((record.get("payload") or {}).get("id"))
    except OSError:
        pass
    return ""


_RESUME_OPTIONS_WITH_VALUE = {
    "-a",
    "--ask-for-approval",
    "-c",
    "--config",
    "-C",
    "--cd",
    "--add-dir",
    "--disable",
    "--enable",
    "-i",
    "--image",
    "-m",
    "--model",
    "-p",
    "--profile",
    "-s",
    "--sandbox",
}


def _resume_session_id(pid: int) -> str:
    parts = _proc_cmdline(pid)
    try:
        index = parts.index("resume") + 1
    except ValueError:
        return ""
    while index < len(parts):
        value = parts[index]
        if value == "--":
            return _clean(parts[index + 1]) if index + 1 < len(parts) else ""
        if value in _RESUME_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if value.startswith("-"):
            index += 1
            continue
        return _clean(value)
    return ""


def _claude_resume_session_id(pid: int) -> str:
    parts = _proc_cmdline(pid)
    for index, value in enumerate(parts[1:], start=1):
        if value.startswith("--resume="):
            return _clean(value.split("=", 1)[1])
        if value in CLAUDE_RESUME_FLAGS and index + 1 < len(parts):
            return _clean(parts[index + 1])
    return ""


def resume_session_id(pid: int, kind: str) -> str:
    if kind == AGENT_CLAUDE:
        return _claude_resume_session_id(pid)
    return _resume_session_id(pid)


def _same_path(first: str, second: str) -> bool:
    if not first or not second:
        return False
    return _normalized_path(first) == _normalized_path(second)


def _score(
    pid: int,
    kind: str,
    payload: Mapping[str, Any],
    process_env: Mapping[str, str],
) -> int:
    """Rank how strongly one live agent process matches a hook payload."""

    session_id = _clean(payload.get("session_id"))
    transcript_path = _clean(payload.get("transcript_path"))
    if kind == AGENT_CLAUDE:
        recorded = _clean(claude_session_record(pid, process_env).get("sessionId"))
        if recorded:
            transcripts = claude_transcripts_for_session(recorded, process_env)
            if transcript_path and any(
                _same_path(transcript_path, path) for path in transcripts
            ):
                return 400
            if session_id and session_id == recorded:
                return 300
        if session_id and _claude_resume_session_id(pid) == session_id:
            return 200
        return 0

    transcripts = _open_transcripts(pid, process_env)
    if transcript_path and any(_same_path(transcript_path, path) for path in transcripts):
        return 400
    if session_id and any(_transcript_session_id(path) == session_id for path in transcripts):
        return 300
    if session_id and _resume_session_id(pid) == session_id:
        return 200
    return 0


def _candidate(pid: int, kind: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    process_env = _proc_environ(pid)
    socket, expected_server_pid = _tmux_identity(process_env)
    pane = _clean(process_env.get("TMUX_PANE"))
    context = _tmux_probe(socket, pane, expected_server_pid)
    if context is None:
        return None

    score = _score(pid, kind, payload, process_env)
    if score == 0:
        return None

    return {
        **context,
        "agent_kind": kind,
        "agent_pid": pid,
        "agent_start_time": process_start_time(pid),
        "score": score,
        "ambiguous": False,
    }


def _direct_target(env: Mapping[str, str]) -> dict[str, Any] | None:
    socket, expected_server_pid = _tmux_identity(env)
    pane = _clean(env.get("TMUX_PANE"))
    return _tmux_probe(socket, pane, expected_server_pid)


def _ancestor_target(pids: list[tuple[int, str]]) -> dict[str, Any] | None:
    """Return the nearest live tmux target inherited by a parent agent process."""

    for pid, kind in pids:
        process_env = _proc_environ(pid)
        socket, expected_server_pid = _tmux_identity(process_env)
        pane = _clean(process_env.get("TMUX_PANE"))
        context = _tmux_probe(socket, pane, expected_server_pid)
        if context is None:
            continue
        return {
            **context,
            "agent_kind": kind,
            "agent_pid": pid,
            "agent_start_time": process_start_time(pid),
            "score": 600,
            "ambiguous": False,
        }
    return None


def _choose(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not candidates:
        return dict(EMPTY_TARGET)
    top_score = max(_as_int(candidate.get("score"), 0) for candidate in candidates)
    top = [candidate for candidate in candidates if candidate.get("score") == top_score]
    targets = {(candidate["socket"], candidate["pane"]) for candidate in top}
    if len(targets) > 1:
        result = dict(EMPTY_TARGET)
        result["ambiguous"] = True
        result["score"] = top_score
        return result
    return max(
        top,
        key=lambda candidate: (
            _as_int(candidate.get("agent_start_time"), -1),
            _as_int(candidate.get("agent_pid"), -1),
        ),
    )


def resolve_hook_target_details(
    payload: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve a hook target and include process identity used for cache expiry.

    A direct ``TMUX`` and ``TMUX_PANE`` pair is accepted only after a live tmux
    probe verifies the server PID and pane. When hook subprocesses omit those
    variables, their nearest Codex or Claude ancestor is authoritative because
    it is the process that launched the lifecycle hook. Session/transcript
    correlation is retained as a fallback for detached hook runners. Equal
    scoring matches in different panes are reported as ambiguous and are not
    acted on.
    """

    data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    environment: Mapping[str, str] = os.environ if env is None else env
    direct = _direct_target(environment)
    ancestor_pids = _ancestor_agent_pids()

    if direct is not None:
        for pid, kind in ancestor_pids:
            process_env = _proc_environ(pid)
            socket, expected_server_pid = _tmux_identity(process_env)
            pane = _clean(process_env.get("TMUX_PANE"))
            if socket != direct["socket"] or pane != direct["pane"]:
                continue
            if _tmux_probe(socket, pane, expected_server_pid) is None:
                continue
            return {
                **direct,
                "agent_kind": kind,
                "agent_pid": pid,
                "agent_start_time": process_start_time(pid),
                "score": 500,
                "ambiguous": False,
            }

        matching = [
            candidate
            for pid, kind in _agent_pids()
            if (candidate := _candidate(pid, kind, data)) is not None
            and candidate["socket"] == direct["socket"]
            and candidate["pane"] == direct["pane"]
        ]
        chosen = _choose(matching)
        if chosen["socket"]:
            return chosen
        return {
            **direct,
            "agent_kind": "",
            "agent_pid": -1,
            "agent_start_time": -1,
            "score": 100,
            "ambiguous": False,
        }

    ancestor = _ancestor_target(ancestor_pids)
    if ancestor is not None:
        return ancestor

    session_id = _clean(data.get("session_id"))
    transcript_path = _clean(data.get("transcript_path"))
    if not session_id and not transcript_path:
        return dict(EMPTY_TARGET)

    candidates = [
        candidate
        for pid, kind in _agent_pids()
        if (candidate := _candidate(pid, kind, data)) is not None
    ]
    return _choose(candidates)


def resolve_hook_target(
    payload: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    target = resolve_hook_target_details(payload, env)
    if target.get("ambiguous"):
        return "", ""
    return _clean(target.get("socket")), _clean(target.get("pane"))


def agent_processes_for_target(
    socket: str = "", pane: str = "", kind: str = ""
) -> list[dict[str, Any]]:
    """Return live, validated agent process contexts for an optional tmux target."""

    result: list[dict[str, Any]] = []
    for pid, process_kind in _agent_pids():
        if kind and process_kind != kind:
            continue
        process_env = _proc_environ(pid)
        process_socket, expected_server_pid = _tmux_identity(process_env)
        process_pane = _clean(process_env.get("TMUX_PANE"))
        if socket and process_socket != socket:
            continue
        if pane and process_pane != pane:
            continue
        target = _tmux_probe(process_socket, process_pane, expected_server_pid)
        if target is None:
            continue
        if process_kind == AGENT_CLAUDE:
            session = claude_session_record(pid, process_env)
            transcripts = claude_transcripts_for_session(
                session.get("sessionId"), process_env
            )
        else:
            session = {}
            transcripts = _open_transcripts(pid, process_env)
        result.append(
            {
                **target,
                "agent_kind": process_kind,
                "agent_pid": pid,
                "agent_start_time": process_start_time(pid),
                "transcripts": transcripts,
                "resume": resume_session_id(pid, process_kind),
                "session": session,
            }
        )
    return result
