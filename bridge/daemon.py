#!/usr/bin/env python3
"""claude-buddy bridge: Claude Code hook events -> M5 buddy over BLE and USB serial.

Hooks (hook.py) send one JSON line per event to a unix socket. This daemon
keeps per-session state, turns it into the heartbeat snapshot the firmware
expects (see REFERENCE.md), and writes it to every connected transport.
"""
import asyncio
import glob
import json
import os
import sys
import time

SOCK = os.path.expanduser("~/.claude-buddy/bridge.sock")
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # host -> device (write)
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # device -> host (notify)
KEEPALIVE_S = 3          # firmware treats >30s of silence as disconnected
SESSION_TTL_S = 600      # forget sessions that have been silent this long
COMPLETE_HOLD_S = 3      # how long the celebrate flag stays on after a turn ends
FLASH_MIN_S = 8          # a finished turn flashes green only if it ran a tool or took this long
AGENT_TTL_S = 600        # forget subagents that produced no event for this long
MAX_AGENT_ENTRIES = 5
PENDING_DONE_S = 30      # keep a launched Agent call's name this long for SubagentStart
STOPPED_HOLD_S = 30      # ignore late tool events from an agent that just stopped
LOG = os.path.expanduser("~/.claude-buddy/bridge.log")

sessions = {}            # session_id -> {"running": bool, "last": float}
pending = {}             # prompt id -> {"tool", "hint", "fut"} waiting for A/B on the device
attention = {}           # session_id -> {"tool", "ts"}: question for the terminal, no A/B on device
prompt_seq = 0
entries = []             # newest first, "HH:MM text"
msg = "idle"
completed_until = 0.0
transports = []          # objects with .name, .ready, async send(bytes)
wake = None              # asyncio.Event: set when state changed


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"   # undocumented; what /status reads
USAGE_POLL_S = 60
USAGE_SEND_S = 15        # resend the cached value this often (firmware hides it after 5 min)
usage = None             # {"s": 5h %, "w": weekly %, "r": minutes to 5h reset}


def fetch_usage():
    """Plan usage percentages, as /status shows them. Blocking: run in a thread."""
    import subprocess
    import urllib.request
    from datetime import datetime, timezone
    raw = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True, text=True, check=True).stdout
    token = json.loads(raw)["claudeAiOauth"]["accessToken"]
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.load(r)
    five, week = d["five_hour"], d["seven_day"]
    reset_min = 0
    if five.get("resets_at"):
        end = datetime.fromisoformat(five["resets_at"].replace("Z", "+00:00"))
        reset_min = max(0, int((end - datetime.now(timezone.utc)).total_seconds() // 60))
    return {"s": round(five["utilization"]), "w": round(week["utilization"]), "r": reset_min}


async def usage_poller():
    global usage
    while True:
        try:
            usage = await asyncio.to_thread(fetch_usage)
            log("usage", usage)
            wake.set()
        except Exception as e:
            log("usage fetch failed", repr(e))
        await asyncio.sleep(USAGE_POLL_S)


def all_agents(now):
    """Live subagents across sessions, newest first. Prunes stale ones."""
    out = []
    for v in sessions.values():
        ag = v.get("agents", {})
        for aid in [a for a, x in ag.items() if now - x["ts"] > AGENT_TTL_S]:
            del ag[aid]
        pa = v.get("pending_agents", [])
        pa[:] = [p for p in pa if now - p["ts"] <= AGENT_TTL_S
                 and now - p.get("done", now) <= PENDING_DONE_S]
        out.extend(ag.values())
    out.sort(key=lambda a: a["start"], reverse=True)
    return out


def snapshot():
    now = time.time()
    for sid in [s for s, v in sessions.items() if now - v["last"] > SESSION_TTL_S]:
        del sessions[sid]
    for sid in [s for s, v in attention.items() if now - v["ts"] > SESSION_TTL_S]:
        del attention[sid]
    snap = {
        "total": max(len(sessions), len(pending) + len(attention)),
        "running": sum(1 for v in sessions.values() if v["running"]),
        "waiting": len(pending) + len(attention),
        "msg": msg[:23],
        "entries": entries[:5],
        "tokens_today": 0,
    }
    agents = all_agents(now)
    if agents:
        snap["msg"] = "agents running (%d)" % min(len(agents), 99)
        snap["entries"] = [a["name"] for a in agents[:MAX_AGENT_ENTRIES]]
        snap["running"] = max(snap["running"], 1)
        snap["total"] = max(snap["total"], 1)
        snap["agents"] = len(agents)
    if attention and not pending:
        snap["msg"] = "look at screen"
    if pending:
        pid, p = next(iter(pending.items()))
        snap["msg"] = ("approve: " + p["tool"])[:23]
        snap["prompt"] = {"id": pid, "tool": p["tool"][:19], "hint": p["hint"][:43]}
    if now < completed_until and not agents:
        snap["completed"] = True
    return snap


def add_entry(text):
    entries.insert(0, time.strftime("%H:%M ") + text[:80])
    del entries[8:]


def agent_name(typ, desc):
    typ = typ or "agent"
    return ("%s: %s" % (typ, desc) if desc else typ)[:60]


def take_pending(s, typ):
    """Pop the pending Agent tool call that best matches a subagent type, or None."""
    pa = s.setdefault("pending_agents", [])
    match = (next((p for p in pa if p["type"] == typ), None)
             or next((p for p in pa if not p["type"]), None)
             or (pa[0] if pa and not typ else None))
    if match:
        pa.remove(match)
    return match


def register_agent(s, aid, typ, now):
    """Add a subagent, or upgrade a bare-named one once its type/description is known."""
    ag = s.setdefault("agents", {})
    a = ag.get(aid)
    if a is not None and a["named"]:
        return a
    match = take_pending(s, typ)
    if match:
        name, bg, ref = agent_name(match["type"] or typ, match["desc"]), match["bg"], match["tool_use_id"]
    else:
        name, bg, ref = agent_name(typ, ""), False, ""
    named = bool(match or typ)
    if a is None:
        a = ag[aid] = {"name": name, "ts": now, "start": now, "bg": bg, "ref": ref, "named": named}
    elif named:
        a.update(name=name, bg=bg, ref=ref, named=True)
    return a


def handle_agent_event(s, ev, name, now):
    """Subagent lifecycle and subagent tool calls. Never touches msg/entries/prompts.

    Agents are removed only by SubagentStop, SessionEnd or the TTL.
    Returns True if the event was fully handled here.
    """
    aid = ev.get("agent_id")
    typ = ev.get("agent_type") or ""
    if name == "SubagentStart" and aid:
        s.get("stopped", {}).pop(aid, None)
        register_agent(s, aid, typ, now)
    elif name == "SubagentStop" and aid:
        s.get("agents", {}).pop(aid, None)
        stopped = s.setdefault("stopped", {})
        stopped[aid] = now
        for k in [k for k, t in stopped.items() if now - t > STOPPED_HOLD_S]:
            del stopped[k]
    elif aid and name in ("PreToolUse", "PostToolUse", "PermissionRequest"):
        if aid in s.get("agents", {}) or now - s.get("stopped", {}).get(aid, -1e9) > STOPPED_HOLD_S:
            register_agent(s, aid, typ, now)["ts"] = now  # refresh; auto-registers if unseen
    elif name in ("PreToolUse", "PostToolUse") and ev.get("tool") in ("Agent", "Task"):
        if name == "PreToolUse":
            s.setdefault("pending_agents", []).append({
                "type": ev.get("subagent_type") or "", "desc": ev.get("description") or "",
                "bg": bool(ev.get("run_in_background")), "tool_use_id": ev.get("tool_use_id") or "",
                "ts": now})
        else:
            # The call returns at launch, and SubagentStart may still follow, so keep the
            # record briefly for naming; all_agents() prunes it after PENDING_DONE_S.
            for p in s.get("pending_agents", []):
                if p["tool_use_id"] == (ev.get("tool_use_id") or None):
                    p["done"] = now
        return False
    else:
        return False
    return True


def handle_event(ev):
    global msg, completed_until
    name = ev.get("event", "")
    sid = ev.get("session_id") or "default"
    tool = ev.get("tool") or ""
    hint = ev.get("hint") or ""
    s = sessions.setdefault(sid, {"running": False, "last": 0.0})
    s["last"] = time.time()
    if handle_agent_event(s, ev, name, s["last"]):
        wake.set()
        return
    if name == "SessionEnd":
        s.pop("agents", None)
        s.pop("pending_agents", None)
    # Any of these means the tool ran or the turn moved on, so a prompt still showing
    # on the device was answered in the terminal.
    if name == "PostToolUse":
        cancel_pending(sid, tool, "tool ran")
        if attention.get(sid, {}).get("tool") == tool:
            del attention[sid]
    elif name in ("Stop", "UserPromptSubmit", "SessionEnd"):
        cancel_pending(sid, None, name)
        attention.pop(sid, None)
    elif name == "Attention":
        attention[sid] = {"tool": tool, "ts": time.time()}
        add_entry("question: " + (tool or "choice"))
    if name == "UserPromptSubmit":
        s["running"] = True
        s["start"], s["tools"] = time.time(), 0
        msg = "thinking..."
        add_entry("prompt: " + hint)
    elif name == "PreToolUse":
        s["running"] = True
        s["tools"] = s.get("tools", 0) + 1
        msg = tool or "working"
        add_entry((tool + " " + hint).strip())
    elif name == "PostToolUse":
        s["running"] = True
    elif name == "Stop":
        s["running"] = False
        msg = "done"
        took = time.time() - s.get("start", time.time())
        if s.get("tools", 0) > 0 or took >= FLASH_MIN_S:
            completed_until = time.time() + COMPLETE_HOLD_S
        add_entry("done")
    elif name == "SessionEnd":
        sessions.pop(sid, None)
        msg = "idle"
    elif name == "SessionStart":
        msg = "session started"
    wake.set()


def cancel_pending(sid, tool=None, why=""):
    """Drop prompts for this session that were answered somewhere other than the device."""
    for pid, p in list(pending.items()):
        tool_ok = tool is None or p["tool"] == tool
        if p["sid"] == sid and tool_ok and not p["fut"].done():
            log("prompt cancelled", pid, why)
            p["fut"].set_result("none")


async def ask_device(ev, reader):
    """Show a permission prompt on the M5 and wait for A (allow) / B (deny).

    Returns early with "none" if the prompt is answered in the terminal: the hook
    process goes away (socket EOF) or the session's next event arrives.
    """
    global prompt_seq
    # Only BLE carries the device's answer back; without it, defer to the terminal prompt.
    if not any(t.name == "ble" and t.ready for t in transports):
        log("prompt skipped: no ble link")
        return "none"
    if ev.get("agent_id"):
        log("prompt skipped: subagent", ev["agent_id"], ev.get("tool"))
        return "none"
    sid = ev.get("session_id") or "default"
    cancel_pending(sid, why="superseded")
    prompt_seq += 1
    pid = "req_%d_%d" % (int(time.time()) % 100000, prompt_seq)
    fut = asyncio.get_running_loop().create_future()
    pending[pid] = {"sid": sid, "tool": ev.get("tool") or "tool",
                    "hint": ev.get("hint") or "", "fut": fut,
                    "agent_id": ev.get("agent_id") or ""}
    log("prompt", pid, ev.get("tool"), ev.get("hint"), "agent_id=%s" % (ev.get("agent_id") or "-"))
    wake.set()
    gone = asyncio.ensure_future(reader.read(1))     # completes on EOF or stray data
    try:
        done, _ = await asyncio.wait({fut, gone}, timeout=float(ev.get("timeout") or 50),
                                     return_when=asyncio.FIRST_COMPLETED)
        if fut in done:
            return fut.result()
        if gone in done:
            log("prompt cancelled", pid, "hook closed")
        else:
            log("prompt timeout", pid)
        return "none"
    finally:
        gone.cancel()
        await asyncio.gather(gone, return_exceptions=True)
        pending.pop(pid, None)
        wake.set()


def on_device_line(line):
    """A JSON line from the M5 (BLE notify). Only permission decisions matter here."""
    try:
        d = json.loads(line)
    except ValueError:
        return
    if isinstance(d, dict) and d.get("cmd") == "permission":
        p = pending.get(d.get("id"))
        if p and not p["fut"].done():
            decision = "allow" if d.get("decision") == "once" else "deny"
            log("device decision", d.get("id"), decision)
            p["fut"].set_result(decision)


async def on_client(reader, writer):
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("event") == "PermissionRequest":
                decision = await ask_device(ev, reader)
                try:
                    writer.write((json.dumps({"decision": decision}) + "\n").encode())
                    await writer.drain()
                except (ConnectionError, OSError):
                    pass
                break            # one permission request per connection
            else:
                handle_event(ev)
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


class SerialTransport:
    name = "usb"

    def __init__(self):
        self.ser = None

    @property
    def ready(self):
        return self.ser is not None

    def _open(self):
        import serial
        ports = glob.glob("/dev/cu.usbserial-*")
        if not ports:
            return None
        s = serial.Serial()
        s.port, s.baudrate = ports[0], 115200
        s.dtr = False       # keep the ESP32 from resetting when we open the port
        s.rts = False
        s.open()
        return s

    async def send(self, data):
        if self.ser is None:
            try:
                self.ser = await asyncio.to_thread(self._open)
                if self.ser:
                    log("usb connected", self.ser.port)
            except Exception as e:
                log("usb open failed", e)
                return
        if self.ser is None:
            return
        try:
            await asyncio.to_thread(self.ser.write, data)
        except Exception as e:
            log("usb write failed", e)
            self.ser = None


class BleTransport:
    name = "ble"

    def __init__(self):
        self.client = None
        self.ready = False
        self._connecting = False

    async def run(self):
        from bleak import BleakClient, BleakScanner
        delay = 2
        while True:
            try:
                dev = await BleakScanner.find_device_by_filter(
                    lambda d, ad: (d.name or ad.local_name or "").startswith("Claude"),
                    timeout=8)
                if dev is None:
                    await asyncio.sleep(delay)
                    continue
                log("ble found", dev.name, dev.address)
                dropped = asyncio.Event()
                client = BleakClient(dev, disconnected_callback=lambda c: dropped.set())
                await client.connect()
                rxbuf = bytearray()

                def on_notify(_c, data):
                    rxbuf.extend(bytes(data))
                    while b"\n" in rxbuf:
                        raw, _, rest = bytes(rxbuf).partition(b"\n")
                        rxbuf[:] = rest
                        text = raw.decode("utf-8", "replace").strip()
                        if text:
                            log("ble rx", text[:200])
                            on_device_line(text)

                await client.start_notify(NUS_TX, on_notify)
                self.client, self.ready = client, True
                log("ble connected, mtu", getattr(client, "mtu_size", "?"))
                await client.write_gatt_char(NUS_RX, b'{"cmd":"status"}\n', response=False)
                wake.set()
                await dropped.wait()
            except Exception as e:
                log("ble error", repr(e))
            finally:
                self.client, self.ready = None, False
                log("ble disconnected")
            await asyncio.sleep(delay)

    async def send(self, data):
        c = self.client
        if not (self.ready and c):
            return
        step = max(20, (getattr(c, "mtu_size", 23) or 23) - 3)
        try:
            for i in range(0, len(data), step):
                await c.write_gatt_char(NUS_RX, data[i:i + step], response=False)
        except Exception as e:
            log("ble write failed", repr(e))


async def broadcaster():
    last_sent = 0.0
    sent_time_for = set()
    last_usage = 0.0
    while True:
        try:
            await asyncio.wait_for(wake.wait(), timeout=KEEPALIVE_S)
        except asyncio.TimeoutError:
            pass
        wake.clear()
        now = time.time()
        # the celebrate flag is one-shot on the device; send a clearing frame after the hold
        data = (json.dumps(snapshot()) + "\n").encode()
        log("tx", data.decode().strip())
        for t in transports:
            if t.name == "ble" and t.ready and id(t.client) not in sent_time_for:
                sent_time_for = {id(t.client)}
                off = -(time.altzone if time.localtime().tm_isdst > 0 else time.timezone)
                await t.send((json.dumps({"time": [int(now), off]}) + "\n").encode())
            await t.send(data)
        if usage and now - last_usage >= USAGE_SEND_S:
            last_usage = now
            udata = (json.dumps({"usage": usage}) + "\n").encode()
            for t in transports:
                await t.send(udata)
        last_sent = now
        if now < completed_until:
            await asyncio.sleep(max(0.1, completed_until - now + 0.05))
            wake.set()


async def main(use_ble, use_usb):
    global wake
    wake = asyncio.Event()
    os.makedirs(os.path.dirname(SOCK), exist_ok=True)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    await asyncio.start_unix_server(on_client, path=SOCK)
    os.chmod(SOCK, 0o600)
    tasks = [asyncio.create_task(broadcaster()), asyncio.create_task(usage_poller())]
    if use_usb:
        transports.append(SerialTransport())
    if use_ble:
        ble = BleTransport()
        transports.append(ble)
        tasks.append(asyncio.create_task(ble.run()))
    log("daemon up; ble=%s usb=%s" % (use_ble, use_usb))
    await asyncio.gather(*tasks)


def single_instance():
    """Exit if another daemon holds the lock (hooks may race to start one)."""
    import fcntl
    os.makedirs(os.path.dirname(SOCK), exist_ok=True)
    lock = open(os.path.join(os.path.dirname(SOCK), "daemon.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(0)
    return lock          # keep a reference so the lock lives as long as the process


if __name__ == "__main__":
    _lock = single_instance()
    flags = set(sys.argv[1:])
    only_ble, only_usb = "--ble" in flags, "--usb" in flags
    use_ble = only_ble or not only_usb
    use_usb = only_usb or not only_ble
    try:
        asyncio.run(main(use_ble, use_usb))
    except KeyboardInterrupt:
        pass
