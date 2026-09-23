//! The privacy filter: what may leave the helper.
//!
//! A message is reduced to the method, the request id, the thread id and, for
//! the two naming methods only, the thread name. A response that names no
//! thread is reduced to its id. Everything else, including every notification
//! that carries conversation content, is dropped here.

use crate::json::{self, Value};

const SESSION_METHODS: [&str; 12] = [
    "thread/start",
    "thread/resume",
    "thread/fork",
    "thread/name/set",
    "thread/unsubscribe",
    "turn/start",
    "turn/steer",
    "thread/shellCommand",
    "thread/name/updated",
    "thread/started",
    "thread/tokenUsage/updated",
    "turn/started",
];
const NAMING_METHODS: [&str; 2] = ["thread/name/set", "thread/name/updated"];

/// The first of `paths` that leads to a non-empty string.
fn nested_string<'a>(value: Option<&'a Value>, paths: &[&[&str]]) -> Option<&'a str> {
    paths.iter().find_map(|path| {
        let mut current = value?;
        for key in *path {
            current = current.get(key)?;
        }
        current.as_str().filter(|text| !text.is_empty())
    })
}

fn method_message(method: &str, id: Option<Value>, thread: Option<&str>, name: Option<String>) -> Value {
    let mut params = Value::Object(Vec::new());
    if let Some(thread) = thread {
        params.insert("threadId", Value::string(thread));
    }
    if let Some(name) = name {
        params.insert("name", Value::String(name));
    }
    let mut message = Value::object([("method", Value::string(method)), ("params", params)]);
    if let Some(id) = id {
        message.insert("id", id);
    }
    message
}

/// Reduce one complete message. Returns what may be published and the thread
/// this connection is now known to be on.
pub fn message(message: &Value, last_thread: Option<&str>) -> (Option<Value>, Option<String>) {
    let keep = || last_thread.map(str::to_string);
    let params = message.get("params").filter(|params| params.is_object());
    let thread = nested_string(params, &[&["threadId"], &["thread_id"], &["thread", "id"]]);

    if let Some(method) = message.get("method").and_then(Value::as_str) {
        if SESSION_METHODS.contains(&method) {
            // thread/name/set sends `name`; thread/name/updated answers with `threadName`.
            let name = nested_string(params, &[&["name"], &["threadName"], &["thread", "name"]])
                .filter(|_| NAMING_METHODS.contains(&method))
                .map(str::to_string);
            let sanitized = method_message(method, message.get("id").cloned(), thread, name);
            return (Some(sanitized), thread.map(str::to_string).or_else(keep));
        }
    }

    let Some(id) = message.get("id") else {
        return (None, keep());
    };
    if message.get("result").is_none() && message.get("error").is_none() {
        return (None, keep());
    }
    let result = message.get("result");
    if let Some(result_thread) = nested_string(result, &[&["thread", "id"], &["threadId"]]) {
        let mut described = Value::object([("id", Value::string(result_thread))]);
        if let Some(name) = nested_string(result, &[&["thread", "name"]]) {
            described.insert("name", Value::string(name));
        }
        // The TUI runs helper work such as title generation on an ephemeral
        // thread over the same connection. It must not replace the thread
        // that the pane is showing.
        let ephemeral = result
            .and_then(|result| result.get("thread"))
            .filter(|thread| thread.is_object())
            .and_then(|thread| thread.get("ephemeral"))
            == Some(&Value::Bool(true));
        if ephemeral {
            described.insert("ephemeral", Value::Bool(true));
        }
        let sanitized = Value::object([
            ("id", id.clone()),
            ("result", Value::object([("thread", described)])),
        ]);
        let latest = if ephemeral { keep() } else { Some(result_thread.to_string()) };
        return (Some(sanitized), latest);
    }
    if message.get("error").is_some() {
        let sanitized = Value::object([("id", id.clone()), ("error", Value::Object(Vec::new()))]);
        return (Some(sanitized), keep());
    }
    // Most requests are answered without naming a thread; a rename and an
    // unsubscribe among them. The controller needs to know those went through.
    let sanitized = Value::object([("id", id.clone()), ("result", Value::Object(Vec::new()))]);
    (Some(sanitized), keep())
}

/// Reduce the readable beginning of a message whose end was not captured.
pub fn prefix(payload: &[u8], last_thread: Option<&str>) -> (Option<Value>, Option<String>) {
    let keep = || last_thread.map(str::to_string);
    let head = |limit: usize| &payload[..payload.len().min(limit)];

    let Some(method) = search(head(1024), b"\"method\"", method_value) else {
        return (None, keep());
    };
    let Ok(method) = std::str::from_utf8(method) else {
        return (None, keep());
    };
    if !SESSION_METHODS.contains(&method) {
        return (None, keep());
    }
    let thread = search_either(head(2048), b"\"threadId\"", b"\"thread_id\"", identifier_value)
        .or_else(|| search(head(2048), b"\"thread\"", thread_object_id))
        .and_then(|thread| std::str::from_utf8(thread).ok());
    let id = search(head(512), b"\"id\"", id_value).and_then(json::parse);
    let name = NAMING_METHODS
        .contains(&method)
        .then(|| search_either(head(2048), b"\"name\"", b"\"threadName\"", name_value))
        .flatten()
        .and_then(json::parse_string_body);
    let sanitized = method_message(method, id, thread, name);
    (Some(sanitized), thread.map(str::to_string).or_else(keep))
}

// The scanners below read a byte slice the way the controller's original
// regular expressions did: a key is tried at every place it occurs, from the
// left, and the first place where the whole pattern fits is the match.

type Matcher = fn(&[u8]) -> Option<&[u8]>;

fn search<'a>(haystack: &'a [u8], key: &[u8], matcher: Matcher) -> Option<&'a [u8]> {
    occurrences(haystack, key).find_map(|at| matcher(&haystack[at + key.len()..]))
}

fn search_either<'a>(haystack: &'a [u8], first: &[u8], second: &[u8], matcher: Matcher) -> Option<&'a [u8]> {
    let mut places: Vec<(usize, usize)> = occurrences(haystack, first)
        .map(|at| (at, first.len()))
        .chain(occurrences(haystack, second).map(|at| (at, second.len())))
        .collect();
    places.sort_unstable();
    places.into_iter().find_map(|(at, length)| matcher(&haystack[at + length..]))
}

fn occurrences<'a>(haystack: &'a [u8], key: &'a [u8]) -> impl Iterator<Item = usize> + 'a {
    (0..haystack.len().saturating_sub(key.len() - 1)).filter(move |&at| haystack[at..].starts_with(key))
}

fn is_space(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c)
}

/// `\s*:\s*`
fn after_colon(rest: &[u8]) -> Option<&[u8]> {
    let rest = &rest[rest.iter().take_while(|&&byte| is_space(byte)).count()..];
    let rest = rest.strip_prefix(b":")?;
    Some(&rest[rest.iter().take_while(|&&byte| is_space(byte)).count()..])
}

/// `\s*:\s*"([^"\\]+)"`
fn method_value(rest: &[u8]) -> Option<&[u8]> {
    let rest = after_colon(rest)?.strip_prefix(b"\"")?;
    let length = rest.iter().take_while(|&&byte| byte != b'"' && byte != b'\\').count();
    (length > 0 && rest.get(length) == Some(&b'"')).then(|| &rest[..length])
}

/// `\s*:\s*"([\w-]{8,128})"`
fn identifier_value(rest: &[u8]) -> Option<&[u8]> {
    let rest = after_colon(rest)?.strip_prefix(b"\"")?;
    let length = rest
        .iter()
        .take_while(|&&byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
        .count();
    ((8..=128).contains(&length) && rest.get(length) == Some(&b'"')).then(|| &rest[..length])
}

/// `\s*:\s*\{[^{}]{0,512}?"id"\s*:\s*"([\w-]{8,128})"`
fn thread_object_id(rest: &[u8]) -> Option<&[u8]> {
    let body = after_colon(rest)?.strip_prefix(b"{")?;
    for skipped in 0..=512.min(body.len()) {
        if skipped > 0 && matches!(body[skipped - 1], b'{' | b'}') {
            return None;
        }
        if let Some(id) = body[skipped..].strip_prefix(b"\"id\"").and_then(identifier_value) {
            return Some(id);
        }
    }
    None
}

/// The length of `(?:[^"\\]|\\.)` repeated up to `limit` times, if a quote follows.
fn string_body(rest: &[u8], limit: usize) -> Option<usize> {
    let mut at = 0;
    for _ in 0..=limit {
        match *rest.get(at)? {
            b'"' => return Some(at),
            b'\\' => {
                if matches!(rest.get(at + 1), None | Some(b'\n')) {
                    return None;
                }
                at += 2;
            }
            _ => at += 1,
        }
    }
    None
}

/// `\s*:\s*"((?:[^"\\]|\\.){0,512})"`
fn name_value(rest: &[u8]) -> Option<&[u8]> {
    let rest = after_colon(rest)?.strip_prefix(b"\"")?;
    string_body(rest, 512).map(|length| &rest[..length])
}

/// `\s*:\s*("(?:[^"\\]|\\.)*"|-?\d+)`, returned as the JSON token it matched.
fn id_value(rest: &[u8]) -> Option<&[u8]> {
    let rest = after_colon(rest)?;
    if let Some(inner) = rest.strip_prefix(b"\"") {
        return string_body(inner, usize::MAX - 1).map(|length| &rest[..length + 2]);
    }
    let sign = usize::from(rest.first() == Some(&b'-'));
    let digits = rest[sign..].iter().take_while(|byte| byte.is_ascii_digit()).count();
    (digits > 0).then(|| &rest[..sign + digits])
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::websocket;

    fn reduce(text: &str, last: Option<&str>) -> (Option<String>, Option<String>) {
        let (sanitized, latest) = message(&json::parse(text.as_bytes()).unwrap(), last);
        (sanitized.map(|value| value.to_json()), latest)
    }

    #[test]
    fn content_notification_is_dropped_entirely() {
        let delta = r#"{"method":"item/agentMessage/delta","params":{"threadId":"thread-12345678","delta":"private"}}"#;
        assert_eq!(reduce(delta, None), (None, None));
        assert_eq!(reduce(delta, Some("thread-12345678")), (None, Some("thread-12345678".into())));
    }

    #[test]
    fn session_method_keeps_only_thread_and_id() {
        let usage = r#"{"method":"thread/tokenUsage/updated","params":{"threadId":"thread-12345678","tokenUsage":{"secret":1}}}"#;
        assert_eq!(
            reduce(usage, None),
            (
                Some(r#"{"method":"thread/tokenUsage/updated","params":{"threadId":"thread-12345678"}}"#.into()),
                Some("thread-12345678".into())
            )
        );
        let start = r#"{"id":"r-1","method":"turn/start","params":{"thread":{"id":"t-1"},"name":"not a naming method","input":"secret"}}"#;
        assert_eq!(
            reduce(start, Some("old")),
            (Some(r#"{"method":"turn/start","params":{"threadId":"t-1"},"id":"r-1"}"#.into()), Some("t-1".into()))
        );
    }

    #[test]
    fn only_naming_methods_publish_a_name() {
        let set = r#"{"method":"thread/name/set","id":4,"params":{"threadId":"t-1","name":"新名字"}}"#;
        assert_eq!(
            reduce(set, None).0.unwrap(),
            r#"{"method":"thread/name/set","params":{"threadId":"t-1","name":"新名字"},"id":4}"#
        );
        let empty = r#"{"method":"thread/name/updated","params":{"threadId":"t-1","name":""}}"#;
        assert_eq!(reduce(empty, None).0.unwrap(), r#"{"method":"thread/name/updated","params":{"threadId":"t-1"}}"#);
    }

    #[test]
    fn responses_publish_the_thread_and_nothing_else() {
        let result = r#"{"id":2,"result":{"thread":{"id":"t-9","name":"n","preview":"secret"},"model":"m"}}"#;
        assert_eq!(
            reduce(result, Some("t-1")),
            (Some(r#"{"id":2,"result":{"thread":{"id":"t-9","name":"n"}}}"#.into()), Some("t-9".into()))
        );
        let error = r#"{"id":3,"error":{"code":-1,"message":"secret path"}}"#;
        assert_eq!(reduce(error, Some("t-1")), (Some(r#"{"id":3,"error":{}}"#.into()), Some("t-1".into())));
        assert_eq!(
            reduce(r#"{"id":5,"result":{"ok":true}}"#, Some("t-1")),
            (Some(r#"{"id":5,"result":{}}"#.into()), Some("t-1".into()))
        );
        assert_eq!(reduce(r#"{"result":{"thread":{"id":"t-9"}}}"#, None), (None, None));
    }

    #[test]
    fn unsubscribe_and_its_answer_are_both_published() {
        let request = r#"{"method":"thread/unsubscribe","id":7,"params":{"threadId":"t-old"}}"#;
        assert_eq!(
            reduce(request, Some("t-new")),
            (
                Some(r#"{"method":"thread/unsubscribe","params":{"threadId":"t-old"},"id":7}"#.into()),
                Some("t-old".into())
            )
        );
        let response = r#"{"id":7,"result":{"status":"unsubscribed"}}"#;
        assert_eq!(reduce(response, Some("t-new")), (Some(r#"{"id":7,"result":{}}"#.into()), Some("t-new".into())));
    }

    #[test]
    fn a_rename_is_published_with_its_confirmation() {
        // thread/name/set is answered with {} and then announced with threadName.
        assert_eq!(reduce(r#"{"id":4,"result":{}}"#, Some("t-1")).0, Some(r#"{"id":4,"result":{}}"#.into()));
        let announced = r#"{"method":"thread/name/updated","params":{"threadId":"t-1","threadName":"CP-SAT"}}"#;
        assert_eq!(
            reduce(announced, Some("t-1")).0,
            Some(r#"{"method":"thread/name/updated","params":{"threadId":"t-1","name":"CP-SAT"}}"#.into())
        );
        let id = "01a00000-0000-7000-8000-000000000004";
        let truncated = format!(r#"{{"method":"thread/name/updated","params":{{"threadId":"{id}","threadName":"CP-SAT"}}"#);
        assert_eq!(
            prefix(truncated.as_bytes(), None).0.map(|value| value.to_json()),
            Some(format!(r#"{{"method":"thread/name/updated","params":{{"threadId":"{id}","name":"CP-SAT"}}}}"#))
        );
    }

    #[test]
    fn ephemeral_side_thread_does_not_replace_the_pane_thread() {
        let side = r#"{"id":2,"result":{"thread":{"id":"thread-title","ephemeral":true,"preview":"x"}}}"#;
        assert_eq!(
            reduce(side, Some("thread-main")),
            (
                Some(r#"{"id":2,"result":{"thread":{"id":"thread-title","ephemeral":true}}}"#.into()),
                Some("thread-main".into())
            )
        );
    }

    #[test]
    fn partial_masked_frame_yields_the_thread_before_large_content() {
        let mut payload = br#"{"method":"turn/start","id":9,"params":{"threadId":"thread-12345678","input":""#.to_vec();
        payload.extend(vec![b'x'; 8000]);
        let chunk = &websocket::frame(&payload, true, 0x1, true)[..4096];
        let (sanitized, latest) = prefix(&websocket::payload_prefix(chunk, true).unwrap(), None);
        assert_eq!(latest.as_deref(), Some("thread-12345678"));
        assert_eq!(
            sanitized.unwrap().to_json(),
            r#"{"method":"turn/start","params":{"threadId":"thread-12345678"},"id":9}"#
        );
    }

    #[test]
    fn prefix_scanners_follow_the_original_patterns() {
        let reduce = |text: &str| prefix(text.as_bytes(), Some("last")).0.map(|value| value.to_json());
        assert_eq!(reduce(r#"{"method":"item/delta","params":{"threadId":"thread-12345678"}}"#), None);
        assert_eq!(reduce(r#"{"params":{"threadId":"thread-12345678"}}"#), None);
        assert_eq!(
            reduce(r#"{"method" : "thread/name/updated", "params":{"thread":{"x":1,"id":"thread-abcdefgh"},"name":"a\"b\u4e2d"}}"#)
                .unwrap(),
            r#"{"method":"thread/name/updated","params":{"threadId":"thread-abcdefgh","name":"a\"b中"},"id":"thread-abcdefgh"}"#
        );
        // The request id is whichever "id" comes first, here the thread's own.
        // Below: too short for a thread id, a nested object before the id, a name on a non-naming method.
        assert_eq!(
            reduce(r#"{"method":"turn/start","id":"s-1","params":{"threadId":"short","thread":{"a":{},"id":"thread-abcdefgh"},"name":"n"}}"#)
                .unwrap(),
            r#"{"method":"turn/start","params":{},"id":"s-1"}"#
        );
        // An id that is not valid JSON is left out; the first id that fits is taken.
        assert_eq!(reduce(r#"{"id":007,"method":"turn/start"}"#).unwrap(), r#"{"method":"turn/start","params":{}}"#);
        assert_eq!(
            reduce(r#"{"id":null,"method":"turn/start","x":{"id":-12.5}}"#).unwrap(),
            r#"{"method":"turn/start","params":{},"id":-12}"#
        );
    }
}
