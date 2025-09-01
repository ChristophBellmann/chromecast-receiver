#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cast_stream.py — unified Chromecast desktop streaming tool

Modes:
  direct  – start FFmpeg and cast immediately
  wait    – show receiver.html first; start only after 'Stream' button sends {"type":"start"}

Requirements:
  - Linux with PulseAudio/PipeWire (pactl)
  - ffmpeg
  - pychromecast

"""

import argparse, os, sys, time, json, socket, signal, subprocess, threading
from typing import Optional, Tuple, List

# ---- Optional import guard for better error messages
try:
    import pychromecast
    from pychromecast.error import UnsupportedNamespace
    from pychromecast.controllers import BaseController
except Exception as e:
    print("Error: pychromecast missing. Install with: pip install pychromecast")
    raise

# ========== Defaults ==========
DEF_PORT       = 8090
DEF_FPS        = 30
DEF_RES        = "1920x1080"
DEF_DISPLAY    = os.getenv("DISPLAY", ":0")
DEF_SINK_NAME  = "cast_sink"
DEF_APP_ID     = "22B2DA66"
DEF_NS         = "urn:x-cast:com.example.stream"
DEF_FFLOG      = "info"

# ========== Globals for cleanup ==========
ffmpeg_proc    = None
pa_module_idx  = None
original_sink  = None
cast_obj       = None

# ========== Cleanup ==========
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

# ========== Audio (PulseAudio) ==========
def get_default_sink() -> Optional[str]:
    out = subprocess.run(["pactl","info"], text=True, stdout=subprocess.PIPE).stdout
    for l in out.splitlines():
        if l.startswith("Default Sink:"):
            return l.split(":",1)[1].strip()
    return None

def setup_null_sink(sink_name: str) -> str:
    """Create a null sink; set it as default; return its monitor source."""
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

# ========== FFmpeg ==========
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

def build_ffmpeg_cmd(
    audio_src: str, fps: int, res: str, display: str,
    loglevel: str, hw: Optional[str], gop_frames: int, port: int
) -> List[str]:
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
        # unknown string -> fallback
        cmd += ["-c:v","libx264","-preset","veryfast","-crf","18","-pix_fmt","yuv420p"]

    cmd += [
        "-g", str(gop_frames), "-keyint_min", str(gop_frames),
        "-c:a","aac","-b:a","192k",
        "-f","mp4",
        "-movflags","frag_keyframe+empty_moov+default_base_moof",
        "-listen","1", f"http://0.0.0.0:{port}/"
    ]
    return cmd

# ========== Chromecast helpers ==========
def find_chromecast(name_substr: Optional[str], ip: Optional[str]) -> "pychromecast.Chromecast":
    chromecasts, _ = pychromecast.get_chromecasts()
    if not chromecasts:
        print("No Chromecast found on the network.", file=sys.stderr)
        cleanup()

    if ip:
        for c in chromecasts:
            host = getattr(c, "host", None) or c.socket_client.host
            if host == ip:
                return c
        print(f"No Chromecast with IP {ip} found.", file=sys.stderr); cleanup()

    if name_substr:
        for c in chromecasts:
            if name_substr.lower() in c.device.friendly_name.lower():
                return c
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
    try:
        cast.quit_app()  # best effort
    except Exception:
        pass
    ok = cast.start_app(app_id)
    time.sleep(3)
    print("start_app returned:", ok)
    print("running App-ID:", cast.app_id, "| Display-Name:", cast.status.display_name)

# ========== Receiver message controller (wait-mode) ==========
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
            self.event.set()
            return True
        return False

# ========== Main flow ==========
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
    ap.add_argument("--display", default=DEF_DISPLAY, help="X11 DISPLAY to capture (x11grab)")
    ap.add_argument("--gop-seconds", type=float, default=2.0, help="Keyframe interval in seconds")
    ap.add_argument("--hw", choices=["auto","vaapi","cuda","qsv","software"], default="auto",
                    help="Hardware encoder selection")
    ap.add_argument("--sink-name", default=DEF_SINK_NAME, help="PulseAudio sink name to create")
    ap.add_argument("--fflog", default=DEF_FFLOG, help="FFmpeg loglevel (quiet|error|warning|info|debug)")
    args = ap.parse_args()

    # Chromecast
    print("Discovering Chromecast …")
    cast = find_chromecast(args.device, args.ip)
    cast.wait()
    cast_obj = cast
    host, cport = host_port(cast)
    print(f"Chromecast: {cast.device.friendly_name} @ {host}:{cport}")

    # Receiver App
    if args.app_id:
        print(f"Launching receiver app {args.app_id} …")
        start_receiver(cast, args.app_id)

    # Wait-mode: wait for receiver button BEFORE we start FFmpeg
    if args.mode == "wait":
        ctrl = WaitController(args.ns)
        cast.register_handler(ctrl)
        print("Waiting for 'start' from receiver …")
        # you still might want a timeout; here we wait indefinitely
        ctrl.event.wait()
        print("Receiver requested streaming.")

    # Audio + FFmpeg
    print("Setting up PulseAudio null sink …")
    audio_src = setup_null_sink(args.sink_name)
    hw_sel = None
    if args.hw == "auto":
        hw_sel = detect_hwaccel_auto()
    elif args.hw in ("vaapi","cuda","qsv","software"):
        hw_sel = args.hw
    else:
        hw_sel = None

    gop_frames = max(1, int(args.fps * args.gop_seconds))
    cmd = build_ffmpeg_cmd(
        audio_src=audio_src, fps=args.fps, res=args.resolution,
        display=args.display, loglevel=args.fflog, hw=hw_sel,
        gop_frames=gop_frames, port=args.port
    )

    print("Starting FFmpeg …")
    global ffmpeg_proc
    ffmpeg_proc = subprocess.Popen(cmd)
    time.sleep(1)

    # local stream URL
    lip = local_ip_for(host, cport)
    stream_url = f"http://{lip}:{args.port}/"
    print("Stream URL:", stream_url)

    # Start playback
    mc = cast.media_controller
    try: mc.update_status()
    except UnsupportedNamespace: pass
    mc.play_media(stream_url, "video/mp4")
    mc.block_until_active(timeout=10)
    print("Streaming started. Press Ctrl+C to stop.")

    # keep alive
    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
