#!/usr/bin/env python3
"""Merge feature-owned Codex/Claude hooks without touching unrelated settings."""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import shlex
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable


MANAGED_EXECUTABLES = {
    "agent-tmux-notify",
    "claude-tmux-notify-wrapper",
    "codex-tmux-notify-wrapper",
    "codex-tmux-title-sync",
    # Legacy commands replaced during the first migration.
    "codex_hook_wrapper.sh",
    "hook_wrapper.sh",
}


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top-level JSON value must be an object")
    return value


def command_executable(command: Any) -> str:
    if not isinstance(command, str) or not command.strip():
        return ""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return ""
    executable = parts[0]
    if executable in {"bash", "sh", "zsh"} and len(parts) > 1:
        executable = parts[1]
    return Path(executable).name


def is_managed_hook(hook: Any) -> bool:
    if not isinstance(hook, dict):
        return False
    return command_executable(hook.get("command")) in MANAGED_EXECUTABLES


def remove_managed_hooks(document: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(document)
    events = result.get("hooks")
    if not isinstance(events, dict):
        result["hooks"] = {}
        return result

    cleaned_events: dict[str, Any] = {}
    for event, groups in events.items():
        if not isinstance(groups, list):
            cleaned_events[event] = groups
            continue
        cleaned_groups = []
        for group in groups:
            if not isinstance(group, dict):
                cleaned_groups.append(group)
                continue
            hooks = group.get("hooks")
            if not isinstance(hooks, list):
                cleaned_groups.append(group)
                continue
            kept_hooks = [hook for hook in hooks if not is_managed_hook(hook)]
            if kept_hooks:
                new_group = copy.deepcopy(group)
                new_group["hooks"] = kept_hooks
                cleaned_groups.append(new_group)
            elif any(key != "hooks" for key in group):
                # Matcher-only groups have no effect and are safe to omit.
                continue
        if cleaned_groups:
            cleaned_events[event] = cleaned_groups
    result["hooks"] = cleaned_events
    return result


def render(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, replacement in replacements.items():
            value = value.replace("{{" + key + "}}", replacement)
        return value
    if isinstance(value, list):
        return [render(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: render(item, replacements) for key, item in value.items()}
    return value


def apply_fragments(
    document: dict[str, Any],
    fragments: Iterable[dict[str, Any]],
    replacements: dict[str, str],
) -> dict[str, Any]:
    result = remove_managed_hooks(document)
    target_events = result.setdefault("hooks", {})
    if not isinstance(target_events, dict):
        raise ValueError("target hooks value must be an object")

    for raw_fragment in fragments:
        fragment = render(raw_fragment, replacements)
        events = fragment.get("hooks", {})
        if not isinstance(events, dict):
            raise ValueError("fragment hooks value must be an object")
        for event, groups in events.items():
            if not isinstance(groups, list):
                raise ValueError(f"fragment event {event!r} must contain a list")
            target_groups = target_events.setdefault(event, [])
            if not isinstance(target_groups, list):
                raise ValueError(f"target event {event!r} does not contain a list")
            target_groups.extend(copy.deepcopy(groups))
    return result


def hooks_diff(before: dict[str, Any], after: dict[str, Any], label: str) -> str:
    before_text = json.dumps(
        before.get("hooks", {}), indent=2, ensure_ascii=False, sort_keys=True
    ).splitlines()
    after_text = json.dumps(
        after.get("hooks", {}), indent=2, ensure_ascii=False, sort_keys=True
    ).splitlines()
    return "\n".join(
        difflib.unified_diff(
            before_text,
            after_text,
            fromfile=f"{label}:hooks.before",
            tofile=f"{label}:hooks.after",
            lineterm="",
        )
    )


def atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    old_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, old_mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_fragments(paths: Iterable[Path]) -> list[dict[str, Any]]:
    return [load_json(path) for path in paths]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("apply", "remove"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--fragment", type=Path, action="append", default=[])
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    before = load_json(args.config)
    replacements = {
        "BIN_DIR": str(args.bin_dir.expanduser().resolve()),
        "REPO_ROOT": str(
            (args.repo_root or Path(__file__).resolve().parents[1]).resolve()
        ),
    }
    if args.operation == "apply":
        after = apply_fragments(
            before, load_fragments(args.fragment), replacements
        )
    else:
        after = remove_managed_hooks(before)

    diff = hooks_diff(before, after, str(args.config))
    if diff:
        print(diff)
    else:
        print(f"{args.config}: hooks already up to date")
    if not args.dry_run and before != after:
        atomic_write_json(args.config, after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
