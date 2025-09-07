#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chromecast desktop streamer mit optionalem virtuellem Display (xvfb/xephyr)
und Latenz-Presets.

- virtual backends: xvfb (headless) oder xephyr (windowed)
- druckt "👉 Run apps in: DISPLAY=:N <app> &", sobald das virtuelle X bereit ist
- passt -video_size an tatsächliche DISPLAY-Größe an (xdpyinfo)
- Latenz: normal / low / ultra (wirkt auf FFmpeg-Muxer/Encoder-Flags & GOP)
- setzt Media stream_type=LIVE, damit der CC möglichst wenig puffert
- beendet ffmpeg/WM/X-Server sauber bei SIGINT/SIGTERM
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

def debug(msg): print(msg, flush=True)

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
    """Start Xvfb oder Xephyr. Returns (proc_x, proc_wm, display, actual_size)."""
    if display == "auto":
        display = pick_free_display()
    W,H = map(int, res.lower().split("x"))
    procs = []
    chosen = backend
    if backend == "auto":
        chosen = "xvfb" if which("Xvfb") else ("xephyr" if which("Xephyr") else None)
    if chosen is None:
        raise RuntimeError("Neither Xvfb nor Xephyr found. Install: sudo apt install xvfb xserver-xephyr")

    if chosen == "xvfb":
        if not which("Xvfb"):
            raise RuntimeError("Xvfb not found. Install: sudo apt install xvfb")
        args = ["Xvfb", display, "-screen", "0", f"{W}x{H}x24", "-nolisten","tcp","-noreset"]
        procs.append(subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      preexec_fn=os.setsid))
    else:
        if not which("Xephyr"):
            raise RuntimeError("Xephyr not found. Install: sudo apt install xserver-xephyr")
        args = ["Xephyr", display, "-screen", f"{W}x{H}", "-fullscreen", "-resizeable"]
        procs.append(subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      preexec_fn=os.setsid))

    # warte bis X-Socket existiert
    for _ in range(200):
        if os.path.exists(f"/tmp/.X11-unix/X{display[1:]}"):
            break
        time.sleep(0.02)

    actual = xdpy_size(display) or (W,H)

    wm_proc = None
    if start_wm:
        env = os.environ.copy(); env["DISPLAY"] = display
        if which("openbox"):
            wm_proc = subprocess.Popen(["openbox"], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       preexec_fn=os.setsid)
        else:
            print("⚠️  Openbox nicht gefunden (sudo apt install openbox). Starte ohne WM.")

    print(f"DISPLAY_READY={display}")
    print(f"👉 Run apps in: DISPLAY={display} <app> &")
    return procs[0], wm_proc, display, actual

# -------------- Latenz Presets --------------
def latency_flags(latency, hw_codec, gop_frames):
    """
    Liefert (extra_input_flags, extra_global_flags, extra_encoder_flags, gop_override)
    """
    # Input & global Flags wirken bei allen Codecs
    base_in = [
        "-thread_queue_size","1024",  # x11
    ]
    base_glob = []
    base_enc = []
    gop_override = None

    if latency == "normal":
        # robust, guter Kompromiss
        base_glob += ["-probesize","1M","-analyzeduration","1M"]
    elif latency == "low":
        # niedrige Latenz, kleine Puffer, weniger Szene-Analyse
        base_glob += ["-fflags","nobuffer", "-flags","+low_delay",
                      "-probesize","64k", "-analyzeduration","0",
                      "-flush_packets","1", "-sc_threshold","0",
                      "-use_wallclock_as_timestamps","1"]
        gop_override = max(1, gop_frames // 2)  # halbe GOP
    else:  # ultra
        base_glob += ["-fflags","nobuffer", "-flags","+low_delay",
                      "-probesize","32k", "-analyzeduration","0",
                      "-flush_packets","1", "-sc_threshold","0",
                      "-use_wallclock_as_timestamps","1"]
        gop_override = max(1, gop_frames // 3)  # sehr kurze GOP

    # Encoder-spezifische Feintuning
    if hw_codec == "libx264":
        if latency in ("low","ultra"):
            base_enc += ["-tune","zerolatency"]
    elif hw_codec == "h264_nvenc":
        # NVENC: Lookahead aus, Low-Latency-Tunes
        if latency == "low":
            base_enc += ["-tune","ll","-rc-lookahead","0"]
        elif latency == "ultra":
            base_enc += ["-tune","ull","-rc-lookahead","0"]
    elif hw_codec == "h264_vaapi":
        # B-Frames aus = niedrigere Latenz
        base_enc += ["-bf","0"]
    elif hw_codec == "h264_qsv":
        base_enc += ["-look_ahead","0"]

    return base_in, base_glob, base_enc, gop_override

# -------------- FFmpeg --------------
def build_ffmpeg_cmd(display, size, fps, hw, gop_s, port, loglevel, sink_name, latency):
    W,H = map(int, size.split("x"))
    gop_frames = max(int(fps*max(gop_s, 0.25)), 1)

    # Encoderwahl
    enc_name = None
    vf_pre  = []
    if   hw=="vaapi":
        enc_name = "h264_vaapi"
        vf_pre = ["-vaapi_device","/dev/dri/renderD128","-vf","format=nv12,hwupload"]
    elif hw=="cuda":
        enc_name = "h264_nvenc"
    elif hw=="qsv":
        enc_name = "h264_qsv"
    elif hw=="software":
        enc_name = "libx264"
    else:
        det = detect_hwaccel()
        if det=="vaapi":
            enc_name = "h264_vaapi"; vf_pre = ["-vaapi_device","/dev/dri/renderD128","-vf","format=nv12,hwupload"]
        elif det=="cuda":
            enc_name = "h264_nvenc"
        elif det=="qsv":
            enc_name = "h264_qsv"
        else:
            enc_name = "libx264"

    in_flags, glob_flags, enc_flags, gop_override = latency_flags(latency, enc_name, gop_frames)
    gop_use = gop_override or gop_frames

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", loglevel,
        # reduce frame duplication / buffering for live x11grab
        "-rtbufsize", "100M",
        # X11 (Video)
        # NOTE: do NOT use -re for live capture; it causes timing/dup issues
        *in_flags,
        "-f","x11grab","-framerate", str(fps),
        "-video_size", f"{W}x{H}", "-i", display,
        "-vsync", "0",
        # Pulse (Audio)
        "-thread_queue_size","1024",
        "-f","pulse","-i", f"{sink_name}.monitor",
        "-draw_mouse","1",
        *glob_flags,
        *vf_pre,
        "-c:v", enc_name,
    ]

    # Encoder Defaults
    if enc_name == "h264_vaapi":
        cmd += ["-qp","24"]
    elif enc_name == "h264_nvenc":
        cmd += ["-preset","p1","-cq","23"]
    elif enc_name == "h264_qsv":
        cmd += ["-global_quality","24"]
    else:  # libx264
        cmd += ["-preset","veryfast","-crf","18","-pix_fmt","yuv420p"]

    # Latenz-spezifische Encoderflags
    cmd += enc_flags

    cmd += [
        "-g", str(gop_use), "-keyint_min", str(gop_use),
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

def same_lan(a, b):
    """Sehr einfache Heuristik: /24 Vergleich."""
    try:
        a3 = ".".join(a.split(".")[:3])
        b3 = ".".join(b.split(".")[:3])
        return a3 == b3
    except Exception:
        return False

# -------------- Main --------------
FF_PROC = None
VIRT_PROC = None
WM_PROC = None
CAST_OBJ = None

def stop_everything():
    global FF_PROC, VIRT_PROC, WM_PROC, CAST_OBJ
    try:
        if CAST_OBJ:
            try:
                CAST_OBJ.media_controller.stop()
                time.sleep(0.4)
                CAST_OBJ.quit_app()
            except Exception:
                pass
            try:
                CAST_OBJ.disconnect()
            except Exception:
                pass
    except Exception:
        pass
    if FF_PROC and FF_PROC.poll() is None:
        try:
            os.killpg(os.getpgid(FF_PROC.pid), signal.SIGINT)
        except Exception:
            try: FF_PROC.terminate()
            except Exception: pass
        try: FF_PROC.wait(timeout=3)
        except Exception:
            try:
                os.killpg(os.getpgid(FF_PROC.pid), signal.SIGKILL)
            except Exception:
                try: FF_PROC.kill()
                except Exception: pass
    if WM_PROC and WM_PROC.poll() is None:
        try: os.killpg(os.getpgid(WM_PROC.pid), signal.SIGTERM)
        except Exception: 
            try: WM_PROC.terminate()
            except Exception: pass
    if VIRT_PROC and VIRT_PROC.poll() is None:
        try: os.killpg(os.getpgid(VIRT_PROC.pid), signal.SIGTERM)
        except Exception: 
            try: VIRT_PROC.terminate()
            except Exception: pass

def sig_handler(signum, frame):
    print(f"--- cleanup (signal {signum}) ---", flush=True)
    stop_everything()
    print("Receiver stopped & disconnected.", flush=True)
    print("--- cleanup done ---", flush=True)
    sys.exit(0)

def main():
    global FF_PROC, VIRT_PROC, WM_PROC, CAST_OBJ

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
    ap.add_argument("--latency", default="normal", choices=["normal","low","ultra"])
    ap.add_argument("--lan-only", action="store_true")
    # virtual
    ap.add_argument("--virtual", action="store_true")
    ap.add_argument("--virtual-res", default="3840x2160")
    ap.add_argument("--virtual-display", default="auto")
    ap.add_argument("--virtual-wm", action="store_true")
    ap.add_argument("--virtual-backend", default="auto", choices=["auto","xvfb","xephyr"])
    ap.add_argument("--save-config", action="store_true")
    args = ap.parse_args()

    # Signale
    signal.signal(signal.SIGINT,  sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    # setup audio
    print("Setting up PulseAudio null sink …")
    orig_sink, pa_idx, monitor_name = setup_null_sink(args.sink_name)

    virt_display = args.display
    used_size = args.resolution

    try:
        # virtuelles Display
        if args.virtual:
            print("Starting virtual display …")
            VIRT_PROC, WM_PROC, virt_display, actual = start_virtual(
                args.virtual_display, args.virtual_res, backend=args.virtual_backend, start_wm=args.virtual_wm)
            reqW,reqH = map(int, args.virtual_res.split("x"))
            actW,actH = actual
            if (actW,actH) != (reqW,reqH):
                print(f"⚠️  Adjusting capture size from {reqW}x{reqH} → {actW}x{actH} to fit display {virt_display}")
            used_size = f"{actW}x{actH}"

        # FFmpeg Server
        ff_cmd = build_ffmpeg_cmd(virt_display if args.virtual else args.display,
                                  used_size, args.fps, args.hw, args.gop_seconds,
                                  args.port, args.fflog, args.sink_name, args.latency)
        print("Starting FFmpeg …")
        print("$", shlex_join(ff_cmd))
        FF_PROC = subprocess.Popen(ff_cmd, preexec_fn=os.setsid)

        # Chromecast finden
        print("Discovering Chromecast …")
        CAST_OBJ = find_cast(args.device, args.ip)
        if not CAST_OBJ:
            print("⚠️  No Chromecast found.")
            raise SystemExit(2)

        host = getattr(CAST_OBJ, "host", None) or CAST_OBJ.socket_client.host
        cport = getattr(CAST_OBJ, "port", None) or CAST_OBJ.socket_client.port
        friendly = None
        try:
            friendly = getattr(CAST_OBJ, "name", None) or CAST_OBJ.device.friendly_name
        except Exception:
            friendly = "unknown"
        print(f"✓ Chromecast: {friendly} @ {host}:{cport}")

        # Receiver starten
        if args.app_id:
            print(f"Launching receiver app {args.app_id} …")
            try:
                CAST_OBJ.start_app(args.app_id)
                time.sleep(1.5)
                print("Receiver app launched.")
            except Exception as e:
                print("⚠️  Error launching app:", e)

        # lokale IP für URL
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, cport)); local_ip = s.getsockname()[0]; s.close()
        url = f"http://{local_ip}:{args.port}/"
        print("Stream URL:", url)

        # Pfadcheck (LAN-only Hinweis)
        print("\n--- Path Check (LAN vs. Internet) ---")
        print("Server-LAN-IP:", local_ip)
        print("Chromecast-IP:", host)
        if same_lan(local_ip, host):
            print("→ Chromecast verbindet wahrscheinlich direkt im LAN/WLAN.")
            ok_lan = True
        else:
            print("→ Achtung: Quelle/Empfänger wirken in unterschiedlichen Subnetzen/VPN.")
            ok_lan = False
        print("--- End Path Check ---\n")
        if args.lan_only and not ok_lan:
            print("LAN-only aktiv → beende, da Pfad nicht lokal ist.")
            raise SystemExit(5)

        # Media starten (LIVE)
        mc = CAST_OBJ.media_controller
        try: mc.update_status()
        except UnsupportedNamespace: pass

        mc.play_media(url, "video/mp4", stream_type="LIVE")
        mc.block_until_active(timeout=10)

        print("Streaming started. Press Ctrl+C to stop.")
        # Haupt-Loop (warten bis Signal)
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        pass
    finally:
        # Cleanup
        stop_everything()
        restore_sinks(orig_sink, pa_idx)

if __name__ == "__main__":
    main()

