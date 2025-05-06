#!/usr/bin/env python3
"""
Permanent Desktop‑&‑Audio‑Streamer für Chromecast
-------------------------------------------------
* FFmpeg‑HTTP‑Server startet sofort (Video+Audio laufen ständig).
* Chromecast spielt erst, wenn der Receiver {type:"start"} sendet.
* Direkt nach dem Start sendet das Skript einen Zeitsync‑Ping
  {type:"sync", t: <unix‑ms>} an den Receiver.
* Hardware‑Beschleunigung (vaapi, cuda, qsv) & PulseAudio‑Null‑Sink wie im Original.
"""

import os, sys, time, socket, signal, subprocess
import pychromecast
from pychromecast.controllers import BaseController
from pychromecast.error import UnsupportedNamespace

# ───── Config ──────────────────────────────────────────────────────
PORT, FPS = 8090, 30
GOP       = FPS * 2                 # Keyframe‑Intervall 2 s
RES       = "1920x1080"
DISPLAY   = os.getenv("DISPLAY", ":0")
NULL_SINK = "cast_sink"
APP_ID    = "22B2DA66"              # Custom‑Receiver ID
NS        = "urn:x-cast:com.example.stream"
# ───────────────────────────────────────────────────────────────────

ffmpeg_proc = cast = mc = None
pa_idx = orig_sink = None
stream_url = None

# ───── Aufräumen ──────────────────────────────────────────────────
def cleanup(*_):
    if mc:   mc.stop()
    if cast: cast.quit_app()
    if ffmpeg_proc and ffmpeg_proc.poll() is None: ffmpeg_proc.terminate()
    if orig_sink:
        subprocess.run(["pactl","set-default-sink",orig_sink],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if pa_idx:
        subprocess.run(["pactl","unload-module",pa_idx],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sys.exit(0)
for sig in (signal.SIGINT, signal.SIGTERM): signal.signal(sig, cleanup)

# ───── Cast‑Controller (start & sync) ─────────────────────────────
class StreamCtl(BaseController):
    def __init__(self): super().__init__(NS)
    def receive_message(self, _, data):
        if data.get("type") == "start":
            mc.play_media(stream_url, "video/mp4"); mc.block_until_active(10)
            self.send_message({"type": "sync", "t": int(time.time()*1000)})
        return True

# ───── System‑Hilfen ──────────────────────────────────────────────
def get_default_sink():
    try:
        out = subprocess.run(["pactl","info"], text=True,
                             stdout=subprocess.PIPE, check=True).stdout
        for ln in out.splitlines():
            if ln.startswith("Default Sink:"):
                return ln.split(":",1)[1].strip()
    except Exception:
        pass
    return None

def setup_null_sink():
    global pa_idx, orig_sink
    orig_sink = get_default_sink()
    pa_idx = subprocess.run(
        ["pactl","load-module","module-null-sink",
         f"sink_name={NULL_SINK}",
         "sink_properties=device.description=ChromecastSink"],
        text=True, stdout=subprocess.PIPE, check=True).stdout.strip()
    subprocess.run(["pactl","set-default-sink",NULL_SINK], check=True)
    return f"{NULL_SINK}.monitor"

def detect_hwaccel():
    try:
        acc=set(subprocess.run(["ffmpeg","-hwaccels"],
            text=True,stdout=subprocess.PIPE,check=True).stdout.split()[2:])
    except Exception: return None
    if "vaapi" in acc and os.path.exists("/dev/dri/renderD128"): return "vaapi"
    if any(x in acc for x in ("cuda","nvenc")):                 return "cuda"
    if "qsv" in acc:                                            return "qsv"
    return None

def ffmpeg_cmd(audio, hw):
    c=["ffmpeg","-hide_banner","-loglevel","warning",
       "-f","x11grab","-framerate",str(FPS),"-video_size",RES,"-i",DISPLAY,
       "-f","pulse","-i",audio]
    if hw=="vaapi":
        c+=["-vaapi_device","/dev/dri/renderD128",
            "-vf","format=nv12,hwupload","-c:v","h264_vaapi","-qp","24"]
    elif hw=="cuda":
        c+=["-c:v","h264_nvenc","-preset","p1","-cq","23"]
    elif hw=="qsv":
        c+=["-c:v","h264_qsv","-global_quality","24"]
    else:
        c+=["-c:v","libx264","-preset","veryfast","-crf","18","-pix_fmt","yuv420p"]
    c+=["-g",str(GOP),"-keyint_min",str(GOP),
        "-c:a","aac","-b:a","192k",
        "-movflags","frag_keyframe+empty_moov+default_base_moof",
        "-f","mp4","-listen","1",f"http://0.0.0.0:{PORT}/"]
    return c

# ───── Main ───────────────────────────────────────────────────────
def main():
    global ffmpeg_proc, cast, mc, stream_url
    audio = setup_null_sink()
    hw    = detect_hwaccel(); print("HW‑Accel:", hw or "Software")
    ffmpeg_proc = subprocess.Popen(ffmpeg_cmd(audio, hw)); time.sleep(1)

    casts,_ = pychromecast.get_chromecasts(); cast=casts[0]; cast.wait()
    cast.start_app(APP_ID); time.sleep(3)
    cast.register_handler(StreamCtl()); mc = cast.media_controller

    host, port = cast.socket_client.host, cast.socket_client.port
    s = socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect((host,port))
    stream_url = f"http://{s.getsockname()[0]}:{PORT}/"; s.close()
    print("Stream läuft -> warte auf ‘Stream’‑Button …")
    while True: time.sleep(1)

if __name__ == "__main__": main()
