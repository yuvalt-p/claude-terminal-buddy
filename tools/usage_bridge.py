#!/usr/bin/env python3
"""
Push Claude plan usage (the 5h and weekly percentages /status shows) to the
stick over USB serial, one JSON line a minute:  {"usage":{"s":42,"w":17,"r":134}}

Reads Claude Code's OAuth token from the macOS Keychain at runtime and sends
it only to api.anthropic.com. The usage endpoint is undocumented and may change.

Usage:
  python3 tools/usage_bridge.py [--port /dev/cu.usbserial-XXXX] [--once] [--dry-run]

  --dry-run  fetch and print the numbers, don't open the serial port
  --once     send one update and exit

Needs pyserial (not for --dry-run). Stop it before `pio run -t upload`.
"""
import argparse, glob, json, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone

URL = "https://api.anthropic.com/api/oauth/usage"
INTERVAL = 60


def token() -> str:
    raw = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True, text=True, check=True).stdout
    return json.loads(raw)["claudeAiOauth"]["accessToken"]


def fetch() -> dict:
    req = urllib.request.Request(URL, headers={
        "Authorization": f"Bearer {token()}",
        "anthropic-beta": "oauth-2025-04-20",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.load(r)
    five, week = d["five_hour"], d["seven_day"]
    reset_min = 0
    if five.get("resets_at"):
        end = datetime.fromisoformat(five["resets_at"].replace("Z", "+00:00"))
        reset_min = max(0, int((end - datetime.now(timezone.utc)).total_seconds() // 60))
    return {"s": round(five["utilization"]), "w": round(week["utilization"]), "r": reset_min}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    ser = None
    if not a.dry_run:
        import serial
        port = a.port or next(iter(glob.glob("/dev/cu.usbserial-*")), None)
        if not port:
            sys.exit("no /dev/cu.usbserial-* found; plug in the stick or pass --port")
        ser = serial.Serial(port, 115200, timeout=1)

    while True:
        try:
            u = fetch()
            line = json.dumps({"usage": u}, separators=(",", ":"))
            print(f"session {u['s']}%  week {u['w']}%  resets in {u['r']}m", flush=True)
            if ser:
                ser.write((line + "\n").encode())
        except urllib.error.HTTPError as e:
            msg = " (token expired: run `claude` to refresh login)" if e.code == 401 else ""
            print(f"usage fetch failed: HTTP {e.code}{msg}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"usage fetch failed: {e}", file=sys.stderr, flush=True)
        if a.once or a.dry_run:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
