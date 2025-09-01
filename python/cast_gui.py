#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, subprocess, threading, queue, signal
import tkinter as tk
from tkinter import ttk, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
CAST_STREAM = os.path.join(HERE, "cast_stream.py")

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Chromecast Streamer")
        self.geometry("760x520")
        self.proc = None
        self.q = queue.Queue()
        self.create_widgets()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def create_widgets(self):
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="x")

        # Row 1
        self.mode = tk.StringVar(value="direct")
        self.appid = tk.StringVar(value="22B2DA66")
        ttk.Label(frm, text="Mode:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(frm, textvariable=self.mode, values=["direct","wait"], width=10, state="readonly").grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(frm, text="App-ID:").grid(row=0, column=2, sticky="w", padx=(16,0))
        ttk.Entry(frm, textvariable=self.appid, width=16).grid(row=0, column=3, sticky="w")

        # Row 2
        self.name = tk.StringVar()
        self.ip = tk.StringVar()
        ttk.Label(frm, text="Device (Name enthält):").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(frm, textvariable=self.name, width=22).grid(row=1, column=1, sticky="w")
        ttk.Label(frm, text="oder IP:").grid(row=1, column=2, sticky="w")
        ttk.Entry(frm, textvariable=self.ip, width=16).grid(row=1, column=3, sticky="w")

        # Row 3
        self.res = tk.StringVar(value="1920x1080")
        self.fps = tk.IntVar(value=30)
        self.hw  = tk.StringVar(value="auto")
        ttk.Label(frm, text="Auflösung:").grid(row=2, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.res, width=12).grid(row=2, column=1, sticky="w")
        ttk.Label(frm, text="FPS:").grid(row=2, column=2, sticky="w")
        ttk.Entry(frm, textvariable=self.fps, width=6).grid(row=2, column=3, sticky="w")
        ttk.Label(frm, text="HW:").grid(row=2, column=4, sticky="w", padx=(16,0))
        ttk.Combobox(frm, textvariable=self.hw, values=["auto","vaapi","cuda","qsv","software"], width=10, state="readonly").grid(row=2, column=5, sticky="w")

        # Row 4
        self.port = tk.IntVar(value=8090)
        ttk.Label(frm, text="Port:").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Entry(frm, textvariable=self.port, width=8).grid(row=3, column=1, sticky="w")

        # Buttons
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=6, sticky="w", pady=(8,0))
        self.start_btn = ttk.Button(btns, text="Start", command=self.on_start)
        self.stop_btn  = ttk.Button(btns, text="Stop", command=self.on_stop, state="disabled")
        self.start_btn.grid(row=0, column=0, padx=(0,6))
        self.stop_btn.grid(row=0, column=1)

        # Log
        self.txt = tk.Text(self, height=20, wrap="word")
        self.txt.pack(fill="both", expand=True, padx=10, pady=10)
        self.txt.insert("end", "Bereit. Wähle Start, um den Stream (direct) zu starten.\n")

        # Periodically poll queue
        self.after(100, self.drain)

    def cmdline(self):
        args = [sys.executable, CAST_STREAM,
                "--mode", self.mode.get(),
                "--app-id", self.appid.get(),
                "--resolution", self.res.get(),
                "--fps", str(self.fps.get()),
                "--port", str(self.port.get()),
                "--hw", self.hw.get()]
        if self.name.get():
            args += ["--device", self.name.get()]
        if self.ip.get():
            args += ["--ip", self.ip.get()]
        return args

    def reader(self, pipe):
        for line in iter(pipe.readline, b""):
            try:
                self.q.put(line.decode(errors="replace"))
            except Exception:
                pass
        pipe.close()

    def on_start(self):
        if self.proc:
            return
        if not os.path.exists(CAST_STREAM):
            messagebox.showerror("Fehlt", f"{CAST_STREAM} nicht gefunden.")
            return
        args = self.cmdline()
        self.txt.insert("end", f"$ {' '.join(args)}\n"); self.txt.see("end")
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception as e:
            messagebox.showerror("Fehler", str(e)); self.proc=None; return
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        threading.Thread(target=self.reader, args=(self.proc.stdout,), daemon=True).start()

    def on_stop(self):
        if not self.proc: return
        try:
            self.proc.send_signal(signal.SIGINT)
        except Exception:
            self.proc.terminate()
        self.proc = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.txt.insert("end", "Gestoppt.\n"); self.txt.see("end")

    def drain(self):
        try:
            while True:
                line = self.q.get_nowait()
                self.txt.insert("end", line)
                self.txt.see("end")
        except queue.Empty:
            pass
        self.after(100, self.drain)

    def on_close(self):
        if self.proc:
            self.on_stop()
        self.destroy()

if __name__ == "__main__":
    try:
        import tkinter  # ensure available
    except Exception:
        print("Bitte 'python3-tk' installieren: sudo apt install python3-tk", file=sys.stderr)
        sys.exit(1)
    App().mainloop()
