# Chromecast Receiver & Desktop Streamer

> Streamt deinen Linux-Desktop **direkt** auf einen Chromecast – wahlweise sofort (**direct**) oder erst nach Klick im Receiver-UI (**wait**).

[![Developer Setup Guide](https://img.shields.io/badge/Docs-Developer%20Setup%20Guide-0A84FF?logo=google-chrome&logoColor=white)](./docs/DEV_SETUP.md)
![Status](https://img.shields.io/badge/OS-Pop!_OS%20%7C%20Ubuntu-blue)
![FFmpeg](https://img.shields.io/badge/FFmpeg-required-brightgreen)
![Python](https://img.shields.io/badge/Python-3.8%2B-informational)
![pychromecast](https://img.shields.io/badge/pychromecast-OK-success)

<img alt="splash" src="./splash-0.1.png" width="520">

---

## ✨ Features

- **Ein Tool** (`python/cast_stream.py`) für beide Modi:
  - **direct**: startet sofort (empfohlen)
  - **wait**: zeigt zuerst deine `receiver.html` (Intro/Buttons), Stream beginnt nach „Stream“-Klick
- **Auto-Encoder** (VAAPI / NVENC / QSV) mit Fallback auf Software (x264)
- **Volle Kontrolle via Flags** (Auflösung, FPS, Port, Gerät, Loglevel, …)
- **GUI inklusive** (`python/cast_gui.py`) – Start/Stop per Klick
- **Sauberes Cleanup**: Receiver beenden, FFmpeg stoppen, PulseAudio zurücksetzen

> 🔧 **Developer Setup Guide:** siehe [docs/DEV_SETUP.md](./docs/DEV_SETUP.md)

---

## 🗂 Projektstruktur

```
chromecast-receiver/
├─ LICENSE
├─ README.md
├─ receiver.html          # dein Custom Receiver (CAF)
├─ splash-0.1.png
├─ python/
│  ├─ cast_stream.py      # EIN Streaming-Tool (CLI)
│  └─ cast_gui.py         # kleines GUI (Start/Stop)
└─ scripts/
   ├─ install.sh          # Installer für Pop!_OS/Ubuntu
   └─ uninstall.sh        # Uninstaller (entfernt alles wieder)
```

---

## 🚀 Quick Start (Pop!_OS / Ubuntu)

```bash
cd ~/Dokumente/Entwicklung/chromecast-receiver
chmod +x scripts/install.sh
./scripts/install.sh
```

Danach:

- **Anwendungsmenü → „Chromecast Streamer“** (GUI), oder
- Terminal: `chromecast-streamer`

> Der Installer legt alles unter `~/.local/share/chromecast-receiver` ab, erstellt ein virtuelles Python-Env und einen Menüeintrag.

---

## 🖥️ GUI-Nutzung

1. **Chromecast** (optional) per Name (`--device`) oder IP (`--ip`) wählen  
2. Auflösung, FPS, HW-Encoder anpassen  
3. **Start** → Stream beginnt (Modus **direct**)  
4. **Stop** → beendet Stream & setzt Audio zurück

---

## 🧰 CLI-Nutzung (Modus: direct)

**Direkt losstreamen:**

```bash
python3 python/cast_stream.py --mode direct   --app-id 22B2DA66   --resolution 1920x1080 --fps 30 --gop-seconds 2   --hw auto --port 8090
```

**Chromecast wählen:**

```bash
# per Name (Substring)
python3 python/cast_stream.py --mode direct --device "Der Professor"

# per IP
python3 python/cast_stream.py --mode direct --ip 192.168.178.167
```

> **Hinweis:** `--app-id` ist deine **Custom Receiver App ID** aus der Cast Developer Console.

---

## 🕹️ CLI-Nutzung (Modus: wait)

Zeigt zuerst dein Receiver-UI auf dem TV; der Stream startet **erst nach Knopfdruck** in `receiver.html`:

```bash
python3 python/cast_stream.py --mode wait   --app-id 22B2DA66   --ns "urn:x-cast:com.example.stream"
```

**Receiver-Button-Event (wichtiger Fix):**

In `receiver.html` sollte der Stream-Button **broadcasten**:

```js
// statt bus.send({type:'start'})
bus.broadcast(JSON.stringify({ type: 'start' }));
```

---

## ⚙️ Wichtige Flags (Übersicht)

| Flag | Beschreibung | Standard |
|---|---|---|
| `--mode {direct,wait}` | Startverhalten | `direct` |
| `--app-id APPID` | Custom Receiver App ID | `22B2DA66` |
| `--ns NAMESPACE` | Namespace (nur `wait`) | `urn:x-cast:com.example.stream` |
| `--device NAME` | Chromecast per Name (Substring) | – |
| `--ip IP` | Chromecast per IP | – |
| `--resolution WxH` | Aufnahmegröße (z. B. `1920x1080`) | `1920x1080` |
| `--fps N` | Bilder pro Sekunde | `30` |
| `--gop-seconds SEC` | Keyframe-Intervall | `2.0` |
| `--display DISP` | X11-Display (x11grab) | `:0` |
| `--port PORT` | HTTP-Port für MP4-Stream | `8090` |
| `--hw {auto,vaapi,cuda,qsv,software}` | Encoder-Auswahl | `auto` |
| `--sink-name NAME` | PulseAudio-Sink | `cast_sink` |
| `--fflog LVL` | FFmpeg-Loglevel | `info` |

Beenden: **Ctrl+C** (CLI) bzw. **Stop** (GUI).  
Cleanup setzt Standard-Audio-Sink zurück, stoppt FFmpeg und schließt den Receiver.

---

## 🛠 Voraussetzungen

- **Linux** (getestet: Pop!_OS / Ubuntu)
- **PulseAudio/PipeWire** (mit `pactl`)
- **FFmpeg**
- **Python 3** + `pychromecast`  
  *(wird vom Installer im venv installiert)*

---

## ☁️ Custom Receiver einrichten (einmalig)

1. In der **Cast Developer Console** eine **Custom Receiver App** anlegen  
2. `receiver.html` per **HTTPS** hosten und als App-URL eintragen  
3. **Chromecast** im **Entwicklermodus** / unter **Devices** registrieren  
4. **App-ID** notieren und im Startbefehl (`--app-id`) verwenden

> Tipp: Wenn die HTML nicht lädt, zeigt der TV oft nur Backdrop. In dem Fall stimmen App-ID / Device-Whitelist / HTTPS-URL meist nicht.

---

## ❗️ Troubleshooting

- **Receiver-HTML lädt nicht**  
  - Chromecast **Dev-Modus** aktiv & Gerät in *Devices* registriert?  
  - App-Status **Published** oder **Draft (Tester erlaubt)**?  
  - App-URL **HTTPS** & im Browser erreichbar?
- **Kein Bild unter Wayland**  
  - `x11grab` braucht **X11/XWayland**. (Wayland-Screencast ist nicht Teil dieses Tools.)
- **Kein Audio**  
  - `pactl` verfügbar? PipeWire-Pulse/PulseAudio läuft?  
- **Firewall**  
  - Lokaler Port (`8090`) erreichbar?

---

## 🔧 Entwickeln

- **CLI**: `python3 python/cast_stream.py --help`  
- **GUI**: `python3 python/cast_gui.py`  
- Logs im GUI-Fenster; für CLI die Konsole.

---

## 🗑 Uninstall

Alles, was der Installer angelegt hat, wieder entfernen:

```bash
./scripts/uninstall.sh
```

oder manuell:

```bash
rm -rf ~/.local/share/chromecast-receiver
rm -f  ~/.local/bin/chromecast-streamer
rm -f  ~/.local/share/applications/chromecast-streamer.desktop
rm -f  ~/.config/autostart/chromecast-streamer.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
```

---

## 📄 Lizenz

Siehe [`LICENSE`](./LICENSE).
