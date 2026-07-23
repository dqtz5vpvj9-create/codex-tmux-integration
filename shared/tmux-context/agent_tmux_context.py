#!/usr/bin/env python3
"""Resolve the tmux server and pane that own a Codex lifecycle hook."""

import json
import os
from pathlib import Path


def _clean(value):
    return str(value or "").strip()


def _tmux_socket(env):
    value = _clean(env.get("TMUX"))
    return value.split(",", 1)[0] if value else ""


def _proc_environ(pid):
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    result = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        result[key.decode(errors="ignore")] = value.decode(errors="ignore")
    return result


def _proc_cmdline(pid):
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="ignore") for part in raw.split(b"\0") if part]


def _codex_pids():
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text(errors="ignore").strip() == "codex":
                yield int(entry.name)
        except OSError:
            continue


def _open_transcripts(pid):
    try:
        descriptors = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return []
    paths = []
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        if target.endswith(".jsonl") and "/.codex/sessions/" in target:
            paths.append(target)
    return paths


def _transcript_session_id(path):
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
            record = json.loads(stream.readline())
    except (OSError, json.JSONDecodeError):
        return ""
    if record.get("type") != "session_meta":
        return ""
    return _clean((record.get("payload") or {}).get("id"))


def _resume_session_id(pid):
    parts = _proc_cmdline(pid)
    for index, part in enumerate(parts):
        if part == "resume" and index + 1 < len(parts):
            return _clean(parts[index + 1])
    return ""


def resolve_hook_target(payload=None, env=None):
    """Return ``(socket, pane)`` for a hook, or ``("", "")`` if unknown.

    Current Codex hook subprocesses may omit TMUX and TMUX_PANE even though the
    owning Codex process inherited them.  In that case, correlate the stable
    hook session/transcript fields with the live Codex process and recover its
    environment from /proc.
    """

    payload = payload if isinstance(payload, dict) else {}
    env = os.environ if env is None else env
    direct_socket = _tmux_socket(env)
    direct_pane = _clean(env.get("TMUX_PANE"))
    if direct_socket and direct_pane:
        return direct_socket, direct_pane

    session_id = _clean(payload.get("session_id"))
    transcript_path = _clean(payload.get("transcript_path"))
    if not session_id and not transcript_path:
        return "", ""

    best = None
    for pid in _codex_pids():
        process_env = _proc_environ(pid)
        socket = _tmux_socket(process_env)
        pane = _clean(process_env.get("TMUX_PANE"))
        if not socket or not pane:
            continue

        transcripts = _open_transcripts(pid)
        score = 0
        if transcript_path and transcript_path in transcripts:
            score = 100
        elif session_id and _resume_session_id(pid) == session_id:
            score = 90
        elif session_id and any(_transcript_session_id(path) == session_id for path in transcripts):
            score = 80
        if score and (best is None or score > best[0]):
            best = (score, socket, pane)

    return (best[1], best[2]) if best else ("", "")
