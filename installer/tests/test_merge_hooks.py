import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "merge_hooks.py"
SPEC = importlib.util.spec_from_file_location("merge_hooks_test", MODULE_PATH)
merge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(merge)


def test_merge_preserves_unrelated_settings_and_is_idempotent():
    original = {
        "env": {"SECRET": "do-not-touch"},
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/home/example/.local/bin/codex-tmux-title-sync",
                        }
                    ]
                },
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/home/example/.codex/xbrd/quota.sh",
                        }
                    ]
                },
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
                            "command": "{{BIN_DIR}}/agent-tmux-notify",
                        }
                    ]
                }
            ]
        }
    }
    expected = merge.apply_fragments(original, [fragment], {"BIN_DIR": "/tmp/bin"})
    repeated = merge.apply_fragments(expected, [fragment], {"BIN_DIR": "/tmp/bin"})
    assert expected == repeated
    assert expected["env"]["SECRET"] == "do-not-touch"
    commands = [
        hook["command"]
        for group in expected["hooks"]["Stop"]
        for hook in group["hooks"]
    ]
    assert "/home/example/.codex/xbrd/quota.sh" in commands
    assert "/tmp/bin/agent-tmux-notify" in commands
    assert all("codex-tmux-title-sync" not in value for value in commands)


def test_remove_drops_legacy_claude_wrapper_only():
    original = {
        "hooks": {
            "Notification": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/opt/example/hook_wrapper.sh notification",
                        },
                        {"type": "command", "command": "/opt/keep-me"},
                    ]
                }
            ]
        }
    }
    cleaned = merge.remove_managed_hooks(original)
    assert cleaned["hooks"]["Notification"][0]["hooks"] == [
        {"type": "command", "command": "/opt/keep-me"}
    ]
