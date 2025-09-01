#!/usr/bin/env bash
set -euo pipefail

# in das Repo wechseln (Ordner, in dem dieses Skript liegt)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p python scripts

# 1) python/cast_stream.py (pychromecast-Fix integriert)
cat > python/cast_stream.py <<'PY'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cast_stream.py — unified Chromecast desktop streaming tool (direct / wait)

Works with multiple pychromecast versions (friendly_name access is robust).
"""

import argparse, os, sys, time, json, socket, signal, subprocess, threading
from typing import Optional, Tuple, List

try:
    import pychromecast
    from pychromecast.error import UnsupportedNamespace
    from pychromecast.controllers import BaseController
except Exception:
    print("Error: pychromecast missing. Install with: pip install pychromecast")
    raise

# Defaults
DEF_PORT       = 8090
DEF_FPS        = 30
DEF_RES        = "1920x1080"
DEF_DISPLAY    = os.getenv("DISPLAY", ":0")
DEF_SINK_NAME  = "cast_sink"
DEF_APP_ID     = "22B2DA66"
DEF_NS         = "urn:x-cast:com.example.stream"
DEF_FFLOG      = "info"

# Globals for cleanup
ffmpeg_proc    = None
pa_module_idx  = None
original_sink  = None
cast_obj       = None

def cleanup(_sig=None, _frm=None):
    """Gracefully stop FFmpeg, restore audio, quit receiver."""
    global ffmpeg_proc, original_sink, pa_module_idx, cast_obj
    try:
        if ffmpeg_proc and ffmpeg_proc.poll() is None:
            ffmpeg_proc.terminate()
            try: ffmpeg_proc.wait(5)
            except subprocess.TimeoutExpired: ffmpeg_proc.kill()
        if original_sink:
            subprocess.run(["pactl","set-default-sink",original_sink],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if pa_module_idx:
            subprocess.run(["pactl","unload-module",pa_module_idx],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if cast_obj:
            try: cast_obj.quit_app()
            except Exception: pass
    finally:
        sys.exit(0)

for _sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(_sig, cleanup)

# ---------- PulseAudio ----------
def get_default_sink() -> Optional[str]:
    out = subprocess.run(["pactl","info"], text=True, stdout=subprocess.PIPE).stdout
    for l in out.splitlines():
        if l.startswith("Default Sink:"):
            return l.split(":",1)[1].strip()
    return None

def setup_null_sink(sink_name: str) -> str:
    global pa_module_idx, original_sink
    original_sink = get_default_sink()
    res = subprocess.run(
        ["pactl","load-module","module-null-sink",
         f"sink_name={sink_name}",
         "sink_properties=device.description=ChromecastSink"],
        text=True, stdout=subprocess.PIPE, check=True
    )
    pa_module_idx = res.stdout.strip()
    subprocess.run(["pactl","set-default-sink",sink_name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return f"{sink_name}.monitor"

# ---------- FFmpeg ----------
def detect_hwaccel_auto() -> Optional[str]:
    try:
        out = subprocess.run(["ffmpeg","-hwaccels"], text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        methods = {l.strip() for l in out.splitlines()[1:] if l.strip()}
    except Exception:
        return None
    if "vaapi" in methods and os.path.exists("/dev/dri/renderD128"): return "vaapi"
    if any(m in methods for m in ("cuda","nvenc")): return "cuda"
    if "qsv" in methods: return "qsv"
    return None

def build_ffmpeg_cmd(audio_src: str, fps: int, res: str, display: str,
                     loglevel: str, hw: Optional[str], gop_frames: int, port: int) -> List[str]:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", loglevel, "-re",
        "-thread_queue_size","512",
        "-f","x11grab","-framerate",str(fps),
        "-video_size",res,"-i",display,
        "-thread_queue_size","512",
        "-f","pulse","-i",audio_src,
    ]
    if   hw == "vaapi":
        cmd += ["-vaapi_device","/dev/dri/renderD128",
                "-vf","format=nv12,hwupload","-c:v","h264_vaapi","-qp","24"]
    elif hw == "cuda":
        cmd += ["-c:v","h264_nvenc","-preset","p1","-cq","23"]
    elif hw == "qsv":
        cmd += ["-c:v","h264_qsv","-global_quality","24"]
    elif hw == "software" or hw is None:
        cmd += ["-c:v","libx264","-preset","veryfast","-tune","film","-crf","18","-pix_fmt","yuv420p"]
    else:
        cmd += ["-c:v","libx264","-preset","veryfast","-crf","18","-pix_fmt","yuv420p"]

    cmd += [
        "-g", str(gop_frames), "-keyint_min", str(gop_frames),
        "-c:a","aac","-b:a","192k",
        "-f","mp4",
        "-movflags","frag_keyframe+empty_moov+default_base_moof",
        "-listen","1", f"http://0.0.0.0:{port}/"
    ]
    return cmd

# ---------- Chromecast helpers ----------
def cc_friendly_name(c) -> str:
    """Return a friendly name across pychromecast versions."""
    return (
        getattr(getattr(c, "device", None), "friendly_name", None)
        or getattr(c, "name", None)
        or getattr(getattr(c, "cast_info", None), "friendly_name", None)
        or getattr(getattr(getattr(c, "socket_client", None), "device", None), "friendly_name", None)
        or "Chromecast"
    )

def find_chromecast(name_substr: Optional[str], ip: Optional[str]) -> "pychromecast.Chromecast":
    chromecasts, _ = pychromecast.get_chromecasts()
    if not chromecasts:
        print("No Chromecast found on the network.", file=sys.stderr); cleanup()
    if ip:
        for c in chromecasts:
            host = getattr(c, "host", None) or c.socket_client.host
            if host == ip: return c
        print(f"No Chromecast with IP {ip} found.", file=sys.stderr); cleanup()
    if name_substr:
        for c in chromecasts:
            if name_substr.lower() in cc_friendly_name(c).lower(): return c
        print(f"No Chromecast with name containing '{name_substr}' found.", file=sys.stderr); cleanup()
    return chromecasts[0]

def host_port(cast) -> Tuple[str,int]:
    host = getattr(cast, "host", None) or cast.socket_client.host
    port = getattr(cast, "port", None) or cast.socket_client.port
    return host, int(port)

def local_ip_for(remote_host: str, remote_port: int) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_host, remote_port))
        return s.getsockname()[0]
    finally:
        s.close()

def start_receiver(cast, app_id: str):
    try: cast.quit_app()
    except Exception: pass
    ok = cast.start_app(app_id)
    time.sleep(3)
    print("start_app returned:", ok)
    # robust print (older versions may lack .status)
    try:
        disp = cast.status.display_name
    except Exception:
        try:
            disp = getattr(getattr(cast, "socket_client", None), "app_display_name", None)
        except Exception:
            disp = None
    print("running App-ID:", getattr(cast, "app_id", "?"), "| Display-Name:", disp or "?")

# ---------- Wait controller ----------
class WaitController(BaseController):
    def __init__(self, namespace: str):
        super().__init__(namespace)
        self.event = threading.Event()
    def receive_message(self, _message, data, **_kwargs):
        if isinstance(data, str):
            try: data = json.loads(data)
            except json.JSONDecodeError: return False
        if isinstance(data, dict) and data.get("type") == "start":
            print("Receiver says: start")
            self.event.set(); return True
        return False

# ---------- Main ----------
def main():
    global ffmpeg_proc, cast_obj

    ap = argparse.ArgumentParser(
        description="Cast your Linux desktop to Chromecast (direct or wait for receiver UI).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--mode", choices=["direct","wait"], default="direct",
                    help="direct: start streaming immediately; wait: wait for receiver button")
    ap.add_argument("--app-id", default=DEF_APP_ID, help="Custom Receiver App ID")
    ap.add_argument("--ns", default=DEF_NS, help="Custom message namespace (wait-mode)")
    ap.add_argument("--device", help="Chromecast name substring to pick a specific device")
    ap.add_argument("--ip", help="Chromecast IP address to pick a specific device")
    ap.add_argument("--port", type=int, default=DEF_PORT, help="Local HTTP port for MP4 stream")
    ap.add_argument("--fps", type=int, default=DEF_FPS, help="Frames per second")
    ap.add_argument("--resolution", default=DEF_RES, help="Capture resolution WxH")
    ap.add_argument("--display", default=DEF_DISPLAY, help="X11 DISPLAY to capture")
    ap.add_argument("--gop-seconds", type=float, default=2.0, help="Keyframe interval in seconds")
    ap.add_argument("--hw", choices=["auto","vaapi","cuda","qsv","software"], default="auto",
                    help="Hardware encoder selection")
    ap.add_argument("--sink-name", default=DEF_SINK_NAME, help="PulseAudio sink name to create")
    ap.add_argument("--fflog", default=DEF_FFLOG, help="FFmpeg loglevel (quiet|error|warning|info|debug)")
    args = ap.parse_args()

    print("Discovering Chromecast …")
    cast = find_chromecast(args.device, args.ip)
    cast.wait()
    cast_obj = cast
    host, cport = host_port(cast)
    print(f"Chromecast: {cc_friendly_name(cast)} @ {host}:{cport}")

    if args.app_id:
        print(f"Launching receiver app {args.app_id} …")
        start_receiver(cast, args.app_id)

    if args.mode == "wait":
        ctrl = WaitController(args.ns)
        cast.register_handler(ctrl)
        print("Waiting for 'start' from receiver …")
        ctrl.event.wait()
        print("Receiver requested streaming.")

    print("Setting up PulseAudio null sink …")
    audio_src = setup_null_sink(args.sink_name)

    if args.hw == "auto":
        hw_sel = detect_hwaccel_auto()
    else:
        hw_sel = args.hw

    gop_frames = max(1, int(args.fps * args.gop_seconds))
    cmd = build_ffmpeg_cmd(audio_src, args.fps, args.resolution, args.display,
                           args.fflog, hw_sel, gop_frames, args.port)

    print("Starting FFmpeg …")
    global ffmpeg_proc
    ffmpeg_proc = subprocess.Popen(cmd)
    time.sleep(1)

    lip = local_ip_for(host, cport)
    stream_url = f"http://{lip}:{args.port}/"
    print("Stream URL:", stream_url)

    mc = cast.media_controller
    try: mc.update_status()
    except UnsupportedNamespace: pass
    mc.play_media(stream_url, "video/mp4")
    mc.block_until_active(timeout=10)
    print("Streaming started. Press Ctrl+C to stop.")

    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
PY
chmod +x python/cast_stream.py

# 2) receiver.html – Broadcast-Fix (nur falls noch nicht geändert)
if grep -q "bus\.send({type:'start'})" receiver.html 2>/dev/null; then
  sed -i "s/bus\.send({type:'start'})/bus.broadcast(JSON.stringify({type:'start'}))/g" receiver.html
  echo "receiver.html: send() -> broadcast(JSON.stringify(...)) geändert."
else
  echo "receiver.html: Broadcast-Fix bereits vorhanden oder Pattern nicht gefunden."
fi

# 3) scripts/install.sh – robuster Installer mit dpkg-Audit
cat > scripts/install.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${HOME}/.local/share/chromecast-receiver"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"

# abbrechen, wenn dpkg/apt in schlechtem Zustand
if ! sudo dpkg --audit >/dev/null 2>&1; then
  echo "dpkg meldet einen inkonsistenten Zustand."
  echo "Bitte zuerst reparieren: sudo dpkg --configure -a && sudo apt-get -f install"
  exit 1
fi

echo "➡️  Installing to ${TARGET_DIR} …"
mkdir -p "${TARGET_DIR}" "${BIN_DIR}" "${APP_DIR}"

echo "➡️  Installing system packages (sudo may ask for password) …"
sudo apt update
sudo apt install -y ffmpeg pulseaudio-utils python3-venv python3-tk

echo "➡️  Copy project files …"
rsync -a --delete "${REPO_DIR}/" "${TARGET_DIR}/"

echo "➡️  Create virtualenv …"
python3 -m venv "${TARGET_DIR}/.venv"
"${TARGET_DIR}/.venv/bin/pip" install --upgrade pip wheel
"${TARGET_DIR}/.venv/bin/pip" install pychromecast

echo "➡️  Install launcher …"
cat > "${BIN_DIR}/chromecast-streamer" <<'EOF'
#!/usr/bin/env bash
APP_DIR="${HOME}/.local/share/chromecast-receiver"
exec "${APP_DIR}/.venv/bin/python" "${APP_DIR}/python/cast_gui.py"
EOF
chmod +x "${BIN_DIR}/chromecast-streamer"

echo "➡️  Desktop entry …"
cat > "${APP_DIR}/chromecast-streamer.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Chromecast Streamer
Comment=Stream your desktop directly to Chromecast
Exec=chromecast-streamer
Icon=display
Terminal=false
Categories=AudioVideo;Network;
EOF

echo "✅ Installation complete."
echo "• Start über Anwendungsmenü: 'Chromecast Streamer'"
echo "• Oder im Terminal: chromecast-streamer"
SH
chmod +x scripts/install.sh

# 4) README.md – inkl. Uninstall
cat > README.md <<'MD'
# Chromecast Receiver & Desktop Streamer

Ein Tool zum **direkten Desktop-Streaming** auf einen Chromecast – wahlweise
- **direct**: Stream startet sofort
- **wait**: zuerst deine `receiver.html` (Intro/Buttons), Start per „Stream“-Button

## Inhalte

- `receiver.html` – Custom Receiver (CAF), per **HTTPS** hosten (in der Cast Developer Console hinterlegen)
- `splash-0.1.png` – Splash-Bild
- `python/cast_stream.py` – EIN Streaming-Tool (CLI; direct/wait)
- `python/cast_gui.py` – kleines Start/Stop-GUI

> **Hinweis:** Im Receiver sollte beim Stream-Button  
> `bus.broadcast(JSON.stringify({type:'start'}))`  
> verwendet werden (statt `send()` ohne `senderId`).

---

## Installation (Pop!_OS / Ubuntu)

```bash
cd ~/Dokumente/Entwicklung/chromecast-receiver
chmod +x scripts/install.sh
./scripts/install.sh
