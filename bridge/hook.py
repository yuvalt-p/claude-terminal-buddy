#!/usr/bin/env python3
"""Claude Code hook -> claude-buddy daemon. Stdlib only, never fails the hook.

Status events are fire-and-forget. PermissionRequest blocks until you press A (allow)
or B (deny) on the M5; with no answer it prints nothing, so the normal terminal prompt
appears.
"""
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SOCK = os.path.expanduser("~/.claude-buddy/bridge.sock")
ASK_TIMEOUT_S = 50       # daemon gives up after this; the hook `timeout` in settings must be larger
DEBUG = os.path.expanduser("~/.claude-buddy/hook-debug.log")
AGENT_TOOLS = {"Agent", "Task"}                        # tools that spawn subagents
ATTENTION_ONLY = {"AskUserQuestion", "ExitPlanMode"}   # decided in the terminal, not with A/B


def hint_for(data):
    ti = data.get("tool_input") or {}
    if data.get("tool_name") in AGENT_TOOLS:
        t, d = ti.get("subagent_type"), ti.get("description")
        if isinstance(t, str) and t:
            return ("%s: %s" % (t, d) if isinstance(d, str) and d else t).replace("\n", " ")[:60]
    for k in ("command", "file_path", "pattern", "url", "description"):
        if isinstance(ti.get(k), str):
            return ti[k].replace("\n", " ")[:60]
    p = data.get("prompt")
    return p.replace("\n", " ")[:60] if isinstance(p, str) else ""


def connect(timeout):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    s.connect(SOCK)
    return s


AGENT = "com.yuval.claude-buddy"


def start_daemon():
    if os.path.exists(os.path.expanduser("~/Library/LaunchAgents/%s.plist" % AGENT)):
        # launchd owns the daemon; kickstart is a no-op if it is already running
        subprocess.Popen(["launchctl", "kickstart", "gui/%d/%s" % (os.getuid(), AGENT)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return
    py = os.path.join(HERE, ".venv", "bin", "python")
    subprocess.Popen([py, os.path.join(HERE, "daemon.py"), "--ble"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def send(ev, wait=None):
    try:
        s = connect(0.5)
    except OSError:
        start_daemon()
        if wait is not None:
            return None          # don't make the user wait for a cold start; use the terminal prompt
        time.sleep(1.0)
        s = connect(0.5)
    try:
        s.sendall((json.dumps(ev) + "\n").encode())
        if wait is None:
            return None
        s.settimeout(wait)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.split(b"\n")[0])
    finally:
        s.close()


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    ev = {
        "event": data.get("hook_event_name", ""),
        "session_id": data.get("session_id", ""),
        "tool": data.get("tool_name", ""),
        "hint": hint_for(data),
    }
    for k in ("agent_id", "agent_type", "tool_use_id"):
        if isinstance(data.get(k), str) and data[k]:
            ev[k] = data[k]
    if ev["tool"] in AGENT_TOOLS:
        ti = data.get("tool_input") or {}
        for k in ("subagent_type", "description"):
            if isinstance(ti.get(k), str):
                ev[k] = ti[k].replace("\n", " ")[:60]
        ev["run_in_background"] = bool(ti.get("run_in_background"))
        tr = data.get("tool_response")
        if isinstance(tr, dict) and isinstance(tr.get("agentId"), str):
            ev["response_agent_id"] = tr["agentId"]
    if ev["event"] == "PermissionRequest" and ev["tool"] in ATTENTION_ONLY:
        # A choice among options, answered in the terminal: just get the user's attention.
        ev["event"] = "Attention"
        send(ev)
        return
    if ev["event"] != "PermissionRequest":
        send(ev)
        return
    try:
        with open(DEBUG, "a") as f:
            f.write(time.strftime("%H:%M:%S ") + json.dumps(data)[:600] + "\n")
    except OSError:
        pass
    ev["timeout"] = ASK_TIMEOUT_S
    reply = send(ev, wait=ASK_TIMEOUT_S + 3) or {}
    decision = reply.get("decision")
    if decision == "allow":
        out = {"behavior": "allow"}
    elif decision == "deny":
        out = {"behavior": "deny", "message": "Denied from the M5 buddy device"}
    else:
        return               # no answer: print nothing, terminal prompt takes over
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                             "decision": out}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
