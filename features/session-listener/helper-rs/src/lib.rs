//! Privileged, metadata-only eBPF capture helper.
//!
//! The helper loads the BPF program as root, drops to the invoking user, and
//! from then on only turns captured app-server bytes into session metadata.
//! User input, assistant output, tool data and raw frames never leave it.

pub mod bpf;
pub mod discover;
pub mod helper;
pub mod json;
pub mod procfs;
pub mod sanitize;
pub mod sys;
pub mod websocket;

pub const CAPTURE_SCHEMA: &str = "codex.session-capture.v1";
pub const MAX_CAPTURE_BYTES: usize = 4096;
pub const MAX_MESSAGE_BYTES: usize = 1024 * 1024;
