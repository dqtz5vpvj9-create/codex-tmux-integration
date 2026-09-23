//! Which descriptors of the app-server belong to verified Codex clients.
//!
//! Ownership comes from `ss`, which is the only component that walks every
//! process's descriptor table. That walk is the expensive part, so the helper
//! runs it when the set of sockets bound to the control path has changed (see
//! `control_socket_inodes`), not on a timer.

use std::collections::{BTreeSet, HashMap};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use crate::procfs::{self, ClientKind};

const COMMAND_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Endpoint {
    pub state: String,
    pub path: Option<String>,
    pub local_inode: u64,
    pub peer_inode: u64,
    pub pid: u32,
    pub fd: i32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TmuxIdentity {
    pub socket_path: String,
    pub pane_id: String,
    pub server_pid: u32,
    pub server_start_ticks: u64,
    pub pane_pid: u32,
    pub pane_start_ticks: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Connection {
    pub server_fd: i32,
    pub server_inode: u64,
    pub client_pid: u32,
    pub client_start_ticks: u64,
    pub client_fd: i32,
    pub client_inode: u64,
    pub client_kind: ClientKind,
    pub tmux: Option<TmuxIdentity>,
}

#[derive(Debug)]
pub struct Discovery {
    pub server_pid: u32,
    pub server_start_ticks: u64,
    pub connections: HashMap<i32, Connection>,
    /// A tmux server named by a client did not answer, so a pane binding may
    /// be missing for a reason that the next attempt can cure.
    pub incomplete: bool,
}

/// One row per descriptor that `ss -xapnH` reports for a unix socket.
pub fn parse_ss_line(line: &str) -> Vec<Endpoint> {
    let fields: Vec<&str> = line.split_whitespace().collect();
    if fields.len() < 8 || !fields[0].starts_with("u_") {
        return Vec::new();
    }
    let (Ok(local_inode), Ok(peer_inode)) = (fields[5].parse(), fields[7].parse()) else {
        return Vec::new();
    };
    let path = (fields[4] != "*").then(|| fields[4].to_string());
    let mut endpoints = Vec::new();
    let mut rest = line;
    // pid=(\d+),fd=(\d+)
    while let Some(at) = rest.find("pid=") {
        rest = &rest[at + 4..];
        let pid_length = rest.bytes().take_while(u8::is_ascii_digit).count();
        let Some(after) = rest[pid_length..].strip_prefix(",fd=") else {
            continue;
        };
        let fd_length = after.bytes().take_while(u8::is_ascii_digit).count();
        if let (Ok(pid), Ok(fd)) = (rest[..pid_length].parse(), after[..fd_length].parse()) {
            endpoints.push(Endpoint {
                state: fields[1].to_string(),
                path: path.clone(),
                local_inode,
                peer_inode,
                pid,
                fd,
            });
        }
    }
    endpoints
}

/// Run a command to completion, or kill it when it overstays.
fn run(program: &str, arguments: &[&str]) -> Result<(bool, String, String), std::io::ErrorKind> {
    let mut child = Command::new(program)
        .args(arguments)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| error.kind())?;
    // `ss` prints more than a pipe holds, so both streams are read while it runs.
    let mut stdout = child.stdout.take().unwrap();
    let mut stderr = child.stderr.take().unwrap();
    let read_stdout = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = stdout.read_to_end(&mut bytes);
        bytes
    });
    let read_stderr = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = stderr.read_to_end(&mut bytes);
        bytes
    });
    let deadline = Instant::now() + COMMAND_TIMEOUT;
    let status = loop {
        match child.try_wait().map_err(|error| error.kind())? {
            Some(status) => break status,
            None if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(std::io::ErrorKind::TimedOut);
            }
            None => std::thread::sleep(Duration::from_millis(2)),
        }
    };
    let text = |bytes: Vec<u8>| String::from_utf8_lossy(&bytes).into_owned();
    Ok((
        status.success(),
        text(read_stdout.join().unwrap_or_default()),
        text(read_stderr.join().unwrap_or_default()),
    ))
}

fn socket_endpoints() -> Result<Vec<Endpoint>, String> {
    match run("/usr/bin/ss", &["-xapnH"]) {
        Err(std::io::ErrorKind::NotFound) => Err("ss is required for socket ownership discovery".into()),
        Err(std::io::ErrorKind::TimedOut) => Err("ss timed out during socket ownership discovery".into()),
        Err(kind) => Err(format!("ss could not run: {kind}")),
        Ok((false, _, stderr)) => Err(format!("ss failed: {}", stderr.trim())),
        Ok((true, stdout, _)) => Ok(stdout.lines().flat_map(parse_ss_line).collect()),
    }
}

/// Ask the addressed tmux server for its real generation and panes. `None`
/// means the server did not answer in time, which the next attempt may cure;
/// a server that is gone simply has no panes.
fn tmux_inventory(socket_path: &str) -> Option<HashMap<String, TmuxIdentity>> {
    let arguments = ["-S", socket_path, "list-panes", "-a", "-F", "#{pid}\t#{pane_id}\t#{pane_pid}"];
    let stdout = match run("/usr/bin/tmux", &arguments) {
        Err(std::io::ErrorKind::TimedOut) => return None,
        Ok((true, stdout, _)) => stdout,
        _ => return Some(HashMap::new()),
    };
    let mut panes = HashMap::new();
    for line in stdout.lines() {
        let fields: Vec<&str> = line.split('\t').collect();
        let [server, pane_id, pane] = fields[..] else {
            continue;
        };
        let (Ok(server_pid), Ok(pane_pid)) = (server.parse(), pane.parse()) else {
            continue;
        };
        let (Some(server_start_ticks), Some(pane_start_ticks)) =
            (procfs::start_ticks(server_pid), procfs::start_ticks(pane_pid))
        else {
            continue;
        };
        panes.insert(
            pane_id.to_string(),
            TmuxIdentity {
                socket_path: socket_path.to_string(),
                pane_id: pane_id.to_string(),
                server_pid,
                server_start_ticks,
                pane_pid,
                pane_start_ticks,
            },
        );
    }
    Some(panes)
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct TmuxCandidate {
    pub socket_path: String,
    pub pane_id: String,
    pub advertised_server_pid: u32,
}

/// The pane a client claims through its inherited `TMUX` and `TMUX_PANE`.
pub fn tmux_candidate(environment: &HashMap<String, String>) -> Option<TmuxCandidate> {
    let pane = environment.get("TMUX_PANE")?;
    let tmux = environment.get("TMUX")?;
    if !pane.starts_with('%') || tmux.is_empty() {
        return None;
    }
    // socket path, server pid, session: split from the right, the path may hold commas.
    let mut fields = tmux.rsplitn(3, ',');
    let (last, middle, first) = (fields.next()?, fields.next(), fields.next());
    let (socket_path, server_pid) = match (first, middle) {
        (Some(path), Some(pid)) => (path, pid),
        (None, Some(path)) => (path, last),
        _ => return None,
    };
    let advertised_server_pid: u32 = server_pid.parse().ok()?;
    (socket_path.starts_with('/') && advertised_server_pid > 1).then(|| TmuxCandidate {
        socket_path: socket_path.to_string(),
        pane_id: pane.clone(),
        advertised_server_pid,
    })
}

/// The control socket path the way the kernel reports it: symlinks resolved,
/// as far as the path exists.
pub fn resolve(path: &Path) -> String {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir().unwrap_or_default().join(path)
    };
    let mut resolved = PathBuf::new();
    let mut missing = false;
    for component in absolute.components() {
        resolved.push(component);
        if !missing {
            match resolved.canonicalize() {
                Ok(real) => resolved = real,
                Err(_) => missing = true,
            }
        }
    }
    resolved.to_string_lossy().into_owned()
}

/// Inodes of every socket bound to the control path: the listener and the
/// app-server's side of each accepted connection. Any connection that opens
/// or closes changes this set, and reading it costs one small file.
pub fn control_socket_inodes(target: &str) -> BTreeSet<u64> {
    let table = std::fs::read_to_string("/proc/net/unix").unwrap_or_default();
    table.lines().skip(1).filter_map(|line| unix_table_row(line, target)).collect()
}

fn unix_table_row(line: &str, target: &str) -> Option<u64> {
    // Num RefCount Protocol Flags Type St Inode Path
    let mut rest = line;
    let mut fields = [""; 7];
    for field in &mut fields {
        rest = rest.trim_start();
        let end = rest.find(' ').unwrap_or(rest.len());
        (*field, rest) = rest.split_at(end);
    }
    (rest.strip_prefix(' ')? == target).then(|| fields[6].parse().ok()).flatten()
}

pub fn discover(control_socket: &Path) -> Result<Discovery, String> {
    let target = resolve(control_socket);
    let endpoints = socket_endpoints()?;
    let listeners: BTreeSet<(u32, i32, u64)> = endpoints
        .iter()
        .filter(|endpoint| endpoint.state == "LISTEN" && endpoint.path.as_deref() == Some(&target))
        .map(|endpoint| (endpoint.pid, endpoint.fd, endpoint.local_inode))
        .collect();
    let (server_pid, listener_fd) = match listeners.iter().collect::<Vec<_>>()[..] {
        [] => return Err(format!("no app-server is listening on {target}")),
        [&(pid, fd, _)] => (pid, fd),
        _ => return Err(format!("multiple app-servers own {target}: {listeners:?}")),
    };
    if !procfs::is_app_server(&procfs::command(server_pid)) {
        return Err(format!("control socket listener {server_pid} is not a native Codex app-server"));
    }
    let server_start_ticks = procfs::start_ticks(server_pid)
        .ok_or_else(|| format!("app-server process {server_pid} disappeared"))?;

    let mut holders: HashMap<(u64, u64), Vec<&Endpoint>> = HashMap::new();
    for endpoint in &endpoints {
        holders.entry((endpoint.local_inode, endpoint.peer_inode)).or_default().push(endpoint);
    }

    struct Pending<'a> {
        server_side: &'a Endpoint,
        client_pid: u32,
        client_fd: i32,
        client_start_ticks: u64,
        client_kind: ClientKind,
        candidate: Option<TmuxCandidate>,
    }
    let mut pending = Vec::new();
    for server_side in endpoints.iter().filter(|endpoint| {
        endpoint.pid == server_pid
            && endpoint.path.as_deref() == Some(&target)
            && endpoint.state == "ESTAB"
            && endpoint.fd != listener_fd
    }) {
        let peers: BTreeSet<(u32, i32)> = holders
            .get(&(server_side.peer_inode, server_side.local_inode))
            .into_iter()
            .flatten()
            .filter(|peer| peer.pid != server_pid)
            .map(|peer| (peer.pid, peer.fd))
            .collect();
        // A client end held by more than one process cannot be attributed.
        let [(client_pid, client_fd)] = peers.into_iter().collect::<Vec<_>>()[..] else {
            continue;
        };
        let Some(client_kind) = procfs::client_kind_of(&procfs::command(client_pid)) else {
            continue;
        };
        let Some(client_start_ticks) = procfs::start_ticks(client_pid) else {
            continue;
        };
        let candidate = (client_kind == ClientKind::Tui)
            .then(|| tmux_candidate(&procfs::environment(client_pid)))
            .flatten();
        pending.push(Pending { server_side, client_pid, client_fd, client_start_ticks, client_kind, candidate });
    }

    let mut incomplete = false;
    let mut inventories: HashMap<&str, HashMap<String, TmuxIdentity>> = HashMap::new();
    for candidate in pending.iter().filter_map(|item| item.candidate.as_ref()) {
        if !inventories.contains_key(candidate.socket_path.as_str()) {
            let inventory = tmux_inventory(&candidate.socket_path);
            incomplete |= inventory.is_none();
            inventories.insert(candidate.socket_path.as_str(), inventory.unwrap_or_default());
        }
    }

    let mut connections = HashMap::new();
    for item in &pending {
        let tmux = item.candidate.as_ref().and_then(|candidate| {
            let identity = inventories.get(candidate.socket_path.as_str())?.get(&candidate.pane_id)?;
            (identity.server_pid == candidate.advertised_server_pid
                && procfs::descends_from(item.client_pid, identity.pane_pid))
            .then(|| identity.clone())
        });
        connections.insert(
            item.server_side.fd,
            Connection {
                server_fd: item.server_side.fd,
                server_inode: item.server_side.local_inode,
                client_pid: item.client_pid,
                client_start_ticks: item.client_start_ticks,
                client_fd: item.client_fd,
                client_inode: item.server_side.peer_inode,
                client_kind: item.client_kind,
                tmux,
            },
        );
    }
    Ok(Discovery { server_pid, server_start_ticks, connections, incomplete })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ss_line_yields_one_endpoint_per_holder() {
        let line = r#"u_str ESTAB 0 0 /home/u/.codex/app.sock 4711 * 4712 users:(("codex",pid=10,fd=38),("codex",pid=11,fd=7))"#;
        let endpoints = parse_ss_line(line);
        assert_eq!(endpoints.len(), 2);
        assert_eq!(endpoints[0].path.as_deref(), Some("/home/u/.codex/app.sock"));
        assert_eq!((endpoints[0].local_inode, endpoints[0].peer_inode), (4711, 4712));
        assert_eq!((endpoints[1].pid, endpoints[1].fd), (11, 7));
        assert_eq!(parse_ss_line("u_str ESTAB 0 0 * 4712 * 4711 users:((\"x\",pid=3,fd=4))")[0].path, None);
        assert!(parse_ss_line("tcp ESTAB 0 0 a 1 b 2 users:((\"x\",pid=3,fd=4))").is_empty());
        assert!(parse_ss_line("u_str ESTAB 0 0 * x * 1 pid=3,fd=4").is_empty());
        assert!(parse_ss_line("u_str ESTAB 0 0 * 1 * 2 pid=3 fd=4").is_empty());
    }

    #[test]
    fn tmux_candidate_needs_a_pane_an_absolute_socket_and_a_real_server_pid() {
        let env = |tmux: &str, pane: &str| {
            HashMap::from([("TMUX".to_string(), tmux.to_string()), ("TMUX_PANE".to_string(), pane.to_string())])
        };
        let candidate = tmux_candidate(&env("/tmp/tmux-1000/de,fault,4242,7", "%12")).unwrap();
        assert_eq!(candidate.socket_path, "/tmp/tmux-1000/de,fault");
        assert_eq!((candidate.advertised_server_pid, candidate.pane_id.as_str()), (4242, "%12"));
        assert_eq!(tmux_candidate(&env("/tmp/s,4242", "%1")).unwrap().advertised_server_pid, 4242);
        assert_eq!(tmux_candidate(&env("/tmp/s,4242,7", "12")), None);
        assert_eq!(tmux_candidate(&env("relative,4242,7", "%1")), None);
        assert_eq!(tmux_candidate(&env("/tmp/s,1,7", "%1")), None);
        assert_eq!(tmux_candidate(&env("/tmp/s", "%1")), None);
        assert_eq!(tmux_candidate(&HashMap::new()), None);
    }

    #[test]
    fn unix_table_rows_are_matched_on_the_whole_path() {
        let listener = "0000000000000000: 00000002 00000000 00010000 0001 01 38001 /run/app server.sock";
        assert_eq!(unix_table_row(listener, "/run/app server.sock"), Some(38001));
        assert_eq!(unix_table_row(listener, "/run/app"), None);
        assert_eq!(unix_table_row("0000000000000000: 00000003 00000000 00000000 0001 03 38002", "/x"), None);
    }

    #[test]
    fn bound_socket_inodes_follow_connections() {
        use std::os::unix::net::{UnixListener, UnixStream};
        let directory = std::env::temp_dir().join(format!("capture-discover-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let path = directory.join("control.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let target = resolve(&path);
        let idle = control_socket_inodes(&target);
        assert_eq!(idle.len(), 1);
        let client = UnixStream::connect(&path).unwrap();
        let (accepted, _) = listener.accept().unwrap();
        assert_eq!(control_socket_inodes(&target).len(), 2);
        drop((client, accepted));
        assert_eq!(control_socket_inodes(&target), idle);
        std::fs::remove_file(&path).unwrap();
        std::fs::remove_dir(&directory).unwrap();
    }

    #[test]
    fn missing_tail_of_a_path_is_kept_as_written() {
        assert_eq!(resolve(Path::new("/proc/self/../nonexistent-dir/x.sock")), "/proc/nonexistent-dir/x.sock");
    }
}
