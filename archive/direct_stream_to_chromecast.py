#!/usr/bin/env python3
import os
import sys
import time
import socket
import signal
import subprocess
import re

import pychromecast
from pychromecast.error import UnsupportedNamespace

# ————— CONFIGURATION —————
PORT                   = 8090
FPS                    = 30
MOVIE_GOP              = FPS * 2      # keyframe every 2s
RESOLUTION             = "1920x1080"
DISPLAY                = os.environ.get("DISPLAY", ":0")
NULL_SINK_NAME         = "cast_sink"
CUSTOM_RECEIVER_APP_ID = "22B2DA66"   # ← your Custom Receiver App ID here
# ————————————————————————

ffmpeg_proc    = None
pa_module_idx  = None
original_sink  = None


def cleanup(signum=None, frame=None):
    print("\n🛑 Stopping everything…")
    if ffmpeg_proc and ffmpeg_proc.poll() is None:
        ffmpeg_proc.terminate()
        try:
            ffmpeg_proc.wait(5)
        except subprocess.TimeoutExpired:
            ffmpeg_proc.kill()
    if original_sink:
        subprocess.run(
            ["pactl", "set-default-sink", original_sink],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    if pa_module_idx:
        subprocess.run(
            ["pactl", "unload-module", pa_module_idx],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)


def detect_hardware_info():
    """Detect CPU model and GPU(s) via lspci."""
    cpu_model = None
    try:
        with open('/proc/cpuinfo') as f:
            for line in f:
                if line.startswith('model name'):
                    cpu_model = line.split(':',1)[1].strip()
                    break
    except Exception:
        pass

    gpu_info = []
    try:
        out = subprocess.run(
            ['lspci'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, check=True
        ).stdout
        for line in out.splitlines():
            if 'VGA' in line or '3D controller' in line:
                gpu_info.append(line.strip())
    except Exception:
        pass

    return cpu_model, gpu_info


def detect_hwaccel():
    """Return best hwaccel: 'vaapi', 'cuda', 'qsv', or None."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hwaccels"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, check=True
        ).stdout
        # skip header, collect each non-empty line
        lines = out.splitlines()[1:]
        methods = {l.strip() for l in lines if l.strip()}
    except Exception:
        return None

    # prioritize
    if "vaapi" in methods and os.path.exists("/dev/dri/renderD128"):
        return "vaapi"
    if any(m in methods for m in ("cuda", "nvenc")):
        return "cuda"
    if "qsv" in methods:
        return "qsv"
    return None


def get_default_sink():
    out = subprocess.run(
        ["pactl", "info"],
        stdout=subprocess.PIPE, text=True, check=True
    ).stdout
    for line in out.splitlines():
        if line.startswith("Default Sink:"):
            return line.split(":",1)[1].strip()
    return None


def setup_null_sink():
    """Create null sink and route all audio into it."""
    global pa_module_idx, original_sink
    original_sink = get_default_sink()
    res = subprocess.run([
        "pactl", "load-module", "module-null-sink",
        f"sink_name={NULL_SINK_NAME}",
        "sink_properties=device.description=ChromecastSink"
    ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
       text=True, check=True)
    pa_module_idx = res.stdout.strip()
    subprocess.run(
        ["pactl", "set-default-sink", NULL_SINK_NAME],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
    )
    return f"{NULL_SINK_NAME}.monitor"


def build_ffmpeg_cmd(audio_src, hwaccel):
    """Construct an FFmpeg commandline with optimal encoding settings."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "info",
        "-re",                     # realtime input
        "-thread_queue_size", "512",
        "-f", "x11grab", "-framerate", str(FPS),
        "-video_size", RESOLUTION, "-i", DISPLAY,
        "-thread_queue_size", "512",
        "-f", "pulse", "-i", audio_src,
    ]

    if hwaccel == "vaapi":
        cmd += [
            "-vaapi_device", "/dev/dri/renderD128",
            "-vf", "format=nv12,hwupload",
            "-c:v", "h264_vaapi", "-qp", "24",
        ]
    elif hwaccel == "cuda":
        cmd += [
            "-c:v", "h264_nvenc", "-preset", "p1", "-cq", "23",
        ]
    elif hwaccel == "qsv":
        cmd += [
            "-c:v", "h264_qsv", "-global_quality", "24",
        ]
    else:
        # software fallback, movie-quality
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast",
            "-tune", "film", "-crf", "18", "-pix_fmt", "yuv420p",
        ]

    cmd += [
        "-g", str(MOVIE_GOP), "-keyint_min", str(MOVIE_GOP),
        "-c:a", "aac", "-b:a", "192k",
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-listen", "1", f"http://0.0.0.0:{PORT}/"
    ]
    return cmd


def main():
    global ffmpeg_proc

    # 0) Hardware summary
    cpu, gpus = detect_hardware_info()
    print(f"🔧 CPU: {cpu or 'Unknown'}")
    print("🖥️  GPU(s):")
    for g in gpus or ["None detected"]:
        print("   -", g)

    # 1) Audio sink
    print("🔊 Setting up audio capture…")
    audio_src = setup_null_sink()
    print(f"⤷ Capturing from: {audio_src}")

    # kill stale FFmpeg
    subprocess.run(
        ["pkill", "-f", f"ffmpeg.*-listen.*{PORT}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    # 2) Detect hwaccel & start FFmpeg
    hw = detect_hwaccel()
    print(f"⚙️  Hardware acceleration: {hw or 'none (software)'}")
    ffmpeg_cmd = build_ffmpeg_cmd(audio_src, hw)
    print("▶️ Starting FFmpeg server…")
    ffmpeg_proc = subprocess.Popen(ffmpeg_cmd)
    time.sleep(1)

    # 3) Discover Chromecast
    print("🔍 Discovering Chromecast…")
    chromecasts, _ = pychromecast.get_chromecasts()
    if not chromecasts:
        print("⚠️ No Chromecast found. Exiting.")
        cleanup()
    cast = chromecasts[0]
    cast.wait()

    d    = cast.device
    host = getattr(cast, "host", None) or cast.socket_client.host
    port = getattr(cast, "port", None) or cast.socket_client.port
    print(f"✅ Found Chromecast: {d.friendly_name} @ {host}:{port}")

    # 4) Launch custom receiver
    if CUSTOM_RECEIVER_APP_ID:
        print(f"🚀 Launching custom receiver ({CUSTOM_RECEIVER_APP_ID})…")
        try:
            cast.start_app(CUSTOM_RECEIVER_APP_ID)
            time.sleep(5)
        except Exception as e:
            print("⚠️ Error launching custom receiver:", e)

    # 5) Play media
    mc = cast.media_controller
    try:
        mc.update_status()
    except UnsupportedNamespace:
        pass

    # compute local IP
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((host, port)); local_ip = s.getsockname()[0]; s.close()
    stream_url = f"http://{local_ip}:{PORT}/"
    print(f"📺 Casting → {stream_url}")
    mc.play_media(stream_url, "video/mp4")
    mc.block_until_active(timeout=10)
    print("🔴 Streaming… Press Ctrl+C to stop.")

    # 6) keep alive
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
