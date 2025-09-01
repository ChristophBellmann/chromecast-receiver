# DEV_SETUP – Chromecast Custom Receiver & Casting

Dieser Leitfaden sammelt alle **Pflichtschritte rund um Fernseher & Google-Konto**, damit deine **Custom Receiver App** zuverlässig startet und mit dem Streaming-Tool funktioniert.

> Kurzfassung: **Entwicklermodus aktivieren → Gerät in der Cast Developer Console registrieren → Custom Receiver App anlegen (HTTPS) → App-ID im Tool verwenden → Accounts abgleichen.**

---

## ✅ Voraussetzungen

- Google Home App (Android/iOS) – zum Verwalten des Chromecasts
- **Cast Developer Console** Zugriff: <https://cast.google.com/publish>
- Gehostete `receiver.html` **per HTTPS** (ohne Login)
- Dein Linux-Rechner & Chromecast im **selben Netzwerk**

---

## 1) Entwicklermodus & Gerät registrieren

1. **Entwicklermodus am Chromecast aktivieren**  
   Google-Home-App → **Gerät** öffnen → **⚙️ Einstellungen** → **Lizenzinformationen** **7× antippen** → Meldung „Entwicklermodus aktiviert“.
2. **Gerät in der Cast Developer Console eintragen**  
   Console → **Devices** → *Add new device* → **Serien-Nr.** eintragen.  
   (Serien-Nr. findest du in der Home-App oder auf dem Gerät)
3. **Neustart empfohlen** (Strom kurz trennen) und ggf. 1–2 Minuten warten.

**Alternative (Device-IDs via mDNS ermitteln):**
```bash
avahi-browse -rt _googlecast._tcp | grep -o 'id=[0-9A-Fa-f-]\+' | sort -u
```
Diese IDs kannst du zusätzlich unter *Test devices* hinterlegen.

---

## 2) Custom Receiver App anlegen

1. **App erstellen** (Typ *Custom Receiver*)
2. **App-URL**: HTTPS-Adresse deiner `receiver.html` eintragen  
   - Keine Logins/Auth, öffentlich erreichbar  
   - Tipp bei Updates: `...?v=4.8` anhängen (Cache-Bypass)
3. **App-Status**: *Published* **oder** *Draft – Available to Testers*  
   - Bei Draft: **Manage testers** → deine Gmail-Adresse(n) hinzufügen
4. **App-ID notieren** (z. B. `22B2DA66`) → im Tool/GUI/CLI verwenden

---

## 3) Konten & „Home“ abgleichen

- Das Konto in der **Cast Developer Console** muss dasselbe sein wie
  - das Konto, mit dem du den **Chromecast** in Google Home verwaltest,
  - und (bei Draft-Apps) das Konto, mit dem du **castest**.
- Der Chromecast muss sich im **richtigen Home** befinden und du dort **Admin** sein.
- Bei mehreren Google-Konten: Konto-Mischmasch vermeiden (häufigste Fehlerquelle).

---

## 4) Receiver-HTML: Startsignal richtig senden

In deiner `receiver.html` muss der Button das Startsignal **broadcasten**, nicht `send` ohne `senderId`:

```js
// statt bus.send({type:'start'})
bus.broadcast(JSON.stringify({ type: 'start' }));
```

Achte darauf, dass **Namespace** in HTML und Tool identisch sind (Standard: `urn:x-cast:com.example.stream`).

---

## 5) Smoke-Tests (ohne Python)

1. **App-URL** im Browser öffnen → lädt die HTML? (HTTPS, keine 404/Mixed-Content)
2. **App-Start per Python-REPL** (nur als Schnelltest):
   ```python
   import pychromecast, time
   cc, _ = pychromecast.get_chromecasts()
   c = cc[0]; c.wait()
   c.start_app("22B2DA66"); time.sleep(3)
   print("running:", getattr(c, "app_id", None))
   ```
   Erwartung: `running: 22B2DA66`.  
   **Wenn stattdessen `CC1AD845`** (Default Media Receiver) → die Custom-App wurde verworfen → Whitelist/HTTPS/Account prüfen.

---

## 6) Nutzung mit diesem Projekt

### GUI
- App starten: **Chromecast Streamer** (Anwendungsmenü)
- Chromecast per Name (`--device`) oder IP (`--ip`) wählen
- Auflösung, FPS, Encoder einstellen → **Start**

### CLI (direct)
```bash
python3 python/cast_stream.py --mode direct   --app-id 22B2DA66   --device "Der Professor"   --resolution 1920x1080 --fps 30 --gop-seconds 2   --hw auto --port 8090
```

---

## 7) Troubleshooting (Schnellreferenz)

| Symptom | Ursache | Lösung |
|---|---|---|
| TV bleibt im **Backdrop**, keine HTML | App-ID/Whitelist/HTTPS nicht korrekt | Entwicklermodus & *Devices* prüfen; App-Status & Tester; App-URL testen |
| Direkt-Streaming geht, „wait“ nicht | Custom-App startet nicht | App-ID/Tester/Account prüfen; siehe Punkt 3 |
| `start_app returned: False` | Falsche App-ID oder App nicht verfügbar | App-ID checken; Tester freischalten |
| Button klickt, Python reagiert nicht | `send` statt `broadcast`; Namespace-Mismatch | In `receiver.html` `broadcast(JSON.stringify(...))`; Namespace angleichen |
| Nur Ton / kein Bild | Wayland ohne XWayland | XWayland aktivieren oder X-Session nutzen (Tool nutzt `x11grab`) |
| Kein Audio | PulseAudio/PipeWire nicht korrekt | `pactl` installieren; PipeWire-Pulse / PulseAudio prüfen |
| Stream startet, bricht ab | Firewall/NAT/Port blockiert | Lokalen Port (Default 8090) freigeben |

---

## 8) Nützliche Kommandos

Chromecast-IDs via mDNS:
```bash
avahi-browse -rt _googlecast._tcp | grep -o 'id=[0-9A-Fa-f-]\+' | sort -u
```

Aktive App prüfen (Python):
```python
import pychromecast, time
cc,_ = pychromecast.get_chromecasts()
c = cc[0]; c.wait()
print("app_id:", getattr(c,"app_id",None))
```

Lokale Stream-URL, die gesendet wird:
```
http://<DEINE-LOCAL-IP>:8090/
```

---

## 9) FAQ

**Warum lädt der Default Media Receiver (App-ID `CC1AD845`) statt meiner HTML?**  
→ Der Chromecast hat deine Custom-App verworfen: meist fehlender Entwicklermodus, fehlende Device-Whitelist, falsches Google-Konto oder HTTP statt HTTPS.

**Muss die App veröffentlicht sein?**  
→ Nein, *Draft – Available to Testers* reicht – sofern **du** oder eingetragene **Tester** casten.

**Brauche ich zwingend HTTPS?**  
→ Ja, für die App-URL in der Developer Console. Der lokale FFmpeg-Stream (`http://…:8090/`) ist davon getrennt.
