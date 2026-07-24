import importlib.machinery
import importlib.util
import os
from pathlib import Path


RUNTIME_PATH = Path(__file__).resolve().parents[1] / "tmux_runtime.py"
RUNTIME_SPEC = importlib.util.spec_from_file_location("tmux_runtime_test", RUNTIME_PATH)
runtime = importlib.util.module_from_spec(RUNTIME_SPEC)
assert RUNTIME_SPEC.loader is not None
RUNTIME_SPEC.loader.exec_module(runtime)

MANAGER_PATH = Path(__file__).resolve().parents[1] / "codex-tmux-hook-manager"
LOADER = importlib.machinery.SourceFileLoader("hook_manager_test", str(MANAGER_PATH))
MANAGER_SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
manager = importlib.util.module_from_spec(MANAGER_SPEC)
LOADER.exec_module(manager)


def test_registry_preserves_custom_sockets(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "server_pid", lambda socket: 123)
    runtime.register_socket("/custom/socket", home=tmp_path)
    assert runtime.registered_sockets(tmp_path) == ["/custom/socket"]
    assert runtime.registry_path(tmp_path).stat().st_mode & 0o777 == 0o600


def test_discovery_combines_default_registry_and_environment(monkeypatch, tmp_path):
    path = runtime.registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("/registered\t1\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "default_socket_candidates", lambda: ["/default"])
    monkeypatch.setenv("TMUX", "/current,1,0")
    assert runtime.discover_sockets(tmp_path) == ["/current", "/default", "/registered"]


def test_hook_commands_are_marker_owned_and_have_no_fixed_index():
    commands = [command for hooks in manager.FEATURE_HOOKS.values() for _, command in hooks]
    assert all("CODEX_TMUX_HOOK=" in command for command in commands)
    assert all("[50]" not in command and "[60]" not in command and "[70]" not in command for command in commands)


def test_attention_clear_hooks_are_synchronous():
    commands = [command for _, command in manager.FEATURE_HOOKS["window-attention"]]
    assert all(command.startswith("run-shell ") for command in commands)
    assert all(not command.startswith("run-shell -b ") for command in commands)


def test_owned_hook_targets_select_only_matching_marker(monkeypatch):
    class Result:
        returncode = 0
        stdout = "\n".join(
            [
                "after-select-window[0] run-shell user-command",
                "after-select-window[1] run-shell 'env CODEX_TMUX_HOOK=title-sync command'",
                "after-select-window[2] run-shell 'env CODEX_TMUX_HOOK=other command'",
            ]
        )

    monkeypatch.setattr(manager, "tmux", lambda *args: Result())
    assert manager.owned_hook_targets("/tmp/socket", "title-sync", "after-select-window") == [
        "after-select-window[1]"
    ]


def test_install_removes_owned_entries_then_appends(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(manager, "remove_feature", lambda socket, feature: calls.append(("remove", feature)) or 2)
    monkeypatch.setattr(manager, "tmux", lambda socket, *args: calls.append(("tmux", *args)) or Result())
    monkeypatch.setattr(manager, "register_socket", lambda socket, pid: calls.append(("register", socket, pid)))
    monkeypatch.setattr(manager, "server_pid", lambda socket: 99)
    installed = manager.install_feature("/tmp/socket", "pane-logging")
    assert installed == 3
    assert calls[0] == ("remove", "pane-logging")
    assert all(call[1:3] == ("set-hook", "-ag") for call in calls[1:4])
    assert calls[-1] == ("register", "/tmp/socket", 99)
