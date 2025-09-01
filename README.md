# Chromecast Receiver & Desktop Streamer

Ein kleiner Baukasten zum direkten Desktop-Streaming auf einen Chromecast –
wahlweise **sofort** (direct) oder **erst nach Klick im Receiver-UI** (wait).

## Features

- Ein einziges Tool: `python/cast_stream.py`
- Zwei Modi:
  - `direct`: Stream startet sofort
  - `wait`: erst Receiver-UI (Intro/Buttons), Stream nach „Stream“-Klick
- Auto- oder manuelle Hardware-Encoder (vaapi, cuda, qsv, software)
- Frei konfigurierbar: Auflösung, FPS, Port, Display
- Sauberes Cleanup (FFmpeg, PulseAudio, Receiver-App)

## Voraussetzungen

- Linux mit PulseAudio/PipeWire (`pactl`)
- `ffmpeg`
- Python 3, `pychromecast`

Installation (user scope):

```bash
pip install --user pychromecast
