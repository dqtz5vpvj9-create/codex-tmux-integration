# Managed by codex-tmux-integration: ssh-autoattach

__tmux_ssh_socket_path() {
  emulate -L zsh
  local server="${1:-default}"
  local base="${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)"

  if [[ "$server" == /* ]]; then
    print -r -- "$server"
  else
    [[ -z "$server" ]] && server=default
    print -r -- "$base/$server"
  fi
}

__tmux_ssh_valid_name() {
  emulate -L zsh
  local kind="$1" value="$2"
  if (( ${#value} > 64 )) || [[ ! "$value" =~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$' ]]; then
    print -u2 -r -- "tmux: invalid ${kind} name '$value' (use 1-64 letters, digits, '.', '_' or '-')"
    return 1
  fi
}

__tmux_ssh_attach_target() {
  emulate -L zsh
  local target="${1:-}"
  local server="${TMUX_SSH_SOCKET:-${TMUX_SSH_SERVER:-default}}"
  local session="${TMUX_SSH_SESSION:-agent}"
  local socket

  if [[ -n "$target" ]]; then
    if [[ "$target" == *:* ]]; then
      server="${target%%:*}"
      session="${target#*:}"
      [[ -z "$server" ]] && server=default
      [[ -z "$session" ]] && session="${TMUX_SSH_SESSION:-agent}"
    else
      session="$target"
    fi
  fi

  if [[ "$server" != /* ]]; then
    __tmux_ssh_valid_name server "$server" || return 2
  fi
  __tmux_ssh_valid_name session "$session" || return 2
  socket="$(__tmux_ssh_socket_path "$server")"
  print -r -- "tmux: attaching ${server}:${session}"
  exec tmux -S "$socket" new-session -A -s "$session"
}

__tmux_ssh_new_session() {
  emulate -L zsh
  local server session socket

  printf 'tmux server/socket [default]: '
  read -r server || return 1
  [[ -z "$server" ]] && server=default
  __tmux_ssh_valid_name server "$server" || return 2

  printf 'tmux session [agent]: '
  read -r session || return 1
  [[ -z "$session" ]] && session=agent
  __tmux_ssh_valid_name session "$session" || return 2

  socket="$(__tmux_ssh_socket_path "$server")"
  exec tmux -S "$socket" new-session -A -s "$session"
}

__tmux_ssh_menu() {
  emulate -L zsh
  local base="${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)"
  local socket session windows attached label pick i
  local -a labels action_sockets action_sessions menu_items

  if [[ -d "$base" ]]; then
    for socket in "$base"/*(N); do
      [[ -S "$socket" ]] || continue
      while IFS='|' read -r session windows attached; do
        [[ -n "$session" ]] || continue
        label="$(basename "$socket"):${session}  ${windows}w ${attached}c"
        labels+=("$label")
        action_sockets+=("$socket")
        action_sessions+=("$session")
      done < <(tmux -S "$socket" list-sessions -F '#{session_name}|#{session_windows}|#{session_attached}' 2>/dev/null)
    done
  fi

  if (( ${#labels[@]} == 0 )); then
    print -r -- 'tmux: no live sessions found; creating default:agent'
    exec tmux -S "$(__tmux_ssh_socket_path default)" new-session -A -s agent
  fi

  if command -v whiptail >/dev/null 2>&1 && [[ "${TERM:-}" != "dumb" ]]; then
    for (( i = 1; i <= ${#labels[@]}; i++ )); do
      menu_items+=("$i" "${labels[$i]}")
    done
    menu_items+=("n" "new session..." "s" "plain shell")
    pick="$(whiptail --title 'tmux SSH attach' --menu 'Select target for this SSH login' 22 78 12 "${menu_items[@]}" 3>&1 1>&2 2>&3)" || return 0
  else
    print -r -- 'tmux sessions:'
    for (( i = 1; i <= ${#labels[@]}; i++ )); do
      print -r -- "  ${i}) ${labels[$i]}"
    done
    print -r -- '  n) new session...'
    print -r -- '  s) plain shell'
    printf 'attach> '
    read -r pick || return 0
  fi

  case "$pick" in
    <->)
      if (( pick >= 1 && pick <= ${#labels[@]} )); then
        exec tmux -S "${action_sockets[$pick]}" attach-session -t "${action_sessions[$pick]}"
      fi
      ;;
    n|N)
      __tmux_ssh_new_session
      ;;
    s|S|'')
      return 0
      ;;
  esac
}

__tmux_ssh_autoattach() {
  emulate -L zsh

  case "${TMUX_SSH_AUTO:-1}" in
    0|false|False|FALSE|no|No|NO|off|Off|OFF)
      return 0
      ;;
  esac

  case "${TMUX_SSH_TARGET:-}" in
    shell|none|skip)
      return 0
      ;;
  esac

  if [[ -n "$TMUX_SSH_TARGET" || -n "$TMUX_SSH_SERVER" || -n "$TMUX_SSH_SOCKET" || -n "$TMUX_SSH_SESSION" ]]; then
    __tmux_ssh_attach_target "$TMUX_SSH_TARGET"
  else
    __tmux_ssh_menu
  fi
}

if [[ -o interactive ]] &&
   [[ -o login ]] &&
   [[ -n "$SSH_CONNECTION" ]] &&
   [[ -t 0 ]] &&
   [[ -t 1 ]] &&
   [[ -z "$TMUX" ]] &&
   [[ "$TERM_PROGRAM" != "vscode" ]] &&
   [[ -z "$VSCODE_IPC_HOOK_CLI" ]] &&
   command -v tmux >/dev/null 2>&1; then
  __tmux_ssh_autoattach
fi
