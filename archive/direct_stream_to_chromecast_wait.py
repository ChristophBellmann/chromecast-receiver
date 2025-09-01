#!/usr/bin/env python3
"""
Wartet auf den „Stream“-Button der Custom‑Receiver‑HTML und
startet erst dann den Desktop‑Stream.

• App‑ID: 22B2DA66  (bitte anpassen)
• Namespace: urn:x-cast:com.example.stream
"""

import os, sys, time, json, socket, signal, threading, subprocess
import pychromecast
from pychromecast.controllers import BaseController
from pychromecast.error import UnsupportedNamespace


# ─── Einstellungen ─────────────────────────────────────────────
PORT                   = 8090
FPS                    = 30
GOP                    = FPS * 2
RESOLUTION             = "1920x1080"
DISPLAY                = os.getenv("DISPLAY", ":0")
NULL_SINK_NAME         = "cast_sink"
CUSTOM_RECEIVER_APP_ID = "22B2DA66"          # deine Receiver‑App
CUSTOM_NS              = "urn:x-cast:com.example.stream"
# ───────────────────────────────────────────────────────────────

ffmpeg_proc   = None
pa_module_idx = None
original_sink = None


# ─── Aufräumen ─────────────────────────────────────────────────
def cleanup(_sig=None, _frm=None):
    print("\n🛑  Cleaning up …")
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
    sys.exit(0)

for sig in (signal.SIGINT, signal.SIGTERM): signal.signal(sig, cleanup)


# ─── PulseAudio‑Null‑Sink ──────────────────────────────────────
def get_default_sink():
    out = subprocess.run(["pactl","info"], text=True,
                         stdout=subprocess.PIPE).stdout
    for l in out.splitlines():
        if l.startswith("Default Sink:"):
            return l.split(":",1)[1].strip()
    return None

def setup_null_sink():
    global pa_module_idx, original_sink
    original_sink = get_default_sink()
    pa_module_idx = subprocess.run(
        ["pactl","load-module","module-null-sink",
         f"sink_name={NULL_SINK_NAME}",
         "sink_properties=device.description=ChromecastSink"],
        text=True, stdout=subprocess.PIPE).stdout.strip()
    subprocess.run(["pactl","set-default-sink",NULL_SINK_NAME],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"{NULL_SINK_NAME}.monitor"


# ─── FFmpeg‑Kommando ───────────────────────────────────────────
def hwaccel():
    try:
        acc=set(subprocess.run(["ffmpeg","-hwaccels"],text=True,
            stdout=subprocess.PIPE).stdout.split()[2:])
    except Exception: return None
    if "vaapi" in acc and os.path.exists("/dev/dri/renderD128"): return "vaapi"
    if any(x in acc for x in ("cuda","nvenc")):                 return "cuda"
    if "qsv"  in acc:                                           return "qsv"
    return None

def ffmpeg_cmd(audio, hw):
    c = ["ffmpeg","-hide_banner","-loglevel","info","-re",
         "-f","x11grab","-framerate",str(FPS),
         "-video_size",RESOLUTION,"-i",DISPLAY,
         "-f","pulse","-i",audio]
    if   hw=="vaapi": c+=["-vaapi_device","/dev/dri/renderD128",
                          "-vf","format=nv12,hwupload",
                          "-c:v","h264_vaapi","-qp","24"]
    elif hw=="cuda":  c+=["-c:v","h264_nvenc","-preset","p1","-cq","23"]
    elif hw=="qsv":   c+=["-c:v","h264_qsv","-global_quality","24"]
    else:             c+=["-c:v","libx264","-preset","veryfast",
                          "-tune","film","-crf","18","-pix_fmt","yuv420p"]
    c+=["-g",str(GOP),"-keyint_min",str(GOP),
        "-c:a","aac","-b:a","192k",
        "-f","mp4","-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-listen","1",f"http://0.0.0.0:{PORT}/"]
    return c


# ─── Controller, der auf „start“ wartet ────────────────────────
class ClickController(BaseController):
    def __init__(self):
        super().__init__(CUSTOM_NS)
        self.event = threading.Event()

    def receive_message(self, _msg, data, **kw):
        if isinstance(data, str):
            try: data = json.loads(data)
            except json.JSONDecodeError: return False
        if isinstance(data, dict) and data.get("type")=="start":
            print("🟢  Receiver‑UI meldet Button‑Klick!")
            self.event.set(); return True
        return False


# ─── Main ──────────────────────────────────────────────────────
def main():
    global ffmpeg_proc

    # 1) Chromecast finden
    print("🔍 Discovering Chromecast …")
    cc, _ = pychromecast.get_chromecasts()
    if not cc:
        print("⚠️  Kein Chromecast gefunden."); cleanup()
    cast = cc[0]; cast.wait()

    host = getattr(cast,"host",None) or cast.socket_client.host
    port = getattr(cast,"port",None) or cast.socket_client.port
    print(f"✅  {cast.device.friendly_name} @ {host}:{port}")

    # 2) Receiver‑App starten
    print("🚀  custom receiver starten …")
    cast.quit_app()                               # (optional) laufende App beenden
    ok = cast.start_app(CUSTOM_RECEIVER_APP_ID)  
    time.sleep(3)
    print("   start_app returned:", ok)
    print("   running App‑ID:", cast.app_id,
          "| Display‑Name:", cast.status.display_name)

    # 3) Controller registrieren und warten
    ctrl = ClickController(); cast.register_handler(ctrl)
    print("📺  Receiver‑UI steht. Warte 90 s auf Button …")
    if not ctrl.event.wait(timeout=90):
        print("⏳  Timeout – kein Button‑Klick empfangen.")
        cleanup()

    # 4) Audio + FFmpeg
    print("🔊  Pulse‑Sink einrichten …")
    audio = setup_null_sink(); print("    capture:", audio)
    hw   = hwaccel();          print("⚙️  HW‑Accel:", hw or "software")
    ffmpeg_proc = subprocess.Popen(ffmpeg_cmd(audio, hw))
    time.sleep(1)

    # 5) lokale IP ➜ Stream‑URL
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    s.connect((host, port)); local_ip=s.getsockname()[0]; s.close()
    url=f"http://{local_ip}:{PORT}/"
    print("🔗  stream url:", url)

    # 6) Abspielen
    mc=cast.media_controller
    try: mc.update_status()
    except UnsupportedNamespace: pass
    mc.play_media(url,"video/mp4"); mc.block_until_active(timeout=10)
    print("🔴  Desktop‑Stream läuft – Ctrl+C beendet.")

    while True: time.sleep(1)


if __name__=="__main__": main()
