//! `codex-session-capture --control-socket PATH`
//!
//! Started as root by the listener through an exact sudoers rule. Writes one
//! JSON record per line to stdout; see `features/session-listener/README.md`.

use std::path::PathBuf;
use std::process::ExitCode;

fn control_socket() -> Option<PathBuf> {
    let mut arguments = std::env::args_os().skip(1);
    let (flag, path, extra) = (arguments.next()?, arguments.next()?, arguments.next());
    (flag == "--control-socket" && extra.is_none()).then(|| PathBuf::from(path))
}

fn main() -> ExitCode {
    let Some(control_socket) = control_socket() else {
        eprintln!("usage: codex-session-capture --control-socket PATH");
        return ExitCode::from(2);
    };
    match session_capture::helper::run(&control_socket) {
        Ok(status) => ExitCode::from(status as u8),
        Err(message) => {
            eprintln!("codex-session-listener: {message}");
            ExitCode::FAILURE
        }
    }
}
