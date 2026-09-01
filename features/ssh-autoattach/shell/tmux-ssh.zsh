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

__tmux_ssh_reconnect_id() {
  emulate -L zsh
  local id="${TMUX_SSH_RECONNECT_ID:-}"

  [[ "$id" =~ '^[A-Fa-f0-9]{32}$' ]] || return 1
  print -r -- "${(L)id}"
}

__tmux_ssh_reconnect_dir() {
  emulate -L zsh
  local root

  if [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
    root="$XDG_RUNTIME_DIR/codex-tmux-integration"
  else
    root="${TMPDIR:-/tmp}/codex-tmux-integration-$(id -u)"
  fi
  print -r -- "$root/ssh-reconnect"
}

__tmux_ssh_remember_target() {
  emulate -L zsh
  local socket="$1" session="$2"
  local id dir file tmp

  id="$(__tmux_ssh_reconnect_id)" || return 0
  [[ "$socket" == /* ]] || return 1
  [[ "$socket" != *$'\n'* ]] || return 1
  __tmux_ssh_valid_name session "$session" || return 1

  dir="$(__tmux_ssh_reconnect_dir)"
  file="$dir/$id"
  tmp="$dir/.$id.$$.${RANDOM:-0}"

  umask 077
  command mkdir -p -- "$dir" || return 1
  command chmod 700 -- "$dir" || return 1
  {
    print -r -- "$socket"
    print -r -- "$session"
  } >| "$tmp" || {
    command rm -f -- "$tmp"
    return 1
  }
  command chmod 600 -- "$tmp" || {
    command rm -f -- "$tmp"
    return 1
  }
  command mv -f -- "$tmp" "$file"
}

__tmux_ssh_try_reconnect() {
  emulate -L zsh
  local id dir file socket session extra

  id="$(__tmux_ssh_reconnect_id)" || return 1
  dir="$(__tmux_ssh_reconnect_dir)"
  file="$dir/$id"
  [[ -f "$file" ]] || return 1

  socket=''
  session=''
  extra=''
  {
    IFS= read -r socket
    IFS= read -r session
    IFS= read -r extra
  } < "$file"

  if [[ "$socket" != /* ]] ||
     [[ "$socket" == *$'\n'* ]] ||
     [[ -n "$extra" ]] ||
     ! __tmux_ssh_valid_name session "$session"; then
    command rm -f -- "$file"
    return 1
  fi

  if ! tmux -S "$socket" has-session -t "$session" 2>/dev/null; then
    command rm -f -- "$file"
    return 1
  fi

  print -r -- "tmux: reconnecting ${socket:t}:${session}"
  exec tmux -S "$socket" attach-session -t "$session"
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
  __tmux_ssh_remember_target "$socket" "$session" || return 2
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
  __tmux_ssh_remember_target "$socket" "$session" || return 2
  exec tmux -S "$socket" new-session -A -s "$session"
}


# The picker lists only sessions someone is working in, adapts to a phone
# screen, takes taps, and attaches to the most recently active session after a
# short countdown.  Anything unexpected -- missing binary, no tty, a session
# name the caller would refuse -- falls through to the whiptail menu below, so
# a broken picker can never strand an SSH login.
__tmux_ssh_menu() {
  emulate -L zsh
  local bin out rc socket session

  bin="${TMUX_SSH_PICKER:-$HOME/.local/bin/tmux-ssh-picker}"
  if [[ -x "$bin" && "${TERM:-}" != dumb ]]; then
    out="$("$bin")"
    rc=$?
    if (( rc == 0 )); then
      case "$out" in
        ATTACH$'\t'*)
          socket="${${out#ATTACH$'\t'}%%$'\t'*}"
          session="${out##*$'\t'}"
          if [[ "$socket" == /* ]] && __tmux_ssh_valid_name session "$session"; then
            __tmux_ssh_remember_target "$socket" "$session" || return 2
            print -r -- "tmux: attaching ${socket:t}:${session}"
            exec tmux -S "$socket" attach-session -t "$session"
          fi
          ;;
        NEW)
          __tmux_ssh_new_session
          return
          ;;
        SHELL)
          return 0
          ;;
      esac
    fi
  fi

  __tmux_ssh_menu_fallback
}

__tmux_ssh_menu_fallback() {
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
    socket="$(__tmux_ssh_socket_path default)"
    print -r -- 'tmux: no live sessions found; creating default:agent'
    __tmux_ssh_remember_target "$socket" agent || return 2
    exec tmux -S "$socket" new-session -A -s agent
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
        __tmux_ssh_remember_target \
          "${action_sockets[$pick]}" \
          "${action_sessions[$pick]}" || return 2
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
  elif __tmux_ssh_try_reconnect; then
    return 0
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
