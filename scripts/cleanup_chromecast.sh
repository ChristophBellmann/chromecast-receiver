#!/usr/bin/env bash
# Cleanup script for lingering streamer/virtual-display processes and PulseAudio null sinks
# Usage: cleanup_chromecast.sh [--dry-run] [--force]

DRY_RUN=0
FORCE=0
SINK_NAME_ARG="cast_sink"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    --sink) SINK_NAME_ARG="$2"; shift 2 ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--dry-run] [--force] [--sink <sink_name>]

--dry-run    Print actions but don't perform destructive operations.
--force      Skip waiting and escalate to SIGKILL faster.
--sink NAME  Name of the null sink to look for (default: cast_sink).
EOF
      exit 0
      ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

run_cmd() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "DRY: $*"
  else
    echo "+ $*"
    eval "$@"
  fi
}

kill_pattern() {
  local pat="$1"
  echo "-- kill pattern: $pat"
  pids=$(pgrep -f "$pat" || true)
  if [[ -z "$pids" ]]; then
    echo "   no matching pids"
    return
  fi
  for pid in $pids; do
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "DRY: would send SIGINT to $pid"
    else
      echo "sending SIGINT to $pid"
      kill -INT "$pid" 2>/dev/null || true
    fi
  done
  sleep 1
  # escalate
  if [[ $FORCE -eq 1 ]]; then
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then
        if [[ $DRY_RUN -eq 1 ]]; then
          echo "DRY: would send SIGKILL to $pid"
        else
          echo "sending SIGKILL to $pid"
          kill -KILL "$pid" 2>/dev/null || true
        fi
      fi
    done
  else
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then
        if [[ $DRY_RUN -eq 1 ]]; then
          echo "DRY: would send SIGTERM to $pid"
        else
          echo "sending SIGTERM to $pid"
          kill -TERM "$pid" 2>/dev/null || true
        fi
      fi
    done
  fi
}

echo "Cleanup script for Chromecast streamer — sink name='$SINK_NAME_ARG'"

# 1) Stop python streamer process (cast_stream.py)
kill_pattern "cast_stream.py"

# 2) Kill ffmpeg instances (they are typically the heavy ones)
kill_pattern "\bffmpeg\b"

# 3) Stop xpra sessions cleanly (use xpra list/stop)
if command -v xpra >/dev/null 2>&1; then
  echo "-- xpra: checking sessions"
  # Robustly extract display tokens like ':2' or ':20' from xpra list output.
  # xpra list can include paths like '/run/user/:1000/xpra:' so filter out the UID token.
  raw=$(xpra list 2>/dev/null || true)
  uid=$(id -u)
    # extract :NUM tokens, exclude the uid token
    sessions=$(echo "$raw" | grep -oE ':[0-9]+' | grep -v "^:$uid$" | tr '\n' ' ' || true)
  # fallback: try a simpler field parse if nothing found
  if [[ -z "$sessions" ]]; then
    sessions=$(echo "$raw" | awk '{print $1}' | grep -E '^:[0-9]+' | tr '\n' ' ' || true)
  fi
  # dedupe and normalize spacing
  if [[ -n "$sessions" ]]; then
    sessions=$(echo $sessions | tr ' ' '\n' | sort -u | tr '\n' ' ')
    echo "xpra sessions: $sessions"
    for s in $sessions; do
      if [[ $DRY_RUN -eq 1 ]]; then
        echo "DRY: xpra stop $s"
      else
        echo "stopping xpra session $s"
        xpra stop "$s" || true
      fi
    done
  else
    echo "no xpra sessions reported by xpra list"
  fi
  # also kill stray xpra attach or server processes
  kill_pattern "\bxpra\b"
else
  echo "xpra not installed"
fi

# 4) Kill nested X servers (Xephyr) and Xvfb
kill_pattern "\bXephyr\b"
kill_pattern "\bXvfb\b"

# 5) Remove xpra per-user sockets/logs
XPRA_DIR="/run/user/$(id -u)/xpra"
if [[ -d "$XPRA_DIR" ]]; then
  echo "-- xpra runtime dir: $XPRA_DIR"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "DRY: would remove files in $XPRA_DIR"
  else
    ls -la "$XPRA_DIR" || true
    echo "removing xpra runtime files (logs/sockets)"
    rm -rf "$XPRA_DIR"/* || true
  fi
fi

# 6) Unload PulseAudio null-sink modules created for the streamer
if command -v pactl >/dev/null 2>&1; then
  echo "-- pactl: looking for module-null-sink instances matching sink name '$SINK_NAME_ARG'"
  mapfile -t module_ids < <(pactl list short modules | grep module-null-sink | grep -F "$SINK_NAME_ARG" | awk '{print $1}' || true)
  if [[ ${#module_ids[@]} -gt 0 ]]; then
    echo "found module ids: ${module_ids[*]}"
    for mid in "${module_ids[@]}"; do
      if [[ $DRY_RUN -eq 1 ]]; then
        echo "DRY: pactl unload-module $mid"
      else
        echo "unloading module $mid"
        pactl unload-module "$mid" || true
      fi
    done
    # try to set a sensible default sink (first available)
    if [[ $DRY_RUN -eq 0 ]]; then
      first_sink=$(pactl list short sinks | awk '{print $2}' | grep -v "$SINK_NAME_ARG" | head -n1 || true)
      if [[ -n "$first_sink" ]]; then
        echo "setting default sink to $first_sink"
        pactl set-default-sink "$first_sink" || true
      fi
    fi
  else
    echo "no module-null-sink for '$SINK_NAME_ARG' found"
  fi
else
  echo "pactl not available"
fi

# 7) As a final sweep, try to kill any remaining python processes that look like our streamer
kill_pattern "chromecast-receiver/python/cast_stream.py"
kill_pattern "chromecast-receiver/python/cast_gui.py"

echo "Cleanup done. If you still see problems, run:"
echo "  ps aux | grep -E 'xpra|Xephyr|Xvfb|ffmpeg|cast_stream.py'"

exit 0
