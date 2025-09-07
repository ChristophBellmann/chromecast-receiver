#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, subprocess, threading, queue, signal, configparser, shutil, re, shlex, time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

HERE = os.path.dirname(os.path.abspath(__file__))
CAST_STREAM = os.path.join(HERE, "cast_stream.py")
CONFIG_USER = os.path.expanduser("~/.config/chromecast-streamer/config.ini")
CONFIG_LOCAL = os.path.abspath(os.path.join(HERE, "..", "config.local.ini"))

# ----------------------------- Config IO -----------------------------
def load_cfg():
    cfg = configparser.ConfigParser()
    cfg.read([CONFIG_USER, CONFIG_LOCAL])
    c = cfg["cast"] if "cast" in cfg else {}
    s = cfg["stream"] if "stream" in cfg else {}
    v = cfg["virtual"] if "virtual" in cfg else {}
    n = cfg["net"] if "net" in cfg else {}
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
    def geti_bool(sec, key, default=False):
        try:
            return str(geti(sec, key, str(default))).lower() == "true"
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
        "latency":    geti(s, "latency", "normal"),
        # virtual
        "virt_enabled": geti(v, "enabled", "false").lower() == "true",
        "virt_res":     geti(v, "resolution", "3840x2160"),
        "virt_disp":    geti(v, "display", "auto"),
        "virt_wm":      geti(v, "wm", "true").lower() == "true",
        "virt_backend": geti(v, "backend", "auto"),
        # app launcher
        "app_choice":   geti(v, "app_choice", "none"),
        "app_arg":      geti(v, "app_arg", ""),
        # net
        "lan_only":     geti_bool(n, "lan_only", False),
        "vlc_web_auto": geti_bool(n, "vlc_web_auto", False),
        "vlc_web_port": geti_int(n, "vlc_web_port", 8080),
        "vlc_web_pass": geti(n, "vlc_web_pass", "cast"),
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
        "latency": d["latency"],
    }
    cfg["virtual"] = {
        "enabled": str(d["virt_enabled"]),
        "resolution": d["virt_res"],
        "display": d["virt_disp"],
        "wm": str(d["virt_wm"]),
        "backend": d["virt_backend"],
        "app_choice": d["app_choice"],
        "app_arg": d["app_arg"],
    }
    cfg["net"] = {
        "lan_only": str(d["lan_only"]),
        "vlc_web_auto": str(d["vlc_web_auto"]),
        "vlc_web_port": str(d["vlc_web_port"]),
        "vlc_web_pass": d["vlc_web_pass"],
    }
    with open(CONFIG_USER, "w") as f:
        cfg.write(f)

# ----------------------------- Helpers -----------------------------
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

def which(cmd): return shutil.which(cmd)

def has_flatpak(app_id=None):
    if not which("flatpak"): return False
    if not app_id: return True
    try:
        r = subprocess.run(["flatpak","info",app_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False

def has_snap(app_name):
    if not which("snap"): return False
    try:
        r = subprocess.run(["snap","list",app_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False

def resolve_firefox_cmd():
    if which("firefox"): return (["firefox"], None)
    if has_flatpak("org.mozilla.firefox"): return (["flatpak","run","org.mozilla.firefox"], None)
    if has_snap("firefox"): return (["snap","run","firefox"], None)
    return (None, "Firefox nicht gefunden. Installiere z.B.:\n  sudo apt install firefox\n  snap install firefox\n  flatpak install flathub org.mozilla.firefox")

def resolve_vlc_cmd():
    if which("vlc"): return (["vlc"], None)
    if has_flatpak("org.videolan.VLC"): return (["flatpak","run","org.videolan.VLC"], None)
    if has_snap("vlc"): return (["snap","run","vlc"], None)
    return (None, "VLC nicht gefunden. Installiere z.B.:\n  sudo apt install vlc\n  snap install flathub org.videolan.VLC")

# ----------------------------- GUI -----------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Chromecast Streamer")
        self.geometry("940x760")
        self.proc = None
        self.q = queue.Queue()
        self.runtime_virtual_display = None
        self._pending_restart = False
        self._pending_restart_reason = None
        self._is_stopping = False
        self._last_path_summary = None
        self._build_ui()
        self.after(120, self._drain)

    # ---------- UI ----------
    def _build_ui(self):
        d = load_cfg()
        frm = ttk.Frame(self, padding=10); frm.pack(fill="x")

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
        self.latency = tk.StringVar(value=d["latency"])
        # virtual
        self.virt_enabled = tk.BooleanVar(value=d["virt_enabled"])
        self.virt_res     = tk.StringVar(value=d["virt_res"])
        self.virt_disp    = tk.StringVar(value=d["virt_disp"])
        self.virt_wm      = tk.BooleanVar(value=d["virt_wm"])
        self.virt_backend = tk.StringVar(value=d["virt_backend"])
        # app launcher
        self.app_choice   = tk.StringVar(value=d["app_choice"])
        self.app_arg      = tk.StringVar(value=d["app_arg"])
        # net
        self.lan_only     = tk.BooleanVar(value=d["lan_only"])
        self.vlc_web_auto = tk.BooleanVar(value=d["vlc_web_auto"])
        self.vlc_web_port = tk.IntVar(value=d["vlc_web_port"])
        self.vlc_web_pass = tk.StringVar(value=d["vlc_web_pass"])

        r = 0
        ttk.Label(frm, text="Mode:").grid(row=r, column=0, sticky="w")
        ttk.Combobox(frm, textvariable=self.mode, values=["direct","wait"], width=10, state="readonly").grid(row=r, column=1, sticky="w", padx=6)
        ttk.Label(frm, text="App-ID:").grid(row=r, column=2, sticky="w", padx=(16,0))
        ttk.Entry(frm, textvariable=self.appid, width=16).grid(row=r, column=3, sticky="w")
        ttk.Label(frm, text="Namespace:").grid(row=r, column=4, sticky="w", padx=(16,0))
        ttk.Entry(frm, textvariable=self.ns, width=30).grid(row=r, column=5, sticky="w")

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
        ttk.Label(frm, text="Latenz:").grid(row=r, column=4, sticky="w")
        self.latency_cb = ttk.Combobox(frm, textvariable=self.latency, values=["normal","low","ultra"], width=10, state="readonly")
        self.latency_cb.grid(row=r, column=5, sticky="w")
        self.latency_cb.bind("<<ComboboxSelected>>", self._on_latency_changed)

        # Virtual display section
        vr = ttk.LabelFrame(self, text="Virtueller Monitor")
        vr.pack(fill="x", padx=10, pady=(8,8))
        ttk.Checkbutton(vr, text="Virtuellen Monitor verwenden", variable=self.virt_enabled, command=self._toggle_virtual).grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Label(vr, text="Backend:").grid(row=0, column=1, sticky="w")
        ttk.Combobox(vr, textvariable=self.virt_backend, values=["auto","xephyr","xvfb"], width=8, state="readonly").grid(row=0, column=2, sticky="w")
        ttk.Button(vr, text="4K-Preset", command=self._set_4k).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Label(vr, text="Virtuelles Display:").grid(row=1, column=0, sticky="w", padx=8)
        self.entry_vdisp = ttk.Entry(vr, textvariable=self.virt_disp, width=8)
        self.entry_vdisp.grid(row=1, column=1, sticky="w")
        ttk.Label(vr, text="Auflösung:").grid(row=1, column=2, sticky="w", padx=(16,0))
        ttk.Entry(vr, textvariable=self.virt_res, width=12).grid(row=1, column=3, sticky="w")
        ttk.Checkbutton(vr, text="Openbox starten (Fenster-Manager)", variable=self.virt_wm).grid(row=1, column=4, sticky="w", padx=(16,0))

        # App Launcher
        al = ttk.LabelFrame(self, text="App im virtuellen Monitor starten (optional)")
        al.pack(fill="x", padx=10, pady=(0,8))
        ttk.Label(al, text="App:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Combobox(al, textvariable=self.app_choice, values=["none","Firefox","VLC","Custom"], width=10, state="readonly").grid(row=0, column=1, sticky="w")
        ttk.Label(al, text="URL / Datei / Befehl:").grid(row=0, column=2, sticky="w", padx=(16,0))
        ttk.Entry(al, textvariable=self.app_arg, width=48).grid(row=0, column=3, sticky="w")
        ttk.Button(al, text="Datei…", command=self._pick_file).grid(row=0, column=4, sticky="w", padx=6)
        ttk.Button(al, text="App starten", command=self.on_launch_app).grid(row=0, column=5, sticky="w", padx=8)

        # Netzwerk & Autostart
        net = ttk.LabelFrame(self, text="Netzwerk & Autostart")
        net.pack(fill="x", padx=10, pady=(0,8))
        ttk.Checkbutton(net, text="LAN-only (abbrechen, wenn Pfad nicht lokales Netzwerk ist)", variable=self.lan_only).grid(row=0, column=0, sticky="w", padx=8, pady=4, columnspan=3)
        ttk.Checkbutton(net, text="VLC Web-Remote Autostart", variable=self.vlc_web_auto).grid(row=1, column=0, sticky="w", padx=8)
        ttk.Label(net, text="Port:").grid(row=1, column=1, sticky="e")
        ttk.Entry(net, textvariable=self.vlc_web_port, width=6).grid(row=1, column=2, sticky="w")
        ttk.Label(net, text="Passwort:").grid(row=1, column=3, sticky="e", padx=(12,0))
        ttk.Entry(net, textvariable=self.vlc_web_pass, width=12, show="•").grid(row=1, column=4, sticky="w")

        btns = ttk.Frame(self); btns.pack(fill="x", padx=10, pady=(0,8))
        self.start_btn = ttk.Button(btns, text="Start", command=self.on_start)
        self.stop_btn  = ttk.Button(btns, text="Stop", command=lambda: self.on_stop(reason="Stop-Button"), state="disabled")
        self.start_btn.pack(side="left"); self.stop_btn.pack(side="left", padx=(8,0))

        self.status = tk.StringVar(value="bereit")
        st = ttk.Frame(self); st.pack(fill="x", padx=10, pady=(0,4))
        ttk.Label(st, text="Status:").pack(side="left")
        self.status_lbl = ttk.Label(st, textvariable=self.status)
        self.status_lbl.pack(side="left")

        self.txt = tk.Text(self, height=18, wrap="word")
        self.txt.pack(fill="both", expand=True, padx=10, pady=10)
        self._log("Bereit. 'Start' beginnt den Stream (direct).")

        self._toggle_virtual()

    # ---------- Misc UI actions ----------
    def _set_4k(self):
        self.virt_res.set("3840x2160")

    def _set_status(self, s):
        self.status.set(s)

    def _maybe_status(self, s):
        """Nur Status setzen, wenn wir nicht gerade stoppen/neustarten."""
        if self._is_stopping or self._pending_restart:
            return
        self._set_status(s)

    def _log(self, s):
        self.txt.insert("end", s + ("\n" if not s.endswith("\n") else ""))
        self.txt.see("end")

    def _pick_file(self):
        path = filedialog.askopenfilename()
        if path:
            self.app_arg.set(path)

    def _toggle_virtual(self):
        use = self.virt_enabled.get()
        self.entry_disp.config(state=("disabled" if use else "normal"))
        for w in (self.entry_vdisp,):
            w.config(state=("normal" if use else "disabled"))

    # ---------- Process building ----------
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
                "--latency", self.latency.get(),
                "--save-config"]
        if self.name.get(): args += ["--device", self.name.get()]
        if self.ip.get():   args += ["--ip", self.ip.get()]
        if self.lan_only.get(): args += ["--lan-only"]
        if self.virt_enabled.get():
            args += ["--virtual", "--virtual-res", self.virt_res.get(),
                     "--virtual-display", self.virt_disp.get(),
                     "--virtual-backend", self.virt_backend.get()]
            if self.virt_wm.get():   args += ["--virtual-wm"]
        return args

    # ---------- Runtime parsing ----------
    def _parse_runtime(self, line: str):
        m = re.search(r"DISPLAY=(:\d+)", line)
        if m:
            self.runtime_virtual_display = m.group(1)

        s = line.strip()

        # feingranulare Status-Phasen
        if "Output #0, mp4" in s or "Press [q] to stop" in s:
            self._maybe_status("Encoder läuft …")
        elif "Discovering Chromecast" in s:
            self._maybe_status("suche Chromecast …")
        elif s.startswith("✓ Chromecast"):
            self._maybe_status("Chromecast gefunden …")
        elif "Launching receiver app" in s:
            self._maybe_status("Receiver-App starten …")
        elif "Receiver app launched" in s:
            self._maybe_status("Receiver bereit …")
        elif s.startswith("Stream URL:"):
            self._maybe_status("warte Chromecast-Verbindung …")
        elif s.startswith("--- Path Check"):
            self._maybe_status("prüfe Pfad …")

        # Path-Check-Zusammenfassung für (LAN)/(Internet)
        if "→" in s:
            self._last_path_summary = s

        if "Streaming started" in s:
            tag = ""
            if self._last_path_summary:
                txt = self._last_path_summary
                if "LAN/WLAN" in txt:
                    tag = " (LAN)"
                elif "Internet" in txt:
                    tag = " (Internet)"
            self._maybe_status("streamt" + tag)

        # Cleanup-Ende -> Status in _on_process_end
        if s.startswith("--- cleanup done"):
            return

        # während Stop: keine Statusänderung (Logs zeigen wir weiter an)
        if s.startswith("--- Path Check") and self._is_stopping:
            return

    # ---------- Queue-Drain ----------
    def _drain(self):
        try:
            while True:
                s = self.q.get_nowait()
                self._parse_runtime(s)
                self._log(s.rstrip("\n"))
        except queue.Empty:
            pass
        self.after(120, self._drain)

    # ---------- Reader/Watcher ----------
    def _reader(self, pipe):
        for line in iter(pipe.readline, ""):
            try:
                self.q.put(line)
            except Exception:
                pass
        try:
            pipe.close()
        except Exception:
            pass

    def _watch_proc(self, p):
        p.wait()
        self.after(0, self._on_process_end)

    def _on_process_end(self):
        self.proc = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        if not self._pending_restart:
            self._is_stopping = False
            self._set_status("bereit")
        self._log("Prozess beendet.")
        if self._pending_restart:
            why = self._pending_restart_reason or "Neustart"
            self._pending_restart = False
            self._pending_restart_reason = None
            self._set_status(f"Starte neu ({why}) …")
            self.after(600, self.on_start)

    # ---------- Start/Stop ----------
    def _save_all_cfg(self):
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
            "latency": self.latency.get(),
            "virt_enabled": self.virt_enabled.get(),
            "virt_res": self.virt_res.get(),
            "virt_disp": self.virt_disp.get(),
            "virt_wm": self.virt_wm.get(),
            "virt_backend": self.virt_backend.get(),
            "app_choice": self.app_choice.get(),
            "app_arg": self.app_arg.get(),
            "lan_only": self.lan_only.get(),
            "vlc_web_auto": self.vlc_web_auto.get(),
            "vlc_web_port": self.vlc_web_port.get(),
            "vlc_web_pass": self.vlc_web_pass.get(),
        })

    def on_start(self):
        if self.proc: return
        if not os.path.exists(CAST_STREAM):
            messagebox.showerror("Fehlt", f"{CAST_STREAM} nicht gefunden."); return

        if not self.virt_enabled.get() and not validate_display(self.disp.get()):
            if messagebox.askyesno("Display prüfen", f"Display '{self.disp.get()}' ist evtl. nicht erreichbar.\nTrotzdem starten (oder 'Virtuellen Monitor' aktivieren)?") is False:
                return

        self._save_all_cfg()

        self.runtime_virtual_display = None
        self._last_path_summary = None
        self._is_stopping = False

        args = self._cmdline()
        self._log("$ " + " ".join(args))
        try:
            self.proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                preexec_fn=os.setsid
            )
        except Exception as e:
            messagebox.showerror("Fehler", str(e)); self.proc=None; return
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._set_status("startet …")

        # kleiner Nudge: falls nach 2s noch "startet …", Fortschritt signalisieren
        def _nudge():
            if self.proc and self.status.get().startswith("startet"):
                self._maybe_status("initialisiert …")
        self.after(2000, _nudge)

        threading.Thread(target=self._reader, args=(self.proc.stdout,), daemon=True).start()
        threading.Thread(target=self._watch_proc, args=(self.proc,), daemon=True).start()

    def on_stop(self, reason="Stop"):
        if not self.proc: return
        self._is_stopping = True
        self._set_status("stoppt …")
        self._log(f"Stoppe Stream … ({reason})")
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGINT)
        except Exception:
            try: self.proc.terminate()
            except Exception: pass

        def _ensure_dead():
            if self.proc and self.proc.poll() is None:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    try: self.proc.kill()
                    except Exception: pass
        self.after(1800, _ensure_dead)

    # ---------- Latency quick restart ----------
    def _on_latency_changed(self, _evt=None):
        self._save_all_cfg()
        if self.proc:
            self._quick_restart("Latenz geändert")
        else:
            self._set_status("Latenz übernommen (wirkt beim nächsten Start).")

    def _quick_restart(self, reason="Einstellungen geändert"):
        if self._pending_restart:
            return
        self._pending_restart = True
        self._pending_restart_reason = reason
        self._set_status(f"Neustart: {reason} …")
        self.on_stop(reason=reason)

    # ---------- App launcher ----------
    def on_launch_app(self):
        if not self.virt_enabled.get():
            messagebox.showinfo("Virtueller Monitor", "Bitte zuerst 'Virtuellen Monitor' aktivieren.")
            return
        disp = self.runtime_virtual_display or (self.virt_disp.get() if self.virt_disp.get().lower()!="auto" else None)
        if not disp:
            messagebox.showinfo("Warte auf Display", "Der virtuelle Monitor startet gerade. Starte den Stream oder warte, bis im Log die Zeile\n'👉 Run apps in: DISPLAY=:N' erscheint.")
            return

        choice = self.app_choice.get()
        arg = self.app_arg.get().strip()
        if choice == "none":
            messagebox.showinfo("App", "Bitte eine App auswählen.")
            return

        if choice == "Firefox":
            argv, hint = resolve_firefox_cmd()
            if not argv: messagebox.showerror("Firefox fehlt", hint); return
            if arg: argv = argv + [arg]
        elif choice == "VLC":
            argv, hint = resolve_vlc_cmd()
            if not argv: messagebox.showerror("VLC fehlt", hint); return
            if arg: argv = argv + [arg]
        else:
            if not arg:
                messagebox.showinfo("Custom", "Bitte Befehl eingeben, z.B. 'chromium https://example.com'")
                return
            argv = shlex.split(arg)

        env = os.environ.copy()
        env["DISPLAY"] = disp
        try:
            subprocess.Popen(argv, env=env)
            self._log(f"🚀 Gestartet auf {disp}: {' '.join(argv)}")
        except Exception as e:
            messagebox.showerror("Start fehlgeschlagen", str(e))

# ----------------------------- Main -----------------------------
if __name__ == "__main__":
    try:
        import tkinter  # noqa
    except Exception:
        print("Bitte 'python3-tk' installieren: sudo apt install python3-tk", file=sys.stderr); sys.exit(1)
    App().mainloop()

