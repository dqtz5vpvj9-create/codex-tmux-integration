from __future__ import annotations

import runpy
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "bin/codex-tmux-model-switch"
MODULE = runpy.run_path(str(SCRIPT))


def test_parse_model_rows_uses_visible_order():
    screen = """
  Select Model and Effort
  1. gpt-5.6-sol (default)
› 2. gpt-5.6-terra (current)
  3. gpt-5.6-luna
"""
    assert MODULE["parse_model_rows"](screen) == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]


def test_parse_reasoning_rows_normalizes_current_and_more_reasoning():
    screen = """
  Select Reasoning Level for gpt-5.6-sol
  1. Low (default)         Fast
  2. Medium               Balanced
› 3. Extra high (current) More
  4. More reasoning…      Max and Ultra
"""
    assert MODULE["parse_reasoning_rows"](screen) == [
        "low",
        "medium",
        "extra high",
        "more reasoning",
    ]


def test_navigation_for_first_row_has_no_down_key():
    keys = MODULE["navigation_keys"](0, 7)
    assert keys[-1] == "Enter"
    assert "Down" not in keys
    assert keys.count("Up") == 9


def test_navigation_rejects_out_of_range_index():
    with pytest.raises(MODULE["SwitchError"]):
        MODULE["navigation_keys"](3, 3)


def test_relative_navigation_sends_only_the_minimum_keys():
    assert MODULE["relative_navigation_keys"](4, 1, 5) == ("Up", "Up", "Up", "Enter")
    assert MODULE["relative_navigation_keys"](1, 3, 5) == ("Down", "Down", "Enter")
    assert MODULE["relative_navigation_keys"](2, 2, 5) == ("Enter",)


def test_current_selection_prefers_visible_model_and_effort():
    screen = """
    model: gpt-5.6-sol xhigh /model to change
    • Model changed to gpt-5.6-sol max
    • Model changed to gpt-5.6-luna max
    gpt-5.6-luna max · ~/src
    """
    assert MODULE["_current_selection"](screen) == (2, 3)


def test_grid_layout_fits_a_narrow_terminal():
    model_width, cell_width, left = MODULE["_grid_layout"](72)
    assert model_width + cell_width * 4 + left <= 72
    assert cell_width >= 9


def test_narrow_grid_uses_compact_effort_labels():
    assert MODULE["_effort_display"]("xhigh", 9) == "XHi"


CLAUDE_COMPOSER = """
╭──────────────────────────────────────╮
│ > /model opus                        │
╰──────────────────────────────────────╯
"""


class FakeTmux:
    """Minimal stand-in for the tmux driver used by the Claude apply path."""

    def __init__(self, screens):
        self.screens = list(screens)
        self.sent = []
        self.stats = MODULE["PollStats"]()

    def capture(self):
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]

    def send_literal(self, value):
        self.sent.append(("literal", value))

    def send_keys(self, *keys):
        self.sent.append(("keys", keys))


def test_presets_resolve_to_each_agent_vocabulary():
    resolve = MODULE["resolve_selection"]
    namespace = type("Args", (), {"preset": "1", "model": None, "effort": None})()
    assert resolve("codex", namespace) == ("gpt-5.6-sol", "max")
    assert resolve("claude", namespace) == ("opus", "")


def test_codex_requires_an_effort_but_claude_does_not():
    resolve = MODULE["resolve_selection"]
    namespace = type("Args", (), {"preset": None, "model": "opus", "effort": None})()
    assert resolve("claude", namespace) == ("opus", "")
    with pytest.raises(MODULE["SwitchError"]):
        resolve("codex", namespace)


def test_a_missing_selection_is_rejected():
    resolve = MODULE["resolve_selection"]
    namespace = type("Args", (), {"preset": None, "model": None, "effort": None})()
    with pytest.raises(MODULE["SwitchError"]):
        resolve("claude", namespace)


def test_claude_models_can_be_configured(monkeypatch):
    monkeypatch.setenv("CODEX_TMUX_CLAUDE_MODELS", "opus, sonnet")
    assert MODULE["claude_models"]() == ("opus", "sonnet")


def test_claude_model_names_that_could_inject_keys_are_rejected(monkeypatch):
    monkeypatch.setenv("CODEX_TMUX_CLAUDE_MODELS", "opus; rm -rf /")
    with pytest.raises(MODULE["SwitchError"]):
        MODULE["claude_models"]()


def test_claude_command_is_recognized_inside_its_composer_border():
    parse = MODULE["_claude_command_visible"]("", "/model opus")
    assert parse(CLAUDE_COMPOSER) is True
    assert parse("") is None


def test_claude_switch_types_the_command_then_submits_it():
    submitted = CLAUDE_COMPOSER + "\nSet model to opus\n"
    tmux = FakeTmux(["", CLAUDE_COMPOSER, CLAUDE_COMPOSER, submitted])
    MODULE["apply_claude_model"](tmux, "opus")
    assert tmux.sent == [("literal", "/model opus"), ("keys", ("Enter",))]


def test_unknown_claude_model_sends_nothing():
    tmux = FakeTmux([""])
    with pytest.raises(MODULE["SwitchError"]):
        MODULE["apply_claude_model"](tmux, "gpt-5.6-sol")
    assert tmux.sent == []
