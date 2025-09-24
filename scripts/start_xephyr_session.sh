#!/usr/bin/env bash
set -euo pipefail

# Kleinskript zum Starten einer sichtbaren Xephyr-Sitzung mit Openbox,
# Alt+F2 -> rofi (via xbindkeys) und optionalem Autostart einer App.
# Usage:
#   ./scripts/start_xephyr_session.sh [DISPLAY] [RESOLUTION] ["command to start app"]
# Beispiele:
#   ./scripts/start_xephyr_session.sh :2 3840x2160 "vlc --video-on-top"
#   ./scripts/start_xephyr_session.sh :3 1920x1080

DISPLAY_ARG="${1:-:2}"
RES="${2:-3840x2160}"
APP_CMD="${3:-}"
# Optional fourth arg: host-scale (like 0.5) or host window size (e.g. 1920x1080)
HOST_SCALE_OR_RES="${4:-}"

XEPHYR_LOG="/tmp/xephyr-${DISPLAY_ARG#*:}.log"
XBINDS_CFG="/tmp/xephyr-xbindkeys-${DISPLAY_ARG#*:}.conf"

command_exists(){ command -v "$1" >/dev/null 2>&1; }

if ! command_exists Xephyr; then
  echo "Xephyr nicht gefunden. Bitte installiere package 'xserver-xephyr' oder ähnliches." >&2
  exit 2
fi

if ! command_exists xbindkeys || ! command_exists rofi; then
  echo "xbindkeys und/oder rofi fehlen. Installiere sie für Alt+F2-Run-Dialog." >&2
  echo "Debian/Ubuntu: sudo apt install xbindkeys rofi" >&2
fi

echo "Starte Xephyr auf ${DISPLAY_ARG} mit Auflösung ${RES} (Log: ${XEPHYR_LOG})"

# Build Xephyr args. If a scale was requested and Xephyr supports -scale, use it.
XEPHYR_ARGS=( -ac -screen "${RES}" "${DISPLAY_ARG}" )
if [ -n "${HOST_SCALE_OR_RES}" ]; then
  # detect if it's a scale (float or 0.x) or a WxH spec
  if [[ "${HOST_SCALE_OR_RES}" =~ ^[0-9]*\.[0-9]+$ || "${HOST_SCALE_OR_RES}" =~ ^0\.[0-9]+$ || "${HOST_SCALE_OR_RES}" =~ ^1$ ]]; then
    # test if Xephyr supports -scale
    if Xephyr -help 2>&1 | grep -qi "scale"; then
      XEPHYR_ARGS=( -ac -scale "${HOST_SCALE_OR_RES}" -screen "${RES}" "${DISPLAY_ARG}" )
      echo "Verwende Xephyr -scale ${HOST_SCALE_OR_RES}"
    else
      echo "Xephyr unterstützt kein -scale auf diesem System; Host-Scale wird ignoriert." >&2
    fi
  fi
fi

# Start Xephyr detached (nohup/setsid) so das Terminal nicht blockiert.
setsid Xephyr "${XEPHYR_ARGS[@]}" >"${XEPHYR_LOG}" 2>&1 &
XEPHYR_PID=$!
sleep 0.2

# Warte kurz auf Verfügbarkeit des Displays (xdpyinfo ist robust, fallback auf xset)
wait_for_display(){
  local tries=0
  local max=50
  while true; do
    if command_exists xdpyinfo; then
      if xdpyinfo -display "${DISPLAY_ARG}" >/dev/null 2>&1; then
        return 0
      fi
    else
      if xset -display "${DISPLAY_ARG}" q >/dev/null 2>&1; then
        return 0
      fi
    fi
    tries=$((tries+1))
    if [ $tries -ge $max ]; then
      return 1
    fi
    sleep 0.1
  done
}

if wait_for_display; then
  echo "Display ${DISPLAY_ARG} ist verfügbar." 
else
  echo "Warnung: Display ${DISPLAY_ARG} wurde nicht innerhalb des Zeitlimits verfügbar. Schau in ${XEPHYR_LOG} nach Fehlern." >&2
fi

# Start Openbox inside the Xephyr display
if command_exists openbox; then
  echo "Starte Openbox im Display ${DISPLAY_ARG}"
  setsid env DISPLAY="${DISPLAY_ARG}" openbox >/dev/null 2>&1 &
  sleep 0.05
else
  echo "Openbox nicht gefunden. Du kannst alternativ einen anderen Window-Manager starten (z.B. fluxbox, openbox)." >&2
fi

# Schreibe eine kleine xbindkeys-Konfiguration, die Alt+F2 an rofi -show run bindet
cat >"${XBINDS_CFG}" <<'XBINDS'
"rofi -show run"
Mod1+F2
XBINDS

if command_exists xbindkeys; then
  echo "Starte xbindkeys (Konfig: ${XBINDS_CFG})"
  setsid env DISPLAY="${DISPLAY_ARG}" xbindkeys -f "${XBINDS_CFG}" >/dev/null 2>&1 &
else
  echo "xbindkeys fehlt; Alt+F2 Run-Dialog wird nicht automatisch funktionieren." >&2
fi

## Helper: launch a command inside the Xephyr DISPLAY and log output
APP_LOG="/tmp/xephyr-app-${DISPLAY_ARG#*:}.log"
launch_in_display(){
  local cmd="${1}"
  local log="${2:-${APP_LOG}}"
  echo "Starte im Display ${DISPLAY_ARG}: ${cmd} (log: ${log})"
  # Use nohup so the child survives this script; run via bash -lc to allow shell syntax.
  setsid env DISPLAY="${DISPLAY_ARG}" bash -lc "nohup ${cmd} >\"${log}\" 2>&1 &" || true
}

# Wait a little for the WM to settle so clients can connect
sleep 0.3

if [ -n "${APP_CMD}" ]; then
  echo "Autostarte App im Xephyr: ${APP_CMD}"
  launch_in_display "${APP_CMD}" "${APP_LOG}"
else
  # If no app requested, try to start a graphical terminal so the user can launch programs.
  echo "Kein Autostart-Befehl angegeben — versuche Default-Terminal zu starten."
  TERM_CANDIDATES=(xterm gnome-terminal xfce4-terminal lxterminal konsole kitty alacritty)
  TERM_CMD=""
  for t in "${TERM_CANDIDATES[@]}"; do
    if command -v "$t" >/dev/null 2>&1; then
      case "$t" in
        xterm) TERM_CMD="$t -hold" ;;
        gnome-terminal) TERM_CMD="$t --" ;;
        xfce4-terminal) TERM_CMD="$t --disable-server" ;;
        lxterminal) TERM_CMD="$t -e bash" ;;
        konsole) TERM_CMD="$t -e bash" ;;
        kitty|alacritty|terminator) TERM_CMD="$t" ;;
        *) TERM_CMD="$t" ;;
      esac
      break
    fi
  done

  if [ -n "$TERM_CMD" ]; then
    launch_in_display "$TERM_CMD" "${APP_LOG}"
  else
    # As a last resort, start a simple X demo app so the window is visible (xeyes or xclock)
    if command -v xeyes >/dev/null 2>&1; then
      launch_in_display "xeyes" "/tmp/xephyr-xeyes-${DISPLAY_ARG#*:}.log"
    elif command -v xclock >/dev/null 2>&1; then
      launch_in_display "xclock" "/tmp/xephyr-xclock-${DISPLAY_ARG#*:}.log"
    else
      echo "Kein Terminal oder xeyes/xclock gefunden. Installiere z.B. xterm oder x11-apps." >&2
    fi
  fi
fi

# Kleine Diagnose: welche X-Clients laufen im Xephyr-Display?
sleep 0.25
echo
echo "=== Aktive X-Clients auf ${DISPLAY_ARG} (wenn verfügbar) ==="
if command -v xlsclients >/dev/null 2>&1; then
  xlsclients -display "${DISPLAY_ARG}" || echo "(xlsclients konnte keine Clients listen)"
else
  echo "xlsclients nicht installiert; keine Liste verfügbar. (Installiere x11-utils)"
fi
echo "(Logs: ${APP_LOG} und ${XEPHYR_LOG})"

echo
echo "Xephyr gestartet (PID: ${XEPHYR_PID}), Log: ${XEPHYR_LOG}"

# Wenn ein Host-Resolution (z.B. 1920x1080) angegeben wurde, versuche das Fenster zu skalieren/resizen
if [ -n "${HOST_SCALE_OR_RES}" ] && [[ "${HOST_SCALE_OR_RES}" =~ ^[0-9]+x[0-9]+$ ]]; then
  HOST_W=${HOST_SCALE_OR_RES%x*}
  HOST_H=${HOST_SCALE_OR_RES#*x}
  echo "Versuche Xephyr-Window auf ${HOST_W}x${HOST_H} zu skalieren (benutze xdotool/wmctrl wenn verfügbar)"
  sleep 0.25
  if command -v xdotool >/dev/null 2>&1; then
    # suche das erste sichtbare Fenster mit 'Xephyr' im Namen
    WID=$(xdotool search --onlyvisible --name "Xephyr" | head -n1 || true)
    if [ -n "$WID" ]; then
      xdotool windowsize "$WID" "$HOST_W" "$HOST_H" || true
      xdotool windowmove "$WID" 0 0 || true
      echo "Fenstergröße gesetzt mit xdotool (WID=$WID)"
    else
      echo "Konnte Xephyr-Fenster nicht finden (xdotool)." >&2
    fi
  elif command -v wmctrl >/dev/null 2>&1; then
    # versuche Fenster per Titel zu matchen
    wmctrl -r "Xephyr" -e 0,0,0,${HOST_W},${HOST_H} || echo "wmctrl konnte Fenster nicht anpassen" >&2
  else
    echo "Weder xdotool noch wmctrl installiert; cannot resize window." >&2
  fi
fi
echo "Wenn du die Sitzung beenden willst, kannst du z.B.:"
echo "  pkill -f 'Xephyr.*${DISPLAY_ARG#*:}'   # oder: ./scripts/cleanup_chromecast.sh --force"
echo "xbindkeys-Konfig: ${XBINDS_CFG}"

echo "Viel Erfolg — die Xephyr-Window sollte jetzt interaktiv sein. Alt+F2 öffnet rofi (sofern installiert)."
