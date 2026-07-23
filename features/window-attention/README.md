# window-attention

Highlights the owning tmux window when Codex or Claude stops or asks for
attention. A window with `window_active_clients > 0` is not highlighted.
Feature-owned tmux hooks clear the style when the user focuses or attaches to
the window.
