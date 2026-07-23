# title-sync

Consumes Codex lifecycle hook JSON, resolves the owning tmux socket and pane,
caches a title per `socket + pane`, and renames the corresponding window.
Focus/select tmux hooks restore the cached title without scanning all
processes. Process scanning remains a compatibility fallback.
