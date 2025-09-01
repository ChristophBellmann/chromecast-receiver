#!/usr/bin/env bash
# scripts/dpkg-repair.sh — Diagnose & Reparatur für dpkg/apt-Zustand
set -euo pipefail

echo "🔎 dpkg --audit"
sudo dpkg --audit || true
echo

echo "🔎 Pakete: desired=install, aber nicht 'ii' (fertig installiert)"
dpkg -l | awk '$1 ~ /^i/ && $1 !~ /^ii/ {print $0}' || true
echo

echo "🔎 half-configured / triggers aus /var/lib/dpkg/status"
grep -n -B1 -A3 -E 'Status: .*half-|Status: .*triggers-' /var/lib/dpkg/status || true
echo

echo "🛠  Reparaturversuche …"
sudo dpkg --configure -a || true
sudo apt-get -f install -y || true
sudo apt --fix-broken install -y || true
sudo apt-get -o Dpkg::Options::="--force-confnew" --fix-broken install -y || true
echo

echo "🔁 Nachkontrolle:"
sudo dpkg --audit || true
echo
echo "Wenn weiterhin Probleme bestehen, bitte die Ausgaben oben posten."