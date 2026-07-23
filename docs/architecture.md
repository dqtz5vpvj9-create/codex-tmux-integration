# Architecture

The repository is divided by user-visible behavior, not by file type.

Each `features/<name>` directory may contain:

- a `feature.json` install manifest;
- commands under `bin/` or adapters under `adapters/`;
- Codex and Claude hook fragments under `hooks/`;
- tmux or shell configuration owned by that feature;
- feature-local tests and a README.

Only hook-to-pane resolution is shared because both title synchronization and
window attention require exactly the same routing invariant.

## Routing invariant

The stable identity of a pane is:

```text
tmux socket path + pane id
```

The pane id alone is insufficient because every tmux server has its own pane
id namespace. Hook handlers first use inherited `TMUX` and `TMUX_PANE`; when
Codex omits those values in a hook subprocess, the resolver correlates
`session_id` or `transcript_path` with the live Codex process.

## Configuration ownership

The repository does not own complete user dotfiles. The installer generates
aggregate files and places one marked source block in `.tmux.conf` and
`.zshrc`. JSON hooks are merged by executable identity while all unrelated
fields are preserved.
