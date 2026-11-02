"""Run the daemon at login: a LaunchAgent on macOS, a systemd user unit on Linux."""
import os
import shutil
import subprocess
import sys

from tabnap.config import HOME, LOG_FILE, STATE_DIR

LABEL = "io.github.tabnap"
PLIST = os.path.join(HOME, "Library/LaunchAgents", f"{LABEL}.plist")
UNIT = os.path.join(HOME, ".config/systemd/user/tabnap.service")
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def program():
    exe = shutil.which("tabnap")
    return [exe, "daemon"] if exe else [sys.executable, "-m", "tabnap", "daemon"]


def install():
    os.makedirs(STATE_DIR, exist_ok=True)
    if sys.platform == "darwin":
        args = "".join(f"<string>{a}</string>" for a in program())
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{PATH}</string></dict>
  <key>StandardOutPath</key><string>{LOG_FILE}</string>
  <key>StandardErrorPath</key><string>{LOG_FILE}</string>
</dict></plist>
"""
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        with open(PLIST, "w") as fh:
            fh.write(plist)
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, PLIST], check=True)
        return PLIST
    unit = f"""[Unit]
Description=tabnap: hibernate idle AI agent sessions in terminal tabs

[Service]
ExecStart={' '.join(program())}
Restart=always
RestartSec=15
Environment=PATH={PATH}
StandardOutput=append:{LOG_FILE}
StandardError=append:{LOG_FILE}

[Install]
WantedBy=default.target
"""
    os.makedirs(os.path.dirname(UNIT), exist_ok=True)
    with open(UNIT, "w") as fh:
        fh.write(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "tabnap.service"], check=True)
    return UNIT


def uninstall():
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        path = PLIST
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", "tabnap.service"], capture_output=True)
        path = UNIT
    if os.path.exists(path):
        os.remove(path)
    return path


def restart():
    if sys.platform == "darwin":
        r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    else:
        r = subprocess.run(["systemctl", "--user", "restart", "tabnap.service"], capture_output=True)
    return r.returncode == 0
