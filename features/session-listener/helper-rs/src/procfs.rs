//! Process identity read from /proc.

use std::collections::HashMap;
use std::fs;

fn stat_fields(pid: u32) -> Option<Vec<String>> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // `comm` may contain spaces and parentheses; the fields start after the last ')'.
    let fields = stat.get(stat.rfind(')')? + 2..)?;
    Some(fields.split_whitespace().map(str::to_string).collect())
}

/// Start time in clock ticks, which tells a process from a later one that reuses its pid.
pub fn start_ticks(pid: u32) -> Option<u64> {
    stat_fields(pid)?.get(19)?.parse().ok()
}

pub fn parent_pid(pid: u32) -> Option<u32> {
    stat_fields(pid)?.get(1)?.parse().ok()
}

pub fn descends_from(pid: u32, ancestor: u32) -> bool {
    let mut current = pid;
    let mut seen = Vec::new();
    for _ in 0..128 {
        if current == ancestor {
            return true;
        }
        if current <= 1 || seen.contains(&current) {
            return false;
        }
        seen.push(current);
        match parent_pid(current) {
            Some(parent) => current = parent,
            None => return false,
        }
    }
    false
}

/// The inode of the socket behind one descriptor of a live process.
pub fn fd_socket_inode(pid: u32, fd: i32) -> Option<u64> {
    let target = fs::read_link(format!("/proc/{pid}/fd/{fd}")).ok()?;
    let inode = target.to_str()?.strip_prefix("socket:[")?.strip_suffix(']')?;
    (!inode.is_empty() && inode.bytes().all(|byte| byte.is_ascii_digit()))
        .then(|| inode.parse().ok())
        .flatten()
}

pub fn environment(pid: u32) -> HashMap<String, String> {
    let raw = fs::read(format!("/proc/{pid}/environ")).unwrap_or_default();
    raw.split(|&byte| byte == 0)
        .filter_map(|item| {
            let split = item.iter().position(|&byte| byte == b'=')?;
            Some((
                String::from_utf8_lossy(&item[..split]).into_owned(),
                String::from_utf8_lossy(&item[split + 1..]).into_owned(),
            ))
        })
        .collect()
}

pub fn command(pid: u32) -> Vec<String> {
    let raw = fs::read(format!("/proc/{pid}/cmdline")).unwrap_or_default();
    raw.split(|&byte| byte == 0)
        .filter(|item| !item.is_empty())
        .map(|item| String::from_utf8_lossy(item).into_owned())
        .collect()
}

pub fn comm(pid: u32) -> String {
    fs::read_to_string(format!("/proc/{pid}/comm"))
        .map(|text| text.trim_end().to_string())
        .unwrap_or_default()
}

pub fn pids() -> Vec<u32> {
    fs::read_dir("/proc")
        .map(|entries| {
            entries
                .flatten()
                .filter_map(|entry| entry.file_name().to_str()?.parse().ok())
                .collect()
        })
        .unwrap_or_default()
}

fn is_codex(command: &[String]) -> bool {
    command
        .first()
        .is_some_and(|program| program.trim_end_matches('/').rsplit('/').next() == Some("codex"))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClientKind {
    Tui,
    RemoteProxy,
}

impl ClientKind {
    pub fn as_str(self) -> &'static str {
        match self {
            ClientKind::Tui => "tui",
            ClientKind::RemoteProxy => "remote_proxy",
        }
    }
}

/// Classify trusted native clients of the shared app-server socket.
pub fn client_kind_of(command: &[String]) -> Option<ClientKind> {
    if !is_codex(command) {
        return None;
    }
    let arguments = &command[1..];
    if arguments.iter().any(|argument| argument == "app-server-control") {
        return None;
    }
    match arguments.iter().position(|argument| argument == "app-server") {
        None => Some(ClientKind::Tui),
        Some(index) if arguments.get(index + 1).is_some_and(|next| next == "proxy") => {
            Some(ClientKind::RemoteProxy)
        }
        Some(_) => None,
    }
}

pub fn is_app_server(command: &[String]) -> bool {
    is_codex(command) && command[1..].iter().any(|argument| argument == "app-server")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn words(text: &str) -> Vec<String> {
        text.split(' ').map(str::to_string).collect()
    }

    #[test]
    fn classifies_native_codex_processes() {
        assert_eq!(client_kind_of(&words("/opt/bin/codex resume x")), Some(ClientKind::Tui));
        assert_eq!(client_kind_of(&words("codex app-server proxy")), Some(ClientKind::RemoteProxy));
        assert_eq!(client_kind_of(&words("codex app-server")), None);
        assert_eq!(client_kind_of(&words("codex app-server-control app-server proxy")), None);
        assert_eq!(client_kind_of(&words("python3 listener.py")), None);
        assert_eq!(client_kind_of(&[]), None);
        assert!(is_app_server(&words("/x/codex --flag app-server")));
        assert!(!is_app_server(&words("python3 fake_server.py app-server")));
    }

    #[test]
    fn reads_its_own_identity() {
        let me = std::process::id();
        assert!(start_ticks(me).is_some());
        assert!(descends_from(me, parent_pid(me).unwrap()));
        assert!(!descends_from(me, u32::MAX));
        assert!(pids().contains(&me));
        assert_eq!(comm(me).is_empty(), false);
    }

    #[test]
    fn only_socket_descriptors_have_a_socket_inode() {
        use std::os::fd::AsRawFd;
        let me = std::process::id();
        let (left, _right) = std::os::unix::net::UnixStream::pair().unwrap();
        assert!(fd_socket_inode(me, left.as_raw_fd()).is_some());
        let file = std::fs::File::open("/proc/self/stat").unwrap();
        assert_eq!(fd_socket_inode(me, file.as_raw_fd()), None);
        assert_eq!(fd_socket_inode(me, 1_000_000), None);
    }
}
