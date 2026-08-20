import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "merge_hooks.py"
SPEC = importlib.util.spec_from_file_location("merge_hooks_test", MODULE_PATH)
merge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(merge)


REPLACEMENTS = {"BIN_DIR": "/tmp/bin", "REPO_ROOT": "/tmp/repo"}


def test_merge_preserves_unrelated_settings_and_is_idempotent():
    original = {
        "env": {"SETTING": "keep"},
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/tmp/bin/codex-tmux-title-sync",
                        },
                        {
                            "type": "command",
                            "command": "/opt/user/quota.sh",
                        },
                    ]
                }
            ],
            "Custom": [{"hooks": [{"type": "command", "command": "/opt/custom"}]}],
        },
    }
    fragment = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "env CODEX_TMUX_INTEGRATION=window-attention {{BIN_DIR}}/agent-tmux-notify",
                        }
                    ]
                }
            ]
        }
    }
    expected = merge.apply_fragments(original, [fragment], REPLACEMENTS)
    repeated = merge.apply_fragments(expected, [fragment], REPLACEMENTS)
    assert expected == repeated
    assert expected["env"]["SETTING"] == "keep"
    commands = [
        hook["command"]
        for group in expected["hooks"]["Stop"]
        for hook in group["hooks"]
    ]
    assert "/opt/user/quota.sh" in commands
    assert any("CODEX_TMUX_INTEGRATION=window-attention" in value for value in commands)
    assert all(value != "/tmp/bin/codex-tmux-title-sync" for value in commands)


def test_same_basename_outside_install_bin_is_preserved():
    original = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/opt/user/codex-tmux-title-sync",
                        }
                    ]
                }
            ]
        }
    }
    assert merge.remove_managed_hooks(original, REPLACEMENTS) == original


def test_marker_owned_wrapper_is_removed_independent_of_path():
    original = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "env CODEX_TMUX_INTEGRATION=window-attention /some/wrapper stop",
                        },
                        {"type": "command", "command": "/opt/keep"},
                    ]
                }
            ]
        }
    }
    cleaned = merge.remove_managed_hooks(original, REPLACEMENTS)
    assert cleaned["hooks"]["Stop"][0]["hooks"] == [
        {"type": "command", "command": "/opt/keep"}
    ]


def test_empty_matcher_group_is_removed_with_owned_hook():
    original = {
        "hooks": {
            "PermissionRequest": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "env CODEX_TMUX_INTEGRATION=window-attention /tmp/bin/agent-tmux-notify",
                        }
                    ],
                }
            ]
        }
    }
    assert merge.remove_managed_hooks(original, REPLACEMENTS)["hooks"] == {}
