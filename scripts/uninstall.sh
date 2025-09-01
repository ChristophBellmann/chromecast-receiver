#!/usr/bin/env bash
set -euo pipefail
rm -rf ~/.local/share/chromecast-receiver
rm -f  ~/.local/bin/chromecast-streamer
rm -f  ~/.local/share/applications/chromecast-streamer.desktop
rm -f  ~/.config/autostart/chromecast-streamer.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
echo "✅ Uninstall complete."
