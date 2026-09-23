//! Incremental WebSocket text-frame decoding with bounded buffering.
//!
//! The decoder sees one direction of one connection, in the chunks the BPF
//! program captured. A chunk that was cut short leaves the stream position
//! unknown; the decoder then waits for a chunk that parses cleanly from its
//! first byte to its last before it trusts the stream again.

use crate::MAX_MESSAGE_BYTES;

const HANDSHAKE_STARTS: [&[u8]; 2] = [b"GET ", b"HTTP/1.1 "];

pub struct Decoder {
    expect_masked: bool,
    buffer: Vec<u8>,
    handshake_complete: bool,
    fragment_opcode: Option<u8>,
    fragment: Vec<u8>,
    desynchronized: bool,
    /// Payload bytes of an oversized frame that are still to be discarded.
    /// The buffer is empty whenever this is non-zero.
    skipping: u64,
}

impl Decoder {
    pub fn new(expect_masked: bool) -> Self {
        Decoder {
            expect_masked,
            buffer: Vec::new(),
            handshake_complete: false,
            fragment_opcode: None,
            fragment: Vec::new(),
            desynchronized: false,
            skipping: 0,
        }
    }

    /// Feed one captured chunk and return the text messages it completed.
    /// `truncated` says that the capture dropped bytes after this chunk.
    pub fn feed(&mut self, data: &[u8], truncated: bool) -> Vec<Vec<u8>> {
        if self.desynchronized {
            return self.attempt_resync(data);
        }
        self.append(data);
        let messages = self.drain();
        if truncated {
            self.buffer.clear();
            self.skipping = 0;
            self.fragment_opcode = None;
            self.fragment.clear();
            self.desynchronized = true;
        }
        messages
    }

    fn append(&mut self, data: &[u8]) {
        let skipped = self.skipping.min(data.len() as u64) as usize;
        self.skipping -= skipped as u64;
        self.buffer.extend_from_slice(&data[skipped..]);
    }

    fn attempt_resync(&mut self, data: &[u8]) -> Vec<Vec<u8>> {
        self.buffer.clear();
        self.buffer.extend_from_slice(data);
        self.handshake_complete = true;
        self.desynchronized = false;
        let messages = self.drain();
        if !self.buffer.is_empty() || self.skipping > 0 {
            self.buffer.clear();
            self.skipping = 0;
            self.desynchronized = true;
        }
        messages
    }

    fn consume_handshake(&mut self) -> bool {
        if self.handshake_complete {
            return true;
        }
        if HANDSHAKE_STARTS.iter().any(|start| self.buffer.starts_with(start)) {
            let Some(boundary) = find(&self.buffer, b"\r\n\r\n") else {
                return false;
            };
            self.buffer.drain(..boundary + 4);
        }
        self.handshake_complete = true;
        true
    }

    fn drain(&mut self) -> Vec<Vec<u8>> {
        let mut messages = Vec::new();
        if !self.consume_handshake() {
            return messages;
        }
        while let Some((fin, opcode, payload)) = self.next_frame() {
            match opcode {
                0x8 | 0x9 | 0xA => continue,
                0x1 | 0x2 => {
                    self.fragment_opcode = Some(opcode);
                    self.fragment = payload;
                }
                0x0 if self.fragment_opcode.is_some() => self.fragment.extend_from_slice(&payload),
                _ => {
                    self.reset_fragment();
                    continue;
                }
            }
            if self.fragment.len() > MAX_MESSAGE_BYTES {
                self.reset_fragment();
                continue;
            }
            if fin {
                if self.fragment_opcode == Some(0x1) {
                    messages.push(std::mem::take(&mut self.fragment));
                }
                self.reset_fragment();
            }
        }
        messages
    }

    fn reset_fragment(&mut self) {
        self.fragment_opcode = None;
        self.fragment.clear();
    }

    fn desync(&mut self) {
        self.desynchronized = true;
        self.buffer.clear();
    }

    fn next_frame(&mut self) -> Option<(bool, u8, Vec<u8>)> {
        loop {
            let header = match parse_header(&self.buffer, self.expect_masked) {
                Header::Incomplete => return None,
                Header::Invalid => {
                    self.desync();
                    return None;
                }
                Header::Frame(header) => header,
            };
            if header.length > MAX_MESSAGE_BYTES as u64 {
                // No message can come out of this frame, so its payload is
                // dropped as it arrives instead of being collected first.
                if !matches!(header.opcode, 0x8 | 0x9 | 0xA) {
                    self.reset_fragment();
                }
                self.buffer.drain(..header.payload_offset);
                let buffered = std::mem::take(&mut self.buffer);
                self.skipping = header.length;
                self.append(&buffered);
                if self.skipping > 0 {
                    return None;
                }
                continue;
            }
            let frame_end = header.payload_offset + header.length as usize;
            if self.buffer.len() < frame_end {
                return None;
            }
            let mut payload = self.buffer[header.payload_offset..frame_end].to_vec();
            if let Some(mask) = header.mask {
                unmask(&mut payload, mask);
            }
            self.buffer.drain(..frame_end);
            return Some((header.fin, header.opcode, payload));
        }
    }
}

struct FrameHeader {
    fin: bool,
    opcode: u8,
    length: u64,
    mask: Option<[u8; 4]>,
    payload_offset: usize,
}

enum Header {
    Incomplete,
    Invalid,
    Frame(FrameHeader),
}

fn parse_header(buffer: &[u8], expect_masked: bool) -> Header {
    if buffer.len() < 2 {
        return Header::Incomplete;
    }
    let (first, second) = (buffer[0], buffer[1]);
    let opcode = first & 0x0F;
    let masked = second & 0x80 != 0;
    if first & 0x70 != 0
        || !matches!(opcode, 0x0 | 0x1 | 0x2 | 0x8 | 0x9 | 0xA)
        || masked != expect_masked
    {
        return Header::Invalid;
    }
    let (length, mut offset) = match second & 0x7F {
        126 => {
            if buffer.len() < 4 {
                return Header::Incomplete;
            }
            (u16::from_be_bytes([buffer[2], buffer[3]]) as u64, 4)
        }
        127 => {
            if buffer.len() < 10 {
                return Header::Incomplete;
            }
            (u64::from_be_bytes(buffer[2..10].try_into().unwrap()), 10)
        }
        short => (short as u64, 2),
    };
    let mask = if masked {
        if buffer.len() < offset + 4 {
            return Header::Incomplete;
        }
        offset += 4;
        Some(buffer[offset - 4..offset].try_into().unwrap())
    } else {
        None
    };
    Header::Frame(FrameHeader { fin: first & 0x80 != 0, opcode, length, mask, payload_offset: offset })
}

fn unmask(payload: &mut [u8], mask: [u8; 4]) {
    for (index, byte) in payload.iter_mut().enumerate() {
        *byte ^= mask[index % 4];
    }
}

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|window| window == needle)
}

/// The beginning of a data frame's payload, from a chunk whose end was cut off.
pub fn payload_prefix(data: &[u8], masked: bool) -> Option<Vec<u8>> {
    if HANDSHAKE_STARTS.iter().any(|start| data.starts_with(start)) || data.len() < 2 {
        return None;
    }
    let (first, second) = (data[0], data[1]);
    if first & 0x70 != 0 || !matches!(first & 0x0F, 0x1 | 0x2) || (second & 0x80 != 0) != masked {
        return None;
    }
    let mut offset = match second & 0x7F {
        126 if data.len() < 4 => return None,
        126 => 4,
        127 if data.len() < 10 => return None,
        127 => 10,
        _ => 2,
    };
    if !masked {
        return Some(data[offset..].to_vec());
    }
    if data.len() < offset + 4 {
        return None;
    }
    let mask: [u8; 4] = data[offset..offset + 4].try_into().unwrap();
    offset += 4;
    let mut payload = data[offset..].to_vec();
    unmask(&mut payload, mask);
    Some(payload)
}

/// Build one frame; the mask is fixed because only tests need frames.
pub fn frame(payload: &[u8], masked: bool, opcode: u8, fin: bool) -> Vec<u8> {
    let mut out = vec![(if fin { 0x80 } else { 0 }) | opcode];
    let mask_bit = if masked { 0x80 } else { 0 };
    match payload.len() {
        length if length < 126 => out.push(mask_bit | length as u8),
        length if length < 65536 => {
            out.push(mask_bit | 126);
            out.extend_from_slice(&(length as u16).to_be_bytes());
        }
        length => {
            out.push(mask_bit | 127);
            out.extend_from_slice(&(length as u64).to_be_bytes());
        }
    }
    if masked {
        let mask = [0x37, 0xfa, 0x21, 0x3d];
        out.extend_from_slice(&mask);
        let mut body = payload.to_vec();
        unmask(&mut body, mask);
        out.extend_from_slice(&body);
    } else {
        out.extend_from_slice(payload);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reassembles_a_message_split_across_chunks_and_fragments() {
        let mut stream = b"GET / HTTP/1.1\r\nUpgrade: websocket\r\n\r\n".to_vec();
        stream.extend(frame(b"{\"a\":", true, 0x1, false));
        stream.extend(frame(b"", true, 0x9, true));
        stream.extend(frame(b"1}", true, 0x0, true));
        stream.extend(frame(b"second", true, 0x1, true));
        let mut decoder = Decoder::new(true);
        let mut messages = Vec::new();
        for chunk in stream.chunks(7) {
            messages.extend(decoder.feed(chunk, false));
        }
        assert_eq!(messages, vec![b"{\"a\":1}".to_vec(), b"second".to_vec()]);
    }

    #[test]
    fn binary_messages_are_consumed_but_not_returned() {
        let mut decoder = Decoder::new(false);
        let mut stream = frame(b"\x00\x01", false, 0x2, true);
        stream.extend(frame(b"text", false, 0x1, true));
        assert_eq!(decoder.feed(&stream, false), vec![b"text".to_vec()]);
    }

    #[test]
    fn wrong_mask_direction_desynchronizes_until_a_clean_chunk() {
        let mut decoder = Decoder::new(true);
        assert!(decoder.feed(&frame(b"x", false, 0x1, true), false).is_empty());
        let mut ragged = frame(b"kept", true, 0x1, true);
        ragged.extend(&frame(b"cut off", true, 0x1, true)[..5]);
        assert_eq!(decoder.feed(&ragged, false), vec![b"kept".to_vec()]);
        assert!(decoder.desynchronized);
        assert_eq!(decoder.feed(&frame(b"clean", true, 0x1, true), false), vec![b"clean".to_vec()]);
        assert!(!decoder.desynchronized);
    }

    #[test]
    fn truncated_chunk_drops_state_and_waits_for_a_clean_chunk() {
        let mut decoder = Decoder::new(false);
        let big = frame(&vec![b'x'; 9000], false, 0x1, true);
        assert!(decoder.feed(&big[..4096], true).is_empty());
        assert!(decoder.feed(&big[4096..8192], true).is_empty());
        assert_eq!(decoder.feed(&frame(b"next", false, 0x1, true), false), vec![b"next".to_vec()]);
    }

    #[test]
    fn oversized_frame_is_skipped_without_being_collected() {
        let mut decoder = Decoder::new(false);
        let payload = vec![b'y'; MAX_MESSAGE_BYTES + 10];
        let mut stream = frame(&payload, false, 0x1, true);
        stream.extend(frame(b"after", false, 0x1, true));
        let mut messages = Vec::new();
        for chunk in stream.chunks(4096) {
            messages.extend(decoder.feed(chunk, false));
            assert!(decoder.buffer.len() <= 4096 + 14);
        }
        assert_eq!(messages, vec![b"after".to_vec()]);
    }

    #[test]
    fn fragments_beyond_the_limit_are_dropped() {
        let mut decoder = Decoder::new(false);
        let half = vec![b'z'; MAX_MESSAGE_BYTES / 2 + 1];
        let mut stream = frame(&half, false, 0x1, false);
        stream.extend(frame(&half, false, 0x0, true));
        stream.extend(frame(b"ok", false, 0x1, true));
        assert_eq!(decoder.feed(&stream, false), vec![b"ok".to_vec()]);
    }

    #[test]
    fn prefix_of_a_truncated_masked_frame_is_unmasked() {
        let mut payload = b"{\"method\":\"turn/start\",\"input\":\"".to_vec();
        payload.extend(vec![b'x'; 8000]);
        let chunk = &frame(&payload, true, 0x1, true)[..4096];
        let prefix = payload_prefix(chunk, true).unwrap();
        assert_eq!(prefix.len(), 4096 - 8);
        assert!(prefix.starts_with(b"{\"method\":\"turn/start\""));
        assert_eq!(payload_prefix(chunk, false), None);
        assert_eq!(payload_prefix(b"HTTP/1.1 101 Switching", false), None);
        assert_eq!(payload_prefix(&frame(b"x", false, 0x9, true), false), None);
    }
}
