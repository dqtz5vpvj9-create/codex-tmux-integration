# Managed by codex-tmux-integration: title-sync

# A blank interactive agent process does not create a lifecycle session until
# the first prompt. Mark the pane from zsh's process-launch boundary so the
# title changes before the CLI draws its UI.

__codex_tmux_title_command_starts_agent() {
  emulate -L zsh
  local line="${1:-}"
  local word executable launcher_spec
  local -i index=1
  local -a words launchers

  words=("${(@z)line}")
  (( ${#words[@]} > 0 )) || return 1

  while (( index <= ${#words[@]} )); do
    word="${(Q)words[$index]}"
    if [[ "$word" =~ '^[A-Za-z_][A-Za-z0-9_]*=' ]]; then
      (( index++ ))
      continue
    fi
    case "$word" in
      command|exec|noglob|nocorrect)
        (( index++ ))
        ;;
      env)
        (( index++ ))
        while (( index <= ${#words[@]} )); do
          word="${(Q)words[$index]}"
          if [[ "$word" == "--" ]]; then
            (( index++ ))
            break
          elif [[ "$word" =~ '^[A-Za-z_][A-Za-z0-9_]*=' ]]; then
            (( index++ ))
          elif [[ "$word" == "-u" || "$word" == "--unset" ||
                  "$word" == "-C" || "$word" == "--chdir" ||
                  "$word" == "-S" || "$word" == "--split-string" ]]; then
            (( index += 2 ))
          elif [[ "$word" == -* ]]; then
            (( index++ ))
          else
            break
          fi
        done
        ;;
      *)
        break
        ;;
    esac
  done

  (( index <= ${#words[@]} )) || return 1
  executable="${${(Q)words[$index]}:t}"
  launcher_spec="${CODEX_TMUX_TITLE_LAUNCHERS:-codex claude}"
  launchers=("${(@s: :)launcher_spec}")
  [[ "${launchers[(Ie)$executable]}" -gt 0 ]] || return 1
  # Report the matched launcher without a subshell so the common non-agent
  # command path stays fork-free.
  typeset -g __codex_tmux_title_agent="$executable"
}

__codex_tmux_title_socket() {
  emulate -L zsh
  local socket="${TMUX:-}"
  [[ -n "$socket" ]] || return 1
  socket="${socket%,*}"
  socket="${socket%,*}"
  [[ -n "$socket" ]] || return 1
  print -r -- "$socket"
}

__codex_tmux_title_preexec() {
  emulate -L zsh
  [[ -n "${TMUX_PANE:-}" ]] || return 0
  __codex_tmux_title_command_starts_agent "${1:-}" || return 0

  local socket helper="$HOME/.local/bin/codex-tmux-title-sync"
  socket="$(__codex_tmux_title_socket)" || return 0
  [[ -x "$helper" ]] || return 0

  # Record ownership before the helper exposes the provisional title. A very
  # fast Ctrl-C can otherwise interrupt this function after tmux says "codex"
  # but before precmd knows that it must restore the shell title.
  typeset -g __codex_tmux_title_launch_pending=1
  "$helper" \
    --socket "$socket" \
    --pane "$TMUX_PANE" \
    --starting \
    --title "${CODEX_TMUX_START_TITLE:-${__codex_tmux_title_agent:-codex}}" \
    >/dev/null 2>&1
}

__codex_tmux_title_precmd() {
  emulate -L zsh
  (( ${__codex_tmux_title_launch_pending:-0} )) || return 0
  typeset -g __codex_tmux_title_launch_pending=0

  local socket helper="$HOME/.local/bin/codex-tmux-title-sync"
  socket="$(__codex_tmux_title_socket)" || return 0
  [[ -x "$helper" && -n "${TMUX_PANE:-}" ]] || return 0
  "$helper" --socket "$socket" --pane "$TMUX_PANE" --shell-ready >/dev/null 2>&1
}

if [[ -o interactive ]]; then
  autoload -Uz add-zsh-hook
  typeset -g __codex_tmux_title_launch_pending="${__codex_tmux_title_launch_pending:-0}"
  typeset -g __codex_tmux_title_agent="${__codex_tmux_title_agent:-codex}"
  add-zsh-hook -d preexec __codex_tmux_title_preexec 2>/dev/null
  add-zsh-hook -d precmd __codex_tmux_title_precmd 2>/dev/null
  add-zsh-hook preexec __codex_tmux_title_preexec
  add-zsh-hook precmd __codex_tmux_title_precmd
fi
