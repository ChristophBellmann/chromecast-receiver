#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${HOME}/.local/share/chromecast-receiver"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"

attempt_repair() {
  echo "🩺 Attempting to repair dpkg/apt state …"
  sudo dpkg --configure -a || true
  sudo apt-get -f install -y || true
  sudo apt --fix-broken install -y || true
  # If prompts block due to config file questions, prefer maintainer version:
  sudo apt-get -o Dpkg::Options::="--force-confnew" --fix-broken install -y || true
}

# First pass: if audit reports issues, try to fix automatically
if sudo dpkg --audit | grep -q .; then
  echo "⚠️  dpkg reports an inconsistent state."
  attempt_repair
fi

# Second pass: if still inconsistent, print diagnostics and exit
if sudo dpkg --audit | grep -q .; then
  echo "❌ dpkg still inconsistent. Diagnostics:"
  sudo dpkg --audit || true
  echo
  echo "—— Packages with desired=install but not fully installed ——"
  dpkg -l | awk '$1 ~ /^i/ && $1 !~ /^ii/ {print $0}' || true
  echo
  echo "—— Half-configured / triggers-pending from status file ——"
  grep -n -B1 -A3 -E 'Status: .*half-|Status: .*triggers-' /var/lib/dpkg/status || true
  echo
  echo "Please resolve the above (or run scripts/dpkg-repair.sh) and try again."
  exit 1
fi

echo "➡️  Installing to ${TARGET_DIR} …"
mkdir -p "${TARGET_DIR}" "${BIN_DIR}" "${APP_DIR}"

echo "➡️  Installing system packages (sudo may ask for password) …"
sudo apt update
sudo apt install -y ffmpeg pulseaudio-utils python3-venv python3-tk rsync

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