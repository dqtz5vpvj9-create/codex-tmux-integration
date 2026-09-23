# codex-session-capture

The privileged half of `session-listener`: it loads the BPF program, drops to
the invoking user, and turns the captured app-server bytes into metadata-only
records for the unprivileged controller. `../README.md` describes the feature
and its privilege model; this file is about the crate.

| Module | Responsibility |
| --- | --- |
| `bpf` | Open libbpf by soname, load and attach the object, ring buffers, the watched-descriptor map |
| `discover` | Which app-server descriptors belong to verified Codex clients, and the cheap check for when to look again |
| `procfs` | Process generations, ancestry, socket inodes |
| `websocket` | Incremental frame decoding of one direction of one connection, with bounded buffering |
| `sanitize` | The privacy filter: what of a message may leave the helper |
| `json` | A reader that accepts what Python's `json.loads` accepts, and a writer |
| `helper` | The loop that ties these together |

There are no dependencies on purpose. The binary is started as root, so
everything it runs is in this crate, in std, or in the system's libbpf.

## Records

One JSON object per line on stdout, all with `"schema":"codex.session-capture.v1"`:

| `kind` | Meaning |
| --- | --- |
| `topology` | The verified set of connections changed; the controller rediscovers |
| `protocol` | One reduced JSON-RPC message of one connection and direction |
| `process` | A `claude` or `codex` process started (`exec`) or exited (`exit`) |

Exit status 75 asks the controller to start over because the app-server was
replaced or is gone.

## Tests

```bash
cargo test --locked --offline
```

`tests/reference_corpus.rs` replays cases whose answers came from the Python
helper this crate replaced, produced by a randomized differential run. The two
implementations differed in one deliberate respect: an escaped lone surrogate
becomes U+FFFD here, because a Rust string cannot hold it.
