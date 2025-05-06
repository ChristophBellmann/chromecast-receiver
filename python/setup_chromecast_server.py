#!/usr/bin/env python3
import subprocess
import re
import webbrowser
import os
import sys

# Default streaming script to update
DEFAULT_STREAM_SCRIPT = "stream_to_chromecast.py"

def discover_cast_ids():
    '''Use avahi-browse to find Chromecast device IDs (mDNS 'id' TXT records).'''
    try:
        subprocess.run(['avahi-browse', '--version'], check=True, stdout=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: avahi-browse not found. Please install it with:")
        print("    sudo apt update && sudo apt install avahi-utils")
        sys.exit(1)

    print("🔍 Discovering Chromecast devices on the network...")
    result = subprocess.run(
        ['avahi-browse', '-rt', '_googlecast._tcp'],
        stdout=subprocess.PIPE, text=True
    )
    txts = re.findall(r'txt = \[(.*?)\]', result.stdout, re.DOTALL)
    ids = set()
    for txt in txts:
        for m in re.findall(r'"id=([^"]+)"', txt):
            ids.add(m)
    return ids

def open_cast_console():
    url = "https://cast.google.com/publish"
    print(f"🌐 Opening Google Cast SDK Developer Console:\n   {url}")
    webbrowser.open(url)

def update_receiver_app_id(script_path, app_id):
    '''Replace or insert the CUSTOM_RECEIVER_APP_ID line in the given script.'''
    if not os.path.isfile(script_path):
        print(f"Error: Script '{script_path}' not found.")
        sys.exit(1)
    lines, replaced = [], False
    with open(script_path, 'r') as f:
        for line in f:
            if line.strip().startswith("CUSTOM_RECEIVER_APP_ID"):
                lines.append(f'CUSTOM_RECEIVER_APP_ID = "{app_id}"\n')
                replaced = True
            else:
                lines.append(line)
    if not replaced:
        # Insert at top
        lines.insert(0, f'CUSTOM_RECEIVER_APP_ID = "{app_id}"\n')
    with open(script_path, 'w') as f:
        f.writelines(lines)
    print(f"✅ Updated '{script_path}' with CUSTOM_RECEIVER_APP_ID = {app_id}")

def main():
    print("\n=== Chromecast Custom Receiver Setup ===\n")

    # Step 1: discover mDNS IDs
    ids = discover_cast_ids()
    if not ids:
        print("⚠️  No Chromecast devices found. Check power and network.")
        sys.exit(1)
    print("Found Cast device ID(s):")
    for device_id in ids:
        print("  -", device_id)
    input("\nCopy these IDs into 'Test devices' in the Cast SDK Console, then press Enter...")

    # Step 2: open console
    resp = input("Open Cast SDK Console now? (y/N): ").strip().lower()
    if resp == 'y':
        open_cast_console()
        input("Press Enter once you've created your Custom Receiver and whitelisted the device ID(s)...")

    # Step 3: get App ID
    app_id = input("Enter your Custom Receiver App ID: ").strip()
    if not app_id:
        print("⚠️  No App ID entered. Exiting.")
        sys.exit(1)

    # Step 4: update streaming script
    path = input(f"Path to your streaming script [{DEFAULT_STREAM_SCRIPT}]: ").strip() or DEFAULT_STREAM_SCRIPT
    update_receiver_app_id(path, app_id)

    print("\n🎉 Setup complete! Run your streaming script to cast full-screen without overlay.\n")

if __name__ == "__main__":
    main()
    