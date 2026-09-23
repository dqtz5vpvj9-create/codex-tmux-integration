//! The few libc entry points the helper needs, declared directly.

use std::ffi::{c_char, c_int, c_void};

extern "C" {
    pub fn geteuid() -> u32;
    pub fn setuid(uid: u32) -> c_int;
    pub fn setgid(gid: u32) -> c_int;
    pub fn setgroups(size: usize, list: *const u32) -> c_int;
    pub fn signal(signum: c_int, handler: usize) -> usize;
    pub fn dlopen(filename: *const c_char, flags: c_int) -> *mut c_void;
    pub fn dlsym(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
}

pub const SIGINT: c_int = 2;
pub const SIGTERM: c_int = 15;
pub const RTLD_NOW: c_int = 2;
pub const EINTR: i32 = 4;
pub const ENOENT: i32 = 2;
