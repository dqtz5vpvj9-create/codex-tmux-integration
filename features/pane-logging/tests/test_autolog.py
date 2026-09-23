import importlib.machinery
import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "tmux-autolog"
LOADER = importlib.machinery.SourceFileLoader("tmux_autolog_test", str(MODULE_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
autolog = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(autolog)


def test_server_key_changes_when_server_restarts():
    assert autolog.server_key("/tmp/server", 1) != autolog.server_key("/tmp/server", 2)


def test_existing_user_pipe_is_preserved(monkeypatch):
    monkeypatch.setattr(autolog, "server_pid", lambda socket: 1)
    monkeypatch.setattr(
        autolog,
        "pane_info",
        lambda socket, pane: {"pane": pane, "pipe": True, "window": "shell"},
    )
    monkeypatch.setattr(autolog, "marker", lambda socket, pane: None)
    monkeypatch.setattr(
        autolog,
        "tmux_result",
        lambda *args: (_ for _ in ()).throw(AssertionError()),
    )
    assert not autolog.enable_on("/tmp/server", "%1")


def test_owned_pipe_is_not_replaced(monkeypatch):
    monkeypatch.setattr(autolog, "server_pid", lambda socket: 1)
    monkeypatch.setattr(
        autolog,
        "pane_info",
        lambda socket, pane: {"pane": pane, "pipe": True, "window": "shell"},
    )
    monkeypatch.setattr(autolog, "marker", lambda socket, pane: {"version": 2})
    assert autolog.enable_on("/tmp/server", "%1")


def test_cleanup_preserves_replaced_user_pipe(monkeypatch, tmp_path):
    pid_file = tmp_path / "sink.pid"
    logfile = tmp_path / "pane.log"
    monkeypatch.setattr(
        autolog,
        "pane_info",
        lambda socket, pane: {"pane": pane, "pipe": True, "window": "shell"},
    )
    monkeypatch.setattr(
        autolog,
        "marker",
        lambda socket, pane: {"pid_file": str(pid_file), "logfile": str(logfile)},
    )
    calls = []
    monkeypatch.setattr(autolog, "tmux_result", lambda socket, *args: calls.append(args))
    monkeypatch.setattr(autolog, "unset_marker", lambda socket, pane: calls.append(("unset", pane)))
    assert autolog.close_owned_pipe("/tmp/server", "%1")
    assert not any(call and call[0] == "pipe-pane" for call in calls)
    assert ("unset", "%1") in calls


def test_migrate_replaces_only_verified_legacy_sink(monkeypatch, tmp_path):
    monkeypatch.setattr(autolog, "LOGDIR", tmp_path)
    monkeypatch.setattr(autolog, "list_panes", lambda socket: ["%1", "%2"])
    monkeypatch.setattr(
        autolog,
        "pane_info",
        lambda socket, pane: {"pane": pane, "pipe": True, "window": "shell"},
    )
    monkeypatch.setattr(autolog, "marker", lambda socket, pane: None)
    legacy_one = tmp_path / autolog.legacy_server_key("/tmp/server") / "1.log"
    monkeypatch.setattr(
        autolog,
        "_find_sink_pid",
        lambda path: 100 if path == str(legacy_one) else -1,
    )
    calls = []
    monkeypatch.setattr(autolog, "tmux_result", lambda socket, *args: calls.append(args))
    monkeypatch.setattr(autolog, "enable_on", lambda socket, pane: calls.append(("enable", pane)) or True)
    assert autolog.migrate("/tmp/server") == 1
    assert ("pipe-pane", "-t", "%1") in calls
    assert ("enable", "%1") in calls
    assert not any(call == ("pipe-pane", "-t", "%2") for call in calls)


def test_prune_operation_uses_the_configured_log_root(monkeypatch, tmp_path):
    monkeypatch.setattr(autolog, "LOGDIR", tmp_path)
    monkeypatch.setattr(autolog, "active_log_paths", lambda sockets: set())
    monkeypatch.setattr(autolog, "active_log_records", lambda sockets: [])
    calls = []
    monkeypatch.setattr(
        autolog,
        "prune_log_files",
        lambda root, **kwargs: calls.append((root, kwargs)) or 0,
    )
    assert autolog.prune_logs() == 0
    assert calls == [
        (
            tmp_path,
            {
                "protected": set(),
                "collect_unprotected": True,
            },
        )
    ]


def test_active_log_paths_only_protect_live_owned_sinks(monkeypatch, tmp_path):
    pid_file = tmp_path / "sink.pid"
    pid_file.write_text("42\n", encoding="ascii")
    logfile = tmp_path / "server" / "1.log"
    monkeypatch.setattr(autolog, "live_sockets", lambda: ["/tmp/socket"])
    monkeypatch.setattr(autolog, "server_pid", lambda socket: 99)
    monkeypatch.setattr(autolog, "list_panes", lambda socket: ["%1", "%2"])
    monkeypatch.setattr(
        autolog,
        "marker",
        lambda socket, pane: (
            {"logfile": str(logfile), "pid_file": str(pid_file)}
            if pane == "%1"
            else {"logfile": str(tmp_path / "server" / "2.log"), "pid_file": str(tmp_path / "missing")}
        ),
    )
    monkeypatch.setattr(autolog, "_sink_process_matches", lambda pid, path: pid == 42)

    assert autolog.active_log_paths() == {logfile}


def test_prune_evicts_oldest_active_group_when_live_budget_is_full(monkeypatch, tmp_path):
    old_log = tmp_path / "server" / "1.log"
    new_log = tmp_path / "server" / "2.log"
    old_log.parent.mkdir()
    old_log.write_bytes(b"old" * 10)
    new_log.write_bytes(b"new" * 10)
    records = [
        {
            "socket": "/tmp/socket",
            "pane": "%1",
            "logfile": old_log,
            "size": old_log.stat().st_size,
            "latest": 1.0,
        },
        {
            "socket": "/tmp/socket",
            "pane": "%2",
            "logfile": new_log,
            "size": new_log.stat().st_size,
            "latest": 2.0,
        },
    ]
    monkeypatch.setattr(autolog, "LOGDIR", tmp_path)
    monkeypatch.setattr(autolog, "max_total_bytes", lambda: 40)
    monkeypatch.setattr(
        autolog,
        "active_log_records",
        lambda sockets: list(records),
    )
    monkeypatch.setattr(
        autolog,
        "active_log_paths",
        lambda sockets: {record["logfile"] for record in records},
    )

    def close(socket, pane):
        assert (socket, pane) == ("/tmp/socket", "%1")
        records.pop(0)
        return True

    monkeypatch.setattr(autolog, "close_owned_pipe", close)

    autolog.prune_logs("/tmp/socket")

    assert records == [
        {
            "socket": "/tmp/socket",
            "pane": "%2",
            "logfile": new_log,
            "size": new_log.stat().st_size,
            "latest": 2.0,
        }
    ]
    assert not old_log.exists()
    assert new_log.exists()


def test_pane_whose_start_command_is_excluded_is_not_logged(monkeypatch):
    monkeypatch.setattr(autolog, "server_pid", lambda socket: 1)
    monkeypatch.setattr(autolog, "marker", lambda socket, pane: None)
    monkeypatch.setattr(
        autolog,
        "pane_info",
        lambda socket, pane: {"pane": pane, "pipe": False, "window": "w",
                              "command": "'/home/u/bin/agent-tmux-sidebar' --inside @3 34 44"},
    )
    options = {autolog.EXCLUDE_OPTION: "agent-tmux-sidebar"}
    monkeypatch.setattr(autolog, "tmux", lambda socket, *args: options.get(args[-1], ""))
    monkeypatch.setattr(
        autolog, "sink_path", lambda: (_ for _ in ()).throw(AssertionError("must not start a sink"))
    )
    assert not autolog.enable_on("/tmp/server", "%7")


def test_no_exclusion_pattern_excludes_nothing(monkeypatch):
    monkeypatch.setattr(autolog, "tmux", lambda socket, *args: "")
    assert not autolog.excluded("/tmp/server", {"command": "agent-tmux-sidebar"})

