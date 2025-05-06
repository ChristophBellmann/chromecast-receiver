#!/usr/bin/env python3
import os, sys, time, socket, signal, subprocess, threading
import pychromecast
from pychromecast.controllers import BaseController

# ── Einstellungen ────────────────────────────────────────────────
PORT, FPS = 8090, 30
GOP       = FPS * 2
RES       = "1920x1080"
DISPLAY   = os.getenv("DISPLAY", ":0")
NULL_SINK = "cast_sink"
APP_ID    = "22B2DA66"
NS        = "urn:x-cast:com.example.stream"
# ─────────────────────────────────────────────────────────────────

ffmpeg_proc = pa_idx = None
orig_sink   = None

# ── Aufräumen ────────────────────────────────────────────────────
def cleanup(*_):
    if ffmpeg_proc and ffmpeg_proc.poll() is None:
        ffmpeg_proc.terminate()
    if orig_sink:
        subprocess.run(["pactl", "set-default-sink", orig_sink],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if pa_idx:
        subprocess.run(["pactl", "unload-module", pa_idx],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sys.exit(0)
for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, cleanup)

# ── PulseAudio‑Null‑Sink ─────────────────────────────────────────
def default_sink():
    out = subprocess.run(["pactl", "info"], text=True,
                         stdout=subprocess.PIPE).stdout
    for l in out.splitlines():
        if l.startswith("Default Sink:"):
            return l.split(":", 1)[1].strip()
    return None

def create_null_sink():
    global pa_idx, orig_sink
    orig_sink = default_sink()
    pa_idx = subprocess.run(
        ["pactl", "load-module", "module-null-sink",
         f"sink_name={NULL_SINK}",
         "sink_properties=device.description=ChromecastSink"],
        text=True, stdout=subprocess.PIPE).stdout.strip()
    subprocess.run(["pactl", "set-default-sink", NULL_SINK])
    return f"{NULL_SINK}.monitor"

# ── HW‑Accel‑Erkennung ───────────────────────────────────────────
def hw_accel():
    try:
        acc = set(subprocess.run(["ffmpeg", "-hwaccels"],
                                 text=True, stdout=subprocess.PIPE).stdout.split()[2:])
    except Exception:
        return None
    if "vaapi" in acc and os.path.exists("/dev/dri/renderD128"):
        return "vaapi"
    if any(x in acc for x in ("cuda", "nvenc")):
        return "cuda"
    if "qsv" in acc:
        return "qsv"
    return None

# ── FFmpeg‑Aufruf bauen ─────────────────────────────────────────
def ffmpeg_cmd(audio, hw):
    c = ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "x11grab", "-framerate", str(FPS), "-video_size", RES, "-i", DISPLAY,
         "-f", "pulse", "-i", audio]
    if hw == "vaapi":
        c += ["-vaapi_device", "/dev/dri/renderD128",
              "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "24"]
    elif hw == "cuda":
        c += ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "23"]
    elif hw == "qsv":
        c += ["-c:v", "h264_qsv", "-global_quality", "24"]
    else:
        c += ["-c:v", "libx264", "-preset", "veryfast",
              "-crf", "18", "-pix_fmt", "yuv420p"]
    c += ["-g", str(GOP), "-keyint_min", str(GOP),
          "-c:a", "aac", "-b:a", "192k",
          "-movflags", "frag_keyframe+empty_moov+default_base_moof",
          "-f", "mp4", "-listen", "1", f"http://0.0.0.0:{PORT}/"]
    return c

# ── Custom‑Controller zum Warten auf „start“ ─────────────────────
class StreamController(BaseController):
    def __init__(self):
        super().__init__(NS)
        self.start_event = threading.Event()

    # wird aufgerufen, wenn der Receiver eine Nachricht schickt
    def receive_message(self, _message, data):
        try:
            if isinstance(data, str):
                import json
                data = json.loads(data)
        except Exception:
            return False
        if isinstance(data, dict) and data.get("type") == "start":
            print("📺  'start' erhalten – starte FFmpeg …")
            self.start_event.set()
            return True
        return False

# ── Hauptlogik ───────────────────────────────────────────────────
def main():
    global ffmpeg_proc

    # Chromecast finden
    cc, _ = pychromecast.get_chromecasts()
    if not cc:
        print("Kein Chromecast gefunden")
        cleanup()
    cast = cc[0]
    cast.wait()

    # Receiver starten
    cast.start_app(APP_ID)
    print("🔔  Receiver läuft – warte auf 'Stream'‑Klick …")

    # Controller registrieren + auf start warten
    sc = StreamController()
    cast.register_handler(sc)
    sc.start_event.wait()        # → Blockiert bis Button gedrückt

    # Jetzt erst FFmpeg und PulseAudio‑Sink aufsetzen
    audio = create_null_sink()
    hw    = hw_accel()
    print("HW‑Accel:", hw or "Software")
    ffmpeg_proc = subprocess.Popen(ffmpeg_cmd(audio, hw))

    # Local IP für Chromecast ermitteln
    host, port = cast.socket_client.host, cast.socket_client.port
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((host, port))
    url = f"http://{s.getsockname()[0]}:{PORT}/"
    s.close()

    # Medienwiedergabe anschieben
    cast.media_controller.play_media(url, "video/mp4")
    print("▶️  Streaming …  Ctrl+C beendet")

    # Idle‑Loop
    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
