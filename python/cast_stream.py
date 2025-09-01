#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cast_stream.py — Chromecast desktop streaming (direct / wait) with Virtual Display (Xephyr)

Features:
- Config-aware (reads ~/.config/chromecast-streamer/config.ini)
- Robust Chromecast selection by IP or friendly name (fallback to discovery)
- Wayland/Xorg hinting and DISPLAY validation
- Optional Virtual Display via Xephyr (auto pick free :N), optional Openbox
- Clean cleanup (FFmpeg, PulseAudio sink, receiver app, Xephyr/Openbox)
"""
import argparse, os, sys, time, json, socket, signal, subprocess, threading, configparser, shutil
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

CONFIG_USER = os.path.expanduser("~/.config/chromecast-streamer/config.ini")
CONFIG_LOCAL = os.path.abspath("config.local.ini")

# Globals for cleanup
ffmpeg_proc    = None
pa_module_idx  = None
original_sink  = None
cast_obj       = None
xephyr_proc    = None
wm_proc        = None
virtual_disp   = None

def cleanup(_sig=None, _frm=None):
    """Gracefully stop FFmpeg, restore audio, quit receiver, stop Xephyr/WM."""
    global ffmpeg_proc, original_sink, pa_module_idx, cast_obj, xephyr_proc, wm_proc
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
        if wm_proc and wm_proc.poll() is None:
            wm_proc.terminate()
            try: wm_proc.wait(3)
            except subprocess.TimeoutExpired: wm_proc.kill()
        if xephyr_proc and xephyr_proc.poll() is None:
            xephyr_proc.terminate()
            try: xephyr_proc.wait(3)
            except subprocess.TimeoutExpired: xephyr_proc.kill()
    finally:
        sys.exit(0)

for _sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(_sig, cleanup)

# ---------- Config ----------
def load_config(paths: List[str]) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read([p for p in paths if p and os.path.exists(p)])
    return cfg

def cfg_get(cfg, section, key, default=None):
    try:
        return cfg.get(section, key, fallback=default)
    except Exception:
        return default

def cfg_getint(cfg, section, key, default=None):
    try:
        return cfg.getint(section, key, fallback=default)
    except Exception:
        return default

# ---------- Checks ----------
def validate_display(disp: str) -> bool:
    """Try xdpyinfo to validate a display; if not present, best-effort True."""
    if not disp:
        return False
    if shutil.which("xdpyinfo") is None:
        return True  # can't validate; don't block
    try:
        r = subprocess.run(["xdpyinfo", "-display", disp],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        return r.returncode == 0
    except Exception:
        return False

def is_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"

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
    return (
        getattr(getattr(c, "device", None), "friendly_name", None)
        or getattr(c, "name", None)
        or getattr(getattr(c, "cast_info", None), "friendly_name", None)
        or getattr(getattr(getattr(c, "socket_client", None), "device", None), "friendly_name", None)
        or "Chromecast"
    )

def host_port(cast) -> Tuple[str,int]:
    host = getattr(cast, "host", None) or cast.socket_client.host
    port = getattr(cast, "port", None) or cast.socket_client.port
    return host, int(port)

def list_discovered(chromecasts: List["pychromecast.Chromecast"]) -> List[Tuple[str,str]]:
    out = []
    for c in chromecasts:
        try:
            h = getattr(c, "host", None) or c.socket_client.host
            nm = cc_friendly_name(c)
            out.append((nm, h))
        except Exception:
            pass
    return out

def connect_by_ip(ip: str) -> Optional["pychromecast.Chromecast"]:
    try:
        c = pychromecast.Chromecast(ip)  # direct connect, no mDNS
        c.wait()
        return c
    except Exception as e:
        print(f"⚠️  Direct connect to {ip} failed: {e}")
        return None

def find_chromecast(name_substr: Optional[str], ip: Optional[str]) -> "pychromecast.Chromecast":
    """Best-effort selection with fallbacks and diagnostics."""
    # 1) If IP provided, try direct connect first (fast path)
    if ip:
        c = connect_by_ip(ip)
        if c:
            print(f"✅ Using Chromecast by IP: {ip} ({cc_friendly_name(c)})")
            return c
        else:
            print(f"⚠️  No Chromecast reachable at IP {ip}. Falling back to discovery…")

    # 2) mDNS discovery
    chromecasts, _ = pychromecast.get_chromecasts()
    if not chromecasts:
        print("❌ No Chromecast found on the network.", file=sys.stderr)
        cleanup()

    print("🔎 Discovered devices:")
    for nm, h in list_discovered(chromecasts):
        print(f"   • {nm} @ {h}")

    # 3) If name requested, try to match
    if name_substr:
        for c in chromecasts:
            if name_substr.lower() in cc_friendly_name(c).lower():
                print(f"✅ Using Chromecast by name: {cc_friendly_name(c)}")
                return c
        print(f"⚠️  No Chromecast with name containing '{name_substr}' found. Using first discovered.")

    # 4) Fallback
    return chromecasts[0]

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
    try:
        disp = cast.status.display_name
    except Exception:
        try:
            disp = getattr(getattr(cast, "socket_client", None), "app_display_name", None)
        except Exception:
            disp = None
    print("running App-ID:", getattr(cast, "app_id", "?"), "| Display-Name:", disp or "?")

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

# ---------- Virtual display (Xephyr) ----------
def pick_free_display(start: int = 2, end: int = 9) -> str:
    for n in range(start, end+1):
        path = f"/tmp/.X11-unix/X{n}"
        if not os.path.exists(path):
            return f":{n}"
    return f":{end+1}"

def wait_for_display(disp: str, timeout: float = 5.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if validate_display(disp):
            return True
        time.sleep(0.2)
    return False

def start_virtual_display(res: str, disp: Optional[str] = None, with_wm: bool = True) -> str:
    global xephyr_proc, wm_proc, virtual_disp
    if shutil.which("Xephyr") is None:
        print("❌ Xephyr not installed. Install with: sudo apt install xserver-xephyr", file=sys.stderr)
        sys.exit(1)
    if with_wm and shutil.which("openbox") is None:
        print("⚠️  openbox not installed. You can still run without WM, or install with:", file=sys.stderr)
        print("    sudo apt install openbox", file=sys.stderr)
        with_wm = False
    virtual_disp = disp if disp and disp.lower() != "auto" else pick_free_display()
    print(f"🖥️  Starting virtual display {virtual_disp} @ {res} …")
    xephyr_proc = subprocess.Popen(["Xephyr", virtual_disp, "-screen", res, "-title", "Cast-Virtual", "-resizeable"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait_for_display(virtual_disp, 6.0):
        print("❌ Xephyr did not come up in time.", file=sys.stderr)
        cleanup()

    if with_wm:
        env = os.environ.copy(); env["DISPLAY"] = virtual_disp
        wm_proc = subprocess.Popen(["openbox"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
    print("   Virtual display ready.")
    return virtual_disp

# ---------- Main ----------
def main():
    global ffmpeg_proc, cast_obj

    ap = argparse.ArgumentParser(
        description="Cast your Linux desktop to Chromecast (direct or wait for receiver UI).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--mode", choices=["direct","wait"], default=None)
    ap.add_argument("--app-id", default=None)
    ap.add_argument("--ns", default=None)
    ap.add_argument("--device", default=None)
    ap.add_argument("--ip", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--fps", type=int, default=None)
    ap.add_argument("--resolution", default=None)
    ap.add_argument("--display", default=None)
    ap.add_argument("--gop-seconds", type=float, default=None)
    ap.add_argument("--hw", choices=["auto","vaapi","cuda","qsv","software"], default=None)
    ap.add_argument("--sink-name", default=None)
    ap.add_argument("--fflog", default=None)
    # Virtual display flags
    ap.add_argument("--virtual", action="store_true", help="Start a virtual X screen via Xephyr and capture that")
    ap.add_argument("--virtual-res", default="3840x2160")
    ap.add_argument("--virtual-display", default="auto")
    ap.add_argument("--virtual-wm", action="store_true", help="Start openbox inside the virtual display")
    ap.add_argument("--config", help="Optional config path to read")
    ap.add_argument("--save-config", action="store_true", help="Persist current effective settings to ~/.config/chromecast-streamer/config.ini")
    args = ap.parse_args()

    # Load config baseline
    cfg = load_config([args.config or "", CONFIG_USER, CONFIG_LOCAL])

    # Resolve effective values (CLI > config > defaults)
    mode        = args.mode        or cfg_get(cfg, "stream","mode",        "direct")
    app_id      = args.app_id      or cfg_get(cfg, "cast","app_id",        DEF_APP_ID)
    ns          = args.ns          or cfg_get(cfg, "cast","namespace",     DEF_NS)
    device      = args.device      or cfg_get(cfg, "cast","device_name",   None)
    ip          = args.ip          or cfg_get(cfg, "cast","device_ip",     None)
    port        = args.port        or cfg_getint(cfg,"stream","port",      DEF_PORT)
    fps         = args.fps         or cfg_getint(cfg,"stream","fps",       DEF_FPS)
    res         = args.resolution  or cfg_get(cfg, "stream","resolution",  DEF_RES)
    disp        = args.display     or cfg_get(cfg, "stream","display",     DEF_DISPLAY)
    gop_s       = args.gop_seconds or float(cfg_get(cfg,"stream","gop_seconds", "2.0"))
    hw          = args.hw          or cfg_get(cfg, "stream","hw",          "auto")
    sink        = args.sink_name   or cfg_get(cfg, "stream","sink_name",   DEF_SINK_NAME)
    fflog       = args.fflog       or cfg_get(cfg, "stream","fflog",       DEF_FFLOG)
    # virtual
    virt        = args.virtual     or (cfg_get(cfg,"virtual","enabled","false").lower() == "true")
    virt_res    = args.virtual_res or cfg_get(cfg,"virtual","resolution","3840x2160")
    virt_disp   = args.virtual_display or cfg_get(cfg,"virtual","display","auto")
    virt_wm     = args.virtual_wm or (cfg_get(cfg,"virtual","wm","true").lower() == "true")

    # Wayland hint
    if is_wayland() and not virt:
        print("ℹ️  Wayland session detected. x11grab works in Xorg, or enable --virtual to use Xephyr.")

    # Optionally persist resolved settings to user config
    if args.save_config:
        os.makedirs(os.path.dirname(CONFIG_USER), exist_ok=True)
        out = configparser.ConfigParser()
        out["cast"] = {
            "app_id": app_id,
            "namespace": ns,
            "device_name": device or "",
            "device_ip": ip or "",
        }
        out["stream"] = {
            "resolution": res,
            "fps": str(fps),
            "gop_seconds": str(gop_s),
            "port": str(port),
            "hw": hw,
            "display": disp,
            "fflog": fflog,
            "sink_name": sink,
            "mode": mode,
        }
        out["virtual"] = {
            "enabled": str(virt),
            "resolution": virt_res,
            "display": virt_disp,
            "wm": str(virt_wm),
        }
        with open(CONFIG_USER, "w") as f:
            out.write(f)
        print(f"Saved config → {CONFIG_USER}")

    print("Discovering Chromecast …")
    cast = find_chromecast(device, ip)
    cast.wait()
    cast_obj = cast
    host = getattr(cast, "host", None) or cast.socket_client.host
    cport = getattr(cast, "port", None) or cast.socket_client.port
    print(f"Chromecast: {cc_friendly_name(cast)} @ {host}:{cport}")

    if app_id:
        print(f"Launching receiver app {app_id} …")
        start_receiver(cast, app_id)

    if mode == "wait":
        ctrl = WaitController(ns)
        cast.register_handler(ctrl)
        print("Waiting for 'start' from receiver …")
        ctrl.event.wait()
        print("Receiver requested streaming.")

    # Virtual display handling
    if virt:
        disp = start_virtual_display(virt_res, virt_disp, with_wm=virt_wm)
        print(f"👉 Start apps inside the virtual screen with: DISPLAY={disp} <app> &")

    # Validate the chosen display (best effort)
    if not validate_display(disp):
        print(f"⚠️  Display '{disp}' might not be accessible. If this fails, try --virtual.", file=sys.stderr)

    print("Setting up PulseAudio null sink …")
    audio_src = setup_null_sink(sink)

    if hw == "auto":
        hw_sel = detect_hwaccel_auto()
    else:
        hw_sel = hw

    gop_frames = max(1, int(fps * gop_s))
    cmd = build_ffmpeg_cmd(audio_src, fps, res, disp, fflog, hw_sel, gop_frames, port)

    print("Starting FFmpeg …")
    global ffmpeg_proc
    ffmpeg_proc = subprocess.Popen(cmd)
    time.sleep(1)

    lip = local_ip_for(host, cport)
    stream_url = f"http://{lip}:{port}/"
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