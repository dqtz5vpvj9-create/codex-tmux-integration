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
    current_home = Path.home().resolve()
    monkeypatch.setattr(
        runtime, "registered_sockets", lambda home: ["/registered"]
    )
    monkeypatch.setattr(runtime, "default_socket_candidates", lambda: ["/default"])
    monkeypatch.setenv("TMUX", "/current,1,0")
    assert runtime.discover_sockets(current_home) == [
        "/current",
        "/default",
        "/registered",
    ]


def test_explicit_other_home_does_not_discover_current_runtime(
    monkeypatch, tmp_path
):
    path = runtime.registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("/registered-other\t1\n", encoding="utf-8")
    monkeypatch.setattr(
        runtime,
        "default_socket_candidates",
        lambda: (_ for _ in ()).throw(
            AssertionError("must not inspect current user's socket directory")
        ),
    )
    monkeypatch.setenv("TMUX", "/current,1,0")
    monkeypatch.setenv("CODEX_TMUX_SOCKETS", "/configured")
    assert runtime.discover_sockets(tmp_path) == ["/registered-other"]


def test_hook_commands_are_marker_owned_and_have_no_fixed_index():
    commands = [command for hooks in manager.FEATURE_HOOKS.values() for _, command in hooks]
    assert all("CODEX_TMUX_HOOK=" in command for command in commands)
    assert all("[50]" not in command and "[60]" not in command and "[70]" not in command for command in commands)


def test_attention_clear_hooks_are_synchronous():
    commands = [command for _, command in manager.FEATURE_HOOKS["window-attention"]]
    assert all(command.startswith("run-shell ") for command in commands)
    assert all(not command.startswith("run-shell -b ") for command in commands)


def test_pane_exit_hook_does_not_require_optional_hook_window():
    commands = dict(manager.FEATURE_HOOKS["title-sync"])
    pane_exit = commands["pane-exited"]
    assert "--pane #{hook_pane} --pane-exited" in pane_exit
    assert "--window" not in pane_exit


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


def test_install_appends_replacements_before_removing_owned_entries(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(
        manager,
        "owned_hook_targets",
        lambda socket, feature, hook_name: [f"{hook_name}[9]"],
    )
    monkeypatch.setattr(manager, "tmux", lambda socket, *args: calls.append(("tmux", *args)) or Result())
    monkeypatch.setattr(manager, "register_socket", lambda socket, pid: calls.append(("register", socket, pid)))
    monkeypatch.setattr(manager, "server_pid", lambda socket: 99)
    installed = manager.install_feature("/tmp/socket", "pane-logging")
    assert installed == len(manager.FEATURE_HOOKS["pane-logging"])
    for hook_name, command in manager.FEATURE_HOOKS["pane-logging"]:
        append = ("tmux", "set-hook", "-ag", hook_name, command)
        remove = ("tmux", "set-hook", "-gu", f"{hook_name}[9]")
        assert calls.index(append) < calls.index(remove)
    assert calls[-1] == ("register", "/tmp/socket", 99)


def test_install_failure_preserves_previous_hook(monkeypatch):
    calls = []

    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(
        manager,
        "FEATURE_HOOKS",
        {"window-attention": (("after-select-window", "new-command"),)},
    )
    monkeypatch.setattr(
        manager,
        "owned_hook_targets",
        lambda socket, feature, hook_name: ["after-select-window[4]"],
    )
    monkeypatch.setattr(manager, "tmux", lambda socket, *args: calls.append(args) or Result())
    monkeypatch.setattr(manager, "register_socket", lambda *args: None)
    monkeypatch.setattr(manager, "server_pid", lambda socket: 99)
    assert manager.install_feature("/tmp/socket", "window-attention") == 1
    assert ("set-hook", "-gu", "after-select-window[4]") not in calls


def test_ensure_complete_feature_does_not_reinstall(monkeypatch):
    registered = []
    monkeypatch.setattr(manager, "feature_is_complete", lambda socket, feature: True)
    monkeypatch.setattr(
        manager,
        "install_feature",
        lambda *args: (_ for _ in ()).throw(AssertionError("unexpected reinstall")),
    )
    monkeypatch.setattr(manager, "server_pid", lambda socket: 99)
    monkeypatch.setattr(
        manager,
        "register_socket",
        lambda socket, pid: registered.append((socket, pid)),
    )
    expected = len(manager.FEATURE_HOOKS["window-attention"])
    assert manager.ensure_feature("/tmp/socket", "window-attention") == expected
    assert registered == [("/tmp/socket", 99)]
