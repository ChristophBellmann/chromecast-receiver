#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chromecast desktop streamer with optional virtual display (xvfb/xephyr).

Highlights
- virtual backends: xvfb (headless) or xephyr (windowed)
- prints: "👉 Run apps in: DISPLAY=:N <app> &" once the virtual X is ready
- adjusts FFmpeg -video_size to the actual DISPLAY size (xdpyinfo)
- works with direct mode (auto start stream)
"""
import os, sys, time, socket, signal, subprocess, argparse, shutil, re
import pychromecast
from pychromecast.error import UnsupportedNamespace

# -------------- Defaults --------------
DEFAULT_APP_ID = "22B2DA66"
DEFAULT_NS     = "urn:x-cast:com.example.stream"

# -------------- Utils --------------
def shlex_join(parts):
    import shlex
    return " ".join(shlex.quote(p) for p in parts)

def run_ok(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)

def which(p): return shutil.which(p) is not None

# -------------- Audio (Pulse) --------------
def get_default_sink():
    try:
        out = run_ok(["pactl","info"]).stdout
        for line in out.splitlines():
            if line.startswith("Default Sink:"):
                return line.split(":",1)[1].strip()
    except Exception:
        pass
    return None

def setup_null_sink(name):
    original = get_default_sink()
    idx = run_ok(["pactl","load-module","module-null-sink",
                  f"sink_name={name}","sink_properties=device.description=ChromecastSink"]).stdout.strip()
    subprocess.run(["pactl","set-default-sink",name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return original, idx, f"{name}.monitor"

def restore_sinks(original, idx):
    try:
        if original: subprocess.run(["pactl","set-default-sink",original], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        if idx: subprocess.run(["pactl","unload-module",idx], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# -------------- HW accel --------------
def detect_hwaccel():
    try:
        out = run_ok(["ffmpeg","-hwaccels"]).stdout.splitlines()[1:]
        methods = {l.strip() for l in out if l.strip()}
    except Exception:
        methods = set()
    if "vaapi" in methods and os.path.exists("/dev/dri/renderD128"): return "vaapi"
    if any(m in methods for m in ("cuda","nvenc")): return "cuda"
    if "qsv" in methods: return "qsv"
    return None

# -------------- Virtual display --------------
def pick_free_display(start=2, end=98):
    for n in range(start, end):
        if not os.path.exists(f"/tmp/.X11-unix/X{n}"):
            return f":{n}"
    raise RuntimeError("No free X display found")

def xdpy_size(display):
    if not which("xdpyinfo"): return None
    try:
        out = run_ok(["xdpyinfo","-display",display]).stdout
        m = re.search(r"dimensions:\s+(\d+)x(\d+)\s+pixels", out)
        if m: return (int(m.group(1)), int(m.group(2)))
    except Exception:
        pass
    return None

def start_virtual(display, res, backend="auto", start_wm=False):
    """Start Xvfb or Xephyr. Returns (proc_x, proc_wm, display, actual_size)."""
    if display == "auto":
        display = pick_free_display()
    W,H = map(int, res.lower().split("x"))
    procs = []
    chosen = backend
    if backend == "auto":
        # prefer xvfb by default (headless, supports large res)
        chosen = "xvfb" if which("Xvfb") else ("xephyr" if which("Xephyr") else None)
    if chosen is None:
        raise RuntimeError("Neither Xvfb nor Xephyr found. Install: sudo apt install xvfb xserver-xephyr")

    if chosen == "xvfb":
        if not which("Xvfb"):
            raise RuntimeError("Xvfb not found. Install: sudo apt install xvfb")
        args = ["Xvfb", display, "-screen", "0", f"{W}x{H}x24", "-nolisten","tcp","-noreset"]
        procs.append(subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
    else:
        if not which("Xephyr"):
            raise RuntimeError("Xephyr not found. Install: sudo apt install xserver-xephyr")
        # windowed; try fullscreen to avoid WM decorations eating pixels
        args = ["Xephyr", display, "-screen", f"{W}x{H}", "-fullscreen", "-resizeable"]
        procs.append(subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT))

    # wait until socket exists
    for _ in range(200):
        if os.path.exists(f"/tmp/.X11-unix/X{display[1:]}"):
            break
        time.sleep(0.02)

    actual = xdpy_size(display) or (W,H)

    wm_proc = None
    if start_wm:
        # Start a simple WM (Openbox) so windows get managed on the virtual display
        env = os.environ.copy(); env["DISPLAY"] = display
        if which("openbox"):
            wm_proc = subprocess.Popen(["openbox"], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            print("⚠️  Openbox nicht gefunden (sudo apt install openbox). Starte ohne WM.")

    print(f"DISPLAY_READY={display}")
    print(f"👉 Run apps in: DISPLAY={display} <app> &")
    return procs[0], wm_proc, display, actual

# -------------- FFmpeg --------------
def build_ffmpeg_cmd(display, size, fps, hw, gop_s, port, loglevel, sink_name):
    W,H = map(int, size.split("x"))
    gop = max(int(fps*max(gop_s, 0.5)), 1)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", loglevel,
        "-re", "-thread_queue_size", "512",
        "-f","x11grab","-framerate", str(fps),
        "-video_size", f"{W}x{H}", "-i", display,
        "-thread_queue_size","512",
        "-f","pulse","-i", f"{sink_name}.monitor",
        "-draw_mouse","1"
    ]
    if   hw=="vaapi":
        cmd += ["-vaapi_device","/dev/dri/renderD128","-vf","format=nv12,hwupload","-c:v","h264_vaapi","-qp","24"]
    elif hw=="cuda":
        cmd += ["-c:v","h264_nvenc","-preset","p1","-cq","23"]
    elif hw=="qsv":
        cmd += ["-c:v","h264_qsv","-global_quality","24"]
    elif hw=="software" or hw is None:
        cmd += ["-c:v","libx264","-preset","veryfast","-tune","film","-crf","18","-pix_fmt","yuv420p"]
    else:
        # auto -> pick detected
        det = detect_hwaccel()
        if det=="vaapi":
            cmd += ["-vaapi_device","/dev/dri/renderD128","-vf","format=nv12,hwupload","-c:v","h264_vaapi","-qp","24"]
        elif det=="cuda":
            cmd += ["-c:v","h264_nvenc","-preset","p1","-cq","23"]
        elif det=="qsv":
            cmd += ["-c:v","h264_qsv","-global_quality","24"]
        else:
            cmd += ["-c:v","libx264","-preset","veryfast","-tune","film","-crf","18","-pix_fmt","yuv420p"]

    cmd += [
        "-g", str(gop), "-keyint_min", str(gop),
        "-c:a","aac","-b:a","192k",
        "-f","mp4","-movflags","frag_keyframe+empty_moov+default_base_moof",
        "-listen","1", f"http://0.0.0.0:{port}/"
    ]
    return cmd

# -------------- Chromecast --------------
def find_cast(name_contains=None, ip=None):
    if ip:
        try:
            cast = pychromecast.Chromecast(ip)
            cast.wait()
            return cast
        except Exception as e:
            print("⚠️  Could not connect to Chromecast at IP", ip, ":", e)
            # continue to discovery fallback
    casts, _ = pychromecast.get_chromecasts()
    if not casts:
        return None
    if name_contains:
        for c in casts:
            try:
                if name_contains.lower() in c.name.lower():
                    c.wait(); return c
            except Exception:
                pass
    c = casts[0]; c.wait(); return c

# -------------- Main --------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["direct","wait"], default="direct")
    ap.add_argument("--app-id", default=DEFAULT_APP_ID)
    ap.add_argument("--ns", default=DEFAULT_NS)
    ap.add_argument("--device", help="Chromecast name contains …")
    ap.add_argument("--ip", help="Chromecast IP")
    ap.add_argument("--resolution", default="1920x1080")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--hw", default="auto", choices=["auto","vaapi","cuda","qsv","software"])
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--display", default=os.environ.get("DISPLAY", ":0"))
    ap.add_argument("--gop-seconds", type=float, default=2.0)
    ap.add_argument("--fflog", default="info", choices=["quiet","error","warning","info","debug"])
    ap.add_argument("--sink-name", default="cast_sink")
    # virtual
    ap.add_argument("--virtual", action="store_true")
    ap.add_argument("--virtual-res", default="3840x2160")
    ap.add_argument("--virtual-display", default="auto")
    ap.add_argument("--virtual-wm", action="store_true")
    ap.add_argument("--virtual-backend", default="auto", choices=["auto","xvfb","xephyr"])
    ap.add_argument("--save-config", action="store_true")
    args = ap.parse_args()

    # setup audio
    print("Setting up PulseAudio null sink …")
    orig_sink, pa_idx, monitor_name = setup_null_sink(args.sink_name)

    # start virtual display if requested
    virt_proc = wm_proc = None
    used_display = args.display
    used_size = args.resolution

    try:
        if args.virtual:
            print("Starting virtual display …")
            virt_proc, wm_proc, used_display, actual = start_virtual(
                args.virtual_display, args.virtual_res, backend=args.virtual_backend, start_wm=args.virtual_wm)
            # adjust capture size if needed
            reqW,reqH = map(int, args.virtual_res.split("x"))
            actW,actH = actual
            if (actW,actH) != (reqW,reqH):
                print(f"⚠️  Adjusting capture size from {reqW}x{reqH} → {actW}x{actH} to fit display {used_display}")
            used_size = f"{actW}x{actH}"
            # use our virtual display
            used_display = used_display

        # start ffmpeg http server
        ff_cmd = build_ffmpeg_cmd(used_display, used_size, args.fps, args.hw, args.gop_seconds, args.port, args.fflog, args.sink_name)
        print("Starting FFmpeg …")
        print("$", shlex_join(ff_cmd))
        ff = subprocess.Popen(ff_cmd)
        time.sleep(1)

        # discover chromecast
        print("Discovering Chromecast …")
        cast = find_cast(args.device, args.ip)
        if not cast:
            print("⚠️  No Chromecast found.")
            raise SystemExit(2)
        host = getattr(cast, "host", None) or cast.socket_client.host
        cport = getattr(cast, "port", None) or cast.socket_client.port
        try:
            friendly = getattr(cast, "name", None) or cast.device.friendly_name
        except Exception:
            friendly = "unknown"

        print(f"✓ Chromecast: {friendly} @ {host}:{cport}")

        # start app (if provided)
        if args.app_id:
            print(f"Launching receiver app {args.app_id} …")
            try:
                cast.start_app(args.app_id)
                time.sleep(3)
                print("   start_app returned: None (ok)")
            except Exception as e:
                print("⚠️  Error launching app:", e)

        # compute local ip for stream url
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, cport)); local_ip = s.getsockname()[0]; s.close()
        url = f"http://{local_ip}:{args.port}/"
        print("Stream URL:", url)

        mc = cast.media_controller
        try: mc.update_status()
        except UnsupportedNamespace: pass
        mc.play_media(url, "video/mp4")
        mc.block_until_active(timeout=10)

        print("Streaming started. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        pass
    finally:
        if wm_proc and wm_proc.poll() is None:
            wm_proc.terminate()
        if virt_proc and virt_proc.poll() is None:
            virt_proc.terminate()
        restore_sinks(orig_sink, pa_idx)

if __name__ == "__main__":
    main()
