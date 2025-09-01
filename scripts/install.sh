#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${HOME}/.local/share/chromecast-receiver"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"

# abbrechen, wenn dpkg/apt in schlechtem Zustand
if ! sudo dpkg --audit >/dev/null 2>&1; then
  echo "dpkg meldet einen inkonsistenten Zustand."
  echo "Bitte zuerst reparieren: sudo dpkg --configure -a && sudo apt-get -f install"
  exit 1
fi

echo "➡️  Installing to ${TARGET_DIR} …"
mkdir -p "${TARGET_DIR}" "${BIN_DIR}" "${APP_DIR}"

echo "➡️  Installing system packages (sudo may ask for password) …"
sudo apt update
sudo apt install -y ffmpeg pulseaudio-utils python3-venv python3-tk

echo "➡️  Copy project files …"
rsync -a --delete "${REPO_DIR}/" "${TARGET_DIR}/"

echo "➡️  Create virtualenv …"
python3 -m venv "${TARGET_DIR}/.venv"
"${TARGET_DIR}/.venv/bin/pip" install --upgrade pip wheel
"${TARGET_DIR}/.venv/bin/pip" install pychromecast

echo "➡️  Install launcher …"
cat > "${BIN_DIR}/chromecast-streamer" <<'EOF'
#!/usr/bin/env bash
APP_DIR="${HOME}/.local/share/chromecast-receiver"
exec "${APP_DIR}/.venv/bin/python" "${APP_DIR}/python/cast_gui.py"
EOF
chmod +x "${BIN_DIR}/chromecast-streamer"

echo "➡️  Desktop entry …"
cat > "${APP_DIR}/chromecast-streamer.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Chromecast Streamer
Comment=Stream your desktop directly to Chromecast
Exec=chromecast-streamer
Icon=display
Terminal=false
Categories=AudioVideo;Network;
EOF

echo "✅ Installation complete."
echo "• Start über Anwendungsmenü: 'Chromecast Streamer'"
echo "• Oder im Terminal: chromecast-streamer"
