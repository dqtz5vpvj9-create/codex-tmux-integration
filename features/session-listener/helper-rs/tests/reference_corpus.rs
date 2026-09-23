//! Replay of cases whose answers were produced by the Python helper this crate
//! replaced. Each line of the corpus is a request, a tab, and the expected
//! answer; see `answer` for the four request kinds.
//!
//! Ten answers were changed on purpose since: a response that names no thread
//! used to be dropped and is now published as its id with an empty result, so
//! the controller learns that an unsubscribe or a rename went through. A real
//! session id the case generator had picked up was also replaced by a made-up
//! one, in masked frames too, and every answer checked again afterwards.

use session_capture::json::{self, Value};
use session_capture::{sanitize, websocket};

fn unhex(text: &str) -> Vec<u8> {
    (0..text.len() / 2).map(|at| u8::from_str_radix(&text[2 * at..2 * at + 2], 16).unwrap()).collect()
}

fn hex(bytes: &[u8]) -> Value {
    Value::String(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn optional(text: Option<String>) -> Value {
    text.map_or(Value::Null, Value::String)
}

/// Object members in a fixed order, so that two documents compare by content.
fn canonical(value: Value) -> Value {
    match value {
        Value::Array(items) => Value::Array(items.into_iter().map(canonical).collect()),
        Value::Object(mut members) => {
            members.sort_by(|left, right| left.0.cmp(&right.0));
            Value::Object(members.into_iter().map(|(name, value)| (name, canonical(value))).collect())
        }
        other => other,
    }
}

fn answer(request: &str) -> Value {
    let fields: Vec<&str> = request.split(' ').collect();
    let payload = || unhex(fields.get(2).copied().unwrap_or(""));
    match fields[0] {
        // M: a complete message; P: the beginning of a truncated one.
        "M" | "P" => {
            let last = (fields[1] != "-").then(|| String::from_utf8(unhex(fields[1])).unwrap());
            let (sanitized, latest) = if fields[0] == "P" {
                sanitize::prefix(&payload(), last.as_deref())
            } else {
                match json::parse(&payload()).filter(Value::is_object) {
                    Some(message) => sanitize::message(&message, last.as_deref()),
                    None => (None, Some("<unparsed>".to_string())),
                }
            };
            Value::object([("s", sanitized.unwrap_or(Value::Null)), ("l", optional(latest))])
        }
        // W: the payload prefix of one captured chunk.
        "W" => websocket::payload_prefix(&payload(), fields[1] == "1").map_or(Value::Null, |prefix| hex(&prefix)),
        // D: a stream fed chunk by chunk; a chunk marked `:1` was cut short by the capture.
        "D" => {
            let mut decoder = websocket::Decoder::new(fields[1] == "1");
            let per_chunk = fields[2..].iter().map(|chunk| {
                let (data, truncated) = chunk.split_once(':').unwrap();
                Value::Array(decoder.feed(&unhex(data), truncated == "1").iter().map(|m| hex(m)).collect())
            });
            Value::Array(per_chunk.collect())
        }
        other => panic!("unknown request kind {other}"),
    }
}

#[test]
fn answers_match_the_reference_implementation() {
    let corpus = include_str!("fixtures/reference_corpus.tsv");
    let mut published = 0;
    for (number, line) in corpus.lines().enumerate() {
        let (request, expected) = line.split_once('\t').unwrap();
        let expected = canonical(json::parse(expected.as_bytes()).unwrap());
        let actual = canonical(answer(request));
        published += usize::from(matches!(actual.get("s"), Some(Value::Object(_))));
        assert_eq!(actual, expected, "corpus line {}: {}", number + 1, &request[..request.len().min(200)]);
    }
    assert!(corpus.lines().count() > 300 && published > 100);
}
