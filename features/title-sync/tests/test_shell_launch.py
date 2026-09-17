import subprocess
from pathlib import Path

import pytest


SHELL_FRAGMENT = Path(__file__).resolve().parents[1] / "shell" / "codex-title.zsh"


def matched_agent(command: str, *, launchers: str = "codex claude") -> str:
    """Return the launcher the preexec detector matched, or an empty string."""

    script = (
        'source "$1"; '
        'CODEX_TMUX_TITLE_LAUNCHERS="$2" '
        '__codex_tmux_title_command_starts_agent "$3" || exit 1; '
        'print -r -- "$__codex_tmux_title_agent"'
    )
    result = subprocess.run(
        ["zsh", "-fc", script, "test", str(SHELL_FRAGMENT), launchers, command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def detects(command: str, *, launchers: str = "codex claude") -> bool:
    return bool(matched_agent(command, launchers=launchers))


@pytest.mark.parametrize(
    "command",
    (
        "codex",
        "codex --profile test",
        "/opt/bin/codex resume session",
        "FOO=bar codex",
        "command codex",
        "exec codex",
        "env -i FOO=bar codex",
        "codex *",
    ),
)
def test_detects_codex_launch(command):
    assert matched_agent(command) == "codex"


@pytest.mark.parametrize(
    "command",
    (
        "claude",
        "claude --resume",
        "/opt/bin/claude --dangerously-skip-permissions",
        "FOO=bar claude",
        "command claude",
        "exec claude",
        "env -i FOO=bar claude",
    ),
)
def test_detects_claude_launch(command):
    assert matched_agent(command) == "claude"


@pytest.mark.parametrize(
    "command",
    (
        "echo codex",
        "python -m codex",
        "codex-helper",
        "printf '%s' codex",
        "echo claude",
        "claude-helper",
    ),
)
def test_ignores_non_agent_commands(command):
    assert not detects(command)


def test_custom_wrapper_can_be_enabled():
    assert matched_agent("cxc --profile test", launchers="codex cxc") == "cxc"


def test_launcher_list_can_exclude_an_agent():
    assert not detects("claude", launchers="codex")


def test_launch_is_marked_pending_before_helper_exposes_title():
    fragment = SHELL_FRAGMENT.read_text(encoding="utf-8")
    preexec = fragment.partition("__codex_tmux_title_preexec() {")[2].partition(
        "\n}"
    )[0]
    assert preexec.index("__codex_tmux_title_launch_pending=1") < preexec.rindex(
        '"$helper"'
    )


def test_detector_runs_without_forking_a_subshell():
    """A per-command hook must not pay for a fork on every prompt."""

    fragment = SHELL_FRAGMENT.read_text(encoding="utf-8")
    detector = fragment.partition("__codex_tmux_title_command_starts_agent() {")[
        2
    ].partition("\n}")[0]
    assert "$(" not in detector
