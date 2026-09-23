//! A small JSON reader and writer.
//!
//! It accepts what Python's `json.loads` accepts, because the controller that
//! consumes the helper's output was written against that parser: the last of
//! two duplicate keys wins, raw control characters inside a string are an
//! error, and `NaN` and the infinities are numbers. A number keeps its source
//! text, so a request id leaves the helper exactly as it arrived.

use std::fmt::Write;

const MAX_DEPTH: usize = 256;

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Number(String),
    String(String),
    Array(Vec<Value>),
    Object(Vec<(String, Value)>),
}

impl Value {
    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Object(members) => members
                .iter()
                .rev()
                .find(|(name, _)| name == key)
                .map(|(_, value)| value),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::String(text) => Some(text),
            _ => None,
        }
    }

    pub fn is_object(&self) -> bool {
        matches!(self, Value::Object(_))
    }

    pub fn integer(value: impl Into<i128>) -> Value {
        Value::Number(value.into().to_string())
    }

    pub fn string(text: impl Into<String>) -> Value {
        Value::String(text.into())
    }

    pub fn object<const N: usize>(members: [(&str, Value); N]) -> Value {
        Value::Object(
            members
                .into_iter()
                .map(|(name, value)| (name.to_string(), value))
                .collect(),
        )
    }

    pub fn insert(&mut self, key: &str, value: Value) {
        if let Value::Object(members) = self {
            members.push((key.to_string(), value));
        }
    }

    pub fn to_json(&self) -> String {
        let mut out = String::new();
        self.write(&mut out);
        out
    }

    fn write(&self, out: &mut String) {
        match self {
            Value::Null => out.push_str("null"),
            Value::Bool(true) => out.push_str("true"),
            Value::Bool(false) => out.push_str("false"),
            Value::Number(text) => out.push_str(text),
            Value::String(text) => write_string(text, out),
            Value::Array(items) => {
                out.push('[');
                for (index, item) in items.iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    item.write(out);
                }
                out.push(']');
            }
            Value::Object(members) => {
                out.push('{');
                for (index, (name, value)) in members.iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    write_string(name, out);
                    out.push(':');
                    value.write(out);
                }
                out.push('}');
            }
        }
    }
}

fn write_string(text: &str, out: &mut String) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            control if (control as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", control as u32);
            }
            other => out.push(other),
        }
    }
    out.push('"');
}

/// Parse one complete JSON document.
pub fn parse(bytes: &[u8]) -> Option<Value> {
    let text = std::str::from_utf8(bytes).ok()?;
    let mut reader = Reader { bytes: text.as_bytes(), position: 0 };
    reader.skip_whitespace();
    let value = reader.value(0)?;
    reader.skip_whitespace();
    (reader.position == reader.bytes.len()).then_some(value)
}

/// Decode the inside of a string literal, as `json.loads('"' + inner + '"')` would.
pub fn parse_string_body(inner: &[u8]) -> Option<String> {
    let mut quoted = Vec::with_capacity(inner.len() + 2);
    quoted.push(b'"');
    quoted.extend_from_slice(inner);
    quoted.push(b'"');
    match parse(&quoted)? {
        Value::String(text) => Some(text),
        _ => None,
    }
}

struct Reader<'a> {
    bytes: &'a [u8],
    position: usize,
}

impl Reader<'_> {
    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.position).copied()
    }

    fn skip_whitespace(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.position += 1;
        }
    }

    fn literal(&mut self, word: &str) -> bool {
        let matched = self.bytes[self.position..].starts_with(word.as_bytes());
        if matched {
            self.position += word.len();
        }
        matched
    }

    fn value(&mut self, depth: usize) -> Option<Value> {
        if depth > MAX_DEPTH {
            return None;
        }
        match self.peek()? {
            b'{' => self.object(depth),
            b'[' => self.array(depth),
            b'"' => self.string().map(Value::String),
            b't' => self.literal("true").then_some(Value::Bool(true)),
            b'f' => self.literal("false").then_some(Value::Bool(false)),
            b'n' => self.literal("null").then_some(Value::Null),
            _ => self.number(),
        }
    }

    fn object(&mut self, depth: usize) -> Option<Value> {
        self.position += 1;
        let mut members = Vec::new();
        self.skip_whitespace();
        if self.peek()? == b'}' {
            self.position += 1;
            return Some(Value::Object(members));
        }
        loop {
            self.skip_whitespace();
            if self.peek()? != b'"' {
                return None;
            }
            let name = self.string()?;
            self.skip_whitespace();
            if self.peek()? != b':' {
                return None;
            }
            self.position += 1;
            self.skip_whitespace();
            let value = self.value(depth + 1)?;
            members.push((name, value));
            self.skip_whitespace();
            match self.peek()? {
                b',' => self.position += 1,
                b'}' => {
                    self.position += 1;
                    return Some(Value::Object(members));
                }
                _ => return None,
            }
        }
    }

    fn array(&mut self, depth: usize) -> Option<Value> {
        self.position += 1;
        let mut items = Vec::new();
        self.skip_whitespace();
        if self.peek()? == b']' {
            self.position += 1;
            return Some(Value::Array(items));
        }
        loop {
            self.skip_whitespace();
            items.push(self.value(depth + 1)?);
            self.skip_whitespace();
            match self.peek()? {
                b',' => self.position += 1,
                b']' => {
                    self.position += 1;
                    return Some(Value::Array(items));
                }
                _ => return None,
            }
        }
    }

    fn string(&mut self) -> Option<String> {
        self.position += 1;
        let mut text = String::new();
        loop {
            let start = self.position;
            while !matches!(self.peek()?, b'"' | b'\\' | 0..=0x1f) {
                self.position += 1;
            }
            // The input is valid UTF-8 and the run ends on an ASCII byte.
            text.push_str(std::str::from_utf8(&self.bytes[start..self.position]).ok()?);
            match self.peek()? {
                b'"' => {
                    self.position += 1;
                    return Some(text);
                }
                b'\\' => {
                    self.position += 1;
                    self.escape(&mut text)?;
                }
                _ => return None,
            }
        }
    }

    fn escape(&mut self, text: &mut String) -> Option<()> {
        let code = self.peek()?;
        self.position += 1;
        match code {
            b'"' => text.push('"'),
            b'\\' => text.push('\\'),
            b'/' => text.push('/'),
            b'b' => text.push('\u{8}'),
            b'f' => text.push('\u{c}'),
            b'n' => text.push('\n'),
            b'r' => text.push('\r'),
            b't' => text.push('\t'),
            b'u' => {
                let first = self.hex4()?;
                let scalar = if (0xd800..0xdc00).contains(&first)
                    && self.bytes[self.position..].starts_with(b"\\u")
                {
                    let checkpoint = self.position;
                    self.position += 2;
                    match self.hex4()? {
                        second @ 0xdc00..=0xdfff => {
                            0x10000 + ((first - 0xd800) << 10) + (second - 0xdc00)
                        }
                        _ => {
                            self.position = checkpoint;
                            first
                        }
                    }
                } else {
                    first
                };
                // A lone surrogate has no Rust representation.
                text.push(char::from_u32(scalar).unwrap_or('\u{fffd}'));
            }
            _ => return None,
        }
        Some(())
    }

    fn hex4(&mut self) -> Option<u32> {
        let digits = self.bytes.get(self.position..self.position + 4)?;
        if !digits.iter().all(u8::is_ascii_hexdigit) {
            return None;
        }
        self.position += 4;
        u32::from_str_radix(std::str::from_utf8(digits).ok()?, 16).ok()
    }

    fn number(&mut self) -> Option<Value> {
        for word in ["NaN", "Infinity", "-Infinity"] {
            if self.literal(word) {
                return Some(Value::Number(word.to_string()));
            }
        }
        let start = self.position;
        if self.peek() == Some(b'-') {
            self.position += 1;
        }
        match self.peek()? {
            b'0' => self.position += 1,
            b'1'..=b'9' => self.digits(),
            _ => return None,
        }
        if self.peek() == Some(b'.') {
            self.position += 1;
            if !self.peek()?.is_ascii_digit() {
                return None;
            }
            self.digits();
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.position += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.position += 1;
            }
            if !self.peek()?.is_ascii_digit() {
                return None;
            }
            self.digits();
        }
        let token = std::str::from_utf8(&self.bytes[start..self.position]).ok()?;
        Some(Value::Number(token.to_string()))
    }

    fn digits(&mut self) {
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.position += 1;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_nested_documents_and_keeps_number_text() {
        let value = parse(br#" {"id": 12, "params": {"threadId": "t", "n": [1.50, -2e3, true, null]}} "#)
            .unwrap();
        assert_eq!(value.get("id"), Some(&Value::Number("12".into())));
        assert_eq!(value.get("params").unwrap().get("threadId").unwrap().as_str(), Some("t"));
        assert_eq!(
            value.to_json(),
            r#"{"id":12,"params":{"threadId":"t","n":[1.50,-2e3,true,null]}}"#
        );
    }

    #[test]
    fn last_duplicate_key_wins() {
        let value = parse(br#"{"a":1,"a":2}"#).unwrap();
        assert_eq!(value.get("a"), Some(&Value::Number("2".into())));
    }

    #[test]
    fn decodes_escapes_and_surrogate_pairs() {
        let value = parse(br#""a\n\"\u4e2d\ud83d\ude00\/""#).unwrap();
        assert_eq!(value.as_str(), Some("a\n\"中😀/"));
        assert_eq!(parse(br#""\ud83d""#).unwrap().as_str(), Some("\u{fffd}"));
        assert_eq!(parse_string_body(br#"x\u0041"#).as_deref(), Some("xA"));
    }

    #[test]
    fn rejects_what_python_rejects() {
        for bad in [
            &b"{\"a\":1,}"[..],
            b"[1 2]",
            b"\"raw\nnewline\"",
            b"\"bad\\x\"",
            b"01",
            b"1.",
            b"{\"a\":1} x",
            b"\xff",
            b"",
            b"'single'",
        ] {
            assert_eq!(parse(bad), None, "{:?}", String::from_utf8_lossy(bad));
        }
        assert!(parse(&[b'['; 100_000]).is_none());
    }

    #[test]
    fn writes_control_characters_escaped() {
        assert_eq!(Value::string("a\"b\\c\n\u{1}").to_json(), r#""a\"b\\c\n\u0001""#);
        assert_eq!(
            Value::object([("k", Value::integer(7u32)), ("s", Value::string("中"))]).to_json(),
            r#"{"k":7,"s":"中"}"#
        );
    }
}
