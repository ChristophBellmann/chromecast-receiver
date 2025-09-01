#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, subprocess, threading, queue, signal, configparser, shutil
import tkinter as tk
from tkinter import ttk, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
CAST_STREAM = os.path.join(HERE, "cast_stream.py")
CONFIG_USER = os.path.expanduser("~/.config/chromecast-streamer/config.ini")
CONFIG_LOCAL = os.path.abspath(os.path.join(HERE, "..", "config.local.ini"))

def load_cfg():
    cfg = configparser.ConfigParser()
    cfg.read([CONFIG_USER, CONFIG_LOCAL])
    c = cfg["cast"] if "cast" in cfg else {}
    s = cfg["stream"] if "stream" in cfg else {}
    v = cfg["virtual"] if "virtual" in cfg else {}
    getenv_disp = os.environ.get("DISPLAY", ":0")
    def geti(sec, key, default=""):
        try:
            return (sec.get(key) if hasattr(sec, "get") else default) or default
        except Exception:
            return default
    def geti_int(sec, key, default):
        try:
            return int(geti(sec, key, default))
        except Exception:
            return default
    def geti_float(sec, key, default):
        try:
            return float(geti(sec, key, default))
        except Exception:
            return default

    return {
        "mode":       geti(s, "mode", "direct"),
        "app_id":     geti(c, "app_id", "22B2DA66"),
        "namespace":  geti(c, "namespace", "urn:x-cast:com.example.stream"),
        "device":     geti(c, "device_name", ""),
        "ip":         geti(c, "device_ip", ""),
        "resolution": geti(s, "resolution", "1920x1080"),
        "fps":        geti_int(s, "fps", 30),
        "hw":         geti(s, "hw", "auto"),
        "port":       geti_int(s, "port", 8090),
        "display":    geti(s, "display", getenv_disp),
        "gop":        geti_float(s, "gop_seconds", 2.0),
        "fflog":      geti(s, "fflog", "info"),
        "sink":       geti(s, "sink_name", "cast_sink"),
        # virtual
        "virt_enabled": geti(v, "enabled", "false").lower() == "true",
        "virt_res":     geti(v, "resolution", "3840x2160"),
        "virt_disp":    geti(v, "display", "auto"),
        "virt_wm":      geti(v, "wm", "true").lower() == "true",
    }

def save_cfg(d):
    os.makedirs(os.path.dirname(CONFIG_USER), exist_ok=True)
    cfg = configparser.ConfigParser()
    cfg["cast"] = {
        "app_id": d["app_id"],
        "namespace": d["namespace"],
        "device_name": d["device"],
        "device_ip": d["ip"],
    }
    cfg["stream"] = {
        "resolution": d["resolution"],
        "fps": str(d["fps"]),
        "gop_seconds": str(d["gop"]),
        "port": str(d["port"]),
        "hw": d["hw"],
        "display": d["display"],
        "fflog": d["fflog"],
        "sink_name": d["sink"],
        "mode": d["mode"],
    }
    cfg["virtual"] = {
        "enabled": str(d["virt_enabled"]),
        "resolution": d["virt_res"],
        "display": d["virt_disp"],
        "wm": str(d["virt_wm"]),
    }
    with open(CONFIG_USER, "w") as f:
        cfg.write(f)

def validate_display(disp: str) -> bool:
    if not disp:
        return False
    if shutil.which("xdpyinfo") is None:
        return True
    try:
        r = subprocess.run(["xdpyinfo","-display",disp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        return r.returncode == 0
    except Exception:
        return False

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Chromecast Streamer")
        self.geometry("820x620")
        self.proc = None
        self.q = queue.Queue()
        self._build_ui()
        self.after(100, self._drain)

    def _build_ui(self):
        d = load_cfg()

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="x")

        self.mode = tk.StringVar(value=d["mode"])
        self.appid = tk.StringVar(value=d["app_id"])
        self.ns    = tk.StringVar(value=d["namespace"])
        self.name  = tk.StringVar(value=d["device"])
        self.ip    = tk.StringVar(value=d["ip"])
        self.res   = tk.StringVar(value=d["resolution"])
        self.fps   = tk.IntVar(value=d["fps"])
        self.hw    = tk.StringVar(value=d["hw"])
        self.port  = tk.IntVar(value=d["port"])
        self.disp  = tk.StringVar(value=d["display"])
        self.gop   = tk.DoubleVar(value=d["gop"])
        self.fflog = tk.StringVar(value=d["fflog"])
        self.sink  = tk.StringVar(value=d["sink"])
        # virtual
        self.virt_enabled = tk.BooleanVar(value=d["virt_enabled"])
        self.virt_res     = tk.StringVar(value=d["virt_res"])
        self.virt_disp    = tk.StringVar(value=d["virt_disp"])
        self.virt_wm      = tk.BooleanVar(value=d["virt_wm"])

        r = 0
        ttk.Label(frm, text="Mode:").grid(row=r, column=0, sticky="w")
        ttk.Combobox(frm, textvariable=self.mode, values=["direct","wait"], width=10, state="readonly").grid(row=r, column=1, sticky="w", padx=6)
        ttk.Label(frm, text="App-ID:").grid(row=r, column=2, sticky="w", padx=(16,0))
        ttk.Entry(frm, textvariable=self.appid, width=16).grid(row=r, column=3, sticky="w")
        ttk.Label(frm, text="Namespace:").grid(row=r, column=4, sticky="w", padx=(16,0))
        ttk.Entry(frm, textvariable=self.ns, width=28).grid(row=r, column=5, sticky="w")

        r += 1
        ttk.Label(frm, text="Device (Name enthält):").grid(row=r, column=0, sticky="w", pady=6)
        ttk.Entry(frm, textvariable=self.name, width=22).grid(row=r, column=1, sticky="w")
        ttk.Label(frm, text="oder IP:").grid(row=r, column=2, sticky="w")
        ttk.Entry(frm, textvariable=self.ip, width=16).grid(row=r, column=3, sticky="w")

        r += 1
        ttk.Label(frm, text="Auflösung:").grid(row=r, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.res, width=12).grid(row=r, column=1, sticky="w")
        ttk.Label(frm, text="FPS:").grid(row=r, column=2, sticky="w")
        ttk.Entry(frm, textvariable=self.fps, width=6).grid(row=r, column=3, sticky="w")
        ttk.Label(frm, text="HW:").grid(row=r, column=4, sticky="w", padx=(16,0))
        ttk.Combobox(frm, textvariable=self.hw, values=["auto","vaapi","cuda","qsv","software"], width=10, state="readonly").grid(row=r, column=5, sticky="w")

        r += 1
        ttk.Label(frm, text="Port:").grid(row=r, column=0, sticky="w", pady=6)
        ttk.Entry(frm, textvariable=self.port, width=8).grid(row=r, column=1, sticky="w")
        ttk.Label(frm, text="Display:").grid(row=r, column=2, sticky="w")
        self.entry_disp = ttk.Entry(frm, textvariable=self.disp, width=10)
        self.entry_disp.grid(row=r, column=3, sticky="w")
        ttk.Label(frm, text="GOP (s):").grid(row=r, column=4, sticky="w")
        ttk.Entry(frm, textvariable=self.gop, width=8).grid(row=r, column=5, sticky="w")

        r += 1
        ttk.Label(frm, text="FFmpeg-Log:").grid(row=r, column=0, sticky="w", pady=6)
        ttk.Combobox(frm, textvariable=self.fflog, values=["quiet","error","warning","info","debug"], width=10, state="readonly").grid(row=r, column=1, sticky="w")
        ttk.Label(frm, text="Sink-Name:").grid(row=r, column=2, sticky="w")
        ttk.Entry(frm, textvariable=self.sink, width=16).grid(row=r, column=3, sticky="w")

        # Virtual display section
        vr = ttk.LabelFrame(self, text="Virtueller Monitor (Xephyr)")
        vr.pack(fill="x", padx=10, pady=(0,8))
        ttk.Checkbutton(vr, text="Virtuellen Monitor verwenden", variable=self.virt_enabled, command=self._toggle_virtual).grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Label(vr, text="Virtuelles Display:").grid(row=1, column=0, sticky="w", padx=8)
        self.entry_vdisp = ttk.Entry(vr, textvariable=self.virt_disp, width=8)
        self.entry_vdisp.grid(row=1, column=1, sticky="w")
        ttk.Label(vr, text="Auflösung:").grid(row=1, column=2, sticky="w", padx=(16,0))
        ttk.Entry(vr, textvariable=self.virt_res, width=12).grid(row=1, column=3, sticky="w")
        ttk.Checkbutton(vr, text="Openbox starten (Fenster-Manager)", variable=self.virt_wm).grid(row=1, column=4, sticky="w", padx=(16,0))

        btns = ttk.Frame(self); btns.pack(fill="x", padx=10, pady=(0,8))
        self.start_btn = ttk.Button(btns, text="Start", command=self.on_start)
        self.stop_btn  = ttk.Button(btns, text="Stop", command=self.on_stop, state="disabled")
        self.start_btn.pack(side="left"); self.stop_btn.pack(side="left", padx=(8,0))

        self.txt = tk.Text(self, height=18, wrap="word")
        self.txt.pack(fill="both", expand=True, padx=10, pady=10)
        self.txt.insert("end", "Bereit. 'Start' beginnt den Stream (direct).")

        self._toggle_virtual()
        self.after(100, self._drain)

    def _toggle_virtual(self):
        use = self.virt_enabled.get()
        state = "disabled" if use else "normal"
        self.entry_disp.config(state=state)
        # virtual inputs enabled when using virt
        for w in (self.entry_vdisp,):
            w.config(state="normal" if use else "disabled")

    def _cmdline(self):
        args = [sys.executable, CAST_STREAM,
                "--mode", self.mode.get(),
                "--app-id", self.appid.get(),
                "--ns", self.ns.get(),
                "--resolution", self.res.get(),
                "--fps", str(self.fps.get()),
                "--port", str(self.port.get()),
                "--hw", self.hw.get(),
                "--display", self.disp.get(),
                "--gop-seconds", str(self.gop.get()),
                "--fflog", self.fflog.get(),
                "--sink-name", self.sink.get(),
                "--save-config"]
        if self.name.get(): args += ["--device", self.name.get()]
        if self.ip.get():   args += ["--ip", self.ip.get()]
        if self.virt_enabled.get():
            args += ["--virtual", "--virtual-res", self.virt_res.get()]
            if self.virt_disp.get(): args += ["--virtual-display", self.virt_disp.get()]
            if self.virt_wm.get():   args += ["--virtual-wm"]
        return args

    def _reader(self, pipe):
        for line in iter(pipe.readline, b""):
            try: self.q.put(line.decode(errors="replace"))
            except Exception: pass
        pipe.close()

    def on_start(self):
        if self.proc: return
        if not os.path.exists(CAST_STREAM):
            messagebox.showerror("Fehlt", f"{CAST_STREAM} nicht gefunden."); return

        # Basic checks
        if not self.virt_enabled.get() and not validate_display(self.disp.get()):
            if messagebox.askyesno("Display prüfen", f"Display '{self.disp.get()}' ist evtl. nicht erreichbar. Trotzdem starten (oder 'Virtuellen Monitor' aktivieren)?") is False:
                return

        # Save config first
        save_cfg({
            "mode": self.mode.get(),
            "app_id": self.appid.get(),
            "namespace": self.ns.get(),
            "device": self.name.get(),
            "ip": self.ip.get(),
            "resolution": self.res.get(),
            "fps": self.fps.get(),
            "hw": self.hw.get(),
            "port": self.port.get(),
            "display": self.disp.get(),
            "gop": self.gop.get(),
            "fflog": self.fflog.get(),
            "sink": self.sink.get(),
            "virt_enabled": self.virt_enabled.get(),
            "virt_res": self.virt_res.get(),
            "virt_disp": self.virt_disp.get(),
            "virt_wm": self.virt_wm.get(),
        })

        args = self._cmdline()
        self.txt.insert("end", "$ " + " ".join(args) + ""); self.txt.see("end")
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception as e:
            messagebox.showerror("Fehler", str(e)); self.proc=None; return
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        threading.Thread(target=self._reader, args=(self.proc.stdout,), daemon=True).start()

    def on_stop(self):
        if not self.proc: return
        try: self.proc.send_signal(signal.SIGINT)
        except Exception: self.proc.terminate()
        self.proc = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.txt.insert("end", "Gestoppt."); self.txt.see("end")

    def _drain(self):
        try:
            while True:
                line = self.q.get_nowait()
                self.txt.insert("end", line); self.txt.see("end")
        except queue.Empty:
            pass
        self.after(100, self._drain)

if __name__ == "__main__":
    try:
        import tkinter  # noqa: F401
    except Exception:
        print("Bitte 'python3-tk' installieren: sudo apt install python3-tk", file=sys.stderr); sys.exit(1)
    App().mainloop()