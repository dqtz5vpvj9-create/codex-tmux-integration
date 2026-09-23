//! The capture loop: verify, decode, reduce, publish.

use std::collections::{BTreeSet, HashMap};
use std::ffi::{c_int, c_void};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use crate::bpf::{Capture, CaptureEvent, ProcessEvent};
use crate::discover::{self, Connection, Discovery};
use crate::json::{self, Value};
use crate::websocket::{self, Decoder};
use crate::{procfs, sanitize, sys, CAPTURE_SCHEMA, MAX_CAPTURE_BYTES};

/// How often the cheap check for a changed connection set runs.
const REFRESH_INTERVAL: Duration = Duration::from_secs(2);
/// A full verification runs this often even when nothing seems to have changed.
const RECONCILE_INTERVAL: Duration = Duration::from_secs(30);
/// The app-server is gone after this many failed verifications in a row.
const MISSING_LIMIT: u32 = 3;
/// Exit status that tells the controller to start over with a new app-server.
pub const RESTART: i32 = 75;

static STOPPING: AtomicBool = AtomicBool::new(false);

extern "C" fn request_stop(_signal: c_int) {
    STOPPING.store(true, Ordering::Relaxed);
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
enum Direction {
    ClientToServer,
    ServerToClient,
}

impl Direction {
    fn as_str(self) -> &'static str {
        match self {
            Direction::ClientToServer => "client_to_server",
            Direction::ServerToClient => "server_to_client",
        }
    }
}

type Signature = Vec<(i32, u64, u32, u64, i32, u64, u32, u64, String, u32, u64)>;

fn signature_of(discovery: &Discovery) -> Signature {
    let mut rows: Signature = discovery
        .connections
        .values()
        .map(|connection| {
            let tmux = connection.tmux.as_ref();
            (
                connection.server_fd,
                connection.server_inode,
                connection.client_pid,
                connection.client_start_ticks,
                connection.client_fd,
                connection.client_inode,
                tmux.map_or(0, |tmux| tmux.server_pid),
                tmux.map_or(0, |tmux| tmux.server_start_ticks),
                tmux.map_or(String::new(), |tmux| tmux.pane_id.clone()),
                tmux.map_or(0, |tmux| tmux.pane_pid),
                tmux.map_or(0, |tmux| tmux.pane_start_ticks),
            )
        })
        .collect();
    rows.sort();
    rows
}

fn signature_json(signature: &Signature) -> Value {
    Value::Array(
        signature
            .iter()
            .map(|row| {
                Value::Array(vec![
                    Value::integer(row.0),
                    Value::integer(row.1),
                    Value::integer(row.2),
                    Value::integer(row.3),
                    Value::integer(row.4),
                    Value::integer(row.5),
                    Value::integer(row.6),
                    Value::integer(row.7),
                    Value::string(row.8.as_str()),
                    Value::integer(row.9),
                    Value::integer(row.10),
                ])
            })
            .collect(),
    )
}

struct Helper {
    control_socket: PathBuf,
    target: String,
    active_identity: Option<(u32, u64)>,
    signature: Option<Signature>,
    connections: HashMap<i32, Connection>,
    decoders: HashMap<(i32, Direction), Decoder>,
    last_threads: HashMap<i32, String>,
    last_drop_generation: Option<u64>,
    missing_refreshes: u32,
    bound_inodes: Option<BTreeSet<u64>>,
    verify_again: bool,
    last_verified: Option<Instant>,
}

impl Helper {
    fn publish(&self, record: Value) {
        let mut line = record.to_json();
        line.push('\n');
        let mut stdout = std::io::stdout().lock();
        if stdout.write_all(line.as_bytes()).and_then(|()| stdout.flush()).is_err() {
            // The controller is gone.
            STOPPING.store(true, Ordering::Relaxed);
        }
    }

    fn forget(&mut self, fd: i32) {
        self.decoders.remove(&(fd, Direction::ClientToServer));
        self.decoders.remove(&(fd, Direction::ServerToClient));
        self.last_threads.remove(&fd);
    }

    /// Whether the descriptors may have changed since they were last verified.
    fn verification_due(&mut self) -> bool {
        let bound = discover::control_socket_inodes(&self.target);
        let changed = self.bound_inodes.as_ref() != Some(&bound);
        self.bound_inodes = Some(bound);
        changed
            || self.verify_again
            || self.last_verified.is_none_or(|at| at.elapsed() >= RECONCILE_INTERVAL)
    }

    /// Verify the app-server's connections again. `Err` carries an exit status.
    fn refresh(&mut self, capture: &mut Capture) -> Result<(), i32> {
        if !self.verification_due() {
            return Ok(());
        }
        let current = match discover::discover(&self.control_socket) {
            Ok(current) => current,
            Err(_) => {
                self.verify_again = true;
                self.missing_refreshes += 1;
                if self.active_identity.is_some() && self.missing_refreshes >= MISSING_LIMIT {
                    return Err(RESTART);
                }
                return Ok(());
            }
        };
        self.missing_refreshes = 0;
        self.verify_again = current.incomplete;
        self.last_verified = Some(Instant::now());
        let identity = (current.server_pid, current.server_start_ticks);
        if self.active_identity.is_some_and(|active| active != identity) {
            return Err(RESTART);
        }
        self.active_identity = Some(identity);

        let watched = current
            .connections
            .values()
            .map(|connection| (u64::from(current.server_pid) << 32) | connection.server_fd as u32 as u64)
            .collect();
        if let Err(message) = capture.watch(watched) {
            eprintln!("codex-session-listener: {message}");
            return Err(1);
        }

        let signature = signature_of(&current);
        self.connections = current.connections;
        if self.signature.as_ref() == Some(&signature) {
            return Ok(());
        }
        let open: BTreeSet<i32> = signature.iter().map(|row| row.0).collect();
        let closed: Vec<i32> = self
            .signature
            .iter()
            .flatten()
            .map(|row| row.0)
            .filter(|fd| !open.contains(fd))
            .collect();
        for fd in closed {
            self.forget(fd);
        }
        self.publish(Value::object([
            ("schema", Value::string(CAPTURE_SCHEMA)),
            ("kind", Value::string("topology")),
            ("server_pid", Value::integer(current.server_pid)),
            ("server_start_ticks", Value::integer(current.server_start_ticks)),
            ("signature", signature_json(&signature)),
        ]));
        self.signature = Some(signature);
        Ok(())
    }

    fn on_capture(&mut self, event: &CaptureEvent) {
        let (pid, fd) = (event.pid, event.fd);
        let verified = self.connections.get(&fd).filter(|connection| {
            self.active_identity.is_some_and(|(server_pid, _)| server_pid == pid)
                && procfs::fd_socket_inode(pid, fd) == Some(connection.server_inode)
                && procfs::start_ticks(connection.client_pid) == Some(connection.client_start_ticks)
        });
        let Some(connection) = verified.cloned() else {
            self.forget(fd);
            return;
        };
        let direction =
            if event.direction == 1 { Direction::ServerToClient } else { Direction::ClientToServer };
        let masked = direction == Direction::ClientToServer;
        let captured = &event.data[..(event.captured_len as usize).min(MAX_CAPTURE_BYTES)];
        let truncated = event.original_len != event.captured_len;
        let decoder = self.decoders.entry((fd, direction)).or_insert_with(|| Decoder::new(masked));

        let mut reduced = Vec::new();
        if truncated {
            // The rest of this message is lost, but its beginning usually
            // still names the method and the thread.
            if let Some(prefix) = websocket::payload_prefix(captured, masked) {
                reduced.push(sanitize::prefix(&prefix, self.last_threads.get(&fd).map(String::as_str)));
            }
            decoder.feed(captured, true);
        } else {
            for payload in decoder.feed(captured, false) {
                if let Some(message) = json::parse(&payload).filter(Value::is_object) {
                    // Each message sees the thread the previous one established.
                    let (sanitized, latest) =
                        sanitize::message(&message, self.last_threads.get(&fd).map(String::as_str));
                    if let Some(latest) = &latest {
                        self.last_threads.insert(fd, latest.clone());
                    }
                    reduced.push((sanitized, latest));
                }
            }
        }

        for (sanitized, latest) in reduced {
            if let Some(latest) = latest {
                self.last_threads.insert(fd, latest);
            }
            let Some(message) = sanitized else {
                continue;
            };
            let pane_is_current = connection.tmux.as_ref().is_none_or(|tmux| {
                procfs::start_ticks(tmux.server_pid) == Some(tmux.server_start_ticks)
                    && procfs::start_ticks(tmux.pane_pid) == Some(tmux.pane_start_ticks)
                    && procfs::descends_from(connection.client_pid, tmux.pane_pid)
            });
            if !pane_is_current {
                continue;
            }
            self.publish(Value::object([
                ("schema", Value::string(CAPTURE_SCHEMA)),
                ("kind", Value::string("protocol")),
                ("pid", Value::integer(pid)),
                ("fd", Value::integer(fd)),
                ("direction", Value::string(direction.as_str())),
                ("message", message),
            ]));
        }
    }

    fn on_lost_events(&mut self, count: u64) {
        self.decoders.clear();
        self.last_threads.clear();
        eprintln!("codex-session-listener: discarded stream state after {count} lost events");
    }

    fn on_process(&self, event: &ProcessEvent) {
        let name = match event.kind {
            1 => "exec",
            2 => "exit",
            _ => return,
        };
        // The BPF program decides which processes are agents; an exit may
        // carry any name, because a process can rename itself after it started.
        let length = event.comm.iter().position(|&byte| byte == 0).unwrap_or(event.comm.len());
        let comm = String::from_utf8_lossy(&event.comm[..length]);
        self.publish(Value::object([
            ("schema", Value::string(CAPTURE_SCHEMA)),
            ("kind", Value::string("process")),
            ("event", Value::string(name)),
            ("pid", Value::integer(event.pid)),
            ("comm", Value::string(comm)),
        ]));
    }
}

/// Borrow a ring buffer sample as `T`. Samples are 8-byte aligned by the kernel.
unsafe fn sample<'a, T>(data: *mut c_void, size: usize) -> Option<&'a T> {
    let fits = size >= std::mem::size_of::<T>() && data as usize % std::mem::align_of::<T>() == 0;
    fits.then(|| unsafe { &*data.cast::<T>() })
}

extern "C" fn capture_sample(context: *mut c_void, data: *mut c_void, size: usize) -> c_int {
    let helper = unsafe { &mut *context.cast::<Helper>() };
    let Some(event) = (unsafe { sample::<CaptureEvent>(data, size) }) else {
        return 0;
    };
    let generation = event.drop_generation;
    let lost = match helper.last_drop_generation {
        None => generation,
        Some(last) => generation.wrapping_sub(last),
    };
    if lost != 0 {
        helper.on_lost_events(lost);
    }
    helper.last_drop_generation = Some(generation);
    helper.on_capture(event);
    0
}

extern "C" fn process_sample(context: *mut c_void, data: *mut c_void, size: usize) -> c_int {
    let helper = unsafe { &*context.cast::<Helper>() };
    if let Some(event) = unsafe { sample::<ProcessEvent>(data, size) } {
        helper.on_process(event);
    }
    0
}

/// Give up root for good. The helper only needs it to load and attach BPF.
fn drop_privileges() -> Result<(), String> {
    if unsafe { sys::geteuid() } != 0 {
        return Ok(());
    }
    let identity = |name: &str| std::env::var(name).ok();
    let (Some(uid), Some(gid)) = (identity("SUDO_UID"), identity("SUDO_GID")) else {
        return Err("capture helper requires SUDO_UID and SUDO_GID for mandatory privilege drop".into());
    };
    let (Ok(uid), Ok(gid)) = (uid.parse::<u32>(), gid.parse::<u32>()) else {
        return Err("invalid SUDO_UID or SUDO_GID".into());
    };
    if uid == 0 || gid == 0 {
        return Err("refusing to retain root as the runtime listener user".into());
    }
    let dropped = unsafe {
        sys::setgroups(0, std::ptr::null()) == 0 && sys::setgid(gid) == 0 && sys::setuid(uid) == 0
    };
    if !dropped {
        return Err(format!("dropping privileges failed: {}", std::io::Error::last_os_error()));
    }
    Ok(())
}

fn installed_bpf_object() -> Result<PathBuf, String> {
    let executable = std::env::current_exe().map_err(|error| format!("cannot locate the helper: {error}"))?;
    let object = executable.parent().unwrap_or(Path::new("/")).join("bpf/codex_session_capture.bpf.o");
    if object.is_file() {
        Ok(object)
    } else {
        Err(format!("installed BPF object is missing: {}", object.display()))
    }
}

/// Run until stopped. Returns the process exit status.
pub fn run(control_socket: &Path) -> Result<i32, String> {
    if unsafe { sys::geteuid() } != 0 {
        return Err("capture helper must run as root".into());
    }
    unsafe {
        let handler = request_stop as extern "C" fn(c_int) as usize;
        sys::signal(sys::SIGTERM, handler);
        sys::signal(sys::SIGINT, handler);
    }
    // The callbacks reach the helper through this pointer while `poll` runs;
    // between polls the loop below is its only user.
    let helper = Box::into_raw(Box::new(Helper {
        control_socket: control_socket.to_path_buf(),
        target: discover::resolve(control_socket),
        active_identity: None,
        signature: None,
        connections: HashMap::new(),
        decoders: HashMap::new(),
        last_threads: HashMap::new(),
        last_drop_generation: None,
        missing_refreshes: 0,
        bound_inodes: None,
        verify_again: false,
        last_verified: None,
    }));
    let outcome = (|| -> Result<i32, String> {
        let object = installed_bpf_object()?;
        let mut capture = Capture::load(&object, capture_sample, process_sample, helper.cast())?;
        drop_privileges()?;
        let mut last_refresh: Option<Instant> = None;
        while !STOPPING.load(Ordering::Relaxed) {
            if last_refresh.is_none_or(|at| at.elapsed() >= REFRESH_INTERVAL) {
                if let Err(status) = unsafe { &mut *helper }.refresh(&mut capture) {
                    return Ok(status);
                }
                last_refresh = Some(Instant::now());
            }
            capture.poll(100)?;
        }
        Ok(0)
    })();
    drop(unsafe { Box::from_raw(helper) });
    outcome
}
