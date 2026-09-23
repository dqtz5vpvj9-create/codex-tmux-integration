//! Load the precompiled BPF object through the system's libbpf.
//!
//! libbpf is opened at run time by its soname, so the helper needs neither the
//! development package nor a bindings crate. Only the calls below are used.

use std::collections::BTreeSet;
use std::ffi::{c_char, c_int, c_long, c_void, CString};
use std::path::Path;

use crate::{procfs, sys, MAX_CAPTURE_BYTES};

pub type SampleCallback = extern "C" fn(context: *mut c_void, data: *mut c_void, size: usize) -> c_int;

/// `struct event_t` of the BPF program.
#[repr(C)]
pub struct CaptureEvent {
    pub timestamp_ns: u64,
    pub drop_generation: u64,
    pub pid: u32,
    pub tid: u32,
    pub fd: i32,
    pub direction: u8,
    pub original_len: u32,
    pub captured_len: u32,
    pub data: [u8; MAX_CAPTURE_BYTES],
}

/// `struct process_event_t` of the BPF program.
#[repr(C)]
pub struct ProcessEvent {
    pub timestamp_ns: u64,
    pub pid: u32,
    pub kind: u8,
    pub comm: [u8; 16],
}

pub const AGENT_COMMS: [&str; 2] = ["claude", "codex"];

struct Library {
    object_open_file: unsafe extern "C" fn(*const c_char, *const c_void) -> *mut c_void,
    get_error: unsafe extern "C" fn(*const c_void) -> c_long,
    object_load: unsafe extern "C" fn(*mut c_void) -> c_int,
    object_next_program: unsafe extern "C" fn(*const c_void, *mut c_void) -> *mut c_void,
    program_attach: unsafe extern "C" fn(*const c_void) -> *mut c_void,
    link_destroy: unsafe extern "C" fn(*mut c_void) -> c_int,
    object_find_map_fd_by_name: unsafe extern "C" fn(*const c_void, *const c_char) -> c_int,
    map_update_elem: unsafe extern "C" fn(c_int, *const c_void, *const c_void, u64) -> c_int,
    map_delete_elem: unsafe extern "C" fn(c_int, *const c_void) -> c_int,
    ring_buffer_new: unsafe extern "C" fn(c_int, SampleCallback, *mut c_void, *const c_void) -> *mut c_void,
    ring_buffer_add: unsafe extern "C" fn(*mut c_void, c_int, SampleCallback, *mut c_void) -> c_int,
    ring_buffer_poll: unsafe extern "C" fn(*mut c_void, c_int) -> c_int,
    ring_buffer_free: unsafe extern "C" fn(*mut c_void),
    object_close: unsafe extern "C" fn(*mut c_void),
}

impl Library {
    fn open() -> Result<Self, String> {
        let handle = unsafe { sys::dlopen(c"libbpf.so.1".as_ptr(), sys::RTLD_NOW) };
        if handle.is_null() {
            return Err("libbpf is required by the capture helper".into());
        }
        macro_rules! symbol {
            ($name:literal) => {{
                let address = unsafe { sys::dlsym(handle, $name.as_ptr()) };
                if address.is_null() {
                    return Err(format!("libbpf lacks {}", $name.to_string_lossy()));
                }
                // The field's type is the C prototype of the symbol it names.
                unsafe { std::mem::transmute::<*mut c_void, _>(address) }
            }};
        }
        Ok(Library {
            object_open_file: symbol!(c"bpf_object__open_file"),
            get_error: symbol!(c"libbpf_get_error"),
            object_load: symbol!(c"bpf_object__load"),
            object_next_program: symbol!(c"bpf_object__next_program"),
            program_attach: symbol!(c"bpf_program__attach"),
            link_destroy: symbol!(c"bpf_link__destroy"),
            object_find_map_fd_by_name: symbol!(c"bpf_object__find_map_fd_by_name"),
            map_update_elem: symbol!(c"bpf_map_update_elem"),
            map_delete_elem: symbol!(c"bpf_map_delete_elem"),
            ring_buffer_new: symbol!(c"ring_buffer__new"),
            ring_buffer_add: symbol!(c"ring_buffer__add"),
            ring_buffer_poll: symbol!(c"ring_buffer__poll"),
            ring_buffer_free: symbol!(c"ring_buffer__free"),
            object_close: symbol!(c"bpf_object__close"),
        })
    }
}

fn describe(code: i64) -> String {
    std::io::Error::from_raw_os_error(code.unsigned_abs() as i32).to_string()
}

pub struct Capture {
    library: Library,
    object: *mut c_void,
    links: Vec<*mut c_void>,
    ring_buffer: *mut c_void,
    watched_map: c_int,
    watched: BTreeSet<u64>,
}

impl Capture {
    /// Load and attach every program of the object. Both callbacks receive
    /// `context` and run on the thread that calls `poll`.
    pub fn load(
        object_path: &Path,
        on_capture: SampleCallback,
        on_process: SampleCallback,
        context: *mut c_void,
    ) -> Result<Self, String> {
        let library = Library::open()?;
        let path = CString::new(object_path.as_os_str().as_encoded_bytes())
            .map_err(|_| "BPF object path contains a NUL byte".to_string())?;
        let object = unsafe { (library.object_open_file)(path.as_ptr(), std::ptr::null()) };
        let mut capture = Capture {
            library,
            object: std::ptr::null_mut(),
            links: Vec::new(),
            ring_buffer: std::ptr::null_mut(),
            watched_map: -1,
            watched: BTreeSet::new(),
        };
        capture.check(object, "open BPF object")?;
        capture.object = object;

        let loaded = unsafe { (capture.library.object_load)(object) };
        if loaded != 0 {
            return Err(format!("load BPF object failed: {}", describe(loaded.into())));
        }
        let mut program = std::ptr::null_mut();
        loop {
            program = unsafe { (capture.library.object_next_program)(object, program) };
            if program.is_null() {
                break;
            }
            let link = unsafe { (capture.library.program_attach)(program) };
            capture.check(link, "attach BPF tracepoint")?;
            capture.links.push(link);
        }

        let map = |name: &std::ffi::CStr| unsafe {
            (capture.library.object_find_map_fd_by_name)(object, name.as_ptr())
        };
        let (watched, events, process_events, agent_pids) =
            (map(c"watched_fds"), map(c"events"), map(c"process_events"), map(c"agent_pids"));
        if watched < 0 || events < 0 || process_events < 0 || agent_pids < 0 {
            return Err("required BPF maps are missing".into());
        }
        capture.watched_map = watched;

        let ring_buffer =
            unsafe { (capture.library.ring_buffer_new)(events, on_capture, context, std::ptr::null()) };
        capture.check(ring_buffer, "create BPF ring buffer")?;
        capture.ring_buffer = ring_buffer;
        if unsafe { (capture.library.ring_buffer_add)(ring_buffer, process_events, on_process, context) } != 0 {
            return Err("add BPF process ring buffer failed".into());
        }

        // Agents that started before the tracepoints did; their exits count too.
        for pid in procfs::pids() {
            if AGENT_COMMS.contains(&procfs::comm(pid).as_str()) {
                let present = 1u8;
                unsafe {
                    (capture.library.map_update_elem)(
                        agent_pids,
                        (&pid as *const u32).cast(),
                        (&present as *const u8).cast(),
                        0,
                    );
                }
            }
        }
        Ok(capture)
    }

    fn check(&self, pointer: *mut c_void, action: &str) -> Result<(), String> {
        match unsafe { (self.library.get_error)(pointer) } {
            0 => Ok(()),
            code => Err(format!("{action} failed: {}", describe(code.into()))),
        }
    }

    /// Replace the set of `(pid << 32) | fd` keys whose traffic is captured.
    pub fn watch(&mut self, wanted: BTreeSet<u64>) -> Result<(), String> {
        for key in self.watched.difference(&wanted) {
            let result = unsafe { (self.library.map_delete_elem)(self.watched_map, (key as *const u64).cast()) };
            if result != 0 && result != -sys::ENOENT {
                return Err(format!("delete watched BPF fd failed: {}", describe(result.into())));
            }
        }
        for key in wanted.difference(&self.watched) {
            let present = 1u8;
            let result = unsafe {
                (self.library.map_update_elem)(
                    self.watched_map,
                    (key as *const u64).cast(),
                    (&present as *const u8).cast(),
                    0,
                )
            };
            if result != 0 {
                return Err(format!("update watched BPF fd failed: {}", describe(result.into())));
            }
        }
        self.watched = wanted;
        Ok(())
    }

    pub fn poll(&self, timeout_ms: c_int) -> Result<(), String> {
        let result = unsafe { (self.library.ring_buffer_poll)(self.ring_buffer, timeout_ms) };
        if result < 0 && result != -sys::EINTR {
            return Err(format!("poll BPF buffer failed: {}", describe(result.into())));
        }
        Ok(())
    }
}

impl Drop for Capture {
    fn drop(&mut self) {
        unsafe {
            if !self.ring_buffer.is_null() {
                (self.library.ring_buffer_free)(self.ring_buffer);
            }
            for link in self.links.drain(..).rev() {
                (self.library.link_destroy)(link);
            }
            if !self.object.is_null() {
                (self.library.object_close)(self.object);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::mem::{offset_of, size_of};

    #[test]
    fn event_layouts_match_the_bpf_structures() {
        assert_eq!(size_of::<CaptureEvent>(), 4136);
        assert_eq!(offset_of!(CaptureEvent, direction), 28);
        assert_eq!(offset_of!(CaptureEvent, original_len), 32);
        assert_eq!(offset_of!(CaptureEvent, data), 40);
        assert_eq!(size_of::<ProcessEvent>(), 32);
        assert_eq!(offset_of!(ProcessEvent, comm), 13);
    }
}
