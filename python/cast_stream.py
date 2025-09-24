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
import threading
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
def pick_free_display(start=20, end=98):
    # avoid very low display numbers which xpra warns about
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
    # res: "WIDTHxHEIGHT", display: ":N" or "auto"
    W, H = [int(x) for x in str(res).split("x")]
    virt_proc = None
    attach_proc = None
    procs = []
    global XPRA_MANAGED_DISPLAY, XPRA_DAEMONIZED

    # remember whether user explicitly requested a display or asked for 'auto'
    requested_display = display
    # pick display if requested (only when 'auto' or empty)
    if not display or str(display).lower() == "auto":
        display = pick_free_display()

    # backend selection
    chosen = backend
    if backend == "auto":
        if which("xpra"):
            chosen = "xpra"
        elif which("Xephyr"):
            chosen = "xephyr"
        elif which("Xvfb"):
            chosen = "xvfb"
        else:
            chosen = None

    if chosen is None:
        raise RuntimeError("Kein virtuelles X Backend gefunden. Installiere xpra, xserver-xephyr oder xvfb.")

    # start chosen backend
    if chosen == "xvfb":
        if not which("Xvfb"):
            raise RuntimeError("Xvfb nicht gefunden. Installiere: sudo apt install xvfb")
        args = ["Xvfb", display, "-screen", "0", f"{W}x{H}x24", "-nolisten", "tcp", "-noreset"]
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
        virt_proc = p
        procs.append(p)
    elif chosen == "xephyr":
        if not which("Xephyr"):
            raise RuntimeError("Xephyr nicht gefunden. Installiere: sudo apt install xserver-xephyr")
        args = ["Xephyr", display, "-screen", f"{W}x{H}", "-resizeable", "-ac"]
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
        virt_proc = p
        procs.append(p)
    elif chosen == "xpra":
        if not which("xpra"):
            raise RuntimeError("xpra nicht gefunden. Installiere: sudo apt install xpra")
        # Start xpra server in daemon mode (let xpra manage its own process). We will poll
        # 'xpra list' to confirm the server is live, then attach a client to show a host window.
        # if user requested a very low display number (like :2) xpra warns and may conflict
        try:
            num = int(str(display).lstrip(":"))
            # Only auto-repick when user didn't explicitly request a display
            if num < 20 and (not requested_display or str(requested_display).lower() == "auto"):
                print(f"⚠️  Requested virtual display {display} is low; picking a safer free display instead.", flush=True)
                display = pick_free_display()
        except Exception:
            pass

        server_args = ["xpra", "start", display, "--daemon=yes"]
        # detect a terminal we can exec into the session later
        term = None
        for t in ("xterm","uxterm","x-terminal-emulator","lxterminal","gnome-terminal","konsole"):
            if which(t):
                term = t
                break
        # run xpra start as a short-lived command (daemonizes) and capture output
        try:
            res = subprocess.run(server_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            print(f"[xpra-server] exitcode={res.returncode}", flush=True)
            if res.stdout:
                for ln in res.stdout.splitlines():
                    print(f"[xpra-server] {ln}", flush=True)
            # If xpra reports that the display is already active, try once with a
            # different free display (helps when a stale X lock or previous server
            # holds the requested low-numbered display like :2).
            try_retry = False
            if res.stdout and 'Server is already active for display' in res.stdout:
                try_retry = True
            # also inspect per-session log later and set try_retry accordingly
            # print xpra per-session log (if present) to surface startup errors
            try:
                log_path = f"/run/user/{os.getuid()}/xpra/{display}.log"
                if os.path.exists(log_path):
                    print(f"[xpra-server] per-session log: {log_path}", flush=True)
                    with open(log_path, "r", errors="replace") as f:
                        lines = f.read().splitlines()[-200:]
                        for l in lines:
                            print(f"[xpra-server] LOG: {l}", flush=True)
                        # hint for common failure
                        if any("dbus-launch failed" in l or "dbus-launch --sh-syntax" in l for l in lines):
                            print("⚠️  xpra startup: dbus-launch failed in session log. Install 'dbus-x11' or ensure dbus-launch is available.", flush=True)
            except Exception:
                pass
            # Check if xpra registered the display
            try:
                xl = run_ok(["xpra","list"]).stdout
                print(f"[xpra-server] xpra list:\n{xl}", flush=True)
            except Exception:
                xl = ""
            if display in xl:
                # daemonized start succeeded
                XPRA_MANAGED_DISPLAY = display
                XPRA_DAEMONIZED = True
                # if we found a terminal candidate, try to exec it inside the xpra session
                if term:
                    try:
                        # Some xpra versions don't provide 'exec'. Start the terminal directly
                        # in the xpra-managed DISPLAY by setting DISPLAY in the env. This
                        # creates a regular X client inside the virtual display and avoids
                        # depending on a non-existent 'xpra exec' subcommand.
                        env = os.environ.copy()
                        env["DISPLAY"] = display
                        pterm = subprocess.Popen([term], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
                        procs.append(pterm)
                        print(f"[xpra-server] started {term} inside {display}", flush=True)
                    except Exception as e:
                        print(f"[xpra-server] failed to start {term} inside {display}: {e}", flush=True)
            else:
                # fallback: start xpra in foreground so we can see logs and keep it alive
                # If we detected the 'already active' message, attempt one retry on a
                # safer free display before falling back to foreground.
                if try_retry:
                    try:
                        old = display
                        display = pick_free_display()
                        print(f"[xpra-server] requested display {old} already active — retrying with {display}", flush=True)
                        server_args = ["xpra", "start", display, "--daemon=yes"]
                        res = subprocess.run(server_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                        print(f"[xpra-server] retry exitcode={res.returncode}", flush=True)
                        if res.stdout:
                            for ln in res.stdout.splitlines():
                                print(f"[xpra-server] {ln}", flush=True)
                        try:
                            xl = run_ok(["xpra","list"]).stdout
                            print(f"[xpra-server] xpra list after retry:\n{xl}", flush=True)
                        except Exception:
                            xl = ""
                        if display in xl:
                            XPRA_MANAGED_DISPLAY = display
                            XPRA_DAEMONIZED = True
                    except Exception:
                        pass

                fallback_args = ["xpra", "start", display, "--no-daemon"]
                print("[xpra-server] daemon start didn't register display, falling back to foreground start", flush=True)
                p = subprocess.Popen(fallback_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
                virt_proc = p
                procs.append(p)
                # forward server output
                def _forward_server_output2(pipe):
                    try:
                        for ln in iter(pipe.readline, b""):
                            if not ln:
                                break
                            try:
                                s = ln.decode("utf-8", errors="replace").rstrip()
                            except Exception:
                                s = str(ln)
                            print(f"[xpra-server] {s}", flush=True)
                    except Exception:
                        pass
                    try: pipe.close()
                    except Exception: pass
                threading.Thread(target=_forward_server_output2, args=(p.stdout,), daemon=True).start()
        except Exception as e:
            print("⚠️  Failed to start xpra server:", e, flush=True)
            raise
        # If xpra did not succeed in daemonizing and we have no running virt_proc,
        # fall back to Xephyr (more reliably creates a visible host window)
        if chosen == "xpra" and not XPRA_DAEMONIZED and (virt_proc is None or (virt_proc.poll() is not None)):
            if which("Xephyr"):
                print("[virtual] xpra failed to provide a live server — falling back to Xephyr.", flush=True)
                args = ["Xephyr", display, "-screen", f"{W}x{H}", "-resizeable", "-ac"]
                p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
                virt_proc = p
                procs.append(p)
            elif which("Xvfb"):
                print("[virtual] xpra failed — falling back to Xvfb.", flush=True)
                args = ["Xvfb", display, "-screen", "0", f"{W}x{H}x24", "-nolisten", "tcp", "-noreset"]
                p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
                virt_proc = p
                procs.append(p)
            else:
                print("⚠️  xpra failed and no Xephyr/Xvfb found to fallback to.", flush=True)
    else:
        raise RuntimeError(f"Unbekanntes virtuelles Backend: {chosen}")

    # Warte auf Display bereit (xdpyinfo bevorzugt). For xpra the unix socket may not exist,
    # so prefer xdpyinfo; fall back to assuming requested size after timeout.
    actual = None
    # wait up to 20s for display to be usable
    for _ in range(400):
        try:
            size = xdpy_size(display)
            if size:
                actual = size
                break
        except Exception:
            pass
        # fallback check for X socket (works for xvfb/xephyr)
        sock = f"/tmp/.X11-unix/X{display.lstrip(':')}"
        if os.path.exists(sock):
            actual = (W, H)
            break
        # For xpra-daemonized servers, check 'xpra list' for readiness
        try:
            if XPRA_DAEMONIZED:
                out = run_ok(["xpra","list"]).stdout
                if display in out:
                    # xpra server active; try xdpyinfo again next loop
                    pass
        except Exception:
            pass
        time.sleep(0.05)
    if actual is None:
        # timeout → nutze gewünschte Größe
        actual = (W, H)

    # optionaler Window-Manager (z.B. openbox) im virtuellen DISPLAY
    wm_proc = None
    if start_wm:
        if which("openbox"):
            env = os.environ.copy()
            env["DISPLAY"] = display
            wm_proc = subprocess.Popen(["openbox"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
        else:
            wm_proc = None

    # Try to locate an XAUTHORITY/Xauthority file xpra may have created so local
    # clients (ffmpeg, terminals) can authenticate. Common place is /run/user/<uid>/xpra
    xauth_path = None
    try:
        xpra_dir = f"/run/user/{os.getuid()}/xpra"
        if os.path.isdir(xpra_dir):
            for fn in os.listdir(xpra_dir):
                if fn.endswith('.log') or fn == 'run-xpra':
                    continue
                # pick files that reference the display name or look like an authority file
                if display.lstrip(':') in fn or 'Xauthority' in fn or fn.lower().endswith('.auth'):
                    cand = os.path.join(xpra_dir, fn)
                    if os.path.isfile(cand) and os.path.getsize(cand) > 0:
                        xauth_path = cand
                        break
    except Exception:
        xauth_path = None

    # If xpra was chosen, start the local attach client now (so it appears on the host X)
    if chosen == "xpra":
        try:
            host_env = os.environ.copy()
            # ensure the attach client opens windows on the host display
            if os.environ.get("DISPLAY"):
                host_env["DISPLAY"] = os.environ.get("DISPLAY")
            # If we discovered an XAUTH file, provide it to the attach client
            if xauth_path:
                host_env["XAUTHORITY"] = xauth_path
            attach_args = ["xpra", "attach", display]
            attach_proc = subprocess.Popen(attach_args, env=host_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
            procs.append(attach_proc)
            # Forward attach output for debugging (non-blocking thread)
            def _forward_attach_output(pipe):
                try:
                    for ln in iter(pipe.readline, b""):
                        if not ln:
                            break
                        try:
                            s = ln.decode("utf-8", errors="replace").rstrip()
                        except Exception:
                            s = str(ln)
                        print(f"[xpra-attach] {s}", flush=True)
                except Exception:
                    pass
                try: pipe.close()
                except Exception: pass
            threading.Thread(target=_forward_attach_output, args=(attach_proc.stdout,), daemon=True).start()
        except Exception as e:
            print("⚠️  xpra attach failed:", e, flush=True)
            attach_proc = None

    # Gib handles zurück: xpra kann zusätzlich einen attach-proc liefern.
    return virt_proc, attach_proc, wm_proc, display, actual, xauth_path

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
ATTACH_PROC = None
XPRA_MANAGED_DISPLAY = None
XPRA_DAEMONIZED = False

def stop_everything():
    global FF_PROC, VIRT_PROC, WM_PROC, CAST_OBJ
    global ATTACH_PROC
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
    if ATTACH_PROC and ATTACH_PROC.poll() is None:
        try: os.killpg(os.getpgid(ATTACH_PROC.pid), signal.SIGTERM)
        except Exception:
            try: ATTACH_PROC.terminate()
            except Exception: pass
    if VIRT_PROC and VIRT_PROC.poll() is None:
        try: os.killpg(os.getpgid(VIRT_PROC.pid), signal.SIGTERM)
        except Exception: 
            try: VIRT_PROC.terminate()
            except Exception: pass
    # If xpra was started in daemon mode, try to stop it explicitly
    try:
        if XPRA_DAEMONIZED and XPRA_MANAGED_DISPLAY:
            subprocess.run(["xpra","stop", XPRA_MANAGED_DISPLAY], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

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
    # default: prefer Xephyr (nested visible X) when present
    default_backend = "xephyr" if which("Xephyr") else "xpra" if which("xpra") else "auto"
    ap.add_argument("--virtual-backend", default=default_backend, choices=["auto","xvfb","xephyr","xpra"])
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
            VIRT_PROC, ATTACH_PROC, WM_PROC, virt_display, actual, xauth_path = start_virtual(
                args.virtual_display, args.virtual_res, backend=args.virtual_backend, start_wm=args.virtual_wm)
            reqW,reqH = map(int, args.virtual_res.split("x"))
            actW,actH = actual
            if (actW,actH) != (reqW,reqH):
                # If the virtual display reports a different size, don't automatically
                # increase the capture resolution beyond what the user requested —
                # that can cause excessive CPU/GPU and network load. Prefer to cap
                # the capture to the requested resolution.
                cappedW = min(actW, reqW)
                cappedH = min(actH, reqH)
                print(f"⚠️  Adjusting capture size from {reqW}x{reqH} → reported {actW}x{actH}; using capped {cappedW}x{cappedH} for capture on {virt_display}")
                used_size = f"{cappedW}x{cappedH}"
            else:
                used_size = f"{actW}x{actH}"

        # If xpra was requested, wait briefly for xpra to register the display so
        # the local attach client (host window) can connect. This avoids starting
        # FFmpeg before xpra is ready (which causes attach to fail and no host
        # window to appear). Timeout after ~15s and proceed.
        if args.virtual and args.virtual_backend in ("xpra", "auto") and which("xpra"):
            wait_display = virt_display
            waited = 0
            found = False
            while waited < 15:
                try:
                    out = run_ok(["xpra","list"]).stdout
                    if wait_display in out:
                        found = True
                        break
                except Exception:
                    pass
                time.sleep(0.5); waited += 0.5
            if found:
                print(f"[xpra] display {wait_display} registered after {waited:.1f}s", flush=True)
            else:
                print(f"⚠️  xpra display {wait_display} not registered after {waited:.1f}s — continuing startup", flush=True)

        # FFmpeg Server
        ff_cmd = build_ffmpeg_cmd(virt_display if args.virtual else args.display,
                                  used_size, args.fps, args.hw, args.gop_seconds,
                                  args.port, args.fflog, args.sink_name, args.latency)
        print("Starting FFmpeg …")
        print("$", shlex_join(ff_cmd))
        # Provide XAUTHORITY to FFmpeg and other spawned clients if xpra created one
        ff_env = os.environ.copy()
        if args.virtual and 'xauth_path' in locals() and xauth_path:
            ff_env['XAUTHORITY'] = xauth_path
        FF_PROC = subprocess.Popen(ff_cmd, env=ff_env, preexec_fn=os.setsid)

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

