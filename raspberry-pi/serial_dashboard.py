#!/usr/bin/env python3
"""
Smart City Operations Center - Python bridge (pastel bento dashboard)

Reads the Arduino Nano's serial output directly (no browser Web Serial
needed) and serves a live dashboard on port 8765 (reachable from any device on the same network)

Setup:
    pip install pyserial
    python serial_dashboard.py            # lists available ports
    python serial_dashboard.py COM5       # Windows example
    python serial_dashboard.py /dev/ttyUSB0   # Linux example
    python serial_dashboard.py /dev/cu.usbserial-1410  # Mac example

Then open http://<this-machine's-ip>:8765 from any device on the same network
(the script prints the exact address when it starts).
"""

import sys
import socket
import json
import re
import threading
import time
import queue
import urllib.request
import gzip
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("pyserial isn't installed. Run: pip install pyserial")
    sys.exit(1)

BAUD_RATE = 115200   # must match Serial.begin() in nano_sensor_node.ino
HTTP_PORT = 8765

# ---- shared state, updated by the serial reader thread ----
state_lock = threading.Lock()
state = {
    "connected": False,
    "lastUpdate": None,
    "sensors": {},       # Rain / Gas / Soil -> {aPin, raw, volt, dPin, digital}
}

# ---- Mountain NodeMCU (motion / vibration), posted over wifi to /mountain ----
MOUNTAIN_KEYS = ("motion", "vibration")
mountain = {"seen": None, "values": {k: False for k in MOUNTAIN_KEYS},
            "last": {k: None for k in MOUNTAIN_KEYS}, "light": None}


# ---- Online temperature (replaces the temperature sensor) ----
# The Pi asks Open-Meteo (free, no key) for the current temperature at this spot.
WEATHER_PLACE = "Thaliparamba"     # Thaliparamba, Kannur
WEATHER_LAT = 12.04
WEATHER_LON = 75.36
WEATHER_EVERY_SEC = 300      # ask every 5 minutes (retries after 30 s if it fails)
WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               "&current=temperature_2m&timezone=auto")
weather = {"tempC": None, "fetched": None}


# ---- commands to the nodes (traffic, display, seismic answers) ----
cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


# ---- Traffic signal NodeMCU (red / yellow / green + ultrasonic) ----
TRAFFIC_PORT = 8768   # the traffic NodeMCU listens for commands here
traffic = {"seen": None, "ip": None, "mode": "manual",
           "red": False, "yellow": False, "green": False, "cm": None}


# ---- Intruder alarm: motion AND vibration together for ALARM_BOTH_SEC ----
ALARM_BOTH_SEC = 0.5     # both sensors on this long -> alarm
ALARM_CLEAR_SEC = 30     # both sensors back to normal this long -> alarm stops by itself
alarm = {"active": False, "since": None, "both_since": None, "calm_since": None, "armed": True}

# ---- Seismic node (XIAO ESP32-C3): vibration pulses + ultrasonic movement, 5 times a second ----
SEA_MOVE_CM = 2.0         # water surface moved this much (cm, from its normal level) = the sea is moving
QUAKE_SHAKE_MIN = 50      # vibration shake level (0-100) at or above this = strong shaking
QUAKE_BOTH_SEC = 0.5      # sea moving and strong shaking both on together this long -> tsunami alert
QUAKE_WINDOW = 1.0        # nothing for this long = calm (used to stop the alert)
SEISMIC_ACK_PORT = 8770   # we answer the seismic node here, so it knows the Pi hears it
QUAKE_CLEAR_SEC = 30      # no movement and no shaking this long -> earthquake alert stops by itself
seismic = {"seen": None, "ip": None, "vib": False, "pulses": 0, "cm": None, "base": None, "move": 0.0,
           "intensity": 0, "shake_at": None, "move_at": None, "both": False, "ack_at": 0}
quake = {"active": False, "since": None, "both_since": None, "calm_since": None, "armed": True, "peak": 0}


# ---- Parking gate: RC522 NFC reader + servo, on the seismic NodeMCU ----
# The node opens the gate itself for a SAVED card (so it works even if the Pi is off); the Pi keeps
# the list of saved cards (gate_cards.json next to this script) and sends it to the node with every answer.
GATE_CARDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate_cards.json")
GATE_MAX_CARDS = 10        # the node keeps up to 10 cards
GATE_OPEN_SEC = 2          # (set on the node) a saved card opens the gate this long
GATE_PENDING_SEC = 120     # an unknown card waits this long in the "Allow?" box
RE_CARD_ID = re.compile(r"^[0-9A-F]{8,20}$")
gate = {"seen": None, "ip": None, "open": False, "nfc": False, "node_cards": 0,
        "last": None, "log": [], "seq": None, "opens": 0, "opens_day": None}
gate_cards = []            # [{"uid": "04A1B2C3", "name": "Card 1", "added": epoch}]


def load_gate_cards():
    global gate_cards
    try:
        with open(GATE_CARDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        gate_cards = [c for c in data if isinstance(c, dict) and RE_CARD_ID.match(str(c.get("uid", "")))][:GATE_MAX_CARDS]
        print(f"Parking gate: {len(gate_cards)} saved card(s)")
    except FileNotFoundError:
        gate_cards = []
    except (OSError, ValueError) as e:
        print(f"Could not read {GATE_CARDS_FILE}: {e}")
        gate_cards = []


def save_gate_cards():
    tmp = GATE_CARDS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(gate_cards, f, indent=1)
        os.replace(tmp, GATE_CARDS_FILE)
        return True
    except OSError as e:
        print(f"Could not save {GATE_CARDS_FILE}: {e}")
        return False


def card_name(uid):
    for c in gate_cards:
        if c["uid"] == uid:
            return c.get("name") or uid
    return None


def gate_ack():
    """Answer to the seismic node: "I hear you" + the saved cards."""
    return json.dumps({"ack": "seismic", "cards": ",".join(c["uid"] for c in gate_cards)},
                      separators=(",", ":")).encode("utf-8")


def gate_push():
    """Send the saved cards to the node right away (after Allow / Remove)."""
    ip = gate["ip"] or seismic["ip"]
    if not ip:
        return
    try:
        for _ in range(2):
            cmd_sock.sendto(gate_ack(), (ip, SEISMIC_ACK_PORT))
    except OSError:
        pass


def fmt_card(uid):
    return ":".join(uid[i:i + 2] for i in range(0, len(uid), 2))


def apply_gate_scan(data, addr):
    """A card touched the reader: {"gate":"scan","uid":"04A1B2C3","ok":1,"n":7} (sent twice, copy ignored)."""
    uid = str(data.get("uid", "")).upper()
    ok = data.get("ok")
    n = data.get("n")
    if not RE_CARD_ID.match(uid) or ok not in (0, 1, True, False) or isinstance(n, bool) or not isinstance(n, int):
        return False
    now = time.time()
    with state_lock:
        key = (addr[0], n)
        if gate["seq"] and gate["seq"][0] == key and now - gate["seq"][1] < 3:
            return True                             # the second copy
        gate["seq"] = (key, now)
        gate["ip"] = addr[0]
        name = card_name(uid)
        entry = {"uid": uid, "name": name, "ok": bool(ok), "t": now}
        gate["last"] = entry
        gate["log"].insert(0, entry)
        del gate["log"][12:]
        day = city_time(now)[:3]
        if gate["opens_day"] != day:
            gate.update(opens=0, opens_day=day)
        if ok:
            gate["opens"] += 1
        out_of_sync = not ok and name is not None   # saved here but the node doesn't know yet
    if ok:
        print(f"Parking gate opened by {name or uid}")
        hist_event("gate", "Parking gate opened · " + (name or fmt_card(uid)), "ok")
    elif out_of_sync:
        gate_push()
    else:
        print(f"Parking gate: unknown card {uid} denied")
        hist_event("gate", "Unknown card at the parking gate · " + fmt_card(uid), "warn")
    broadcast()
    return True


def gate_allow(uid):
    uid = str(uid or "").upper()
    if not RE_CARD_ID.match(uid):
        return False, "Bad card ID"
    with state_lock:
        if card_name(uid) is None:
            if len(gate_cards) >= GATE_MAX_CARDS:
                return False, "The gate holds %d cards · remove one first" % GATE_MAX_CARDS
            used = {c.get("name") for c in gate_cards}
            k = 1
            while "Card %d" % k in used:
                k += 1
            name = "Card %d" % k
            gate_cards.append({"uid": uid, "name": name, "added": round(time.time())})
            if not save_gate_cards():
                gate_cards.pop()
                return False, "Could not save the card on the Pi"
            for e in gate["log"]:
                if e["uid"] == uid:
                    e["name"] = name
        else:
            name = card_name(uid)
    gate_push()
    hist_event("gate", "Card saved for the parking gate · " + name, "info")
    broadcast()
    return True, None


def gate_remove(uid):
    uid = str(uid or "").upper()
    with state_lock:
        keep = [c for c in gate_cards if c["uid"] != uid]
        if len(keep) == len(gate_cards):
            return False, "That card is not saved"
        name = card_name(uid)
        old = list(gate_cards)
        gate_cards[:] = keep
        if not save_gate_cards():
            gate_cards[:] = old
            return False, "Could not save on the Pi"
        for e in gate["log"]:
            if e["uid"] == uid:
                e["name"] = None
    gate_push()
    hist_event("gate", "Card removed from the parking gate · " + name, "neutral")
    broadcast()
    return True, None


def gate_snapshot(now):
    """Call with state_lock held."""
    saved = {c["uid"] for c in gate_cards}
    last = gate["last"]
    pend = None
    for e in gate["log"]:                          # newest unknown card, if it's recent and still not saved
        if not e["ok"] and e["uid"] not in saved:
            if now - e["t"] <= GATE_PENDING_SEC:
                pend = {"uid": e["uid"], "agoSec": round(now - e["t"], 1)}
            break
    day = city_time(now)[:3]
    return {
        "ageSec": None if gate["seen"] is None else round(now - gate["seen"], 1),
        "open": gate["open"], "nfc": gate["nfc"], "nodeCards": gate["node_cards"],
        "openSec": GATE_OPEN_SEC, "max": GATE_MAX_CARDS,
        "cards": [{"uid": c["uid"], "name": c.get("name") or c["uid"]} for c in gate_cards],
        "last": None if last is None else {"uid": last["uid"], "name": card_name(last["uid"]), "ok": last["ok"],
                                           "agoSec": round(now - last["t"], 1)},
        "pending": pend,
        "log": [{"uid": e["uid"], "name": card_name(e["uid"]), "ok": e["ok"], "agoSec": round(now - e["t"], 1)}
                for e in gate["log"][:6]],
        "opens": gate["opens"] if gate["opens_day"] == day else 0,
    }


# ---- Status display NodeMCU (OLED, was the 8x8 matrix): says hello, we send it the city status every second ----
MATRIX_PORT = 8769
MATRIX_RAIN_WHEN = "LOW"   # rain module DO level that means "raining" (same as WET_WHEN on the page)
GAS_ALERT_PCT = 50         # gas level (analog, 0-100 %) at or above this = "gas detected" (same as on the page)
GAS_CLEAR_PCT = 45         # ...and back to "safe" only below this (no flicker at the edge)
_gas_on = False


def gas_detected(raw):
    """Gas alert from the analog level (higher reading = more gas), with a little hysteresis."""
    global _gas_on
    pct = raw / 1023 * 100
    if pct >= GAS_ALERT_PCT:
        _gas_on = True
    elif pct < GAS_CLEAR_PCT:
        _gas_on = False
    return _gas_on
matrix = {"seen": None, "ip": None, "screen": None}
MATRIX_SCREENS = ("wait", "alarm", "quake", "weather", "temp", "air")


def snapshot():
    """JSON snapshot of everything, with mountain ages worked out server-side
    (so the browser never has to compare its clock with the Pi's)."""
    with state_lock:
        snap = dict(state)
        now = time.time()
        if mountain["seen"] is None:
            snap["mountain"] = None
        else:
            snap["mountain"] = {
                "values": dict(mountain["values"]),
                "light": mountain["light"],          # 0-100 %, or None if no LDR
                "ageSec": round(now - mountain["seen"], 1),
                "agoSec": {k: (None if t is None else round(now - t, 1))
                           for k, t in mountain["last"].items()},
            }
        if weather["fetched"] is None:
            snap["weather"] = None
        else:
            snap["weather"] = {"tempC": weather["tempC"], "place": WEATHER_PLACE,
                               "ageSec": round(now - weather["fetched"], 1)}
        snap["alarm"] = {"active": alarm["active"],
                         "agoSec": round(now - alarm["since"], 1) if alarm["since"] else 0}
        snap["matrix"] = None if matrix["seen"] is None else {"ageSec": round(now - matrix["seen"], 1),
                                                                   "screen": matrix["screen"]}
        if seismic["seen"] is None:
            snap["seismic"] = None
        else:
            snap["seismic"] = {k: seismic[k] for k in ("vib", "pulses", "cm", "base", "move", "intensity")}
            snap["seismic"]["ageSec"] = round(now - seismic["seen"], 1)
        snap["gate"] = gate_snapshot(now)
        if system_info.get("t"):
            snap["system"] = {k: v for k, v in system_info.items() if k not in ("t", "started")}
            snap["system"]["ageSec"] = round(now - system_info["t"], 1)
            snap["system"]["runSec"] = int(now - system_info["started"])
        snap["quake"] = {"active": quake["active"], "peak": quake["peak"],
                         "agoSec": round(now - quake["since"], 1) if quake["since"] else 0}
        if traffic["seen"] is None:
            snap["traffic"] = None
        else:
            snap["traffic"] = {"mode": traffic["mode"], "red": traffic["red"],
                               "yellow": traffic["yellow"], "green": traffic["green"],
                               "cm": traffic["cm"], "ageSec": round(now - traffic["seen"], 1)}
    snap["density"] = density_snapshot(now)
    return json.dumps(snap, separators=(",", ":"))

# ---- Raspberry Pi system info for the dashboard (every 5 s) ----
import shutil
import subprocess
SYSTEM_EVERY_SEC = 5
system_info = {"t": None}


def _read(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except OSError:
        return ""


def read_system():
    info = {}
    t = _read("/sys/class/thermal/thermal_zone0/temp").strip()
    info["tempC"] = round(int(t) / 1000, 1) if t.isdigit() else None
    info["power"] = None                              # None = unknown, "ok", "low" (now), "was" (since boot)
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"0x([0-9a-fA-F]+)", out)
        if m:
            v = int(m.group(1), 16)
            info["power"] = "low" if v & 0x1 else "was" if v & 0x10000 else "ok"
            info["throttled"] = bool(v & 0x4)
    except (OSError, subprocess.SubprocessError):
        pass
    la = _read("/proc/loadavg").split()
    cores = os.cpu_count() or 1
    info["cpu"] = min(100, round(float(la[0]) / cores * 100)) if la else None
    mem = dict(re.findall(r"^(\w+):\s+(\d+)", _read("/proc/meminfo"), re.M))
    if "MemTotal" in mem and "MemAvailable" in mem:
        tot, av = int(mem["MemTotal"]), int(mem["MemAvailable"])
        info["mem"] = round((tot - av) / tot * 100)
        info["memMB"] = round(tot / 1024)
    up = _read("/proc/uptime").split()
    info["uptime"] = int(float(up[0])) if up else None
    ssid = ""
    try:
        ssid = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    if not ssid:
        try:
            out = subprocess.run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"], capture_output=True,
                                 text=True, timeout=4).stdout
            ssid = next((l[4:] for l in out.splitlines() if l.startswith("yes:")), "")
        except (OSError, subprocess.SubprocessError):
            pass
    info["ssid"] = ssid or None
    sig = None
    for line in _read("/proc/net/wireless").splitlines()[2:]:
        parts = line.split()
        if parts and parts[0].startswith("wlan"):
            try:
                q = float(parts[2].rstrip("."))
                sig = round(min(100, q / 70 * 100))
            except (ValueError, IndexError):
                pass
    info["signal"] = sig
    info["ip"] = get_lan_ip()
    try:
        du = shutil.disk_usage("/")
        info["diskFreeGB"] = round(du.free / 1e9, 1)
        info["diskPct"] = round(du.used / du.total * 100)
    except OSError:
        pass
    model = _read("/proc/device-tree/model").strip("\x00\n ")
    info["model"] = model.replace("Raspberry Pi", "Pi").replace(" Model ", " ").replace(" Rev ", " r") or None
    info["host"] = socket.gethostname()
    return info


def system_loop():
    while True:
        try:
            data = read_system()
            with state_lock:
                system_info.clear()
                system_info.update(data, t=time.time(), started=STARTED_AT)
        except Exception as e:
            print("System info error:", e)
        time.sleep(SYSTEM_EVERY_SEC)


STARTED_AT = time.time()


def restart_dashboard():
    """Restart this program (same arguments). Works under systemd and when started by hand."""
    print("Restarting the dashboard (asked from the page)")
    hist_event("system", "Dashboard restarted from the page")
    time.sleep(0.6)                                 # let the HTTP answer go out first
    sys.stdout.flush()
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except OSError:
        os._exit(1)                                 # systemd (Restart=always) brings it back


RE_ANALOG_DIGITAL = re.compile(
    r'^\[(\w+)\]\s+(\S+)\s+raw=(-?\d+)\s+volt=([\d.]+)V\s*\|\s*\[(\w+)\]\s+digital=(HIGH|LOW)$'
)

UDP_PORT = 8766   # the NodeMCU broadcasts its readings here


def apply_mountain(data):
    """Validate a reading from the NodeMCU (dict) and store it. True if accepted."""
    if not isinstance(data, dict):
        return False
    values = {}
    for k in MOUNTAIN_KEYS:
        v = data.get(k)
        if v not in (0, 1, True, False):
            return False
        values[k] = bool(v)
    light = data.get("light")                 # optional LDR reading, 0-100 %
    if light is not None:
        if isinstance(light, bool) or not isinstance(light, (int, float)) or not 0 <= light <= 100:
            return False
        light = round(float(light))
    now = time.time()
    with state_lock:
        mountain["seen"] = now
        if light is not None:
            mountain["light"] = light
        mountain["values"] = values
        for k, v in values.items():
            if v:
                mountain["last"][k] = now
    broadcast()
    return True


def alarm_tick():
    """Checks the mountain sensors and starts / stops the alarm. Runs 10 times a second."""
    now = time.time()
    changed = False
    with state_lock:
        online = mountain["seen"] is not None and now - mountain["seen"] < 6
        m, v = mountain["values"]["motion"], mountain["values"]["vibration"]
        both = online and m and v
        calm = online and not m and not v
        if not both:
            alarm["both_since"] = None
            alarm["armed"] = True            # after Silence, re-arm once they stop overlapping
        elif alarm["both_since"] is None:
            alarm["both_since"] = now
        if (not alarm["active"] and alarm["armed"] and both
                and now - alarm["both_since"] >= ALARM_BOTH_SEC):
            alarm.update(active=True, since=now, calm_since=None)
            changed = True
            print("ALARM: motion + vibration together")
        if alarm["active"]:
            if not calm:
                alarm["calm_since"] = None
            elif alarm["calm_since"] is None:
                alarm["calm_since"] = now
            elif now - alarm["calm_since"] >= ALARM_CLEAR_SEC:
                alarm.update(active=False, since=None, calm_since=None)
                changed = True
                print("Alarm cleared (sensors normal)")
        if quake_tick(now):
            changed = True
    if changed:
        broadcast()
    return changed


def seismic_intensity(pulses, vib, dt):
    """0-100 shake level from the vibration sensor only (flips scaled to a 0.2 s report)."""
    per_report = pulses * 0.2 / max(0.15, min(1.2, dt))
    return int(min(100, per_report * 6 + (8 if vib else 0)))


def apply_seismic(data, addr):
    """Report from the seismic node: {"seismic":1,"vib":0/1,"pulses":N,"cm":X,"base":Y,"move":Z}."""
    vib = data.get("vib")
    if vib not in (0, 1, True, False):
        return False
    nums = {}
    for k, lo, hi in (("pulses", 0, 100000), ("cm", -1, 1000), ("base", -1, 1000), ("move", 0, 1000)):
        v = data.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            return False
        nums[k] = v
    now = time.time()
    pulses, move = int(nums["pulses"]), round(float(nums["move"]), 1)
    with state_lock:
        dt = now - seismic["seen"] if seismic["seen"] else 0.2
        cm = None if nums["cm"] < 0 else round(float(nums["cm"]), 1)
        g = data.get("gate")
        if g in (0, 1) and not isinstance(g, bool):          # parking gate status rides along
            nc = data.get("cards", 0)
            gate.update(seen=now, ip=addr[0], open=bool(g), nfc=data.get("nfc") == 1,
                        node_cards=nc if isinstance(nc, int) and not isinstance(nc, bool) else 0)
        seismic.update(seen=now, ip=addr[0], vib=bool(vib), pulses=pulses, cm=cm,
                       base=None if nums["base"] < 0 else round(float(nums["base"]), 1),
                       move=move, intensity=seismic_intensity(pulses, vib, dt))
        shaking = seismic["intensity"] >= QUAKE_SHAKE_MIN          # strong shaking
        moved = cm is not None and move >= SEA_MOVE_CM              # ultrasonic sees the water move
        if shaking:
            seismic["shake_at"] = now
        if moved:
            seismic["move_at"] = now
        seismic["both"] = shaking and moved      # this report had BOTH
        if quake["active"]:
            quake["peak"] = max(quake["peak"], seismic["intensity"])
        quake_tick(now)
        ack = now - seismic["ack_at"] >= 2
        if ack:
            seismic["ack_at"] = now
    if ack:                                        # "I hear you" + the saved gate cards (at most every 2 s)
        try:
            cmd_sock.sendto(gate_ack(), (addr[0], SEISMIC_ACK_PORT))
        except OSError:
            pass
    broadcast()
    return True


def quake_tick(now):
    """Tsunami alert = water moving (ultrasonic, >= SEA_MOVE_CM) AND strong shaking, together for QUAKE_BOTH_SEC.
    Call with state_lock held."""
    s = seismic
    online = s["seen"] is not None and now - s["seen"] < 3
    recent = lambda t, sec: online and t is not None and now - t <= sec
    both = s["both"] and recent(s["seen"], 0.5)   # latest report (0.2 s apart) had movement AND shaking
    calm = not recent(s["shake_at"], QUAKE_WINDOW) and not recent(s["move_at"], QUAKE_WINDOW)   # offline counts as calm
    changed = False
    if not both:
        quake["both_since"] = None
        quake["armed"] = True                  # after Silence, re-arm once they stop overlapping
    elif quake["both_since"] is None:
        quake["both_since"] = now
    if (not quake["active"] and quake["armed"] and both
            and now - quake["both_since"] >= QUAKE_BOTH_SEC):
        quake.update(active=True, since=now, calm_since=None, peak=s["intensity"])
        changed = True
        print("TSUNAMI ALERT: sea moving + strong shaking for %.1f s" % QUAKE_BOTH_SEC)
    if quake["active"]:
        if not calm:
            quake["calm_since"] = None
        elif quake["calm_since"] is None:
            quake["calm_since"] = now
        elif now - quake["calm_since"] >= QUAKE_CLEAR_SEC:
            quake.update(active=False, since=None, calm_since=None)
            changed = True
            print("Tsunami alert cleared (calm)")
    return changed


def alarm_loop():
    while True:
        alarm_tick()
        time.sleep(0.1)


def silence_alarm():
    with state_lock:
        alarm.update(active=False, since=None, calm_since=None, armed=False)
        quake.update(active=False, since=None, both_since=None, calm_since=None, armed=False)
    print("Alarm silenced from the dashboard")
    broadcast()


def apply_matrix(msg, addr):
    scr = msg.get("screen")
    with state_lock:
        matrix["seen"] = time.time()
        matrix["ip"] = addr[0]
        matrix["screen"] = scr if scr in MATRIX_SCREENS else None
    broadcast()          # once a second: keeps the "Display" chip up to date
    return True


def matrix_status():
    """What the matrix shows: rain, temperature, gas, alarm (None = unknown)."""
    now = time.time()
    with state_lock:
        s = state["sensors"]
        nano_ok = state["connected"] and state["lastUpdate"] and now - state["lastUpdate"] < 5
        rain = int(s["Rain"]["digital"] == MATRIX_RAIN_WHEN) if nano_ok and "Rain" in s else None
        gas = int(gas_detected(s["Gas"]["raw"])) if nano_ok and "Gas" in s else None
        fresh_wx = weather["fetched"] is not None and now - weather["fetched"] < 1200
        temp = round(weather["tempC"], 1) if fresh_wx else None
        alarm_on = int(alarm["active"])
        quake_on = int(quake["active"])
    return {"rain": rain, "temp": temp, "gas": gas, "alarm": alarm_on, "quake": quake_on}


def matrix_loop():
    while True:
        with state_lock:
            ip, seen = matrix["ip"], matrix["seen"]
        if ip and seen and time.time() - seen < 10:
            body = json.dumps(matrix_status(), separators=(",", ":")).encode("utf-8")
            try:
                cmd_sock.sendto(body, (ip, MATRIX_PORT))
            except OSError:
                pass
        time.sleep(1)


def apply_traffic(data, addr):
    """Status heartbeat from the traffic NodeMCU: remember its state and its IP."""
    mode = data.get("traffic")
    if mode not in ("auto", "manual"):
        return False
    lights = {}
    for name, short in (("red", "r"), ("yellow", "y"), ("green", "g")):
        v = data.get(short)
        if v not in (0, 1, True, False):
            return False
        lights[name] = bool(v)
    cm = data.get("cm")
    if cm is not None:
        if isinstance(cm, bool) or not isinstance(cm, (int, float)) or not -1 <= cm <= 1000:
            return False
        cm = None if cm < 0 else round(float(cm), 1)   # -1 = nothing in range
    with state_lock:
        traffic.update(seen=time.time(), ip=addr[0], mode=mode, cm=cm, **lights)
    count_car(cm)
    broadcast()
    return True


def send_traffic(payload):
    """Send a mode / light command to the traffic NodeMCU. Returns (ok, error text)."""
    with state_lock:
        ip, seen = traffic["ip"], traffic["seen"]
    if ip is None:
        return False, "Traffic node not found yet"
    if time.time() - seen > 6:
        return False, "Traffic node offline"
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    for i in range(3):                  # UDP can drop a packet, so send it 3 times
        try:
            cmd_sock.sendto(body, (ip, TRAFFIC_PORT))
        except OSError as e:
            return False, "Send failed: " + str(e)
        if i < 2:
            time.sleep(0.06)
    return True, ""


def udp_listener():
    """Listens for the NodeMCU's UDP broadcast, so the Pi's IP can be anything."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", UDP_PORT))
    except OSError as e:
        print(f"Could not listen on UDP {UDP_PORT}: {e}")
        return
    print(f"Listening for the mountain NodeMCU on UDP {UDP_PORT}")
    while True:
        try:
            packet, addr = sock.recvfrom(512)
            msg = json.loads(packet.decode("utf-8"))
            if isinstance(msg, dict) and "traffic" in msg:
                apply_traffic(msg, addr)
            elif isinstance(msg, dict) and msg.get("gate") == "scan":
                apply_gate_scan(msg, addr)
            elif isinstance(msg, dict) and "seismic" in msg:
                apply_seismic(msg, addr)
            elif isinstance(msg, dict) and msg.get("matrix") == "ok":
                apply_matrix(msg, addr)
            else:
                apply_mountain(msg)
        except (ValueError, UnicodeDecodeError):
            continue
        except OSError:
            time.sleep(0.5)


# ---- SSE subscribers: each connected browser gets its own queue, and
# ---- gets pushed a fresh snapshot once per complete reading block
# ---- (instead of the browser polling /data every second on its own) ----
subscribers = []
subscribers_lock = threading.Lock()


# ---- History: 24 h charts + event log ----
# Starts filled with SAMPLE data (marked "sample" everywhere) so the charts are never empty.
# Real readings replace the sample values hour by hour, and real events are added to the log.
HISTORY_HOURS = 24
HISTORY_EVERY = 2          # seconds between history samples
LIGHT_DARK_BELOW = 15      # ambient light % -> "dark, street lights on" (same as "Dark" on the page)
LIGHT_DAY_ABOVE = 45       # ambient light % -> "daylight, street lights off"
CAR_NEAR_CM = 20           # something closer than this to the traffic sensor = one vehicle
SOIL_DRY_WHEN = "HIGH"     # soil module DO level that means "dry" (same as DRY_WHEN on the page)
history_lock = threading.Lock()
history = {"buckets": {}, "events": [], "cars_near": False}
HIST_AVG_KEYS = ("temp", "gas", "soil", "light")
CITY_UTC_OFFSET_MIN = 330  # India time (UTC+5:30), so the hours line up even if the Pi's clock zone is UTC


def city_time(t):
    """time.struct_time in India time, whatever time zone the Pi is set to."""
    return time.gmtime(t + CITY_UTC_OFFSET_MIN * 60)


def hour_start(t):
    lt = city_time(t)
    return int(t - lt.tm_min * 60 - lt.tm_sec)


def hist_bucket(h):
    b = history["buckets"].get(h)
    if b is None:
        b = {"fake": {}, "sum": {}, "n": {}, "rainSec": 0.0, "nanoSec": 0.0, "cars": 0, "trafficSec": 0.0}
        history["buckets"][h] = b
    return b


def hist_event(kind, text, level="neutral", t=None, sample=False):
    """Add one line to the event log (newest kept, max 80)."""
    with history_lock:
        history["events"].append({"t": round(t if t is not None else time.time(), 1), "kind": kind,
                                  "text": text, "level": level, "sample": sample})
        history["events"].sort(key=lambda e: e["t"])
        del history["events"][:-80]


def make_sample_history(now=None):
    """Believable last-24-hours data for the model city (Kerala weather, school-day traffic)."""
    import math
    import random
    now = now or time.time()
    rnd = random.Random(int(now // 86400))          # same sample day after a restart
    start = now - HISTORY_HOURS * 3600
    hours = [hour_start(now) - i * 3600 for i in range(HISTORY_HOURS - 1, -1, -1)]
    lt0 = city_time(hours[0])
    midnight0 = hours[0] - lt0.tm_hour * 3600

    def at(day_offset, hh, mm):                      # local clock time -> epoch
        return midnight0 + day_offset * 86400 + hh * 3600 + mm * 60

    def last_time(hh, mm):                           # most recent hh:mm before now, inside the window
        for d in (2, 1, 0):
            t = at(d, hh, mm)
            if start < t < now - 120:
                return t
        return None

    ev = []
    # rain spells: an afternoon thunderstorm and a short early-morning shower
    rains = []
    for (hh, mm, mins) in ((15, rnd.randint(5, 40), rnd.randint(55, 95)), (5, rnd.randint(0, 40), rnd.randint(12, 25))):
        t = last_time(hh, mm)
        if t:
            end = min(t + mins * 60, now - 60)
            rains.append((t, end))
            ev.append((t, "rain", "Rain started", "info"))
            ev.append((end, "rain", "Rain stopped · " + str(int((end - t) / 60)) + " min of rain", "ok"))
    # gas leak test in the morning
    gas_spells = []
    t = last_time(8, rnd.randint(10, 50))
    if t:
        gas_spells.append((t, t + 7 * 60))
        ev.append((t, "gas", "Gas detected · air quality poor", "warn"))
        ev.append((t + 7 * 60, "gas", "Air quality back to normal", "ok"))
    # street lights
    for hh, mm, txt, lvl in ((18, 22, "It got dark · street lights on", "neutral"),
                             (6, 9, "Daylight · street lights off", "neutral")):
        t = last_time(hh, mm)
        if t:
            ev.append((t, "light", txt, lvl))
    # one disaster alarm at night
    t = last_time(2, rnd.randint(5, 50))
    if t:
        ev.append((t, "alarm", "Potential disaster · Mountain (motion + vibration)", "warn"))
        ev.append((t + rnd.randint(35, 70), "alarm", "Alarm cleared · mountain quiet for 30 s", "ok"))
    # soil
    t = last_time(11, 30)
    if t:
        ev.append((t, "soil", "Soil dry · plants need water", "warn"))
    if rains:
        ev.append((rains[0][0] + 20 * 60, "soil", "Soil moist again after rain", "ok"))
    # traffic signal switched by hand
    t = last_time(12, 48)
    if t:
        ev.append((t, "traffic", "Traffic signal set to manual from the dashboard", "neutral"))
        ev.append((t + 4 * 60, "traffic", "Traffic signal back to auto", "neutral"))

    def overlap(h, spells):
        return sum(max(0, min(b, h + 3600) - max(a, h)) for a, b in spells)

    soil = 52.0
    fake = {}
    for h in hours:
        hod = city_time(h).tm_hour + 0.5
        rain_min = overlap(h, rains) / 60
        gas_sec = overlap(h, gas_spells)
        temp = 27.4 + 3.4 * math.cos((hod - 14.5) / 24 * 2 * math.pi) + rnd.uniform(-0.3, 0.3)
        temp -= min(2.5, rain_min / 25)              # rain cools the air
        day = max(0.0, math.sin((hod - 6.2) / 12 * math.pi)) if 6.2 <= hod <= 18.2 else 0.0
        light = 3 + 88 * day * (0.55 if rain_min > 10 else 1.0) + rnd.uniform(-2, 2)
        soil = soil - rnd.uniform(0.6, 1.4) + rain_min * 0.45
        soil = max(28.0, min(86.0, soil))
        rush = math.exp(-((hod - 8.8) / 1.1) ** 2) * 26 + math.exp(-((hod - 16.6) / 1.4) ** 2) * 30
        cars = round(max(0, (2 if 6 <= hod <= 22 else 0.3) + rush + rnd.uniform(-2, 3)))
        gas = 17 + rnd.uniform(-3, 4) + gas_sec / 3600 * 220
        fake[h] = {"temp": round(temp, 1), "rain": round(rain_min, 1), "gas": round(gas, 1),
                   "soil": round(soil, 1), "light": round(max(0, min(100, light)), 1), "cars": cars}
    with history_lock:
        for h, vals in fake.items():
            hist_bucket(h)["fake"] = vals
    for t, kind, text, lvl in ev:
        if start < t < now:
            hist_event(kind, text, lvl, t=t, sample=True)


def history_tick(prev, dt):
    """Add one sample of every live reading to the current hour, and log changes as events."""
    now = time.time()
    with state_lock:
        s = state["sensors"]
        nano_ok = bool(state["connected"] and state["lastUpdate"] and now - state["lastUpdate"] < 5)
        cur = {
            "rain": (s["Rain"]["digital"] == MATRIX_RAIN_WHEN) if nano_ok and "Rain" in s else None,
            "gasOn": gas_detected(s["Gas"]["raw"]) if nano_ok and "Gas" in s else None,
            "gas": s["Gas"]["raw"] / 1023 * 100 if nano_ok and "Gas" in s else None,
            "soil": 100 - s["Soil"]["raw"] / 1023 * 100 if nano_ok and "Soil" in s else None,
            "soilDry": (s["Soil"]["digital"] == SOIL_DRY_WHEN) if nano_ok and "Soil" in s else None,
            "temp": weather["tempC"] if weather["fetched"] and now - weather["fetched"] < 1200 else None,
            "light": mountain["light"] if mountain["seen"] and now - mountain["seen"] < 6 else None,
            "traffic": traffic["seen"] is not None and now - traffic["seen"] < 6,
            "trafficMode": traffic["mode"] if traffic["seen"] else None,
            "alarm": alarm["active"],
            "quake": quake["active"],
        }
    with history_lock:
        b = hist_bucket(hour_start(now))
        if cur["rain"] is not None:
            b["nanoSec"] += dt
            if cur["rain"]:
                b["rainSec"] += dt
        for k in HIST_AVG_KEYS:
            if cur[k] is not None:
                b["sum"][k] = b["sum"].get(k, 0.0) + cur[k]
                b["n"][k] = b["n"].get(k, 0) + 1
        if cur["traffic"]:
            b["trafficSec"] += dt
        oldest = hour_start(now) - (HISTORY_HOURS - 1) * 3600
        for h in [h for h in history["buckets"] if h < oldest]:
            del history["buckets"][h]

    def changed(k):
        return prev.get(k) is not None and cur[k] is not None and prev[k] != cur[k]

    if changed("rain"):
        hist_event("rain", "Rain started" if cur["rain"] else "Rain stopped", "info" if cur["rain"] else "ok")
    if changed("gasOn"):
        hist_event("gas", "Gas detected · air quality poor" if cur["gasOn"] else "Air quality back to normal",
                   "warn" if cur["gasOn"] else "ok")
    if changed("soilDry"):
        hist_event("soil", "Soil dry · plants need water" if cur["soilDry"] else "Soil moist again",
                   "warn" if cur["soilDry"] else "ok")
    if cur["light"] is not None:
        dark = prev.get("dark")
        if cur["light"] < LIGHT_DARK_BELOW and dark is False:
            hist_event("light", "It got dark · street lights on")
        elif cur["light"] > LIGHT_DAY_ABOVE and dark is True:
            hist_event("light", "Daylight · street lights off")
        if cur["light"] < LIGHT_DARK_BELOW:
            cur["dark"] = True
        elif cur["light"] > LIGHT_DAY_ABOVE:
            cur["dark"] = False
        else:
            cur["dark"] = dark
    else:
        cur["dark"] = prev.get("dark")
    if prev.get("alarm") is not None and prev["alarm"] != cur["alarm"]:
        hist_event("alarm", "Potential disaster · Mountain (motion + vibration)" if cur["alarm"]
                   else "Alarm stopped", "warn" if cur["alarm"] else "ok")
    if prev.get("quake") is not None and prev["quake"] != cur["quake"]:
        hist_event("quake", "Tsunami alert · sea moving + strong shaking" if cur["quake"]
                   else "Tsunami alert ended", "warn" if cur["quake"] else "ok")
    if changed("trafficMode"):
        hist_event("traffic", "Traffic signal set to " + cur["trafficMode"])
    return cur


def history_loop():
    prev = {}
    last = time.time()
    while True:
        time.sleep(HISTORY_EVERY)
        now = time.time()
        try:
            prev = history_tick(prev, min(10.0, now - last))
        except Exception as e:                       # never let history stop the dashboard
            print("history error:", e)
        last = now


def count_car(cm):
    """Called with every traffic reading: counts each time something comes close to the sensor."""
    near = cm is not None and cm < CAR_NEAR_CM
    new_car = False
    with history_lock:
        if near and not history["cars_near"]:
            hist_bucket(hour_start(time.time()))["cars"] += 1
            new_car = True
        history["cars_near"] = near
    if new_car:
        density_add(time.time(), cm, True)
        print(f"Vehicle detected at the traffic signal ({cm:.0f} cm)")


# ---- Traffic density: every vehicle the traffic ultrasonic sees is logged ----
# SAMPLE vehicles (marked "sample" on the page) keep the road looking busy like a real town,
# following the time of day; real vehicles from the sensor are added on top and marked "live".
import random
import collections
DENSITY_WINDOW_MIN = 5         # vehicles per minute, averaged over the last 5 minutes
DENSITY_FULL_PER_MIN = 12      # 12 vehicles a minute = 100 % (road full)
DENSITY_KEEP_SEC = 30 * 60     # keep the last 30 minutes
VEHICLE_TYPES = (("Car", 52), ("Bike", 26), ("Auto", 15), ("Bus", 7))
density_lock = threading.Lock()
density = {"log": collections.deque(), "next_fake": None, "live_total": 0, "rnd": random.Random()}


def sample_rate_per_min(t):
    """Sample traffic for the time of day (Kerala town): quiet at night, busy at school/office rush."""
    lt = city_time(t)
    h = lt.tm_hour + lt.tm_min / 60
    if h < 5.5 or h >= 23:
        return 0.6
    if 8 <= h < 10 or 16 <= h < 19:
        return 7.5
    if 6.5 <= h < 21:
        return 4.0
    return 2.0


def density_add(t, cm, live):
    rnd = density["rnd"]
    kind = rnd.choices([k for k, _ in VEHICLE_TYPES], [w for _, w in VEHICLE_TYPES])[0]
    with density_lock:
        density["log"].append((t, kind, None if cm is None else round(float(cm), 1), live))
        if live:
            density["live_total"] += 1


def _density_fill(now):
    """Add sample vehicles up to now (call with density_lock held)."""
    rnd = density["rnd"]
    if density["next_fake"] is None:                    # first time: fill the last 30 minutes
        density["next_fake"] = now - DENSITY_KEEP_SEC
    log = density["log"]
    while density["next_fake"] <= now:
        t = density["next_fake"]
        if t > now - DENSITY_KEEP_SEC:
            kind = rnd.choices([k for k, _ in VEHICLE_TYPES], [w for _, w in VEHICLE_TYPES])[0]
            log.append((t, kind, round(rnd.uniform(4, 18), 1), False))
        density["next_fake"] = t + 60 / sample_rate_per_min(t) * rnd.uniform(0.35, 1.65)   # steady but not robotic
    if any(log[i][0] > log[i + 1][0] for i in range(max(0, len(log) - 8), len(log) - 1)):
        density["log"] = log = collections.deque(sorted(log))
    while log and log[0][0] < now - DENSITY_KEEP_SEC:
        log.popleft()


def density_snapshot(now):
    with density_lock:
        _density_fill(now)
        log = list(density["log"])
        live_total = density["live_total"]
    win = [v for v in log if now - v[0] <= DENSITY_WINDOW_MIN * 60]
    per_min = len(win) / DENSITY_WINDOW_MIN
    pct = min(100, round(per_min / DENSITY_FULL_PER_MIN * 100))
    bars = [0] * 30
    for v in log:
        i = int((now - v[0]) // 60)
        if 0 <= i < 30:
            bars[29 - i] += 1
    gap = None
    if len(win) >= 2:
        gap = round((win[-1][0] - win[0][0]) / (len(win) - 1), 1)
    return {"perMin": round(per_min, 1), "pct": pct,
            "level": "Low" if pct < 35 else "Medium" if pct < 70 else "Heavy",
            "bars": bars, "total30": len(log), "gapSec": gap, "busiest": max(bars),
            "live30": sum(1 for v in log if v[3]), "liveTotal": live_total,
            "log": [{"ago": round(now - t, 1), "type": k, "cm": cm, "live": lv}
                    for t, k, cm, lv in reversed(log[-6:])]}


def history_json():
    now = time.time()
    hours = []
    any_sample = False
    with history_lock:
        for i in range(HISTORY_HOURS - 1, -1, -1):
            h = hour_start(now) - i * 3600
            b = history["buckets"].get(h) or {"fake": {}, "sum": {}, "n": {}, "rainSec": 0, "nanoSec": 0,
                                              "cars": 0, "trafficSec": 0}
            row = {"t": h}
            sample = []
            for k in HIST_AVG_KEYS:
                if b["n"].get(k):
                    row[k] = round(b["sum"][k] / b["n"][k], 1)
                elif k in b["fake"]:
                    row[k] = b["fake"][k]
                    sample.append(k)
                else:
                    row[k] = None
            if b["nanoSec"] > 0:
                row["rain"] = round(b["rainSec"] / 60, 1)
            elif "rain" in b["fake"]:
                row["rain"] = b["fake"]["rain"]
                sample.append("rain")
            else:
                row["rain"] = None
            if b["trafficSec"] > 0 or b["cars"]:
                row["cars"] = b["cars"]
            elif "cars" in b["fake"]:
                row["cars"] = b["fake"]["cars"]
                sample.append("cars")
            else:
                row["cars"] = None
            row["sample"] = sample
            any_sample = any_sample or bool(sample)
            hours.append(row)
        events = [e for e in history["events"] if e["t"] > now - HISTORY_HOURS * 3600][::-1]
    any_sample = any_sample or any(e["sample"] for e in events)
    return json.dumps({"now": round(now), "hours": hours, "events": events, "sample": any_sample},
                      separators=(",", ":"))


BROADCAST_MIN_GAP = 0.2     # at most 5 snapshots a second, however many packets arrive
BROADCAST_HEARTBEAT = 1.0   # and at least one a second, so browsers always see fresh ages
_broadcast_wake = threading.Event()


def broadcast():
    """Something changed: the broadcast thread sends a fresh snapshot to every browser (coalesced)."""
    _broadcast_wake.set()


def _push(q, payload):
    """Give a browser the newest snapshot; a slow browser just skips old ones (it is never dropped)."""
    try:
        q.put_nowait(payload)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass


def broadcast_loop():
    last = 0.0
    while True:
        _broadcast_wake.wait(BROADCAST_HEARTBEAT)
        wait = BROADCAST_MIN_GAP - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        _broadcast_wake.clear()
        try:
            payload = snapshot()
        except Exception as e:                       # never let one bad value stop the live page
            print("snapshot error:", e)
            time.sleep(1)
            continue
        last = time.time()
        with subscribers_lock:
            for q in subscribers:
                _push(q, payload)


def handle_line(line):
    line = line.strip()
    if not line:
        return

    m = RE_ANALOG_DIGITAL.match(line)
    if m:
        a_pin, name, raw, volt, d_pin, digital = m.groups()
        with state_lock:
            state["sensors"][name] = {
                "aPin": a_pin, "raw": int(raw), "volt": float(volt),
                "dPin": d_pin, "digital": digital,
            }
            state["lastUpdate"] = time.time()
        if name == "Soil":
            broadcast()  # Soil is the last line of a reading block - push it
        return


def fetch_temperature():
    """Current temperature (deg C) at WEATHER_LAT/LON from Open-Meteo."""
    url = WEATHER_URL.format(lat=WEATHER_LAT, lon=WEATHER_LON)
    req = urllib.request.Request(url, headers={"User-Agent": "smartcity-dashboard"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read(8192).decode("utf-8"))
    t = data["current"]["temperature_2m"]
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not -60 <= t <= 70:
        raise ValueError("bad temperature value")
    return float(t)


def weather_loop():
    while True:
        wait = WEATHER_EVERY_SEC
        try:
            t = fetch_temperature()
            with state_lock:
                weather["tempC"] = t
                weather["fetched"] = time.time()
            print(f"Online temperature for {WEATHER_PLACE}: {t:.1f} C")
            broadcast()
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"Online temperature failed ({e}), retrying in 30s")
            wait = 30
        time.sleep(wait)


def serial_reader(port_name):
    while True:
        try:
            with serial.Serial(port_name, BAUD_RATE, timeout=1) as ser:
                print(f"Connected to {port_name} at {BAUD_RATE} baud")
                with state_lock:
                    state["connected"] = True
                while True:
                    raw = ser.readline()
                    if not raw:
                        continue
                    try:
                        line = raw.decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                    handle_line(line)
        except serial.SerialException as e:
            with state_lock:
                state["connected"] = False
            broadcast()
            print(f"Serial error ({e}), retrying in 2s...")
            time.sleep(2)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#d4dcd3">
<title>Smart City Operations Center</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600&display=swap" rel="stylesheet" media="print" onload="this.media='all'">
<style>
  :root {
    color-scheme: light;
    --bg: #d4dcd3;
    --soft: #eef1ed;
    --white: #ffffff;
    --ink: #1c1c1c;
    --dim: #6f746e;
    --mint: #a6f4a0;
    --mintD: #34b552;
    --lav: #b4a8fc;
    --lavD: #7462ef;
    --peach: #ffd0b5;
    --peachD: #ee7033;
    --r-card: 36px;
    --r-inner: 30px;
    font-family: "Poppins", "Segoe UI", system-ui, -apple-system, sans-serif;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; min-height: 100%; }
  body { background: var(--bg); color: var(--ink); -webkit-font-smoothing: antialiased; }
  .page { max-width: 1240px; margin: 0 auto; padding: 28px 20px 36px; }

  /* ---------- icons ---------- */
  .icon { width: 56px; height: 56px; border-radius: 50%; display: grid; place-items: center; flex: none; }
  .icon svg, .arr svg, .ricon svg { fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
  .icon svg { width: 24px; height: 24px; }
  .icon.dark { background: var(--ink); color: #fff; }
  .icon.mint { background: var(--mint); color: var(--ink); }
  .icon.white { background: #fff; color: var(--ink); }
  .icon.lav { background: var(--lav); color: var(--ink); }
  .icon.peach { background: var(--peach); color: var(--ink); }
  .arr { width: 26px; height: 26px; border-radius: 50%; display: inline-grid; place-items: center; background: var(--mint); color: var(--ink); flex: none; }
  .arr.white { background: #fff; }
  .arr svg { width: 13px; height: 13px; stroke-width: 2.2; }

  /* ---------- top bar ---------- */
  .top { display: flex; justify-content: space-between; align-items: center; gap: 16px 24px; flex-wrap: wrap; margin-bottom: 26px; }
  .brand { display: flex; align-items: center; gap: 14px; }
  .brand h1 { margin: 0; font-size: 24px; font-weight: 600; letter-spacing: -.01em; line-height: 1.2; }
  .sub { font-size: 13.5px; color: var(--dim); margin-top: 3px; }
  .chips { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .greet { font-size: 22px; font-weight: 600; letter-spacing: -.01em; white-space: nowrap; }
  .topR { display: flex; align-items: center; gap: 10px; margin-left: auto; flex-wrap: wrap; justify-content: flex-end; }
  .topR .greet { margin-right: 6px; }
  .statusRow { display: flex; align-items: center; gap: 12px; margin: -8px 0 26px; }
  .nodes { flex-wrap: nowrap; gap: 0; background: rgba(255, 255, 255, .6); border-radius: 999px; padding: 4px; min-width: 0;
           overflow-x: auto; scrollbar-width: none; -webkit-overflow-scrolling: touch; }
  .nodes::-webkit-scrollbar { display: none; }
  .nodes.more { -webkit-mask: linear-gradient(90deg, #000 88%, transparent); mask: linear-gradient(90deg, #000 88%, transparent); }
  .nodes .chip { background: transparent; padding: 7px 10px; gap: 6px; position: relative; flex: none; }
  .nodes .chip + .chip::before { content: ""; position: absolute; left: 0; top: 9px; bottom: 9px; width: 1px; background: rgba(28, 28, 28, .1); }
  .actions { display: flex; gap: 10px; flex: none; }
  .chip { display: flex; align-items: center; gap: 8px; padding: 9px 16px; border-radius: 999px; background: rgba(255, 255, 255, .6); font-size: 13px; color: var(--dim); white-space: nowrap; }
  .chip b { color: var(--ink); font-weight: 500; }
  .chip.clock b { font-weight: 600; font-variant-numeric: tabular-nums; letter-spacing: .02em; }
  .pulse { width: 8px; height: 8px; border-radius: 50%; background: #9aa39a; flex: none; }
  .pulse.live { background: var(--mintD); box-shadow: 0 0 0 3px rgba(52, 181, 82, .22); }
  .pulse.err { background: var(--peachD); box-shadow: 0 0 0 3px rgba(238, 112, 51, .22); }

  /* ---------- layout ---------- */
  .layout { display: flex; flex-direction: column; gap: 28px; }
  .pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 28px; align-items: stretch; }
  .cols { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 28px; align-items: stretch; }
  .col { display: flex; flex-direction: column; gap: 28px; min-width: 0; }
  .col > .card:last-child { flex: 1 1 auto; }          /* both columns end on the same line */
  .card { border-radius: var(--r-card); padding: 30px; position: relative; box-shadow: 0 34px 60px -44px rgba(40, 64, 40, .45); }
  .card.soft { background: var(--soft); }
  .card.white { background: var(--white); }
  .card.black { background: var(--ink); color: #fff; }
  .black .sub { color: rgba(255, 255, 255, .58); }
  .head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
  .hl { display: flex; align-items: center; gap: 16px; min-width: 0; }
  h2 { margin: 0; font-size: 24px; font-weight: 500; letter-spacing: -.01em; line-height: 1.2; }
  .big { font-size: 64px; font-weight: 500; line-height: 1; letter-spacing: -.02em; font-variant-numeric: tabular-nums; transition: opacity .3s; }
  .big small { font-size: 18px; font-weight: 400; margin-left: 6px; opacity: .6; letter-spacing: 0; }
  .stale .big, .stale .gval { opacity: .32; }

  /* ---------- status pill ---------- */
  .tag {
    flex: none; display: inline-flex; align-items: center; gap: 8px;
    padding: 8px 14px; border-radius: 999px; font-size: 13px; font-weight: 500; white-space: nowrap;
    background: #fff; color: var(--ink); border: 1px solid rgba(28, 28, 28, .08);
  }
  .tag::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: #b5bcb4; }
  .tag.ok::before { background: var(--mintD); }
  .tag.warn { background: var(--peach); border-color: transparent; }
  .tag.warn::before { background: var(--peachD); }
  .tag.info::before { background: var(--lavD); }
  .tag.neutral::before { background: var(--ink); }
  .black .tag { background: transparent; color: #fff; border-color: rgba(255, 255, 255, .26); }
  .black .tag.warn { background: var(--peach); color: var(--ink); border-color: transparent; }
  .black .tag.info { background: var(--lav); color: var(--ink); border-color: transparent; }
  .black .tag.info::before { background: var(--ink); }

  /* ---------- rainfall (black card) ---------- */
  .rainBody { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 24px; margin-top: 30px; }
  .lbl { display: flex; align-items: center; gap: 10px; font-size: 15px; color: rgba(255, 255, 255, .7); }
  .rainInfo .big { margin-top: 14px; font-size: 72px; }
  .meta { margin-top: 12px; font-size: 13px; color: rgba(255, 255, 255, .55); font-variant-numeric: tabular-nums; }
  .bars { display: flex; gap: 10px; align-items: flex-end; height: 210px; flex: 1 1 240px; max-width: 340px; }
  .bar { position: relative; flex: 1; height: 100%; border-radius: 20px; overflow: hidden;
         background: repeating-linear-gradient(135deg, rgba(255, 255, 255, .14) 0 1.2px, transparent 1.2px 7px); }
  .bar i { position: absolute; left: 0; right: 0; bottom: 0; height: 0; border-radius: 20px; background: var(--mint); transition: height .4s ease; }
  .bar.now i { background: var(--lav); }
  .blbl { position: absolute; top: 10px; left: 0; right: 0; text-align: center; font-size: 11.5px; color: rgba(255, 255, 255, .85); z-index: 1; }

  /* ---------- temperature (gauge) ---------- */
  .inner { border-radius: var(--r-inner); padding: 24px; margin-top: 26px; position: relative; }
  .pair .card { display: flex; flex-direction: column; }
  .oledPcb { margin-top: 18px; background: #1f3a6e; border-radius: 14px; padding: 14px 12px; box-shadow: inset 0 0 0 2px #2c4f8f; }
  .oledGlass { background: #05070a; border-radius: 6px; padding: 8px; box-shadow: inset 0 0 0 2px #000; }
  .oledGlass svg { display: block; width: 100%; height: auto; aspect-ratio: 2 / 1; }
  .oledGlass.off svg { opacity: .25; }
  #oledSvg text { font-family: ui-monospace, Menlo, Consolas, monospace; fill: #dff4ff; }
  #oledSvg .ln { stroke: #dff4ff; fill: none; }
  #oledSvg .fl { fill: #dff4ff; }
  .pair .inner { flex: 1; display: flex; flex-direction: column; justify-content: flex-end; }
  .pair .inner .icon { display: none; }
  .pair .gval { font-size: 38px; }
  .inner.mint { background: var(--mint); }
  .tbody { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 10px 16px; margin-top: 6px; }
  .tinfo { padding-bottom: 6px; min-width: 0; }
  .t1 { display: flex; align-items: center; gap: 10px; font-size: 22px; font-weight: 600; }
  .t2 { margin-top: 6px; font-size: 13px; color: rgba(28, 28, 28, .65); }
  .gauge { position: relative; flex: 1 1 220px; max-width: 300px; margin-left: auto; }
  .gauge svg { display: block; width: 100%; height: auto; }
  .gval { position: absolute; left: 0; right: 0; bottom: 0; text-align: center; font-size: 46px; font-weight: 600; line-height: 1; letter-spacing: -.02em; font-variant-numeric: tabular-nums; transition: opacity .3s; }
  .gval small { font-size: 20px; font-weight: 500; margin-left: 2px; }

  /* ---------- street lighting ---------- */
  .lightRow { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 20px; margin-top: 28px; }
  .segs { display: flex; gap: 6px; align-items: flex-end; height: 72px; flex: 1 1 220px; max-width: 340px; }
  .segs i { flex: 1; border-radius: 9px; border: 1.5px solid #cdd3cc; transition: background .3s, border-color .3s; }
  .segs i.on { background: var(--mint); border-color: var(--ink); }
  .foot { margin-top: 18px; font-size: 13px; color: var(--dim); }

  /* ---------- security rows ---------- */
  .rows { display: flex; flex-direction: column; gap: 12px; margin-top: 26px; }
  .row { display: flex; align-items: center; gap: 14px; padding: 14px 16px; border-radius: 24px; background: #dfe3de; transition: background .25s, opacity .3s; }
  .ricon { width: 42px; height: 42px; border-radius: 50%; background: #fff; display: grid; place-items: center; flex: none; transition: background .25s, color .25s; }
  .ricon svg { width: 20px; height: 20px; }
  .rt { min-width: 0; flex: 1; }
  .rt b { display: block; font-weight: 500; font-size: 15px; }
  .rt span { display: block; font-size: 12.5px; color: var(--dim); margin-top: 1px; }
  .row.hit { background: var(--peach); }
  .row.hit .ricon { background: var(--ink); color: #fff; }
  .row.stale { opacity: .6; }

  /* ---------- city overview ---------- */
  .overview { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr); gap: 22px; padding: 24px; }
  .ovL { padding: 6px 0 6px 6px; }
  .tl { list-style: none; margin: 0; padding: 30px 0 0 6px; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 28px 18px; }
  .tl li { position: relative; padding: 34px 0 0; min-width: 0; }
  .tl li::before { content: ""; position: absolute; left: 10px; right: -18px; top: 10px; height: 1.5px; background: #c6ccc5; }
  .tl li:nth-child(4n)::before { right: 0; }
  .tdot { position: absolute; left: 0; top: 0; z-index: 1; width: 20px; height: 20px; border-radius: 50%; background: #fff; border: 1.5px solid #9aa29a; transition: background .25s; }
  .tdot.ok { background: var(--mint); border-color: var(--ink); }
  .tdot.warn { background: var(--ink); border-color: var(--ink); }
  .tl b { display: block; font-size: 16px; font-weight: 600; }
  .tl span { display: block; font-size: 13.5px; margin-top: 3px; }
  .tl small { display: block; font-size: 12.5px; color: var(--dim); margin-top: 2px; }
  .ovR { display: flex; flex-direction: row; gap: 16px; min-width: 0; }
  .ovR .tile { flex: 1; min-width: 0; }
  .tile { border-radius: 30px; padding: 24px; display: flex; flex-direction: column; min-height: 290px; }
  .tile.mint { background: var(--mint); transition: background-color .5s; }
  .tile.mint.warn { background: var(--peach); }
  .tile.warn .tag.warn { background: #fff; }
  .tile.lav { background: var(--lav); }
  .th { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px 10px; font-size: 14.5px; color: rgba(28, 28, 28, .72); }
  .tile .big { font-size: 46px; font-weight: 600; margin-top: 14px; }
  .tile .big small { font-size: 16px; }
  .tsub { font-size: 12.5px; color: rgba(28, 28, 28, .62); margin-top: 8px; font-variant-numeric: tabular-nums; }
  .hwrap { margin-top: auto; padding-top: 36px; }
  .hlbl { font-size: 14px; font-weight: 600; margin-bottom: 34px; }
  .hbar { position: relative; height: 38px; border: 1.6px solid var(--ink); border-radius: 14px; }
  .htrack { position: absolute; inset: 0; border-radius: 12px; overflow: hidden; }
  .hfill { height: 100%; width: 0; border-right: 1.6px solid var(--ink); transition: width .3s;
           background: repeating-linear-gradient(135deg, var(--ink) 0 1.3px, transparent 1.3px 7px); }
  .knob { position: absolute; top: -26px; left: 0; width: 0; height: 0; transition: left .3s; }
  .knob::before { content: ""; position: absolute; left: -.8px; top: 16px; width: 1.6px; height: 48px; background: var(--ink); }
  .knob::after { content: ""; position: absolute; left: -9px; top: 0; width: 18px; height: 18px; border-radius: 50%; background: var(--lav); border: 1.6px solid var(--ink); }
  .chart { position: relative; margin-top: auto; flex: 1 1 130px; min-height: 110px; }
  .tile .gchart { margin-top: 22px; flex: 1 1 90px; min-height: 70px; }
  .tile .gchart + .hwrap { margin-top: 0; padding-top: 30px; }
  .cknob.lavk { background: var(--lav); }
  .chart svg { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
  .chart polyline { fill: none; stroke: var(--ink); stroke-width: 1.8; stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke; }
  .cknob { position: absolute; width: 18px; height: 18px; border-radius: 50%; background: var(--mint); border: 1.6px solid var(--ink); transform: translate(-50%, -50%); display: none; }

  /* ---------- buttons ---------- */
  .sbtn {
    appearance: none; -webkit-appearance: none; cursor: pointer;
    height: 72px; padding: 0 30px; border-radius: 999px; border: 0;
    background: var(--ink); color: #fff; font-family: inherit; font-size: 16px; font-weight: 500;
    display: flex; align-items: center; justify-content: center; gap: 10px; transition: transform .05s;
  }
  .sbtn::before { content: ""; width: 12px; height: 12px; border-radius: 3px; background: var(--peach); }
  .sbtn:disabled { opacity: .3; cursor: not-allowed; }
  .sbtn:active:not(:disabled) { transform: scale(.96); }
  .mmsg { min-height: 18px; margin-top: 12px; font-size: 13px; color: #c2410c; }

  /* ---------- traffic signal ---------- */
  .trBody { display: flex; flex-wrap: wrap; gap: 28px; align-items: stretch; margin-top: 26px; }
  .tlight { flex: none; width: 108px; padding: 16px; border-radius: 40px; background: var(--ink);
            display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 14px; }
  .lamp { appearance: none; -webkit-appearance: none; width: 72px; height: 72px; border-radius: 50%; border: 0;
          cursor: pointer; background: #383838; transition: background .2s, box-shadow .2s, transform .05s; }
  .lamp.red.on { background: #ff6b5b; box-shadow: 0 0 26px rgba(255, 107, 91, .8); }
  .lamp.yellow.on { background: #ffd447; box-shadow: 0 0 26px rgba(255, 212, 71, .75); }
  .lamp.green.on { background: #5ee37d; box-shadow: 0 0 26px rgba(94, 227, 125, .75); }
  .lamp:disabled { cursor: default; }
  .lamp:hover:not(:disabled) { outline: 2px solid rgba(255, 255, 255, .35); outline-offset: 3px; }
  .lamp:active:not(:disabled) { transform: scale(.95); }
  .trSide { flex: 1 1 240px; min-width: 0; display: flex; flex-direction: column; }
  .seg { display: inline-flex; gap: 4px; padding: 5px; border-radius: 999px; background: #dfe3de; align-self: flex-start; }
  .seg button { appearance: none; -webkit-appearance: none; border: 0; background: transparent; cursor: pointer;
                font-family: inherit; font-size: 14px; font-weight: 500; color: var(--ink); padding: 10px 22px; border-radius: 999px; }
  .seg button.act { background: var(--ink); color: #fff; }
  .seg button:disabled { opacity: .4; cursor: not-allowed; }
  .dlbl { margin-top: 24px; font-size: 14px; color: var(--dim); }
  #v-dist { margin-top: 8px; font-size: 52px; }
  .dbar { position: relative; height: 16px; margin-top: 24px; border-radius: 999px; border: 1.6px solid var(--ink);
          background: linear-gradient(90deg, #ff9d90 0 20%, #ffe08a 20% 40%, var(--mint) 40% 100%); }
  .dknob { position: absolute; top: 50%; left: 100%; width: 20px; height: 20px; border-radius: 50%; display: none;
           background: var(--lav); border: 1.6px solid var(--ink); transform: translate(-50%, -50%); transition: left .3s; }
  .dleg { position: relative; height: 16px; margin-top: 6px; font-size: 11.5px; color: var(--dim); }
  .dleg span { position: absolute; top: 0; transform: translateX(-50%); white-space: nowrap; }
  .dleg span:first-child { transform: none; }
  .dleg span:last-child { transform: translateX(-100%); }
  .trSide .msub { margin-top: 16px; }

  /* ---------- intruder alarm ---------- */
  button.chip { border: 0; cursor: pointer; font-family: inherit; }
  button.chip.on { background: var(--ink); color: #fff; }
  .alarmBar { display: none; align-items: center; gap: 16px; flex-wrap: wrap; margin: 0 0 24px; padding: 18px 22px;
              border-radius: 28px; background: #ff5a4a; color: #fff; box-shadow: 0 20px 40px -24px rgba(255, 90, 74, .9);
              animation: apulse 1s ease-in-out infinite; }
  .alarmBar.on { display: flex; }
  .aicon { width: 52px; height: 52px; border-radius: 50%; background: #fff; color: #ff5a4a; display: grid; place-items: center;
           font-size: 26px; font-weight: 700; flex: none; }
  .aicon svg { width: 32px; height: 32px; margin-top: -3px; }
  .atext { flex: 1 1 220px; min-width: 0; }
  .atext b { display: block; font-size: 21px; font-weight: 600; }
  .atext span { display: block; font-size: 13.5px; opacity: .92; margin-top: 2px; }
  .alarmBar .sbtn { height: 54px; padding: 0 28px; }
  .alarmBar .sbtn::before { background: #fff; }
  @keyframes apulse { 50% { background: #e0362a; } }
  .card.alarm { animation: cflash 1s ease-in-out infinite; }
  @keyframes cflash { 50% { box-shadow: 0 0 0 4px #ff5a4a, 0 34px 60px -44px rgba(40, 64, 40, .45); } }
  @media (prefers-reduced-motion: reduce) { .alarmBar, .card.alarm { animation: none; } }

  /* ---------- history ---------- */
  .histGrid { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 24px; margin-top: 22px; align-items: start; }
  .hCharts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
  .hc { background: var(--soft); border-radius: 22px; padding: 14px 14px 10px; position: relative; min-width: 0; }
  .hc h3 { margin: 0; font-size: 13.5px; font-weight: 600; display: flex; align-items: center; gap: 7px; }
  .hc h3 i { width: 9px; height: 9px; border-radius: 3px; flex: none; }
  .hc .hv { font-size: 21px; font-weight: 600; margin: 4px 0 2px; letter-spacing: -.3px; }
  .hc .hv small { font-size: 12px; font-weight: 500; color: var(--dim); margin-left: 3px; }
  .hc .hs { font-size: 11.5px; color: var(--dim); min-height: 15px; }
  .hc svg { display: block; width: 100%; height: 86px; margin-top: 6px; overflow: visible; touch-action: pan-y; }
  .hc .hx { display: flex; justify-content: space-between; font-size: 10.5px; color: var(--dim); margin-top: 4px; }
  .hc .grid { stroke: rgba(28, 28, 28, .09); stroke-width: 1; }
  .hc .cross { stroke: var(--ink); stroke-width: 1; stroke-dasharray: 2 3; }
  .htip { position: absolute; top: 64px; right: 10px; background: var(--ink); color: #fff; font-size: 11.5px; padding: 4px 9px;
          border-radius: 10px; pointer-events: none; opacity: 0; transition: opacity .12s; white-space: nowrap; }
  .hc.hover .htip { opacity: 1; }
  .hLog { background: var(--soft); border-radius: 22px; padding: 14px 6px 6px 16px; min-width: 0; }
  .hLogHead { display: flex; justify-content: space-between; align-items: baseline; padding-right: 12px; font-size: 13.5px; }
  .hLogHead span { font-size: 11.5px; color: var(--dim); }
  .hEvents { list-style: none; margin: 8px 0 0; padding: 0 10px 8px 0; max-height: 452px; overflow-y: auto; }
  .hEvents li { display: grid; grid-template-columns: 44px 10px minmax(0, 1fr); gap: 8px; align-items: start; padding: 8px 0;
                border-top: 1px solid rgba(28, 28, 28, .07); font-size: 12.5px; line-height: 1.35; }
  .hEvents li:first-child { border-top: 0; }
  .hEvents time { color: var(--dim); font-variant-numeric: tabular-nums; }
  .hEvents .dot { width: 9px; height: 9px; border-radius: 50%; margin-top: 4px; background: var(--ink); }
  .hEvents .dot.warn { background: var(--peachD); }
  .hEvents .dot.ok { background: var(--mintD); }
  .hEvents .dot.info { background: var(--lavD); }
  .hEvents .s { color: var(--dim); font-size: 10.5px; border: 1px solid rgba(28, 28, 28, .16); border-radius: 6px; padding: 0 5px; margin-left: 5px; white-space: nowrap; }
  .hEvents .day { display: block; color: var(--dim); font-size: 10.5px; }
  .hEvents .hEmpty { display: block; color: var(--dim); }
  @media (max-width: 1080px) {
    .histGrid { grid-template-columns: minmax(0, 1fr); }
    .hEvents { max-height: 340px; }
  }
  @media (max-width: 700px) { .hCharts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .hx span:nth-child(2), .hx span:nth-child(4) { display: none; } }
  @media (max-width: 340px) { .hCharts { grid-template-columns: minmax(0, 1fr); } }

  /* ---------- City AI ---------- */
  .aiChip { background: #fff !important; position: relative; }
  .aiChip::after { content: ""; position: absolute; inset: -2px; border-radius: 999px; padding: 2px; pointer-events: none;
                   background: conic-gradient(from var(--ang, 0deg), #34b552, #a6f4a0, #ffffff, #a6f4a0, #34b552);
                   -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0); -webkit-mask-composite: xor;
                   mask: linear-gradient(#000 0 0) content-box exclude, linear-gradient(#000 0 0); animation: aiSpin 3s linear infinite; }
  @property --ang { syntax: "<angle>"; inherits: false; initial-value: 0deg; }
  @keyframes aiSpin { to { --ang: 360deg; } }
  .aiDot { width: 8px; height: 8px; border-radius: 50%; background: var(--mintD); flex: none; animation: aiBeat 1.4s ease-in-out infinite; }
  @keyframes aiBeat { 50% { box-shadow: 0 0 0 5px rgba(52, 181, 82, .2); } }
  .aiIcon { background: var(--mint); color: var(--ink); }
  .ai .tag.aiTag { background: #fff; border-color: rgba(52, 181, 82, .45); gap: 8px; }
  .ai .tag.aiTag::before { display: none; }
  .aiGrid { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 7fr); gap: 22px; margin-top: 22px; }
  .aiBrain { background: var(--soft); border-radius: 24px; padding: 14px; display: flex; flex-direction: column; }
  #aiNet { width: 100%; height: auto; display: block; flex: 1; }
  #aiNet .e { stroke: rgba(28, 28, 28, .07); stroke-width: 1; }
  #aiNet .f { stroke: var(--mintD); stroke-width: 1.6; stroke-dasharray: 6 60; animation: aiFlow 1.6s linear infinite; }
  @keyframes aiFlow { to { stroke-dashoffset: -66; } }
  #aiNet .n { fill: #fff; stroke: rgba(28, 28, 28, .28); stroke-width: 1; transition: fill .5s; }
  #aiNet .n.on { fill: var(--mint); stroke: var(--mintD); }
  #aiNet .n.hot { fill: var(--peach); stroke: var(--peachD); }
  #aiNet text { fill: var(--dim); font-size: 8.5px; font-family: inherit; }
  .aiStats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
  .aiStats div { background: #fff; border-radius: 14px; padding: 8px 10px; min-width: 0; }
  .aiStats span { display: block; font-size: 10.5px; color: var(--dim); }
  .aiStats b { display: block; font-size: 14px; font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .aiPreds { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
  .aiP { background: var(--soft); border-radius: 22px; padding: 14px 16px; min-width: 0; }
  .aiP .pt { display: flex; justify-content: space-between; align-items: center; font-size: 13px; color: var(--dim); gap: 8px; }
  .aiP .lv { font-size: 11px; font-weight: 600; padding: 2px 10px; border-radius: 999px; background: var(--mint); color: var(--ink); white-space: nowrap; }
  .aiP .lv.mid { background: var(--peach); }
  .aiP .lv.high { background: #ff5a4a; color: #fff; }
  .aiP .pv { font-size: 34px; font-weight: 600; letter-spacing: -.02em; margin: 6px 0 4px; font-variant-numeric: tabular-nums; }
  .aiP .pv small { font-size: 13px; font-weight: 500; color: var(--dim); margin-left: 4px; }
  .aiP .bar { height: 6px; border-radius: 6px; background: rgba(28, 28, 28, .08); overflow: hidden; }
  .aiP .bar i { display: block; height: 100%; border-radius: 6px; background: var(--mintD); transition: width .8s; }
  .aiP .bar i.mid { background: var(--peachD); }
  .aiP .bar i.high { background: #ff5a4a; }
  .aiP .why { font-size: 11.5px; color: var(--dim); margin-top: 8px; min-height: 30px; line-height: 1.35; }
  .aiBottom { display: grid; grid-template-columns: minmax(0, 7fr) minmax(0, 5fr); gap: 22px; margin-top: 16px; }
  .aiLog { background: var(--soft); border-radius: 18px; padding: 10px 14px; height: 108px; overflow: hidden;
           font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11.5px; line-height: 1.75; color: #8b918a;
           -webkit-mask: linear-gradient(transparent, #000 40%); mask: linear-gradient(transparent, #000 40%);
           display: flex; flex-direction: column; justify-content: flex-end; }
  .aiLog div { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .aiLog div:last-child { color: #1f7a35; font-weight: 600; }
  .aiSuggest { background: var(--mint); border-radius: 18px; padding: 14px 16px; display: flex; flex-direction: column; justify-content: center; gap: 4px; }
  .aiSuggest span { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: rgba(28, 28, 28, .6); font-weight: 600; }
  .aiSuggest b { font-size: 15px; font-weight: 500; line-height: 1.35; }
  .aiSuggest.warn { background: var(--peach); }
  @media (max-width: 900px) {
    .aiGrid, .aiBottom { grid-template-columns: minmax(0, 1fr); }
  }
  @media (max-width: 560px) {
    .aiStats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .aiP { padding: 12px; }
    .aiP .pv { font-size: 26px; }
    .aiP .pt { flex-direction: column; align-items: flex-start; gap: 6px; }
    .aiMore { display: none; }
    .aiLog { font-size: 10.5px; }
    .aiPreds { gap: 10px; }
  }
  @media (prefers-reduced-motion: reduce) { #aiNet .f, .aiChip::after, .aiDot { animation: none; } }
  /* ---------- seismic monitor ---------- */
  .seisTop { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; flex-wrap: wrap; margin-top: 22px; }
  .seisOf { font-size: 18px; color: var(--dim); margin-left: 4px; }
  .seisWord { font-size: 13.5px; color: var(--dim); margin-top: 6px; }
  .seisLv { display: flex; gap: 5px; flex: 1 1 200px; max-width: 300px; }
  .seisLv i { flex: 1; font-style: normal; font-size: 11px; text-align: center; padding: 26px 0 6px; border-radius: 10px;
              border: 1.5px solid #cdd3cc; color: var(--dim); transition: background .3s, border-color .3s, color .3s; }
  .seisLv i.on { border-color: var(--ink); color: var(--ink); font-weight: 600; }
  .seisLv i.on:nth-child(1) { background: var(--mint); }
  .seisLv i.on:nth-child(2) { background: #d9f7a6; }
  .seisLv i.on:nth-child(3) { background: var(--peach); }
  .seisLv i.on:nth-child(4) { background: #ff5a4a; color: #fff; border-color: #ff5a4a; }
  .seisGraph { display: block; width: 100%; height: 96px; margin-top: 20px; background: var(--soft); border-radius: 18px; }
  /* last card in a column fills the leftover height: let the seismograph / gate lists take it, not empty space */
  .col > #c-seis:last-child, .col > #c-gate:last-child { display: flex; flex-direction: column; }
  .col > #c-seis:last-child .seisGraph { flex: 1 1 96px; height: auto; min-height: 96px; max-height: 440px; }
  .col > #c-gate:last-child .gateLists { flex: 1 1 auto; align-items: stretch; }
  .col > #c-seis:last-child > .foot, .col > #c-gate:last-child > .foot { margin-top: auto; padding-top: 18px; }
  .seisGraph .mid { stroke: rgba(28, 28, 28, .12); stroke-width: 1; }
  .seisGraph .lim { stroke: #ff5a4a; stroke-width: 1; stroke-dasharray: 3 4; opacity: .5; }
  .seisAxis { display: flex; justify-content: space-between; font-size: 11px; color: var(--dim); margin-top: 6px; padding: 0 4px; }
  .seisStats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin-top: 14px; }
  .seisStats div { background: var(--soft); border-radius: 14px; padding: 9px 11px; min-width: 0; }
  .seisStats span { display: block; font-size: 11px; color: var(--dim); }
  .seisStats b { display: block; font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  @media (max-width: 560px) { .seisStats { grid-template-columns: repeat(2, minmax(0, 1fr)); } }

  /* ---------- city risk ---------- */
  .risk { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); gap: 28px; align-items: center; padding: 26px 30px; }
  .riskNum { display: flex; align-items: baseline; gap: 10px; margin-top: 18px; flex-wrap: wrap; }
  .riskNum .big { font-size: 72px; font-weight: 600; transition: color .4s; }
  .riskNum .tag { align-self: center; margin-left: 6px; }
  .riskBar { position: relative; height: 12px; border-radius: 999px; margin-top: 16px;
             background: linear-gradient(90deg, var(--mint) 0 30%, #ffe08a 30% 60%, var(--peach) 60% 80%, #ff8a7a 80%); }
  .riskBar i { position: absolute; top: 50%; width: 20px; height: 20px; border-radius: 50%; background: var(--ink); border: 3px solid #fff;
               transform: translate(-50%, -50%); left: 0; transition: left .8s var(--ease); box-shadow: 0 2px 6px rgba(0, 0, 0, .2); }
  .riskWhy { margin-top: 16px; font-size: 14px; padding: 11px 14px; border-radius: 16px; background: var(--soft); }
  .riskWhy b { font-weight: 600; }
  .risk.hi .riskWhy { background: var(--peach); }
  .riskHz { display: grid; gap: 8px; align-content: start; }
  .rz { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; column-gap: 12px; background: var(--soft);
        border-radius: 16px; padding: 10px 14px; transition: background .3s; }
  .rz span { font-size: 13.5px; }
  .rz b { font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .rz .sysMini { grid-column: 1 / -1; margin-top: 7px; }
  .rz.lead { background: #fff3ec; box-shadow: inset 0 0 0 1.5px var(--peach); }
  @media (max-width: 760px) { .risk { grid-template-columns: minmax(0, 1fr); gap: 20px; padding: 24px; } .riskNum .big { font-size: 60px; } }
  /* ---------- traffic density ---------- */
  .densTop { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-top: 20px; flex-wrap: wrap; }
  .densTop .big { font-size: 52px; font-weight: 600; }
  .densG { flex: 1 1 190px; max-width: 280px; }
  .densG .dp { display: flex; justify-content: space-between; font-size: 13px; color: var(--dim); }
  .densG .dp b { color: var(--ink); font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .densBar { position: relative; height: 12px; border-radius: 999px; margin-top: 8px; background: #dfe3de; overflow: hidden; }
  .densBar i { position: absolute; left: 0; top: 0; bottom: 0; width: 0; border-radius: 999px; background: var(--mintD); transition: width .6s var(--ease), background-color .3s; }
  .densBar i.mid { background: #e6b422; }
  .densBar i.hi { background: var(--peachD); }
  .densBars { display: flex; align-items: flex-end; gap: 3px; height: 74px; margin-top: 18px; padding: 8px 10px 0; background: var(--soft); border-radius: 18px; }
  .densBars i { flex: 1; min-width: 0; height: 4%; background: var(--ink); border-radius: 3px 3px 0 0; opacity: .78; transition: height .5s var(--ease); }
  .densBars i:last-child { background: var(--lavD); opacity: 1; }
  .densLog { margin-top: 14px; padding-bottom: 4px; }
  .densLog li { min-height: 36px; padding: 5px 0; }
  .densLog .vt { width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; background: #fff; flex: none; font-size: 15px; }
  .densLog .lv { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: #dfe3de; color: var(--dim); white-space: nowrap; }
  .densLog .lv.live { background: var(--mint); color: var(--ink); }
  .densLog .ago { font-size: 12px; color: var(--dim); white-space: nowrap; font-variant-numeric: tabular-nums; }
  /* ---------- raspberry pi system ---------- */
  .sysTop { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-top: 22px; flex-wrap: wrap; }
  .sysTop .big { font-size: 52px; font-weight: 600; }
  .sysWord { font-size: 13.5px; color: var(--dim); margin-top: 6px; }
  .sysTemp { flex: 1 1 200px; max-width: 300px; }
  .sysBar { position: relative; height: 12px; border-radius: 999px; margin-top: 8px;
            background: linear-gradient(90deg, var(--mint) 0 50%, #ffe08a 50% 80%, var(--peach) 80% 90%, #ff8a7a 90%); }
  .sysBar i { position: absolute; top: 50%; width: 18px; height: 18px; border-radius: 50%; background: var(--ink); border: 3px solid #fff;
              transform: translate(-50%, -50%); left: 0; transition: left .6s var(--ease); box-shadow: 0 2px 6px rgba(0, 0, 0, .2); }
  .sysLeg { position: relative; height: 14px; font-size: 11px; color: var(--dim); margin-top: 6px; }
  .sysLeg span { position: absolute; transform: translateX(-50%); }
  .sysLeg span:first-child { transform: none; }
  .sysLeg span:last-child { transform: translateX(-100%); }
  .sysPow { display: flex; align-items: center; gap: 10px; margin-top: 16px; padding: 11px 14px; border-radius: 16px; background: var(--peach); font-size: 13px; }
  .sysPow[hidden] { display: none; }
  .sysPow b { font-weight: 600; }
  .sysStats { grid-template-columns: repeat(3, minmax(0, 1fr)); margin-top: 18px; }
  .sysStats small { display: block; font-size: 11px; color: var(--dim); margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sysMini { height: 4px; border-radius: 4px; background: #dfe3de; margin-top: 7px; overflow: hidden; }
  .sysMini i { display: block; height: 100%; width: 0; background: var(--ink); border-radius: 4px; transition: width .6s var(--ease), background-color .3s; }
  .sysMini i.hi { background: var(--peachD); }
  .sysFoot { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 18px; flex-wrap: wrap; }
  .sysFoot .foot { margin-top: 0; }
  #sysMsg:empty { display: none; }
  @media (max-width: 560px) { .sysStats { grid-template-columns: repeat(2, minmax(0, 1fr)); } .sysTop .big { font-size: 44px; } }
  /* ---------- parking gate ---------- */
  .gateTop { display: flex; align-items: flex-end; gap: 22px; margin-top: 16px; flex-wrap: wrap; }
  .gateSvg { width: 124px; height: 110px; flex: none; overflow: visible; }
  .gateSvg .ground { stroke: #c6ccc5; stroke-width: 2; stroke-linecap: round; }
  .gateSvg .post { fill: var(--ink); }
  .gateSvg .rest { fill: #c6ccc5; }
  .gateSvg .arm { fill: #fff; stroke: var(--ink); stroke-width: 1.4; }
  .gateSvg .stripe { fill: #ff6b5b; }
  .gateSvg .pivot { fill: #fff; stroke: var(--ink); stroke-width: 1.4; }
  #gateArm { transform-box: view-box; transform-origin: 20px 88px; transition: transform .7s var(--ease); }
  .gateSvg.open #gateArm { transform: rotate(-84deg); }
  .gateSvg.deny #gateArm { animation: gdeny .55s ease-in-out; }
  @keyframes gdeny { 25% { transform: rotate(-6deg); } 55% { transform: rotate(2deg); } 80% { transform: rotate(-2deg); } }
  #gateLamp { fill: #ff5a4a; transition: fill .3s; }
  .gateSvg.open #gateLamp { fill: #5ee37d; }
  .gateSvg.off #gateLamp { fill: #6f746e; }
  .gateSide { min-width: 0; flex: 1 1 170px; padding-bottom: 4px; }
  .gateSide .big { font-size: 46px; font-weight: 600; }
  .gateWord { font-size: 13.5px; color: var(--dim); margin-top: 8px; }
  .gatePend { display: flex; align-items: center; gap: 12px; margin-top: 16px; padding: 12px 12px 12px 16px; border-radius: 18px;
              background: var(--peach); animation: tin .25s ease-out; }
  .gatePend[hidden] { display: none; }
  .gatePend div { flex: 1; min-width: 0; }
  .gatePend b { display: block; font-size: 14.5px; font-weight: 600; }
  .gatePend span { display: block; font-size: 12.5px; color: #7a4a33; margin-top: 2px; overflow-wrap: anywhere; }
  .gbtn { appearance: none; -webkit-appearance: none; border: 1px solid transparent; cursor: pointer; font-family: inherit; font-size: 13.5px;
          font-weight: 600; padding: 10px 18px; border-radius: 999px; background: var(--ink); color: #fff; flex: none; white-space: nowrap; }
  .gbtn.sm { padding: 6px 12px; font-size: 12px; font-weight: 500; }
  .gbtn.ghost { background: #fff; color: var(--ink); border-color: rgba(28, 28, 28, .12); }
  .gbtn.sure { background: #ff5a4a; color: #fff; border-color: transparent; }
  .gbtn:disabled { opacity: .45; cursor: default; }
  .gateStats { margin-top: 16px; }
  .gateLists { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }
  .gList { background: var(--soft); border-radius: 18px; padding: 12px 12px 6px 14px; min-width: 0; }
  .gHead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; font-size: 13px; margin-bottom: 4px; }
  .gHead b { font-weight: 600; }
  .gHead span { color: var(--dim); font-size: 12px; font-variant-numeric: tabular-nums; }
  .gList ul { list-style: none; margin: 0; padding: 0; }
  .gList li { display: flex; align-items: center; gap: 8px; padding: 7px 0; border-top: 1px solid rgba(28, 28, 28, .07); font-size: 13px; min-height: 42px; }
  .gList li:first-child { border-top: 0; }
  .gList .gi { flex: 1; min-width: 0; }
  .gList .gi b { display: block; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .gList .gi > span { display: block; font-size: 11.5px; color: var(--dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-variant-numeric: tabular-nums; }
  .gList .gdot { width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--mintD); }
  .gList .gdot.no { background: var(--peachD); }
  .gList li.gEmpty { color: var(--dim); font-size: 12.5px; }
  @media (max-width: 560px) { .gateLists { grid-template-columns: minmax(0, 1fr); } .gateSide .big { font-size: 38px; } }
  @media (prefers-reduced-motion: reduce) { #gateArm { transition: none; } .gateSvg.deny #gateArm, .gatePend { animation: none; } }

  /* ---------- popups (toasts) ---------- */
  .toasts { position: fixed; right: max(16px, env(safe-area-inset-right)); bottom: max(16px, env(safe-area-inset-bottom)); z-index: 50;
            display: flex; flex-direction: column; gap: 10px; width: min(360px, calc(100vw - 32px)); pointer-events: none; }
  .toast { pointer-events: auto; cursor: pointer; display: flex; align-items: flex-start; gap: 12px; padding: 14px;
           border-radius: 20px; background: #fff; border: 1px solid rgba(28, 28, 28, .06);
           box-shadow: 0 18px 40px -18px rgba(30, 40, 30, .5); animation: tin .25s ease-out; transition: opacity .25s, transform .25s; }
  .toast .ti { width: 36px; height: 36px; border-radius: 50%; display: grid; place-items: center; flex: none; font-weight: 700; font-size: 16px; }
  .toast.warn { border-color: rgba(238, 112, 51, .35); }
  .toast.warn .ti { background: var(--peach); color: var(--peachD); }
  .toast.info .ti { background: var(--lav); color: var(--ink); }
  .toast.ok .ti { background: var(--mint); color: var(--ink); }
  .toast .tt { min-width: 0; flex: 1; }
  .toast b { display: block; font-size: 14.5px; font-weight: 600; }
  .toast .tt span { display: block; font-size: 12.5px; color: var(--dim); margin-top: 2px; }
  .toast .tx { color: var(--dim); font-size: 18px; line-height: 1; padding: 2px 4px; }
  .toast.out { opacity: 0; transform: translateY(8px); }
  @keyframes tin { from { opacity: 0; transform: translateY(12px); } }
  @media (prefers-reduced-motion: reduce) { .toast { animation: none; } }


  /* ---------- smoothness: entrance, hover, press, transitions ---------- */
  html { scroll-behavior: smooth; }
  body { text-rendering: optimizeLegibility; }
  :root { --ease: cubic-bezier(.22, .9, .24, 1); }
  .card { transition: transform .45s var(--ease), box-shadow .45s var(--ease), background-color .4s; }
  @media (hover: hover) {
    .card:hover, .js-anim .rv.in.done:hover { transform: translateY(-3px); box-shadow: 0 40px 70px -42px rgba(40, 64, 40, .55); }
    .chip:is(button):hover { background: #fff; }
  }
  button { -webkit-tap-highlight-color: transparent; }
  button, .lamp, .chip { transition: transform .18s var(--ease), background-color .25s, color .25s, box-shadow .25s, opacity .25s; }
  button:active:not(:disabled), .lamp:active { transform: scale(.94); }
  .tag, .lv, .tdot, .pulse, .seisLv i, .segs i { transition: background-color .35s, color .35s, border-color .35s, box-shadow .35s; }
  .big, .pv, .hv, #seisInt { font-variant-numeric: tabular-nums; }
  .js-anim .rv { opacity: 0; transform: translateY(18px); }
  .js-anim .rv.in { opacity: 1; transform: none;
                    transition: opacity .6s var(--ease) var(--d, 0s), transform .7s var(--ease) var(--d, 0s), box-shadow .45s var(--ease); }
  .js-anim .rv.in.done { transition: transform .45s var(--ease), box-shadow .45s var(--ease), background-color .4s; }
  @media (prefers-reduced-motion: reduce) {
    html { scroll-behavior: auto; }
    .js-anim .rv { opacity: 1; transform: none; }
    .card, .card:hover, button:active:not(:disabled) { transform: none; transition: none; }
  }

  footer { margin-top: 26px; text-align: center; font-size: 12.5px; color: var(--dim); }

  @media (max-width: 1100px) {
    .nodes { max-width: 100%; }
  }
  @media (max-width: 980px) {
    .cols, .pair { grid-template-columns: 1fr; }
    .overview { grid-template-columns: 1fr; }
  }
  @media (max-width: 560px) {
    .page { padding: 18px 14px 28px; }
    .greet { display: none; }
    .top { flex-wrap: wrap; margin-bottom: 18px; gap: 12px; }
    .topR { width: 100%; margin-left: 0; flex-wrap: nowrap; }
    .topR .actions { flex: 1; min-width: 0; }
    .aiLong { display: none; }
    .actions .chip { overflow: hidden; }
    .chip.clock #clockD { display: none; }
    .chip.clock { padding: 8px 12px; }
    .statusRow { margin: -10px 0 18px; }

    .actions .chip { flex: 1 1 0; min-width: 0; justify-content: center; padding: 9px 10px; font-size: 12px; }
    #soundBtn { overflow: hidden; text-overflow: ellipsis; display: block; text-align: center; }
    .card { padding: 22px; border-radius: 28px; }
    .overview { grid-template-columns: 1fr; padding: 18px; }
    .ovL { padding: 6px; }
    .tl { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px 14px; }
    .tl li:nth-child(4n)::before { right: -14px; }
    .tl li:nth-child(2n)::before { right: 0; }
    .ovR { flex-direction: column; }
    .ovR .tile { flex: none; }
    h2 { font-size: 21px; }
    .big { font-size: 52px; }
    .rainInfo .big { font-size: 58px; }
    .bars { height: 160px; gap: 7px; max-width: none; }
    .icon { width: 48px; height: 48px; }
    .tile { min-height: 240px; }
    .sbtn { flex: 1; }
    .gval { font-size: 40px; }
    .tlight { width: 100%; flex-direction: row; padding: 14px; }
    .lamp { width: 64px; height: 64px; }
  }
  /* ---------- phone in landscape: compact, two columns ---------- */
  @media (orientation: landscape) and (max-height: 540px) {
    .page { padding: 12px max(14px, env(safe-area-inset-right)) 20px max(14px, env(safe-area-inset-left)); }
    .top { flex-wrap: nowrap; margin-bottom: 14px; gap: 14px; }
    .brand { flex: none; }
    .brand h1 { font-size: 18px; }
    .brand .sub { display: none; }
    .greet { display: none; }
    .chip.clock #clockD { display: none; }
    .statusRow { margin: -4px 0 14px; }
    .chips { flex-wrap: nowrap; overflow-x: auto; min-width: 0; scrollbar-width: none; -webkit-overflow-scrolling: touch; }
    .chips::-webkit-scrollbar { display: none; }
    .chip { padding: 7px 12px; font-size: 12px; }
    .alarmBar { margin-bottom: 14px; padding: 12px 16px; border-radius: 22px; }
    .alarmBar .sbtn { height: 44px; }
    .layout, .cols, .col, .pair { gap: 14px; }
    .cols, .pair { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .card { padding: 18px; border-radius: 24px; }
    .icon { width: 40px; height: 40px; }
    .icon svg { width: 20px; height: 20px; }
    .hl { gap: 12px; }
    h2 { font-size: 18px; }
    .sub { font-size: 12px; }
    .tag { padding: 6px 11px; font-size: 12px; }
    .big { font-size: 40px; }
    .rainBody { margin-top: 14px; gap: 14px; }
    .rainInfo .big { font-size: 46px; }
    .bars { height: 120px; gap: 6px; }
    .bar, .bar i { border-radius: 14px; }
    .inner { margin-top: 14px; padding: 16px; border-radius: 22px; }
    .inner .icon { display: none; }
    .t1 { font-size: 17px; }
    .gauge { max-width: 230px; flex-basis: 200px; }
    .gval { font-size: 32px; }
    .lightRow { margin-top: 14px; }
    .segs { height: 52px; }
    .foot { margin-top: 10px; }
    .rows { margin-top: 14px; gap: 8px; }
    .row { padding: 9px 12px; border-radius: 18px; }
    .ricon { width: 34px; height: 34px; }
    .trBody { margin-top: 14px; gap: 16px; flex-wrap: nowrap; }
    .tlight { width: 74px; padding: 10px; gap: 9px; border-radius: 30px; }
    .lamp { width: 52px; height: 52px; }
    .seg button { padding: 8px 16px; font-size: 13px; }
    .dlbl { margin-top: 12px; }
    #v-dist { font-size: 38px; }
    .dbar { margin-top: 14px; }
    .overview { grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr); padding: 16px; gap: 14px; }
    .ovL { padding: 4px; }
    .tl { padding-top: 16px; gap: 14px 12px; }
    .tl li { padding-top: 28px; }
    .tl li::before { right: -12px; }
    .tl b { font-size: 13.5px; }
    .tl span { font-size: 12.5px; }
    .tl small { display: none; }
    .ovR { flex-direction: row; gap: 12px; }
    .tile { flex: 1; min-width: 0; min-height: 200px; padding: 16px; border-radius: 22px; }
    .tile .big { font-size: 32px; margin-top: 8px; }
    .th .tag { padding: 5px 9px; }
    .hwrap { padding-top: 22px; }
    .hlbl { margin-bottom: 30px; font-size: 13px; }
    .chart { min-height: 70px; }
    .tile .gchart { min-height: 50px; }
    .sbtn { height: 54px; padding: 0 22px; }
    footer { margin-top: 14px; }
  }
</style>
</head>
<body>
<div class="page">

  <header class="top">
    <div class="brand">
      <span class="icon dark"><svg viewBox="0 0 24 24"><path d="M3 21h18M5 21V10l5-3v14M10 21V4l9 4v13M13.5 11h2.5M13.5 15h2.5M7 13h1M7 17h1"/></svg></span>
      <div>
        <h1>Smart City</h1>
        <div class="sub">Operations Center</div>
      </div>
    </div>
    <div class="topR">
      <div class="greet" id="greet">Hello</div>
      <div class="actions">
        <button class="chip aiChip" id="aiChip" type="button"><i class="aiDot"></i><span>AI<span class="aiLong"> model</span></span><b id="tAi">Running</b></button>
        <button class="chip" id="soundBtn" type="button">🔔 Tap to enable sound</button>
      </div>
      <span class="chip clock"><b id="clockT">--:--:--</b><span id="clockD"></span></span>
    </div>
  </header>
  <div class="statusRow">
    <div class="chips nodes" aria-label="Node status">
      <span class="chip"><i class="pulse" id="pCtl"></i>Controller <b id="tCtl">Connecting</b></span>
      <span class="chip"><i class="pulse" id="pMtn"></i>Mountain <b id="tMtn">Waiting</b></span>
      <span class="chip"><i class="pulse" id="pTr"></i>Traffic <b id="tTr">Waiting</b></span>
      <span class="chip"><i class="pulse" id="pSe"></i>Seismic <b id="tSe">Waiting</b></span>
      <span class="chip"><i class="pulse" id="pMx"></i>Display <b id="tMx">Waiting</b></span>
    </div>
  </div>

  <div class="alarmBar" id="alarmBar" role="alert">
    <span class="aicon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.6 1.6 20.6h20.8z" fill="#ff5a4a"/><path d="M12 9v5.4" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/><circle cx="12" cy="17.4" r="1.4" fill="#fff"/></svg></span>
    <div class="atext">
      <b id="alarmTitle">Potential disaster · Mountain</b>
      <span id="alarmTxt">Motion and vibration detected together</span>
    </div>
    <button class="sbtn" id="silenceBtn" type="button">Silence</button>
  </div>

  <main class="layout">

    <!-- City Risk: one number for all hazards -->
    <section class="card white risk" id="c-risk">
      <div class="riskL">
        <div class="head">
          <div class="hl">
            <span class="icon dark"><svg viewBox="0 0 24 24"><path d="M4 16a8 8 0 0 1 16 0"/><path d="M12 16l4-5"/><circle cx="12" cy="16" r="1.4"/><path d="M3 20h18"/></svg></span>
            <div>
              <h2>City Risk</h2>
              <div class="sub">Every hazard in one score · updates every 2 s</div>
            </div>
          </div>
        </div>
        <div class="riskNum"><span class="big" id="riskVal">--</span><span class="seisOf">/100</span><span class="tag" id="riskPill">Calculating</span></div>
        <div class="riskBar"><i id="riskKnob"></i></div>
        <div class="sysLeg"><span style="left:0">0</span><span style="left:30%">30</span><span style="left:60%">60</span><span style="left:100%">100</span></div>
        <div class="riskWhy" id="riskWhy">Collecting data from the sensors</div>
      </div>
      <div class="riskHz" id="riskHz"></div>
    </section>

    <!-- City overview + air quality + soil -->
      <section class="card white overview">
        <div class="ovL">
          <div class="hl">
            <span class="icon mint"><svg viewBox="0 0 24 24"><rect x="3.5" y="3.5" width="17" height="17" rx="5"/><path d="M7.5 15l3-3 2.5 2 3.5-4.5"/></svg></span>
            <h2>City overview</h2>
          </div>
          <ul class="tl">
            <li><i class="tdot" id="zd-weather"></i><b>Weather</b><span id="zt-weather">No data</span><small>Rainfall · Temperature</small></li>
            <li><i class="tdot" id="zd-environment"></i><b>Environment</b><span id="zt-environment">No data</span><small>Air quality · Soil moisture</small></li>
            <li><i class="tdot" id="zd-security"></i><b>Security</b><span id="zt-security">No data</span><small>Motion · Vibration</small></li>
            <li><i class="tdot" id="zd-seismic"></i><b>Seismic</b><span id="zt-seismic">No data</span><small>Shaking · Sea movement</small></li>
            <li><i class="tdot" id="zd-lighting"></i><b>Street lighting</b><span id="zt-lighting">No data</span><small>Ambient light</small></li>
            <li><i class="tdot" id="zd-traffic"></i><b>Traffic signal</b><span id="zt-traffic">No data</span><small>Red · Yellow · Green · Ultrasonic</small></li>
            <li><i class="tdot" id="zd-gate"></i><b>Parking gate</b><span id="zt-gate">No data</span><small>NFC reader · Servo</small></li>
            <li><i class="tdot" id="zd-display"></i><b>Status display</b><span id="zt-display">No data</span><small>OLED screen</small></li>
          </ul>
        </div>
        <div class="ovR">
          <div class="tile mint" id="c-Gas">
            <div class="th"><span>Air quality</span><span class="tag" id="t-Gas">No data</span></div>
            <div class="big" id="v-Gas">--</div>
            <div class="tsub" id="l-Gas">Gas sensor A1 / D8</div>
            <div class="chart gchart">
              <svg viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true">
                <defs>
                  <pattern id="gHatch" patternUnits="userSpaceOnUse" width="5" height="40">
                    <line x1="0.5" y1="0" x2="0.5" y2="40" stroke="#1c1c1c" stroke-opacity=".22" stroke-width=".5"/>
                  </pattern>
                </defs>
                <polygon id="ar-Gas" points="" fill="#1c1c1c" fill-opacity=".07"/>
                <polygon id="ah-Gas" points="" fill="url(#gHatch)"/>
                <polyline id="ln-Gas" points=""/>
              </svg>
              <div class="cknob lavk" id="kc-Gas"></div>
            </div>
            <div class="hwrap">
              <div class="hlbl" id="pct-Gas">Level --</div>
              <div class="hbar">
                <div class="htrack"><div class="hfill" id="f-Gas"></div></div>
                <div class="knob" id="k-Gas"></div>
              </div>
            </div>
          </div>
          <div class="tile lav" id="c-Soil">
            <div class="th"><span>Soil moisture</span><span class="tag" id="t-Soil">No data</span></div>
            <div class="big" id="v-Soil">--</div>
            <div class="tsub" id="l-Soil">Soil sensor A3 / D10</div>
            <div class="chart">
              <svg viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true">
                <defs>
                  <pattern id="sHatch" patternUnits="userSpaceOnUse" width="5" height="40">
                    <line x1="0.5" y1="0" x2="0.5" y2="40" stroke="#1c1c1c" stroke-opacity=".22" stroke-width=".5"/>
                  </pattern>
                </defs>
                <polygon id="ar-Soil" points="" fill="#1c1c1c" fill-opacity=".07"/>
                <polygon id="ah-Soil" points="" fill="url(#sHatch)"/>
                <polyline id="ln-Soil" points=""/>
              </svg>
              <div class="cknob" id="k-Soil"></div>
            </div>
          </div>
        </div>
      </section>

    <!-- City AI -->
    <section class="card white ai" id="c-ai">
      <div class="head">
        <div class="hl">
          <span class="icon aiIcon"><svg viewBox="0 0 24 24"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M6 6l2 2M16 16l2 2M6 18l2-2M16 8l2-2"/><circle cx="12" cy="12" r="3.2"/></svg></span>
          <div>
            <h2>City AI</h2>
            <div class="sub">CityNet predictive model<span class="aiMore"> · 9 live inputs · 4 predictions</span></div>
          </div>
        </div>
        <span class="tag aiTag" id="aiTag"><i class="aiDot"></i>Model running</span>
      </div>
      <div class="aiGrid">
        <div class="aiBrain">
          <svg id="aiNet" viewBox="0 0 320 170" aria-hidden="true"></svg>
          <div class="aiStats">
            <div><span>Inference</span><b id="aiRuns">#0</b></div>
            <div><span>Latency</span><b id="aiLat">-- ms</b></div>
            <div><span>Confidence</span><b id="aiConf">--%</b></div>
            <div><span>Inputs</span><b id="aiAge">--</b></div>
          </div>
        </div>
        <div class="aiPreds" id="aiPreds"></div>
      </div>
      <div class="aiBottom">
        <div class="aiLog" id="aiLog" aria-live="off"></div>
        <div class="aiSuggest" id="aiSugBox"><span>AI suggests</span><b id="aiSuggest">Warming up the model</b></div>
      </div>
      <div class="foot">How it works: every 2 s the model turns the live sensors (rain, soil, gas, vibration, motion, light, temperature) and the last 24 h of history into weighted risk scores, then squashes them into a 0-100% probability.</div>
    </section>

    <div class="pair">
      <!-- Rainfall -->
      <section class="card black" id="c-Rain">
        <div class="head">
          <div>
            <h2>Rainfall</h2>
            <div class="sub">Weather · rain sensor A0 / D4</div>
          </div>
          <span class="tag" id="t-Rain">No data</span>
        </div>
        <div class="rainBody">
          <div class="rainInfo">
            <div class="lbl">Rain level <span class="arr"><svg viewBox="0 0 24 24"><path d="M7 17L17 7M9 7h8v8"/></svg></span></div>
            <div class="big" id="v-Rain">--</div>
            <div class="meta"><span id="l-Rain">-- V</span> · <span id="r-Rain">D4 --</span></div>
          </div>
          <div class="bars" id="rainBars"></div>
        </div>
      </section>

      <!-- Temperature -->
      <section class="card soft">
        <div class="head">
          <div>
            <h2>Temperature</h2>
            <div class="sub">Weather · live from the internet</div>
          </div>
          <span class="tag" id="t-Temp">No data</span>
        </div>
        <div class="inner mint" id="c-Temp">
          <span class="icon white"><svg viewBox="0 0 24 24"><path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0z"/><path d="M12 9v7"/></svg></span>
          <div class="tbody">
            <div class="tinfo">
              <div class="t1"><span id="p-Temp">Live weather</span><span class="arr white"><svg viewBox="0 0 24 24"><path d="M7 17L17 7M9 7h8v8"/></svg></span></div>
              <div class="t2" id="l-Temp">Waiting for the internet</div>
            </div>
            <div class="gauge">
              <svg viewBox="0 0 320 190" aria-hidden="true">
                <defs>
                  <pattern id="gHatch" patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(45)">
                    <rect width="8" height="8" fill="#a6f4a0"/>
                    <line x1="0" y1="0" x2="0" y2="8" stroke="#1c1c1c" stroke-width="1.6"/>
                  </pattern>
                </defs>
                <path id="gRestO" d="" fill="none" stroke="#1c1c1c" stroke-width="37" stroke-linecap="round"/>
                <path id="gRestH" d="" fill="none" stroke="url(#gHatch)" stroke-width="34" stroke-linecap="round"/>
                <path id="gVal" d="" fill="none" stroke="#1c1c1c" stroke-width="37" stroke-linecap="round"/>
                <g id="gNeedle"><polygon points="178,112 169,106 169,118" fill="#1c1c1c"/></g>
                <circle cx="160" cy="112" r="9" fill="#b4a8fc" stroke="#1c1c1c" stroke-width="1.6"/>
              </svg>
              <div class="gval" id="v-Temp">--</div>
            </div>
          </div>
        </div>
      </section>
    </div>

    <div class="cols">
      <div class="col">
        <!-- Security & safety -->
        <section class="card soft" id="secCard">
          <div class="head">
            <div class="hl">
              <span class="icon dark"><svg viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/></svg></span>
              <div>
                <h2>Security &amp; Safety</h2>
                <div class="sub">Mountain node · PIR + SW-420</div>
              </div>
            </div>
            <span class="tag" id="secSum">No data</span>
          </div>
          <div class="rows">
            <div class="row" id="c-motion">
              <span class="ricon"><svg viewBox="0 0 24 24"><path d="M5 13a7 7 0 0 1 14 0M8.5 13a3.5 3.5 0 0 1 7 0"/><circle cx="12" cy="13" r="1.2"/><path d="M12 17v3"/></svg></span>
              <div class="rt"><b>Motion detection</b><span id="ml-motion">Never triggered</span></div>
              <span class="tag" id="t-motion">No data</span>
            </div>
            <div class="row" id="c-vibration">
              <span class="ricon"><svg viewBox="0 0 24 24"><path d="M2 12h3l2-5 3 10 3-12 3 9 2-2h4"/></svg></span>
              <div class="rt"><b>Vibration monitor</b><span id="ml-vibration">Never triggered</span></div>
              <span class="tag" id="t-vibration">No data</span>
            </div>
          </div>
        </section>

        <!-- Street lighting -->
        <section class="card white" id="c-Light">
          <div class="head">
            <div class="hl">
              <span class="icon mint"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg></span>
              <div>
                <h2>Street Lighting</h2>
                <div class="sub">Ambient light · LDR</div>
              </div>
            </div>
            <span class="tag" id="t-Light">No data</span>
          </div>
          <div class="lightRow">
            <div class="big" id="v-Light">--</div>
            <div class="segs" id="lightSegs"></div>
          </div>
          <div class="foot">Light level measured on the mountain node</div>
        </section>


        <!-- Status display preview -->
        <section class="card white" id="c-disp">
          <div class="head">
            <div class="hl">
              <span class="icon dark"><svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8M12 17v4"/></svg></span>
              <div>
                <h2>Status Display</h2>
                <div class="sub">OLED 0.96 inch · NodeMCU</div>
              </div>
            </div>
            <span class="tag" id="dispPill">Waiting</span>
          </div>
          <div class="oledPcb"><div class="oledGlass" id="oledGlass">
            <svg id="oledSvg" viewBox="0 0 128 64" preserveAspectRatio="xMidYMid meet"></svg>
          </div></div>
          <div class="foot" id="dispFoot">Live copy of what the display in the model is showing</div>
        </section>

        <!-- Raspberry Pi -->
        <section class="card white" id="c-sys">
          <div class="head">
            <div class="hl">
              <span class="icon lav"><svg viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="2.5"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></svg></span>
              <div>
                <h2>Raspberry Pi</h2>
                <div class="sub" id="sysSub">The city's brain · runs this dashboard</div>
              </div>
            </div>
            <span class="tag" id="sysPill">Waiting</span>
          </div>
          <div class="sysTop">
            <div><div class="big" id="sysTemp">--</div><div class="sysWord" id="sysWord">CPU temperature</div></div>
            <div class="sysTemp">
              <div class="sysBar"><i id="sysKnob"></i></div>
              <div class="sysLeg"><span style="left:0">30</span><span style="left:50%">55</span><span style="left:80%">70</span><span style="left:100%">80°</span></div>
            </div>
          </div>
          <div class="sysPow" id="sysPow" hidden><span>⚡</span><div id="sysPowTxt"></div></div>
          <div class="seisStats sysStats">
            <div><span>Power</span><b id="sysPower">--</b><small id="sysPowerS">&nbsp;</small></div>
            <div><span>CPU load</span><b id="sysCpu">--</b><div class="sysMini"><i id="sysCpuB"></i></div></div>
            <div><span>Memory</span><b id="sysMem">--</b><div class="sysMini"><i id="sysMemB"></i></div></div>
            <div><span>Wifi</span><b id="sysWifi">--</b><small id="sysWifiS">&nbsp;</small></div>
            <div><span>Up time</span><b id="sysUp">--</b><small id="sysUpS">&nbsp;</small></div>
            <div><span>SD card</span><b id="sysDisk">--</b><div class="sysMini"><i id="sysDiskB"></i></div></div>
          </div>
          <div class="sysFoot">
            <div class="foot" id="sysAddr">Waiting for the Pi</div>
            <button class="gbtn ghost" id="sysRestart" type="button">Restart dashboard</button>
          </div>
          <div class="mmsg" id="sysMsg"></div>
        </section>

        <!-- Parking gate -->
        <section class="card white" id="c-gate">
          <div class="head">
            <div class="hl">
              <span class="icon dark"><svg viewBox="0 0 24 24"><path d="M5 21V9M3 21h18"/><rect x="5" y="9" width="15" height="3.5" rx="1.75"/><path d="M9.5 9l-1.5 3.5M14 9l-1.5 3.5M18.5 9 17 12.5"/></svg></span>
              <div>
                <h2>Parking Gate</h2>
                <div class="sub">NFC RC522 · servo · seismic NodeMCU</div>
              </div>
            </div>
            <span class="tag" id="gatePill">No data</span>
          </div>
          <div class="gateTop">
            <svg class="gateSvg off" id="gateSvg" viewBox="0 0 124 110" aria-hidden="true">
              <defs><clipPath id="gateClip"><rect x="16" y="84" width="90" height="8" rx="4"/></clipPath></defs>
              <line class="ground" x1="2" y1="106" x2="122" y2="106"/>
              <rect class="rest" x="100" y="92" width="5" height="14" rx="2"/>
              <rect class="post" x="10" y="80" width="20" height="26" rx="4"/>
              <circle id="gateLamp" cx="20" cy="99" r="3.2"/>
              <g id="gateArm">
                <rect class="arm" x="16" y="84" width="90" height="8" rx="4"/>
                <g clip-path="url(#gateClip)"><rect class="stripe" x="36" y="80" width="9" height="16"/><rect class="stripe" x="56" y="80" width="9" height="16"/><rect class="stripe" x="76" y="80" width="9" height="16"/><rect class="stripe" x="96" y="80" width="12" height="16"/></g>
                <rect x="16" y="84" width="90" height="8" rx="4" fill="none" stroke="#1c1c1c" stroke-width="1.4"/>
              </g>
              <circle class="pivot" cx="20" cy="88" r="4.5"/>
            </svg>
            <div class="gateSide">
              <div class="big" id="gateState">--</div>
              <div class="gateWord" id="gateWord">Waiting for the seismic node</div>
            </div>
          </div>
          <div class="gatePend" id="gatePend" hidden>
            <div><b>New card at the gate</b><span id="gatePendId"></span></div>
            <button class="gbtn" id="gateAllow" type="button">Allow</button>
          </div>
          <div class="seisStats gateStats">
            <div><span>Last card</span><b id="gateLast">--</b></div>
            <div><span>Result</span><b id="gateRes">--</b></div>
            <div><span>Opened today</span><b id="gateOpens">--</b></div>
            <div><span>Card reader</span><b id="gateNfc">--</b></div>
          </div>
          <div class="gateLists">
            <div class="gList"><div class="gHead"><b>Saved cards</b><span id="gateCount">0 / 10</span></div><ul id="gateCards"><li class="gEmpty">No saved cards yet</li></ul></div>
            <div class="gList"><div class="gHead"><b>Recent taps</b><span id="gateTaps"></span></div><ul id="gateLog"><li class="gEmpty">No taps yet</li></ul></div>
          </div>
          <div class="mmsg" id="gateMsg"></div>
          <div class="foot">Tap a saved card on the reader: the gate opens for 2 s, then closes by itself. Unknown cards stay shut: press <b>Allow</b> to save one</div>
        </section>
      </div>
      <div class="col">
        <!-- Traffic signal -->
        <section class="card soft" id="c-traffic">
          <div class="head">
            <div class="hl">
              <span class="icon dark"><svg viewBox="0 0 24 24"><rect x="8" y="2.5" width="8" height="19" rx="4"/><circle cx="12" cy="7" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="12" cy="17" r="1.3"/></svg></span>
              <div>
                <h2>Traffic Signal</h2>
                <div class="sub">NodeMCU · HC-SR04 ultrasonic</div>
              </div>
            </div>
            <span class="tag" id="trPill">No data</span>
          </div>
          <div class="trBody">
            <div class="tlight">
              <button class="lamp red" id="lamp-red" type="button" aria-label="Red light"></button>
              <button class="lamp yellow" id="lamp-yellow" type="button" aria-label="Yellow light"></button>
              <button class="lamp green" id="lamp-green" type="button" aria-label="Green light"></button>
            </div>
            <div class="trSide">
              <div class="seg"><button id="mode-manual" type="button">Manual</button><button id="mode-auto" type="button">Auto</button></div>
              <div class="dlbl">Distance to nearest object</div>
              <div class="big" id="v-dist">--</div>
              <div class="dbar"><div class="dknob" id="k-dist"></div></div>
              <div class="dleg"><span style="left:0">0</span><span style="left:20%">10</span><span style="left:40%">20</span><span style="left:100%">50 cm</span></div>
              <div class="msub" id="trSub">Waiting for the traffic node</div>
              <div class="mmsg" id="trMsg"></div>
            </div>
          </div>
        </section>

        <!-- Traffic density -->
        <section class="card white" id="c-dens">
          <div class="head">
            <div class="hl">
              <span class="icon peach"><svg viewBox="0 0 24 24"><path d="M5 16V11l2-5h10l2 5v5"/><path d="M3 16h18v3H3z"/><circle cx="7.5" cy="13" r="1"/><circle cx="16.5" cy="13" r="1"/></svg></span>
              <div>
                <h2>Traffic Density</h2>
                <div class="sub">Vehicle counter · traffic ultrasonic</div>
              </div>
            </div>
            <span class="tag" id="densPill">Waiting</span>
          </div>
          <div class="densTop">
            <div><span class="big" id="densRate">--</span><span class="seisOf">/min</span><div class="seisWord" id="densWord">Vehicles a minute (last 5 min)</div></div>
            <div class="densG">
              <div class="dp"><span>Road density</span><b id="densPct">--%</b></div>
              <div class="densBar"><i id="densFill"></i></div>
            </div>
          </div>
          <div class="densBars" id="densBars" aria-label="Vehicles per minute, last 30 minutes"></div>
          <div class="seisAxis"><span>30 min ago</span><span>15 min</span><span>now</span></div>
          <div class="seisStats">
            <div><span>Last 30 min</span><b id="densTot">--</b></div>
            <div><span>Avg gap</span><b id="densGap">--</b></div>
            <div><span>Busiest min</span><b id="densMax">--</b></div>
            <div><span>Sensor saw</span><b id="densLive">--</b></div>
          </div>
          <div class="gList densLog"><div class="gHead"><b>Vehicle log</b><span>newest first</span></div><ul id="densLog"></ul></div>
          <div class="foot">Each time something passes within 20 cm of the traffic sensor it is logged as a vehicle (live). Sample traffic fills in the rest, like a real town road.</div>
        </section>


        <!-- Seismic monitor -->
        <section class="card white" id="c-seis">
          <div class="head">
            <div class="hl">
              <span class="icon mint"><svg viewBox="0 0 24 24"><path d="M2 12h3l2-6 3 13 3-16 3 14 2-5h4"/></svg></span>
              <div>
                <h2>Seismic Monitor</h2>
                <div class="sub">NodeMCU · vibration + ultrasonic</div>
              </div>
            </div>
            <span class="tag" id="seisPill">No data</span>
          </div>
          <div class="seisTop">
            <div><span class="big" id="seisInt">--</span><span class="seisOf">/100</span><div class="seisWord" id="seisWord">Waiting for the seismic node</div></div>
            <div class="seisLv" id="seisLv"><i>Calm</i><i>Light</i><i>Moderate</i><i>Strong</i></div>
          </div>
          <svg id="seisGraph" class="seisGraph" viewBox="0 0 300 80" preserveAspectRatio="none" aria-label="Shake level over the last 60 seconds"></svg>
          <div class="seisAxis"><span>60 s ago</span><span>30 s</span><span>now</span></div>
          <div class="seisStats">
            <div><span>Distance</span><b id="seisCm">--</b></div>
            <div><span>Sea</span><b id="seisBase">--</b></div>
            <div><span>Shake level</span><b id="seisMove">--</b></div>
            <div><span>Shakes</span><b id="seisPulse">--</b></div>
          </div>
          <div class="foot">Tsunami alert when the ultrasonic sees the water move 2 cm or more <b>and</b> the shake level is 50 or more, both together for 0.5 s</div>
        </section>
      </div>
    </div>

    <!-- History -->
    <section class="card white" id="c-hist">
      <div class="head">
        <div class="hl">
          <span class="icon lav"><svg viewBox="0 0 24 24"><path d="M3 3v18h18"/><path d="M7 15l4-5 3 3 5-7"/></svg></span>
          <div>
            <h2>City History</h2>
            <div class="sub">Last 24 hours · hourly</div>
          </div>
        </div>
        <span class="tag neutral" id="histTag" title="The model has not been running for 24 hours yet, so older hours are sample data. Real readings replace them hour by hour.">Sample data</span>
      </div>
      <div class="histGrid">
        <div class="hCharts" id="hCharts"></div>
        <div class="hLog">
          <div class="hLogHead"><b>Event log</b><span id="hLogCount"></span></div>
          <ol class="hEvents" id="hEvents"><li class="hEmpty">Loading history</li></ol>
        </div>
      </div>
      <div class="foot" id="histFoot">Sample hours and events are marked; real readings replace them as the model runs</div>
    </section>

  </main>
  <footer id="foot">Waiting for the first reading</footer>
</div>

<div class="toasts" id="toasts" aria-live="polite"></div>

<script>
document.documentElement.classList.add("js-anim");
// ---- simple status rules (flip these if a card shows backwards) ----
const WET_WHEN = "LOW";     // rain module DO level that means "raining"
const GAS_ALERT_PCT = 50, GAS_CLEAR_PCT = 45;   // gas level % for "Gas detected" / back to "Safe" (same as the Pi)
let gasAlert = false;
const DRY_WHEN = "HIGH";    // soil module DO level that means "dry"
const TEMP_HIGH_C = 38;     // temperature at or above this shows a warning

const ZONES = [
  {id: "weather",     keys: ["Rain", "Temp"]},
  {id: "environment", keys: ["Gas", "Soil"]},
  {id: "security",    keys: ["motion", "vibration"]},
  {id: "lighting",    keys: ["Light"]}
];
const CARDS = {
  Rain: {mtn: false}, Temp: {mtn: false}, Gas: {mtn: false}, Soil: {mtn: false},
  motion: {mtn: true, binary: true}, vibration: {mtn: true, binary: true}, Light: {mtn: true}
};
const KEYS = Object.keys(CARDS);
const RAIN_BARS = 6, RAIN_EVERY_MS = 2000, SOIL_LEN = 40, LIGHT_SEGS = 12;

const tagState = {};     // key -> [text, class] from the latest reading
const binState = {};     // key -> true/false (motion, vibration)
const trigAt = {};       // key -> browser time (ms) it was last triggered
const latest = {Rain: null, Soil: null, Light: null, Gas: null};
const rainHist = [], soilHist = [], gasHist = [];
let lastRainSample = 0, lastSoilSample = 0;
let lastRecv = 0, nanoConnected = false, linkLost = false, mSeenAt = 0;
let wxAt = 0, wxTemp = 0, wxPlace = "";
let trSeenAt = 0, trMsgTimer = 0;
let alarmActive = false, alarmAt = 0;
let mxSeenAt = 0, mxScreen = null, mxScreenLast = "";
let audioCtx = null, soundOn = true, unlocked = false, unlockedAt = 0, sirenTimer = 0, mp3Trying = false;
const alarmAudio = new Audio("/alarm.mp3");
alarmAudio.loop = true;
alarmAudio.preload = "auto";
// every alarm plays alarm.mp3. If it is ever blocked or fails to load, it is retried every second
// (beeps only fill the gap until the mp3 plays again; nothing switches to beeps for good)
alarmAudio.addEventListener("playing", () => stopBeeps());
alarmAudio.addEventListener("error", () => {           // failed to load: stop, beep for now, retry next second
  mp3Trying = false;
  alarmAudio.pause();
  if (soundOn && unlocked && (alarmActive || quakeActive)) startBeeps();
});
let mp3LastT = -1, mp3StuckSince = 0;
const tr = {mode: "manual", red: false, yellow: false, green: false, cm: null};

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function $(id) { return document.getElementById(id); }

// ---- build the repeated bits once ----
(function () {
  const bars = $("rainBars");
  for (let i = 0; i < RAIN_BARS; i++) {
    const b = el("div", "bar" + (i === RAIN_BARS - 1 ? " now" : ""));
    if (i === 0) b.appendChild(el("span", "blbl", "−" + ((RAIN_BARS - 1) * RAIN_EVERY_MS / 1000) + "s"));
    if (i === RAIN_BARS - 1) b.appendChild(el("span", "blbl", "Now"));
    b.appendChild(el("i"));
    bars.appendChild(b);
  }
  const segs = $("lightSegs");
  for (let i = 0; i < LIGHT_SEGS; i++) {
    const s = el("i");
    s.style.height = (34 + 66 * i / (LIGHT_SEGS - 1)).toFixed(0) + "%";
    segs.appendChild(s);
  }
})();
["red", "yellow", "green"].forEach(c => $("lamp-" + c).addEventListener("click", () => sendTraffic({light: c, on: !tr[c]})));
["manual", "auto"].forEach(m => $("mode-" + m).addEventListener("click", () => sendTraffic({mode: m})));

// ---- helpers ----
// ---- smooth numbers: glide from the old value to the new one ----
const REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
function countTo(node, num, after) {
  const to = parseFloat(num), txt = String(num);
  const dec = txt.indexOf(".") >= 0 ? txt.length - txt.indexOf(".") - 1 : 0;
  const from = node._val;
  node._val = to;
  cancelAnimationFrame(node._raf || 0);
  const put = t => { node.textContent = t; if (after) after(node); };
  if (REDUCED || isNaN(to) || from === undefined || isNaN(from) || from === to || /[^0-9.-]/.test(txt)) { put(txt); return; }
  const t0 = performance.now(), dur = 650;
  const step = now => {
    const k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3);
    put(k < 1 ? (from + (to - from) * e).toFixed(dec) : txt);
    if (k < 1) node._raf = requestAnimationFrame(step);
  };
  node._raf = requestAnimationFrame(step);
}
function setVal(key, num, unit) {
  countTo($("v-" + key), num, unit ? v => v.appendChild(el("small", "", unit)) : null);
}
function setTag(id, text, cls) {
  const t = $(id);
  t.textContent = text;
  t.className = "tag" + (cls ? " " + cls : "");
}
function lightWord(p) {
  return p < 15 ? "Dark" : p < 45 ? "Dim" : p < 80 ? "Bright" : "Very bright";
}
function fmtAgo(sec) {
  if (sec < 5) return "just now";
  if (sec < 60) return Math.round(sec) + "s ago";
  if (sec < 3600) return Math.round(sec / 60) + "m ago";
  return Math.round(sec / 3600) + "h ago";
}
// A node is "offline" only after NODE_GRACE_MS of silence (wifi on a phone hotspot often drops a packet or two),
// and only while the page itself is getting data from the Pi (otherwise it's the page's link, not the node).
const NODE_GRACE_MS = 10000, LINK_OK_MS = 4000;
function linkOk() { return lastRecv > 0 && Date.now() - lastRecv < LINK_OK_MS; }
function nanoFresh() { return nanoConnected && lastRecv > 0 && (Date.now() - lastRecv) < NODE_GRACE_MS; }
function mtnFresh() { return mSeenAt > 0 && (Date.now() - mSeenAt) < NODE_GRACE_MS; }
function wxFresh() { return wxAt > 0 && (Date.now() - wxAt) < 20 * 60 * 1000; }
function trFresh() { return trSeenAt > 0 && (Date.now() - trSeenAt) < NODE_GRACE_MS; }
function freshFor(k) { return k === "Temp" ? wxFresh() : CARDS[k].mtn ? mtnFresh() : nanoFresh(); }

// ---- temperature gauge (0-50 C) ----
const G = {cx: 160, cy: 160, r: 120, gap: 0.12};
function gPoint(f) {
  const a = Math.PI * (1 - f);
  return (G.cx + G.r * Math.cos(a)).toFixed(1) + " " + (G.cy - G.r * Math.sin(a)).toFixed(1);
}
function gArc(a, b) { return "M " + gPoint(a) + " A " + G.r + " " + G.r + " 0 0 1 " + gPoint(b); }
function setGauge(f) {   // f = 0..1, or null for no data
  const rest = f === null ? gArc(0, 1) : (f + G.gap < 1 ? gArc(f + G.gap, 1) : "");
  $("gRestO").setAttribute("d", rest);
  $("gRestH").setAttribute("d", rest);
  if (f === null) {
    $("gVal").setAttribute("d", "");
    $("gNeedle").style.display = "none";
    return;
  }
  f = Math.max(0, Math.min(1, f));
  $("gVal").setAttribute("d", f > 0.005 ? gArc(0, f) : "");
  $("gNeedle").style.display = "";
  $("gNeedle").setAttribute("transform", "rotate(" + (-(180 - f * 180)).toFixed(1) + " 160 112)");
}

// ---- rainfall bars (one bar every 2 s, the last one is live) ----
function drawRain() {
  const bars = $("rainBars").children;
  const off = RAIN_BARS - rainHist.length;
  for (let i = 0; i < RAIN_BARS; i++) {
    const v = i >= off ? rainHist[i - off] : null;
    bars[i].lastChild.style.height = v === null ? "0%" : Math.max(4, v / 1023 * 100).toFixed(1) + "%";
  }
}

// ---- soil moisture chart (one point a second, the last one is live) ----
function drawTrend(key, hist, knobId) {
  const n = hist.length;
  const k = $(knobId);
  if (!n) { k.style.display = "none"; return; }
  const step = 90 / (SOIL_LEN - 1);
  const pts = hist.map((v, i) => [94 - (n - 1 - i) * step, 34 - Math.max(0, Math.min(1, v / 1023)) * 28]);
  const line = pts.map(p => p[0].toFixed(2) + "," + p[1].toFixed(2)).join(" ");
  const area = pts[0][0].toFixed(2) + ",40 " + line + " " + pts[n - 1][0].toFixed(2) + ",40";
  $("ln-" + key).setAttribute("points", line);
  $("ar-" + key).setAttribute("points", area);
  $("ah-" + key).setAttribute("points", area);
  k.style.left = pts[n - 1][0] + "%";
  k.style.top = (pts[n - 1][1] / 40 * 100) + "%";
  k.style.display = "block";
}
function drawSoil() { drawTrend("Soil", soilHist, "k-Soil"); }
function drawGasTrend() { drawTrend("Gas", gasHist, "kc-Gas"); }

function sample() {
  const t = Date.now();
  if (!nanoFresh()) return;
  if (latest.Rain !== null && t - lastRainSample >= RAIN_EVERY_MS) {
    rainHist.push(latest.Rain);
    if (rainHist.length > RAIN_BARS) rainHist.shift();
    lastRainSample = t;
    drawRain();
  }
  if (latest.Soil !== null && t - lastSoilSample >= 1000) {
    if (!soilHist.length) for (let i = 1; i < SOIL_LEN; i++) soilHist.push(latest.Soil);   // start as a full flat line
    soilHist.push(latest.Soil);
    if (soilHist.length > SOIL_LEN) soilHist.shift();
    if (latest.Gas !== null) {
      if (!gasHist.length) for (let i = 1; i < SOIL_LEN; i++) gasHist.push(latest.Gas);
      gasHist.push(latest.Gas);
      if (gasHist.length > SOIL_LEN) gasHist.shift();
      drawGasTrend();
    }
    lastSoilSample = t;
    drawSoil();
  }
}

// ---- air quality bar ----
function setGas(raw) {
  const p = Math.max(0, Math.min(100, raw / 1023 * 100));
  const pos = "calc(12px + (100% - 24px) * " + (p / 100).toFixed(4) + ")";   // stays inside the rounded bar
  $("f-Gas").style.width = pos;
  $("k-Gas").style.left = pos;
  $("pct-Gas").textContent = "Level " + Math.round(p) + "%";
}

// ---- traffic signal ----
function trMsg(text) {
  $("trMsg").textContent = text;
  clearTimeout(trMsgTimer);
  trMsgTimer = setTimeout(() => { $("trMsg").textContent = ""; }, 3500);
}
function sendTraffic(body) {
  fetch("/traffic", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
    .then(r => r.json())
    .then(j => { if (!j.ok) trMsg(j.error || "Command failed"); })
    .catch(() => trMsg("Could not reach the dashboard"));
}
function paintTraffic() {
  const fresh = trFresh(), auto = tr.mode === "auto";
  ["red", "yellow", "green"].forEach(c => {
    const b = $("lamp-" + c);
    b.classList.toggle("on", fresh && tr[c]);
    b.disabled = !fresh || auto;           // in auto mode the sensor is in charge
  });
  $("mode-manual").classList.toggle("act", fresh && !auto);
  $("mode-auto").classList.toggle("act", fresh && auto);
  $("mode-manual").disabled = !fresh;
  $("mode-auto").disabled = !fresh;

  const k = $("k-dist");
  if (!fresh) {
    countTo($("v-dist"), "--");
    k.style.display = "none";
  } else if (tr.cm === null) {
    setVal("dist", "Clear", "nothing in range");
    k.style.display = "none";
  } else {
    setVal("dist", Math.round(tr.cm), "cm");
    k.style.left = Math.min(100, tr.cm / 50 * 100).toFixed(1) + "%";
    k.style.display = "block";
  }
  $("c-traffic").classList.toggle("stale", !fresh);
  $("trSub").textContent = !fresh ? (trSeenAt ? "Traffic node offline" : "Waiting for the traffic node")
    : auto ? "Auto: green normally, yellow under 20 cm, red under 10 cm"
    : "Manual: tap a light to switch it on or off";

  const lit = ["red", "yellow", "green"].filter(c => tr[c]).map(c => c[0].toUpperCase() + c.slice(1));
  const pill = !fresh ? (trSeenAt ? "Offline" : "No data") : auto ? "Auto" : "Manual";
  setTag("trPill", pill, !fresh ? "" : auto ? "info" : "neutral");
  $("zd-traffic").className = "tdot" + (!fresh ? "" : auto && tr.red ? " warn" : " ok");
  $("zt-traffic").textContent = !fresh ? pill : pill + " · " + (lit.length ? lit.join(", ") + " on" : "all off");
  $("pTr").className = "pulse " + (fresh ? "live" : (trSeenAt ? "err" : ""));
  $("tTr").textContent = fresh ? "Online" : trSeenAt ? "Offline" : "Waiting";
}

// ---- intruder alarm: banner, flashing card and siren ----
function beep() {
  if (!audioCtx) return;
  const t = audioCtx.currentTime;
  [[880, 0], [660, 0.25]].forEach(p => {
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.type = "square";
    o.frequency.value = p[0];
    g.gain.setValueAtTime(0.0001, t + p[1]);
    g.gain.exponentialRampToValueAtTime(0.25, t + p[1] + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t + p[1] + 0.22);
    o.connect(g);
    g.connect(audioCtx.destination);
    o.start(t + p[1]);
    o.stop(t + p[1] + 0.24);
  });
}
// browsers only allow sound after the user taps the page once, so the first tap anywhere unlocks it
function unlockSound() {
  if (unlocked) return;
  unlocked = true;
  unlockedAt = Date.now();
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    audioCtx.resume();
  } catch (e) {}
  if (!alarmActive) {
    alarmAudio.muted = true;                 // silent play/pause = permission for later
    alarmAudio.play().then(() => { alarmAudio.pause(); alarmAudio.currentTime = 0; alarmAudio.muted = false; })
      .catch(() => { alarmAudio.muted = false; });
  }
  soundLabel();
  updateSiren();
  if (!alarmActive) setTimeout(() => chime("ok"), 120);   // a short "sound is on" chime
}
["pointerdown", "keydown", "touchstart"].forEach(ev => document.addEventListener(ev, unlockSound, {once: false, passive: true}));
function soundLabel() {
  const b = $("soundBtn");
  b.textContent = !unlocked ? "🔔 Tap to enable sound" : soundOn ? "🔔 Sound on" : "🔕 Sound off";
  b.classList.toggle("on", unlocked && soundOn);
}
function startBeeps() { if (!sirenTimer) { beep(); sirenTimer = setInterval(beep, 500); } }
function stopBeeps() { if (sirenTimer) { clearInterval(sirenTimer); sirenTimer = 0; } }
function playMp3() {
  if (mp3Trying || !alarmAudio.paused) return;
  mp3Trying = true;
  if (alarmAudio.error) alarmAudio.load();          // failed to load before: fetch alarm.mp3 again
  alarmAudio.muted = false;
  alarmAudio.play()
    .then(() => { mp3Trying = false; stopBeeps(); })
    .catch(() => { mp3Trying = false; startBeeps(); });   // blocked for now: beep, retry next second
}
function updateSiren() {
  const want = soundOn && (alarmActive || quakeActive) && unlocked;
  if (want) {
    if (!alarmAudio.paused) {                         // "playing" but not moving for 2 s = stuck: restart it
      const t = alarmAudio.currentTime;
      if (t !== mp3LastT) { mp3LastT = t; mp3StuckSince = Date.now(); stopBeeps(); }
      else if (Date.now() - mp3StuckSince > 2000) { alarmAudio.pause(); alarmAudio.load(); startBeeps(); }
    }
    if (alarmAudio.paused) playMp3();
  } else {
    if (!alarmAudio.paused) alarmAudio.pause();
    alarmAudio.currentTime = 0;                       // next alarm starts the mp3 from the beginning
    stopBeeps();
  }
}
// any tap during an alarm retries the mp3 straight away
document.addEventListener("pointerdown", () => { if (alarmActive || quakeActive) updateSiren(); }, {passive: true});
$("soundBtn").addEventListener("click", () => {
  if (Date.now() - unlockedAt > 600) soundOn = !soundOn;   // the tap that unlocks sound doesn't mute it
  soundLabel();
  if (soundOn && !alarmActive && Date.now() - unlockedAt > 600) chime("ok");
  updateSiren();
});
$("silenceBtn").addEventListener("click", () => {
  fetch("/alarm/silence", {method: "POST"}).catch(() => {});
  alarmActive = false;                     // hide right away; the Pi confirms on every screen
  quakeActive = false;
  paintAlarm();
});
function paintAlarm() {
  const any = alarmActive || quakeActive;
  $("alarmBar").classList.toggle("on", any);
  $("secCard").classList.toggle("alarm", alarmActive);
  $("c-seis").classList.toggle("alarm", quakeActive);
  if (quakeActive) {
    $("alarmTitle").textContent = "Tsunami alert · Sea moving" + (alarmActive ? " + Mountain" : "");
    $("alarmTxt").textContent = "Sea movement and strong shaking together · started " +
      fmtAgo((Date.now() - quakeAt) / 1000) + " · take cover · stops after 30 s of calm, or tap Silence";
  } else if (alarmActive) {
    $("alarmTitle").textContent = "Potential disaster · Mountain";
    $("alarmTxt").textContent = "Motion and vibration detected together · started " +
      fmtAgo((Date.now() - alarmAt) / 1000) + " · stops after 30 s of quiet, or tap Silence";
  }
  document.title = quakeActive ? "⚠️ TSUNAMI ALERT · Smart City" : alarmActive ? "⚠️ DANGER · Smart City" : "Smart City Operations Center";
  updateSiren();
}

// ---- popups (toasts) for city events: fire only when something changes ----
const TOAST_MAX = 4, TOAST_MS = 8000, WARMUP_MS = 12000;
const pageStart = Date.now();
const cond = {};
function chime(kind) {
  if (!audioCtx || !unlocked || !soundOn) return;
  const t = audioCtx.currentTime;
  const notes = kind === "warn" ? [660, 880, 660] : kind === "ok" ? [880, 1175] : [784, 988];
  notes.forEach((f, i) => {
    const o = audioCtx.createOscillator(), g = audioCtx.createGain(), at = t + i * 0.18;
    o.type = "triangle";
    o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, at);
    g.gain.exponentialRampToValueAtTime(0.6, at + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, at + 0.45);
    o.connect(g);
    g.connect(audioCtx.destination);
    o.start(at);
    o.stop(at + 0.47);
  });
}
function toast(kind, title, text, key) {
  const box = $("toasts");
  if (key) [...box.children].forEach(c => { if (c.dataset.key === key) c.remove(); });
  while (box.children.length >= TOAST_MAX) box.firstChild.remove();
  const t = el("div", "toast " + kind);
  if (key) t.dataset.key = key;
  t.setAttribute("role", "status");
  t.appendChild(el("span", "ti", kind === "warn" ? "!" : kind === "ok" ? "✓" : "i"));
  const body = el("div", "tt");
  body.appendChild(el("b", "", title));
  body.appendChild(el("span", "", text));
  t.appendChild(body);
  t.appendChild(el("span", "tx", "×"));
  const close = () => { t.classList.add("out"); setTimeout(() => t.remove(), 260); };
  t.addEventListener("click", close);
  if (kind !== "warn") setTimeout(close, TOAST_MS);    // warnings stay until tapped
  box.appendChild(t);
  chime(kind);
}
// state: true / false, or null when unknown (no fresh data) so nothing fires
function watch(key, state, on, off) {
  if (state === null) return;
  const prev = cond[key];
  cond[key] = state;
  if (prev === undefined || prev === state || Date.now() - pageStart < WARMUP_MS) return;
  if (state) toast(on[0], on[1], on[2], key);
  else if (off) toast(off[0], off[1], off[2], key);
}
function watchEvents() {
  const st = k => (freshFor(k) && tagState[k]) ? tagState[k] : null;
  const g = st("Gas"), r = st("Rain"), so = st("Soil");
  watch("gas", g ? g[1] === "warn" : null,
        ["warn", "Air quality poor", "The gas sensor detects gas in the city air"],
        ["ok", "Air quality back to normal", "Gas level is safe again"]);
  watch("rain", r ? r[0] === "Raining" : null,
        ["info", "It's raining", "The rain sensor turned wet"],
        ["ok", "Rain stopped", "The rain sensor is dry again"]);
  watch("soil", so ? so[0] === "Dry" : null,
        ["info", "Soil is dry", "The green spaces need water"],
        ["ok", "Soil moisture OK", "The soil is moist again"]);
  watch("temp", wxFresh() ? wxTemp >= TEMP_HIGH_C : null,
        ["warn", "High temperature", wxTemp.toFixed(1) + " °C in " + (wxPlace || "the city")],
        ["ok", "Temperature back to normal", wxTemp.toFixed(1) + " °C in " + (wxPlace || "the city")]);
  [["ctl", "Main controller", nanoFresh(), lastRecv > 0],
   ["mtn", "Mountain node", mtnFresh(), mSeenAt > 0],
   ["tr", "Traffic node", trFresh(), trSeenAt > 0],
   ["se", "Seismic node", seisFresh(), seisSeenAt > 0]].forEach(n => {
    if (!n[3]) return;                       // never seen yet: nothing to report
    watch("node-" + n[0], linkOk() ? !n[2] : null,   // page link down: not the node's fault, say nothing
          ["warn", n[1] + " offline", "No data for 10 seconds: check its power and wifi"],
          ["ok", n[1] + " back online", "Receiving data again"]);
  });
}

// ---- new data from the Pi ----
function render(data) {
  lastRecv = Date.now();
  linkLost = false;
  nanoConnected = !!data.connected;

  const sn = data.sensors || {};
  if (sn.Rain) {
    const s = sn.Rain;
    latest.Rain = s.raw;
    setVal("Rain", s.raw, "raw");
    $("l-Rain").textContent = s.volt.toFixed(2) + " V";
    $("r-Rain").textContent = "D4 " + s.digital;
    tagState.Rain = s.digital === WET_WHEN ? ["Raining", "info"] : ["Dry", "ok"];
    if (rainHist.length) { rainHist[rainHist.length - 1] = s.raw; drawRain(); }
  }
  if (sn.Gas) {
    const s = sn.Gas;
    setVal("Gas", s.raw, "raw");
    setGas(s.raw);
    latest.Gas = s.raw;
    if (gasHist.length) { gasHist[gasHist.length - 1] = s.raw; drawGasTrend(); }
    $("l-Gas").textContent = s.volt.toFixed(2) + " V · D8 " + s.digital;
    const gp = s.raw / 1023 * 100;
    if (gp >= GAS_ALERT_PCT) gasAlert = true; else if (gp < GAS_CLEAR_PCT) gasAlert = false;
    tagState.Gas = gasAlert ? ["Gas detected", "warn"] : ["Safe", "ok"];
    $("c-Gas").classList.toggle("warn", gasAlert);
  }
  if (sn.Soil) {
    const s = sn.Soil;
    latest.Soil = s.raw;
    setVal("Soil", s.raw, "raw");
    $("l-Soil").textContent = s.volt.toFixed(2) + " V · D10 " + s.digital;
    tagState.Soil = s.digital === DRY_WHEN ? ["Dry", "warn"] : ["Moist", "ok"];
    if (soilHist.length) { soilHist[soilHist.length - 1] = s.raw; drawSoil(); }
  }

  const w = data.weather;
  if (w && w.tempC !== null && w.tempC !== undefined) {
    wxTemp = w.tempC;
    wxAt = Date.now() - w.ageSec * 1000;
    wxPlace = w.place;
  }

  const m = data.mountain;
  if (m) {
    mSeenAt = Date.now() - m.ageSec * 1000;
    if (m.light !== null && m.light !== undefined) {
      latest.Light = m.light;
      tagState.Light = [lightWord(m.light), "neutral"];
    }
    ["motion", "vibration"].forEach(k => {
      const on = !!m.values[k];
      binState[k] = on;
      tagState[k] = on ? ["Detected", "warn"] : ["Clear", "ok"];
      const ago = m.agoSec[k];
      trigAt[k] = ago === null ? 0 : Date.now() - ago * 1000;
    });
  }
  if (data.seismic) { seis = data.seismic; seisSeenAt = Date.now() - data.seismic.ageSec * 1000; }
  if (data.quake) {
    quakeActive = !!data.quake.active;
    quakeAt = Date.now() - (data.quake.agoSec || 0) * 1000;
    quakePeak = data.quake.peak || 0;
  }
  if (data.gate) gateRender(data.gate);
  if (data.system) { sysd = data.system; sysAt = Date.now() - data.system.ageSec * 1000; }
  if (data.density) { dens = data.density; densAt = Date.now(); }
  if (data.matrix) { mxSeenAt = Date.now() - data.matrix.ageSec * 1000; mxScreen = data.matrix.screen || null; }
  if (data.alarm) {
    alarmActive = !!data.alarm.active;
    alarmAt = Date.now() - (data.alarm.agoSec || 0) * 1000;
  }
  if (data.traffic) {
    const t = data.traffic;
    trSeenAt = Date.now() - t.ageSec * 1000;
    tr.mode = t.mode; tr.red = !!t.red; tr.yellow = !!t.yellow; tr.green = !!t.green; tr.cm = t.cm;
  }
  paint();
}

// ---- repaint everything that depends on freshness or time (every update and every second) ----
function paintTemp() {
  if (wxFresh()) {
    setVal("Temp", wxTemp.toFixed(1), "°C");
    setGauge(wxTemp / 50);
    tagState.Temp = wxTemp >= TEMP_HIGH_C ? ["High", "warn"] : ["Normal", "ok"];
    $("l-Temp").textContent = "Updated " + fmtAgo((Date.now() - wxAt) / 1000) + " · Open-Meteo";
  } else {
    countTo($("v-Temp"), "--");
    setGauge(null);
    $("l-Temp").textContent = wxAt ? "Last update " + fmtAgo((Date.now() - wxAt) / 1000) : "Waiting for the internet";
  }
  $("p-Temp").textContent = wxPlace || "Live weather";
}

function paintLight(fresh) {
  const ok = fresh && latest.Light !== null;
  const lit = ok ? Math.round(latest.Light / 100 * LIGHT_SEGS) : 0;
  const segs = $("lightSegs").children;
  for (let i = 0; i < LIGHT_SEGS; i++) segs[i].classList.toggle("on", i < lit);
  if (ok) setVal("Light", latest.Light, "%");
  else countTo($("v-Light"), "--");
}

const DISP_NAMES = { weather: "Weather", temp: "Temperature", air: "Air quality", alarm: "DANGER", quake: "EARTHQUAKE", wait: "Waiting for Pi" };
function oT(x, y, size, s, anchor, fill) {
  const fs = size * 8;   // size 1 = 8 px tall, like the real screen
  return '<text x="' + x + '" y="' + y + '" font-size="' + fs + '" text-anchor="' + (anchor || "start") + '"' +
    (fill ? ' style="fill:' + fill + '"' : '') + '>' + s + '</text>';
}
function oHead(t) { return oT(0, 7, 1, t) + '<line class="ln" x1="0" y1="10.5" x2="128" y2="10.5"/>'; }
function paintDisplay() {
  const on = mxSeenAt > 0 && (Date.now() - mxSeenAt) < NODE_GRACE_MS;
  const scr = on ? (mxScreen || "wait") : "off";
  $("oledGlass").classList.toggle("off", !on);
  const pill = $("dispPill");
  pill.textContent = on ? (mxScreen ? DISP_NAMES[scr] : "Online") : (mxSeenAt ? "Offline" : "Waiting");
  pill.className = "tag " + (on ? (scr === "alarm" || scr === "quake" ? "warn" : "ok") : "");
  $("dispFoot").textContent = on ? "Live copy of what the display in the model is showing · changes every 5 s"
                                 : "Display not connected: switch on the NodeMCU with the OLED";
  const rain = tagState.Rain && nanoFresh() ? tagState.Rain[0] === "Raining" : null;
  const gas = tagState.Gas && nanoFresh() ? tagState.Gas[0] === "Gas detected" : null;
  let h = "";
  if (scr === "off") {
    h = oHead("SMART CITY") + oT(4, 34, 1, "Display offline");
  } else if (scr === "alarm") {
    const blink = Math.floor(Date.now() / 1000) % 2 === 0;
    const fg = blink ? "#05070a" : "#dff4ff";
    if (blink) h += '<rect class="fl" x="0" y="0" width="128" height="64"/>';
    const bg = blink ? "#dff4ff" : "#05070a";
    h += '<path d="M22 4 L42 44 L2 44 Z" style="fill:' + fg + '"/>' +
         '<rect x="20" y="16" width="4" height="16" style="fill:' + bg + '"/><rect x="20" y="36" width="4" height="4" style="fill:' + bg + '"/>' +
         oT(50, 32, 2, "DANGER", "start", fg) + oT(10, 60, 1, "Potential disaster", "start", fg);
  } else if (scr === "quake") {
    const blink = Math.floor(Date.now() / 1000) % 2 === 0;
    const fg = blink ? "#05070a" : "#dff4ff";
    if (blink) h += '<rect class="fl" x="0" y="0" width="128" height="64"/>';
    const A = [1, 2, 1, 4, 2, 5, 5, 5, 8, 4, 10, 2, 11, 1, 10, 4, 8, 5, 5, 5, 3, 4, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1];
    const pts = A.map((a, i) => (i * 4) + "," + (34 + (i % 2 ? a : -a))).join(" ");
    h += oT(64, 20, 2, "EARTHQUAKE", "middle", fg) +
         '<polyline points="' + pts + '" style="fill:none;stroke:' + fg + ';stroke-width:1.5"/>' +
         oT(64, 60, 1, "Take cover now!", "middle", fg);
  } else if (scr === "weather") {
    h = oHead("WEATHER");
    if (rain) {
      h += '<path class="ln" stroke-width="2" d="M14 40 a8 8 0 0 1 4-15 a10 10 0 0 1 19 2 a7 7 0 0 1 1 13 z"/>' +
           '<path class="ln" stroke-width="2" d="M20 46 l-2 6 M28 46 l-2 6 M36 46 l-2 6"/>' +
           oT(60, 37, 2, "Rain") + oT(60, 52, 1, "raining now");
    } else {
      h += '<circle class="ln" stroke-width="2" cx="28" cy="37" r="8"/>' +
           '<path class="ln" stroke-width="2" d="M28 21v4M28 49v4M12 37h4M40 37h4M17 26l3 3M36 45l3 3M17 48l3-3M36 29l3-3"/>' +
           oT(60, 37, 2, rain === null ? "--" : "Dry") + oT(60, 52, 1, rain === null ? "no data" : "no rain");
    }
  } else if (scr === "temp") {
    h = oHead("TEMPERATURE") + oT(60, 44, 3, wxFresh() ? wxTemp.toFixed(1) : "--", "middle") +
        '<circle class="ln" stroke-width="1.5" cx="' + (wxFresh() ? 106 : 92) + '" cy="23" r="2.5"/>' +
        oT(wxFresh() ? 110 : 96, 36, 2, "C") + oT(64, 62, 1, wxPlace || "Thaliparamba", "middle");
  } else if (scr === "air") {
    h = oHead("AIR QUALITY");
    if (gas) {
      h += '<path class="ln" stroke-width="2" d="M24 20 L42 52 H6 Z"/><path class="ln" stroke-width="2.5" d="M24 31v10M24 45v2"/>' +
           oT(52, 37, 2, "POOR") + oT(52, 52, 1, "gas detected");
    } else {
      h += '<circle class="ln" stroke-width="2" cx="24" cy="37" r="14"/><path class="ln" stroke-width="2.5" d="M17 37l5 5 9-10"/>' +
           oT(52, 37, 2, gas === null ? "--" : "Good") + oT(52, 52, 1, gas === null ? "no data" : "air is safe");
    }
  } else {
    h = oHead("SMART CITY") + oT(4, 32, 1, "Waiting for Pi");
  }
  const key = scr + "|" + h.length + "|" + h.slice(-40) + (scr === "alarm" || scr === "quake" ? Math.floor(Date.now() / 1000) % 2 : "");
  if (key !== mxScreenLast) { $("oledSvg").innerHTML = h; mxScreenLast = key; }
}

function paint() {
  const nf = nanoFresh(), mf = mtnFresh();
  sample();
  paintTemp();
  paintLight(mf);

  KEYS.forEach(k => {
    const c = CARDS[k];
    const fresh = freshFor(k);
    const st = fresh && tagState[k] ? tagState[k] : [c.mtn && mSeenAt ? "Offline" : "No data", ""];
    setTag("t-" + k, st[0], st[1]);
    const card = $("c-" + k);
    card.classList.toggle("stale", !fresh);
    if (c.binary) {
      card.classList.toggle("hit", fresh && !!binState[k]);
      const t = trigAt[k];
      $("ml-" + k).textContent = t ? "Last triggered " + fmtAgo((Date.now() - t) / 1000) : "Never triggered";
    }
  });

  ZONES.forEach(z => {
    let warns = 0, anyFresh = false;
    z.keys.forEach(k => {
      if (!freshFor(k) || !tagState[k]) return;
      anyFresh = true;
      if (tagState[k][1] === "warn") warns++;
    });
    const txt = !anyFresh ? "No data" : warns ? warns + (warns === 1 ? " warning" : " warnings") : "All normal";
    $("zd-" + z.id).className = "tdot" + (!anyFresh ? "" : warns ? " warn" : " ok");
    $("zt-" + z.id).textContent = txt;
    if (z.id === "security") {
      setTag("secSum", !anyFresh ? (mSeenAt ? "Offline" : "No data") : warns ? txt : "All clear",
             !anyFresh ? "" : warns ? "warn" : "ok");
    }
  });

  paintTraffic();
  paintAlarm();
  watchEvents();

  // link chips in the top bar
  const ctlSeen = lastRecv > 0;
  $("pCtl").className = "pulse " + (nf ? "live" : (ctlSeen || linkLost ? "err" : ""));
  $("tCtl").textContent = linkLost ? "Reconnecting" : nf ? "Online" : ctlSeen ? "Offline" : "Connecting";
  $("pMtn").className = "pulse " + (mf ? "live" : (mSeenAt ? "err" : ""));
  $("tMtn").textContent = mf ? "Online" : mSeenAt ? "Offline" : "Waiting";

  const mxOn = mxSeenAt > 0 && (Date.now() - mxSeenAt) < NODE_GRACE_MS;
  $("pMx").className = "pulse " + (mxOn ? "live" : (mxSeenAt ? "err" : ""));
  $("tMx").textContent = mxOn ? "Online" : mxSeenAt ? "Offline" : "Waiting";
  paintDisplay();
  paintSeismic();
  paintGate();
  paintSys();
  paintDensity();
  const sf = seisFresh() && seis;
  $("zd-seismic").className = "tdot" + (!sf ? "" : quakeActive || seisLevel(seis.intensity) >= 2 ? " warn" : " ok");
  $("zt-seismic").textContent = !sf ? (seisSeenAt ? "Offline" : "No data") : quakeActive ? "TSUNAMI ALERT" : SEIS_WORDS[seisLevel(seis.intensity)];
  const dOn = mxSeenAt > 0 && (Date.now() - mxSeenAt) < NODE_GRACE_MS;
  $("zd-display").className = "tdot" + (dOn ? " ok" : "");
  $("zt-display").textContent = dOn ? "Showing " + (DISP_NAMES[mxScreen] || "status").toLowerCase() : mxSeenAt ? "Offline" : "No data";
  const now = new Date(), hr = now.getHours();
  $("greet").textContent = hr >= 5 && hr < 12 ? "Good morning ☀️" : hr >= 12 && hr < 17 ? "Good afternoon 🌤️" : "Good evening 🌙";
  $("clockT").textContent = now.toLocaleTimeString([], {hour12: false});
  $("clockD").textContent = now.toLocaleDateString(undefined, {weekday: "short", day: "numeric", month: "short"});
  const age = lastRecv ? (Date.now() - lastRecv) / 1000 : null;
  $("foot").textContent = age === null ? "Waiting for the first reading" : "Smart City Operations Center · last update " + Math.round(age) + "s ago";
}

// ---- seismic monitor: live shake trace (5 samples a second, last 60 s) ----
const SEIS_N = 300, SEA_MOVE_CM = 2, QUAKE_SHAKE_MIN = 50;   // same as on the Pi
let seis = null, seisSeenAt = 0, seisTrace = [], quakeActive = false, quakeAt = 0, quakePeak = 0;
function seisFresh() { return seisSeenAt > 0 && (Date.now() - seisSeenAt) < NODE_GRACE_MS; }
function seisLevel(v) { return v < 10 ? 0 : v < 35 ? 1 : v < 65 ? 2 : 3; }
const SEIS_WORDS = ["Calm", "Light shaking", "Moderate shaking", "Strong shaking"];
function seisSample() {
  seisTrace.push(seisFresh() && seis ? seis.intensity : null);
  if (seisTrace.length > SEIS_N) seisTrace.shift();
  drawSeis();
}
function drawSeis() {
  const W = 300, H = 80, mid = H / 2, n = seisTrace.length, x0 = W - n;
  let d = "", pen = false;
  seisTrace.forEach((v, i) => {
    if (v === null) { pen = false; return; }
    const amp = Math.max(0.6, v / 100 * (mid - 4));          // seismograph look: swing up and down
    const y = mid + (i % 2 ? amp : -amp);
    d += (pen ? "L" : "M") + (x0 + i) + " " + y.toFixed(1);
    pen = true;
  });
  const lim = 35 / 100 * (mid - 4);
  const col = quakeActive ? "#ff5a4a" : "#1c1c1c";
  $("seisGraph").innerHTML = '<line class="mid" x1="0" y1="' + mid + '" x2="' + W + '" y2="' + mid + '"/>' +
    '<line class="lim" x1="0" y1="' + (mid - lim) + '" x2="' + W + '" y2="' + (mid - lim) + '"/>' +
    '<line class="lim" x1="0" y1="' + (mid + lim) + '" x2="' + W + '" y2="' + (mid + lim) + '"/>' +
    (d ? '<path d="' + d + '" fill="none" stroke="' + col + '" stroke-width="1.6" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>' : "");
}
function paintSeismic() {
  const fresh = seisFresh();
  $("pSe").className = "pulse " + (fresh ? "live" : (seisSeenAt ? "err" : ""));
  $("tSe").textContent = fresh ? "Online" : seisSeenAt ? "Offline" : "Waiting";
  const pill = $("seisPill"), lv = $("seisLv").children;
  $("c-seis").classList.toggle("alarm", quakeActive);
  if (!fresh || !seis) {
    pill.textContent = seisSeenAt ? "Offline" : "No data";
    pill.className = "tag";
    countTo($("seisInt"), "--");
    $("seisWord").textContent = seisSeenAt ? "Seismic node offline · check its power and wifi" : "Waiting for the seismic node";
    for (let i = 0; i < 4; i++) lv[i].classList.remove("on");
    ["seisCm", "seisBase", "seisMove", "seisPulse"].forEach(k => { $(k).textContent = "--"; });
    return;
  }
  const recent = seisTrace.slice(-10).filter(v => v !== null);         // peak of the last 2 s
  const peak = Math.max(seis.intensity, recent.length ? Math.max.apply(null, recent) : 0);
  const L = seisLevel(peak);
  for (let i = 0; i < 4; i++) lv[i].classList.toggle("on", i === L);
  countTo($("seisInt"), seis.intensity);
  $("seisWord").textContent = quakeActive ? "Tsunami alert · strongest shake " + quakePeak + "/100" : SEIS_WORDS[L];
  pill.textContent = quakeActive ? "TSUNAMI ALERT" : SEIS_WORDS[L].split(" ")[0];
  pill.className = "tag " + (quakeActive || L >= 2 ? "warn" : L === 1 ? "info" : "ok");
  $("seisCm").textContent = seis.cm === null ? "no echo" : seis.cm.toFixed(1) + " cm";
  const seaMoving = seis.cm !== null && seis.move >= SEA_MOVE_CM;
  $("seisBase").textContent = seis.cm === null ? "--" : seaMoving ? "Moving" : "Still";
  $("seisBase").style.color = seaMoving ? "#e0362a" : "";
  $("seisMove").textContent = seis.intensity + " / 100";
  $("seisPulse").textContent = (seis.pulses * 5) + " /s";
}
setInterval(seisSample, 200);

// ---- City Risk: one 0-100 score from every hazard (worked out with the AI model every 2 s) ----
const RISK_HZ = [{k: "tsunami", name: "Tsunami", w: 1}, {k: "slide", name: "Landslide", w: 0.9},
                 {k: "flood", name: "Flood", w: 0.85}, {k: "air", name: "Air pollution", w: 0.8},
                 {k: "heat", name: "Heat", w: 0.6}];
function riskBuild() {
  $("riskHz").innerHTML = RISK_HZ.map(h => '<div class="rz" id="rz-' + h.k + '"><span>' + h.name + '</span><b id="rzv-' + h.k +
    '">--</b><div class="sysMini"><i id="rzb-' + h.k + '"></i></div></div>').join("");
}
function paintRisk(p, why) {
  let top = null, sum = 0;
  RISK_HZ.forEach(h => {
    const v = Math.max(0, Math.min(1, p[h.k] || 0)), wv = v * h.w;
    sum += wv;
    if (!top || wv > top.wv) top = {k: h.k, name: h.name, wv: wv, v: v};
    $("rzv-" + h.k).textContent = Math.round(v * 100) + "%";
    sysBar("rzb-" + h.k, v * 100, 60);
  });
  let score = Math.round(100 * (0.75 * top.wv + 0.25 * sum / RISK_HZ.length));
  if (quakeActive || alarmActive) score = Math.max(score, 92);
  score = Math.max(0, Math.min(100, score));
  const lvl = score < 30 ? ["Low", "ok"] : score < 60 ? ["Medium", "info"] : ["High", "warn"];
  countTo($("riskVal"), score);
  $("riskVal").style.color = score >= 60 ? "#e0362a" : score >= 30 ? "#c2410c" : "";
  setTag("riskPill", lvl[0] + " risk", lvl[1]);
  $("riskKnob").style.left = score + "%";
  $("c-risk").classList.toggle("hi", score >= 60);
  RISK_HZ.forEach(h => $("rz-" + h.k).classList.toggle("lead", score >= 15 && h.k === top.k));
  const w = $("riskWhy");
  w.textContent = "";
  if (score < 15) { w.appendChild(el("b", "", "All clear · ")); w.appendChild(document.createTextNode("every hazard is low, the city is safe")); }
  else { w.appendChild(el("b", "", "Main reason: " + top.name + " · ")); w.appendChild(document.createTextNode(why[top.k] || "")); }
}
riskBuild();

// ---- Traffic density: vehicles the traffic ultrasonic counted (+ sample traffic) ----
const DENS_ICON = {Car: "🚗", Bike: "🏍️", Auto: "🛺", Bus: "🚌"};
let dens = null, densAt = 0, densLogSig = "";
(function densBuild() { let h = ""; for (let i = 0; i < 30; i++) h += "<i></i>"; $("densBars").innerHTML = h; })();
function paintDensity() {
  const d = dens;
  if (!d) return;
  const fresh = trFresh();
  $("densRate").textContent = d.perMin.toFixed(1);
  $("densPct").textContent = d.pct + "%";
  const f = $("densFill");
  f.style.width = Math.max(2, d.pct) + "%";
  f.className = d.pct >= 70 ? "hi" : d.pct >= 35 ? "mid" : "";
  setTag("densPill", d.level + " traffic", d.pct >= 70 ? "warn" : d.pct >= 35 ? "info" : "ok");
  $("densWord").textContent = "Vehicles a minute · " + (d.level === "Heavy" ? "road is jammed" : d.level === "Medium" ? "steady flow" : "free flowing");
  const bars = $("densBars").children, mx = Math.max(4, d.busiest);
  for (let i = 0; i < 30; i++) bars[i].style.height = Math.max(4, d.bars[i] / mx * 100) + "%";
  $("densTot").textContent = d.total30 + " vehicles";
  $("densGap").textContent = d.gapSec === null ? "--" : Math.round(d.gapSec) + " s";
  $("densMax").textContent = d.busiest + " /min";
  $("densLive").textContent = fresh || d.liveTotal ? d.liveTotal + " live" : "sensor off";
  const ul = $("densLog"), sig = d.log.map(v => v.type + v.ago.toFixed(0)).join();
  if (sig === densLogSig) return;
  densLogSig = sig;
  ul.textContent = "";
  d.log.forEach(v => {
    const li = el("li", "");
    li.appendChild(el("span", "vt", DENS_ICON[v.type] || "🚗"));
    const gi = el("div", "gi");
    gi.appendChild(el("b", "", v.type + (v.cm !== null ? " · " + Math.round(v.cm) + " cm" : "")));
    li.appendChild(gi);
    li.appendChild(el("span", "lv" + (v.live ? " live" : ""), v.live ? "live" : "sample"));
    li.appendChild(el("span", "ago", v.ago < 60 ? Math.round(v.ago) + " s ago" : Math.round(v.ago / 60) + " min ago"));
    ul.appendChild(li);
  });
}

// ---- Raspberry Pi system card ----
let sysd = null, sysAt = 0, sysSure = false, sysSureT = 0, sysRestarting = 0;
function sysFresh() { return sysAt > 0 && Date.now() - sysAt < 15000; }
function fmtDur(s) {
  if (s === null || s === undefined) return "--";
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? d + "d " + h + "h" : h ? h + "h " + m + "m" : m + "m";
}
function sysBar(id, pct, hi) {
  const b = $(id);
  b.style.width = (pct === null || pct === undefined ? 0 : Math.max(2, Math.min(100, pct))) + "%";
  b.classList.toggle("hi", pct !== null && pct !== undefined && pct >= hi);
}
function paintSys() {
  const s = sysd, fresh = sysFresh() && s;
  $("c-sys").classList.toggle("stale", !fresh);
  if (sysRestarting) {
    if (!linkLost && lastRecv > sysRestarting + 1500) {
      sysRestarting = 0;
      $("sysMsg").textContent = "Dashboard restarted ✓";
      setTimeout(() => { $("sysMsg").textContent = ""; }, 4000);
    } else { setTag("sysPill", "Restarting…", "info"); return; }
  }
  if (!fresh) { setTag("sysPill", sysAt ? "No data" : "Waiting", ""); return; }
  const t = s.tempC, pw = s.power;
  const hot = t !== null && t >= 80, warm = t !== null && t >= 70;
  const bad = hot || pw === "low" || !s.ssid;
  const mid = warm || pw === "was" || (s.signal !== null && s.signal < 30) || (s.diskFreeGB !== undefined && s.diskFreeGB < 1);
  setTag("sysPill", bad ? "Needs attention" : mid ? "Check" : "Healthy", bad || mid ? "warn" : "ok");
  if (t === null) $("sysTemp").textContent = "--";
  else countTo($("sysTemp"), t.toFixed(1), v => v.appendChild(el("small", "", "°C")));
  $("sysWord").textContent = t === null ? "CPU temperature not available" :
    "CPU temperature · " + (hot ? "too hot, slowing down" : warm ? "hot, give it air" : t >= 55 ? "warm, fine" : "cool");
  $("sysKnob").style.left = t === null ? "0%" : Math.max(0, Math.min(100, (t - 30) / 50 * 100)) + "%";
  $("sysPower").textContent = pw === "ok" ? "Good" : pw === "low" ? "LOW" : pw === "was" ? "Dipped" : "--";
  $("sysPower").style.color = pw === "low" ? "#e0362a" : pw === "was" ? "#c2410c" : "";
  $("sysPowerS").textContent = pw === "ok" ? "no under-voltage" : pw === "low" ? "under-voltage now" : pw === "was" ? "since boot" : "not measured";
  $("sysPow").hidden = !(pw === "low" || pw === "was");
  $("sysPowTxt").innerHTML = pw === "low" ? "<b>Power too low right now.</b> Use a 5V 2.5A charger and a short cable, or wifi and USB will drop." :
    "<b>The power dipped since the Pi started.</b> A stronger charger keeps everything stable.";
  $("sysCpu").textContent = s.cpu === null || s.cpu === undefined ? "--" : s.cpu + "%";
  sysBar("sysCpuB", s.cpu, 80);
  $("sysMem").textContent = s.mem === undefined ? "--" : s.mem + "%";
  sysBar("sysMemB", s.mem, 85);
  $("sysWifi").textContent = s.ssid || "Not connected";
  $("sysWifi").style.color = s.ssid ? "" : "#e0362a";
  $("sysWifiS").textContent = s.signal === null || s.signal === undefined ? (s.ssid ? "signal --" : " ") :
    "signal " + s.signal + "% · " + (s.signal >= 70 ? "strong" : s.signal >= 40 ? "OK" : "weak");
  $("sysUp").textContent = fmtDur(s.uptime);
  $("sysUpS").textContent = "dashboard " + fmtDur(s.runSec);
  $("sysDisk").textContent = s.diskFreeGB === undefined ? "--" : s.diskFreeGB + " GB free";
  sysBar("sysDiskB", s.diskPct, 90);
  $("sysSub").textContent = (s.model || "Raspberry Pi") + " · runs this dashboard";
  $("sysAddr").innerHTML = "Open on any phone: <b>http://" + esc(s.ip && s.ip !== "127.0.0.1" ? s.ip : (s.host || "admin") + ".local") + ":8765</b>";
}
$("sysRestart").addEventListener("click", () => {
  const b = $("sysRestart");
  clearTimeout(sysSureT);
  if (!sysSure) {
    sysSure = true; b.textContent = "Sure? Tap again"; b.classList.add("sure");
    sysSureT = setTimeout(() => { sysSure = false; b.textContent = "Restart dashboard"; b.classList.remove("sure"); }, 3000);
    return;
  }
  sysSure = false; b.textContent = "Restart dashboard"; b.classList.remove("sure");
  b.disabled = true; setTimeout(() => { b.disabled = false; }, 8000);
  $("sysMsg").textContent = "Restarting… the page reconnects by itself in a few seconds";
  fetch("/system/restart", {method: "POST"})
    .then(() => { sysRestarting = Date.now(); paintSys(); })
    .catch(() => { $("sysMsg").textContent = "Could not reach the dashboard"; });
});
// ---- parking gate: NFC reader + servo on the seismic NodeMCU ----
let gt = null, gateSeenAt = 0, gateScanAt = 0, gateMsgTimer = 0, gateSure = "", gateSureTimer = 0, gateSig = "", gateLogSig = "";
function gateFresh() { return gateSeenAt > 0 && (Date.now() - gateSeenAt) < NODE_GRACE_MS; }
function cardFmt(u) { return (u.match(/.{1,2}/g) || []).join(":"); }
function esc(t) { return String(t).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]); }
function gateMsg(text) {
  $("gateMsg").textContent = text;
  clearTimeout(gateMsgTimer);
  gateMsgTimer = setTimeout(() => { $("gateMsg").textContent = ""; }, 4000);
}
function gatePost(path, uid) {
  fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({uid: uid})})
    .then(r => r.json())
    .then(j => { if (!j.ok) gateMsg(j.error || "Did not work, try again"); })
    .catch(() => gateMsg("Could not reach the dashboard"));
}
function gateRender(g) {
  const first = gt === null, now = Date.now();
  gt = g;
  gateSeenAt = g.ageSec === null ? 0 : now - g.ageSec * 1000;
  if (g.last) g.last.at = now - g.last.agoSec * 1000;
  if (g.pending) g.pending.at = now - g.pending.agoSec * 1000;
  g.log.forEach(e => { e.at = now - e.agoSec * 1000; });
  if (g.last && g.last.at > gateScanAt + 800) {               // a new tap
    if (!first && now - pageStart > WARMUP_MS && g.last.agoSec < 10) {
      if (g.last.ok) toast("ok", "Parking gate opened", (g.last.name || cardFmt(g.last.uid)) + " · closes in " + g.openSec + " s");
      else {
        toast("info", "Unknown card at the gate", cardFmt(g.last.uid) + " · press Allow to save it");
        const sv = $("gateSvg");
        sv.classList.remove("deny"); void sv.getBoundingClientRect(); sv.classList.add("deny");
      }
    }
    gateScanAt = g.last.at;
  }
}
function paintGate() {
  const g = gt, fresh = gateFresh() && g, now = Date.now();
  const open = !!(fresh && g.open), svg = $("gateSvg");
  svg.classList.toggle("open", open);
  svg.classList.toggle("off", !fresh);
  $("c-gate").classList.toggle("stale", !fresh);
  const off = gateSeenAt ? "Offline" : "No data";
  setTag("gatePill", !fresh ? off : !g.nfc ? "Reader missing" : open ? "Open" : "Closed", !fresh ? "" : !g.nfc ? "warn" : open ? "ok" : "neutral");
  $("gateState").textContent = !fresh ? "--" : open ? "Open" : "Closed";
  const who = g && g.last && g.last.ok ? " " + (g.last.name || cardFmt(g.last.uid)) : "";
  $("gateWord").textContent = !fresh ? (gateSeenAt ? "Seismic node offline · check its power and wifi" : "Waiting for the seismic node") :
    !g.nfc ? "Card reader not found · check 3.3V, SDA → D8 and RST → D0" :
    open ? "Welcome" + who + " · closes by itself in " + g.openSec + " s" :
    g.nodeCards !== g.cards.length ? "Sending the saved cards to the gate…" :
    g.cards.length ? "Tap a saved card on the reader to open" : "No saved cards yet · tap a card, then press Allow";
  $("zd-gate").className = "tdot" + (!fresh ? "" : !g.nfc ? " warn" : " ok");
  $("zt-gate").textContent = !fresh ? off : !g.nfc ? "Reader missing" : open ? "Open" :
    "Closed · " + g.cards.length + (g.cards.length === 1 ? " card" : " cards");
  if (!g) return;

  // new card waiting for "Allow"
  const p = g.pending && now - g.pending.at < 120000 && !g.cards.some(c => c.uid === g.pending.uid) ? g.pending : null;
  $("gatePend").hidden = !p;
  if (p) {
    $("gatePendId").textContent = cardFmt(p.uid) + " · tapped " + fmtAgo((now - p.at) / 1000) + " · it stays shut until you allow it";
    $("gateAllow").dataset.uid = p.uid;
    const full = g.cards.length >= g.max;
    $("gateAllow").disabled = full;
    $("gateAllow").title = full ? "The gate holds " + g.max + " cards · remove one first" : "";
  }

  // stats
  const L = g.last;
  $("gateLast").textContent = L ? (L.name || cardFmt(L.uid)) : "--";
  $("gateRes").textContent = L ? (L.ok ? "Allowed" : "Denied") : "--";
  $("gateRes").style.color = L ? (L.ok ? "#239a43" : "#e0362a") : "";
  $("gateOpens").textContent = g.opens + (g.opens === 1 ? " time" : " times");
  $("gateNfc").textContent = !fresh ? "--" : g.nfc ? "OK" : "Missing";
  $("gateNfc").style.color = fresh && !g.nfc ? "#e0362a" : "";

  // saved cards (rebuilt only when they change, so taps on the buttons never get lost)
  $("gateCount").textContent = g.cards.length + " / " + g.max;
  const sig = JSON.stringify(g.cards) + gateSure;
  if (sig !== gateSig) {
    gateSig = sig;
    $("gateCards").innerHTML = g.cards.length ? g.cards.map(c =>
      '<li><div class="gi"><b>' + esc(c.name) + '</b><span>' + cardFmt(c.uid) + '</span></div>' +
      '<button class="gbtn sm ghost' + (gateSure === c.uid ? " sure" : "") + '" type="button" data-rm="' + c.uid + '">' +
      (gateSure === c.uid ? "Sure?" : "Remove") + '</button></li>').join("") : '<li class="gEmpty">No saved cards yet</li>';
  }
  // recent taps
  const saved = new Set(g.cards.map(c => c.uid));
  const lsig = JSON.stringify(g.log.map(e => [e.uid, e.ok, e.name, Math.round(e.at / 1000)])) + [...saved].join();
  if (lsig !== gateLogSig) {
    gateLogSig = lsig;
    $("gateLog").innerHTML = g.log.length ? g.log.slice(0, 4).map(e =>
      '<li><span class="gdot' + (e.ok ? "" : " no") + '"></span><div class="gi"><b>' + esc(e.name || cardFmt(e.uid)) + '</b>' +
      '<span>' + (e.ok ? "Allowed" : "Denied") + ' · <span class="ago" data-at="' + e.at + '"></span></span></div>' +
      (!e.ok && !saved.has(e.uid) ? '<button class="gbtn sm" type="button" data-allow="' + e.uid + '">Allow</button>' : "") + '</li>').join("")
      : '<li class="gEmpty">No taps yet</li>';
  }
  document.querySelectorAll("#gateLog .ago").forEach(a => { a.textContent = fmtAgo((now - +a.dataset.at) / 1000); });
  $("gateTaps").textContent = g.log.length ? "newest first" : "";
}
$("gateAllow").addEventListener("click", e => { if (e.currentTarget.dataset.uid) gatePost("/gate/allow", e.currentTarget.dataset.uid); });
$("gateLog").addEventListener("click", e => {
  const b = e.target.closest("button[data-allow]");
  if (b) gatePost("/gate/allow", b.dataset.allow);
});
$("gateCards").addEventListener("click", e => {
  const b = e.target.closest("button[data-rm]");
  if (!b) return;
  const uid = b.dataset.rm;
  clearTimeout(gateSureTimer);
  if (gateSure === uid) { gateSure = ""; gatePost("/gate/remove", uid); }
  else { gateSure = uid; gateSureTimer = setTimeout(() => { gateSure = ""; paintGate(); }, 3000); }
  paintGate();
});

// ---- City AI: a small weighted-risk model over the live sensors, runs every 2 s ----
const AI_PREDS = [
  {k: "slide", name: "Landslide risk"},
  {k: "air",   name: "Air pollution risk"},
  {k: "rain",  name: "Rain next hour"},
  {k: "cars",  name: "Traffic next hour"},
];
const AI_IN = ["Rain", "Rain 3h", "Soil", "Gas", "Gas trend", "Vibration", "Motion", "Light", "Temp"];
const AI_LAYERS = [9, 7, 7, 4];
let aiRuns = 0, aiTickN = 0, gasTrail = [], aiLogLines = [], aiW = [];
const sig = z => 1 / (1 + Math.exp(-z));
function aiRand(seed) { let s = seed; return () => { s = (s * 16807) % 2147483647; return s / 2147483647; }; }
function aiBuild() {
  $("aiPreds").innerHTML = AI_PREDS.map(p => '<div class="aiP"><div class="pt"><span>' + p.name + '</span><span class="lv" id="ail-' + p.k + '">--</span></div>' +
    '<div class="pv" id="aiv-' + p.k + '">--</div><div class="bar"><i id="aib-' + p.k + '" style="width:0%"></i></div>' +
    '<div class="why" id="aiw-' + p.k + '">Collecting data</div></div>').join("");
  const xs = [60, 134, 208, 272], pos = [];
  AI_LAYERS.forEach((n, c) => { pos.push([]); for (let i = 0; i < n; i++) pos[c].push(10 + 150 / n * (i + 0.5)); });
  const rnd = aiRand(7);
  aiW = [];
  let e = "", f = "", nodes = "";
  for (let c = 0; c < AI_LAYERS.length - 1; c++) {
    aiW.push([]);
    for (let j = 0; j < AI_LAYERS[c + 1]; j++) {
      aiW[c].push([]);
      for (let i = 0; i < AI_LAYERS[c]; i++) {
        aiW[c][j].push(rnd() * 2.4 - 1.2);
        const d = 'M' + xs[c] + ' ' + pos[c][i].toFixed(1) + 'L' + xs[c + 1] + ' ' + pos[c + 1][j].toFixed(1);
        e += '<path class="e" d="' + d + '"/>';
        if (rnd() < 0.16) f += '<path class="f" d="' + d + '" style="animation-delay:-' + (rnd() * 1.6).toFixed(2) + 's"/>';
      }
    }
  }
  AI_LAYERS.forEach((n, c) => pos[c].forEach((y, i) => {
    nodes += '<circle class="n" id="an-' + c + '-' + i + '" cx="' + xs[c] + '" cy="' + y.toFixed(1) + '" r="' + (c === 3 ? 6.5 : 5) + '"/>';
  }));
  let labels = AI_IN.map((t, i) => '<text x="' + (xs[0] - 9) + '" y="' + (pos[0][i] + 3).toFixed(1) + '" text-anchor="end">' + t + '</text>').join("");
  labels += ["Slide", "Air", "Rain", "Traffic"].map((t, i) => '<text x="' + (xs[3] + 11) + '" y="' + (pos[3][i] + 3).toFixed(1) + '">' + t + '</text>').join("");
  $("aiNet").innerHTML = e + f + nodes + labels;
  $("aiChip").addEventListener("click", () => $("c-ai").scrollIntoView({behavior: "smooth", block: "start"}));
}
function aiLevel(p, words) { return p < 0.3 ? [words[0], ""] : p < 0.6 ? [words[1], "mid"] : [words[2], "high"]; }
function aiSet(k, valHtml, p, lvl, why) {
  $("aiv-" + k).innerHTML = valHtml;
  const b = $("aib-" + k);
  b.style.width = Math.max(3, Math.min(100, p * 100)).toFixed(0) + "%";
  b.className = lvl[1];
  const l = $("ail-" + k);
  l.textContent = lvl[0];
  l.className = "lv " + lvl[1];
  $("aiw-" + k).textContent = why;
}
function aiTick() {
  const t0 = performance.now();
  const nf = nanoFresh(), mf = mtnFresh(), now = Date.now();
  const hs = histData ? histData.hours : [];
  const lastH = k => { for (let i = hs.length - 1; i >= 0; i--) if (hs[i][k] !== null && hs[i][k] !== undefined) return hs[i][k]; return null; };
  const raining = nf && tagState.Rain && tagState.Rain[0] === "Raining";
  const gasOn = nf && tagState.Gas && tagState.Gas[0] === "Gas detected";
  const wet = nf && latest.Rain !== null ? 1 - latest.Rain / 1023 : 0;
  const rain3 = hs.slice(-3).reduce((a, h) => a + (h.rain || 0), 0);
  const soilWet = nf && latest.Soil !== null ? 1 - latest.Soil / 1023 : lastH("soil") !== null ? lastH("soil") / 100 : 0.5;
  const gas = nf && latest.Gas !== null ? latest.Gas / 1023 : lastH("gas") !== null ? lastH("gas") / 100 : 0.2;
  if (nf && latest.Gas !== null) { gasTrail.push(gas); if (gasTrail.length > 30) gasTrail.shift(); }
  const gasTrend = gasTrail.length > 5 ? gas - gasTrail[0] : 0;
  const recent = k => trigAt[k] ? Math.max(0, 1 - (now - trigAt[k]) / 300000) : 0;   // fades out over 5 min
  const seisShake = seisFresh() && seis ? seis.intensity / 100 : 0;
  const vib = Math.max(mf ? Math.max(binState.vibration ? 1 : 0, recent("vibration")) : 0, quakeActive ? 1 : seisShake);
  const mot = mf ? Math.max(binState.motion ? 1 : 0, recent("motion")) : 0;
  const light = mf && latest.Light !== null ? latest.Light / 100 : lastH("light") !== null ? lastH("light") / 100 : 0.5;
  const temp = wxFresh() ? wxTemp : lastH("temp") !== null ? lastH("temp") : 28;
  const hod = new Date().getHours();
  const X = [wet, Math.min(1, rain3 / 60), soilWet, gas, Math.max(0, Math.min(1, 0.5 + gasTrend * 5)), vib, mot, light,
             Math.max(0, Math.min(1, (temp - 20) / 16))];
  const live = (nf ? 5 : 0) + (mf ? 3 : 0) + (wxFresh() ? 1 : 0);

  // 1. landslide: wet ground + recent rain + shaking on the mountain
  const pSlide = sig(-4.2 + 1.8 * wet + 1.6 * X[1] + 1.5 * soilWet + 2.6 * vib + 1.1 * mot + (alarmActive ? 3 : 0) + (quakeActive ? 2.5 : 0) + (raining ? 0.8 : 0));
  // 2. air pollution: gas level, rising gas, gas sensor alarm
  const pAir = sig(-3.6 + 5 * gas + 3 * Math.max(0, gasTrend * 5) + (gasOn ? 2.8 : 0));
  // 3. rain next hour: raining now, recent rain, afternoon storm window, heat
  const afternoon = hod >= 13 && hod <= 19 ? 1 : 0;
  const pRain = sig(-2 + (raining ? 2.4 : 0) + 1.4 * X[1] + 0.9 * afternoon + (temp > 30 ? 0.6 : 0) - (hod >= 7 && hod <= 17 ? 0.8 * Math.max(0, light - 0.7) : 0));
  // City Risk inputs: tsunami (shake + water movement), flood (rain + wet soil), heat (temperature)
  const shakeN = seisFresh() && seis ? seis.intensity / 100 : 0;
  const waveN = seisFresh() && seis && seis.cm !== null ? Math.min(1, seis.move / (SEA_MOVE_CM * 2)) : 0;
  const pTsu = quakeActive ? 1 : Math.min(0.95, 1.5 * shakeN * waveN + 0.25 * Math.max(shakeN, waveN));
  const pFlood = sig(-4 + 2.2 * wet + 2.2 * X[1] + 1.6 * soilWet + (raining ? 1 : 0));
  const pHeat = Math.max(0, Math.min(1, (temp - 31) / 7));
  paintRisk({tsunami: pTsu, slide: pSlide, flood: pFlood, air: pAir, heat: pHeat}, {
    tsunami: quakeActive ? "tsunami alert active, move people away from the coast" :
             "shake " + Math.round(shakeN * 100) + "/100, water moved " + (seis && seis.move ? seis.move.toFixed(1) : "0") + " cm",
    slide: alarmActive ? "disaster alarm on the mountain" : "wet ground " + Math.round(soilWet * 100) + "% and movement on the slope",
    flood: raining ? "raining now, soil " + Math.round(soilWet * 100) + "% wet" : Math.round(rain3) + " min rain in 3 h, soil " + Math.round(soilWet * 100) + "% wet",
    air: gasOn ? "gas sensor triggered" : "gas level " + Math.round(gas * 100) + "%",
    heat: Math.round(temp) + " °C in " + (wxPlace || "the city")
  });

  // 4. traffic next hour: same hour yesterday blended with this hour so far
  let cars = null, ySame = null, curH = null;
  if (hs.length >= 24) {
    ySame = hs[0].cars; curH = hs[23].cars;
    const rate = curH !== null ? curH / (new Date().getMinutes() + 1) * 60 : null;
    cars = ySame === null ? rate : rate === null ? ySame : 0.6 * ySame + 0.4 * rate;
  }

  // forward pass through the little network (lights up the picture)
  const outs = [pSlide, pAir, pRain, cars === null ? 0 : Math.min(1, cars / 35)];
  const mark = (c, vals, on, hot) => vals.forEach((x, i) => {
    const n = $("an-" + c + "-" + i);
    if (n) n.setAttribute("class", "n" + (x > hot ? " hot" : x > on ? " on" : ""));
  });
  mark(0, X, 0.45, 0.8);
  mark(3, outs, 0.45, 0.6);
  let a1 = X;
  for (let c = 0; c < 2; c++) {
    a1 = aiW[c].map(w => sig(w.reduce((a, wi, i) => a + wi * (a1[i] - 0.4) * 2.2, 0)));
    mark(c + 1, a1, 0.55, 0.78);
  }

  // predictions + reasons
  const pc = p => Math.round(p * 100) + '<small>%</small>';
  const drivers = [];
  if (wet > 0.35) drivers.push("rain sensor wet");
  if (rain3 >= 5) drivers.push(Math.round(rain3) + " min rain in 3 h");
  if (soilWet > 0.55) drivers.push("soil " + Math.round(soilWet * 100) + "% wet");
  if (quakeActive || seisShake > 0.2) drivers.push("ground shaking (seismic node)");
  else if (vib > 0.2) drivers.push("vibration on the mountain");
  if (mot > 0.2) drivers.push("movement on the slope");
  aiSet("slide", pc(pSlide), pSlide, aiLevel(pSlide, ["Low", "Moderate", "High"]),
        quakeActive ? "Tsunami alert · sea moving and strong shaking" :
        alarmActive ? "Disaster alarm active · motion and vibration together" :
        drivers.length ? "Driven by " + drivers.slice(0, 2).join(", ") : "Ground dry and still · slopes stable");
  aiSet("air", pc(pAir), pAir, aiLevel(pAir, ["Low", "Moderate", "High"]),
        gasOn ? "Gas sensor triggered · gas level " + Math.round(gas * 100) + "%" :
        "Gas level " + Math.round(gas * 100) + "% · " + (gasTrend > 0.02 ? "rising" : gasTrend < -0.02 ? "falling" : "steady"));
  aiSet("rain", pc(pRain), pRain, aiLevel(pRain, ["Unlikely", "Possible", "Likely"]),
        raining ? "Raining now · likely to continue" :
        afternoon ? "Afternoon storm window · " + Math.round(rain3) + " min rain in 3 h" :
        "Dry spell · " + Math.round(rain3) + " min rain in the last 3 h");
  if (cars === null) aiSet("cars", "--", 0, ["No data", ""], "Needs the 24 h history");
  else aiSet("cars", "~" + Math.round(cars) + '<small>vehicles</small>', Math.min(1, cars / 35),
             cars < 10 ? ["Light", ""] : cars < 20 ? ["Moderate", ""] : ["Busy", "mid"],
             "Same hour yesterday " + (ySame === null ? "--" : ySame) + " · this hour so far " + (curH === null ? "--" : curH));

  // suggestion
  let s = "All clear · city running normally, no action needed";
  if (quakeActive) s = "Tsunami alert! Raise the sea wall and move people away from the coast";
  else if (alarmActive || pSlide >= 0.6) s = "Close the mountain road · landslide risk is high";
  else if (pAir >= 0.6) s = "Ventilate the area and keep people away from the gas source";
  else if (pSlide >= 0.3) s = "Watch the mountain slope · ground is getting wet";
  else if (pRain >= 0.6) s = "Rain expected · switch street lights on early and clear the drains";
  else if (cars !== null && cars >= 20) s = "Busy hour ahead · keep the traffic signal on auto";
  $("aiSuggest").textContent = s;
  $("aiSugBox").classList.toggle("warn", alarmActive || quakeActive || pSlide >= 0.3 || pAir >= 0.6);

  // stats + "thinking" log
  aiRuns++;
  const ms = performance.now() - t0;
  $("aiRuns").textContent = "#" + aiRuns.toLocaleString();
  $("aiLat").textContent = ms.toFixed(2) + " ms";
  const conf = Math.round(Math.min(97, 52 + 40 * live / 9 + 8 * Math.abs(pSlide - 0.5)));
  $("aiConf").textContent = conf + "%";
  $("aiAge").textContent = live + "/9 live";
  $("tAi").textContent = live ? "Running" : "Warming up";
  const tm = new Date().toTimeString().slice(0, 8);
  aiLogLines.push(tm + " › input [" + X.map(v => v.toFixed(2)).join(" ") + "]");
  aiLogLines.push(tm + " › forward pass 9→7→7→4 · " + ms.toFixed(2) + " ms · confidence " + conf + "%");
  aiLogLines.push(tm + " › slide " + Math.round(pSlide * 100) + "% · air " + Math.round(pAir * 100) + "% · rain " +
                  Math.round(pRain * 100) + "% · traffic " + (cars === null ? "--" : "~" + Math.round(cars)));
  aiLogLines = aiLogLines.slice(-6);
  $("aiLog").innerHTML = aiLogLines.map(t => "<div>" + t + "</div>").join("");
}
aiBuild();
setTimeout(aiTick, 600);
setInterval(aiTick, 2000);

// ---- history: 24 h charts + event log (from /history, refreshed every 30 s) ----
const HIST = [
  {k: "temp",  name: "Temperature",    unit: "°C",  kind: "line", color: "#ee7033", fmt: v => v.toFixed(1), min: 20, max: 36, head: "now"},
  {k: "rain",  name: "Rainfall",       unit: "min", kind: "bar",  color: "#7462ef", fmt: v => Math.round(v), min: 0, max: 60, head: "sum", sumWord: "min of rain in 24 h"},
  {k: "gas",   name: "Air quality",    unit: "%",   kind: "line", color: "#1c1c1c", fmt: v => Math.round(v), min: 0, max: 100, head: "now", sub: "gas level, lower is better"},
  {k: "soil",  name: "Soil moisture",  unit: "%",   kind: "line", color: "#34b552", fmt: v => Math.round(v), min: 0, max: 100, head: "now"},
  {k: "light", name: "Ambient light",  unit: "%",   kind: "line", color: "#d9a300", fmt: v => Math.round(v), min: 0, max: 100, head: "now", sub: "street lights on when dark"},
  {k: "cars",  name: "Traffic",        unit: "vehicles", kind: "bar", color: "#4a7fd4", fmt: v => Math.round(v), min: 0, max: 10, head: "sum", sumWord: "vehicles in 24 h"},
];
let histData = null;
function hhmm(t) { const d = new Date(t * 1000); return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); }
function buildHistCharts() {
  $("hCharts").innerHTML = HIST.map(c =>
    '<div class="hc" id="hc-' + c.k + '"><h3><i style="background:' + c.color + '"></i>' + c.name + '</h3>' +
    '<div class="hv" id="hv-' + c.k + '">--</div><div class="hs" id="hs-' + c.k + '">' + (c.sub || "") + '</div>' +
    '<svg id="hsvg-' + c.k + '" viewBox="0 0 240 86" preserveAspectRatio="none" role="img" aria-label="' + c.name + ' over the last 24 hours"></svg>' +
    '<div class="hx" id="hx-' + c.k + '"></div><div class="htip" id="htip-' + c.k + '"></div></div>').join("");
  HIST.forEach(c => {
    const svg = $("hsvg-" + c.k), box = $("hc-" + c.k);
    const show = e => {
      if (!histData) return;
      const r = svg.getBoundingClientRect();
      const n = histData.hours.length;
      const i = Math.max(0, Math.min(n - 1, Math.floor((e.clientX - r.left) / r.width * n)));
      const h = histData.hours[i], v = h[c.k];
      $("htip-" + c.k).textContent = hhmm(h.t) + " · " + (v === null ? "no data" : c.fmt(v) + " " + c.unit) +
        (h.sample.indexOf(c.k) >= 0 ? " · sample" : "");
      box.classList.add("hover");
      drawHist(c, i);
    };
    svg.addEventListener("pointermove", show);
    svg.addEventListener("pointerdown", show);
    svg.addEventListener("pointerleave", () => { box.classList.remove("hover"); drawHist(c, -1); });
  });
}
function drawHist(c, hi) {
  const hs = histData.hours, n = hs.length, W = 240, H = 86, top = 4, bot = 84;
  const vals = hs.map(h => h[c.k]);
  let max = c.max;
  vals.forEach(v => { if (v !== null && v > max) max = v; });
  if (c.kind === "bar" && c.k === "cars") max = Math.ceil(max / 5) * 5;
  const y = v => bot - (Math.max(c.min, Math.min(max, v)) - c.min) / (max - c.min) * (bot - top);
  const cw = W / n;
  let g = '<line class="grid" x1="0" y1="' + bot + '" x2="' + W + '" y2="' + bot + '"/>' +
          '<line class="grid" x1="0" y1="' + ((top + bot) / 2) + '" x2="' + W + '" y2="' + ((top + bot) / 2) + '"/>';
  if (c.kind === "bar") {
    vals.forEach((v, i) => {
      if (v === null || v <= 0) return;
      const x = i * cw + 1, w = cw - 2, yy = Math.min(bot - 2, y(v)), r = Math.min(3, w / 2, bot - yy);
      g += '<path d="M' + x + ' ' + bot + 'V' + (yy + r) + 'Q' + x + ' ' + yy + ' ' + (x + r) + ' ' + yy + 'H' + (x + w - r) +
           'Q' + (x + w) + ' ' + yy + ' ' + (x + w) + ' ' + (yy + r) + 'V' + bot + 'Z" fill="' + c.color + '"' +
           (hi >= 0 && i !== hi ? ' opacity=".35"' : '') + '/>';
    });
  } else {
    let d = "", area = "", started = false, first = 0, last = 0;
    vals.forEach((v, i) => {
      if (v === null) { started = false; return; }
      const x = i * cw + cw / 2;
      d += (started ? "L" : "M") + x.toFixed(1) + " " + y(v).toFixed(1);
      if (!started && !area) first = x;
      if (!area || started) area += (area ? "L" : "M") + x.toFixed(1) + " " + y(v).toFixed(1);
      last = x; started = true;
    });
    if (area) g += '<path d="' + area + 'L' + last.toFixed(1) + ' ' + bot + 'L' + first.toFixed(1) + ' ' + bot + 'Z" fill="' + c.color + '" opacity=".12"/>';
    g += '<path d="' + d + '" fill="none" stroke="' + c.color + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>';
  }
  if (hi >= 0) {
    const x = hi * cw + cw / 2;
    g += '<line class="cross" x1="' + x + '" y1="' + top + '" x2="' + x + '" y2="' + bot + '" vector-effect="non-scaling-stroke"/>';
  }
  $("hsvg-" + c.k).innerHTML = g;
}
function paintHistory() {
  if (!histData) return;
  const hs = histData.hours;
  HIST.forEach(c => {
    drawHist(c, -1);
    const vals = hs.map(h => h[c.k]).filter(v => v !== null);
    let txt = "--";
    if (vals.length) {
      if (c.head === "sum") txt = c.fmt(vals.reduce((a, b) => a + b, 0)) + '<small>' + c.sumWord + '</small>';
      else txt = c.fmt(vals[vals.length - 1]) + '<small>' + c.unit + ' · now</small>';
    }
    $("hv-" + c.k).innerHTML = txt;
    if (!c.sub) {
      const nums = hs.map(h => h[c.k]);
      let mi = -1;
      nums.forEach((v, i) => { if (v !== null && (mi < 0 || v > nums[mi])) mi = i; });
      $("hs-" + c.k).textContent = mi < 0 ? "" : "Peak " + c.fmt(nums[mi]) + " " + c.unit + " at " + hhmm(hs[mi].t);
    }
    const ticks = [0, 6, 12, 18].map(i => hhmm(hs[i].t)).concat(["now"]);
    $("hx-" + c.k).innerHTML = ticks.map(t => "<span>" + t + "</span>").join("");
  });
  const ev = histData.events;
  const today = new Date().toDateString();
  $("hEvents").innerHTML = ev.length ? ev.map(e => {
    const d = new Date(e.t * 1000);
    return '<li><time>' + hhmm(e.t) + (d.toDateString() === today ? "" : '<span class="day">yest.</span>') + '</time>' +
      '<span class="dot ' + e.level + '"></span><span>' + e.text + (e.sample ? '<span class="s">sample</span>' : '') + '</span></li>';
  }).join("") : '<li class="hEmpty">No events yet</li>';
  $("hLogCount").textContent = ev.length + " events";
  $("histTag").style.display = histData.sample ? "" : "none";
  $("histFoot").textContent = histData.sample
    ? "Older hours are sample data (marked in the tooltips and the log); real readings replace them as the model runs"
    : "All readings recorded live by the Raspberry Pi";
}
function loadHistory() {
  fetch("/history", {cache: "no-store"}).then(r => r.json()).then(d => { histData = d; paintHistory(); }).catch(() => {});
}
buildHistCharts();
loadHistory();
setInterval(loadHistory, 30000);
// ---- cards fade + slide in as they come into view (staggered) ----
(function reveal() {
  const cards = Array.from(document.querySelectorAll(".layout .card"));
  cards.forEach(c => c.classList.add("rv"));
  const show = list => list.forEach((c, i) => {
    c.style.setProperty("--d", (i * 0.07).toFixed(2) + "s");
    c.classList.add("in");
    setTimeout(() => c.classList.add("done"), 900 + i * 70);
  });
  if (REDUCED || !("IntersectionObserver" in window)) { show(cards); return; }
  const io = new IntersectionObserver(entries => {
    const vis = entries.filter(e => e.isIntersecting).map(e => e.target);
    vis.forEach(c => io.unobserve(c));
    show(vis);
  }, {rootMargin: "0px 0px -6% 0px"});
  cards.forEach(c => io.observe(c));
  setTimeout(() => show(cards.filter(c => !c.classList.contains("in"))), 4000);   // never leave a card hidden
})();
// node strip: fade the right edge only when it really overflows (swipe hint)
function stripFade() {
  const n = document.querySelector(".nodes");
  if (n) n.classList.toggle("more", n.scrollWidth > n.clientWidth + 1 && n.scrollLeft + n.clientWidth < n.scrollWidth - 4);
}
window.addEventListener("resize", stripFade);
document.querySelector(".nodes").addEventListener("scroll", stripFade, {passive: true});
setInterval(stripFade, 1000);
setInterval(() => { if (!document.hidden) paint(); }, 1000);   // no work while the tab is hidden
paint();

// live link to the Pi: reconnects by itself, and restarts the link if it goes quiet without an error
let es = null, esAt = 0;
function connect() {
  if (es) es.close();
  esAt = Date.now();
  es = new EventSource("/stream");
  es.onmessage = e => { try { render(JSON.parse(e.data)); } catch (err) { console.log("bad data", err); } };
  es.onerror = () => { linkLost = true; };
}
setInterval(() => {
  if (Date.now() - Math.max(lastRecv, esAt) > 6000) { linkLost = true; connect(); }
}, 2000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { if (Date.now() - lastRecv > 3000) connect(); paint(); }
});
connect();
</script>
</body>
</html>
"""


# alarm sound: alarm.mp3 next to this script (the dashboard beeps instead if it's missing)
ALARM_SOUND_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alarm.mp3")
try:
    with open(ALARM_SOUND_PATH, "rb") as f:
        ALARM_SOUND = f.read()
except OSError:
    ALARM_SOUND = None

# the page is built once and kept ready, plain and gzip-compressed
DASHBOARD_BYTES = DASHBOARD_HTML.encode("utf-8")
DASHBOARD_GZ = gzip.compress(DASHBOARD_BYTES, 9)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def handle_traffic_post(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > 128:
                raise ValueError("bad length")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("bad body")
            if "mode" in data:
                if data["mode"] not in ("auto", "manual"):
                    raise ValueError("bad mode")
                payload = {"mode": data["mode"]}
            elif "light" in data:
                if data["light"] not in ("red", "yellow", "green") or data.get("on") not in (True, False, 0, 1):
                    raise ValueError("bad light")
                payload = {"light": data["light"], "on": 1 if data["on"] else 0}
            else:
                raise ValueError("nothing to do")
        except (ValueError, TypeError, UnicodeDecodeError):
            self.send_response(400)
            self.end_headers()
            return
        ok, err = send_traffic(payload)
        body = json.dumps({"ok": ok, "error": err}).encode("utf-8")
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_gate_post(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > 128:
                raise ValueError("bad length")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("bad body")
        except (ValueError, TypeError, UnicodeDecodeError):
            self.send_response(400)
            self.end_headers()
            return
        fn = gate_allow if self.path == "/gate/allow" else gate_remove
        ok, err = fn(data.get("uid"))
        body = json.dumps({"ok": ok, "error": err}).encode("utf-8")
        self.send_response(200 if ok else 409)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/alarm/silence":
            silence_alarm()
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/traffic":
            self.handle_traffic_post()
            return
        if self.path == "/system/restart":
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            threading.Thread(target=restart_dashboard, daemon=True).start()
            return
        if self.path in ("/gate/allow", "/gate/remove"):
            self.handle_gate_post()
            return
        if self.path != "/mountain":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > 512:
                raise ValueError("bad length")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not apply_mountain(data):
                raise ValueError("bad data")
        except (ValueError, TypeError, UnicodeDecodeError):
            self.send_response(400)
            self.end_headers()
            return
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            gz = "gzip" in self.headers.get("Accept-Encoding", "")
            body = DASHBOARD_GZ if gz else DASHBOARD_BYTES
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/alarm.mp3":
            if ALARM_SOUND is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(ALARM_SOUND)))
            self.end_headers()
            self.wfile.write(ALARM_SOUND)
        elif self.path == "/history":
            body = history_json().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/data":
            body = snapshot().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/stream":
            self.send_response(200)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # push events immediately
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            q = queue.Queue(maxsize=2)
            with subscribers_lock:
                subscribers.append(q)
            try:
                self.wfile.write(b"retry: 2000\n\n")                 # browser reconnects after 2 s
                initial = snapshot()
                self.wfile.write(f"data: {initial}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        payload = q.get(timeout=10)
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")              # keeps phones/hotspots from closing it
                    self.wfile.flush()
            except (OSError, ValueError):
                pass
            finally:
                with subscribers_lock:
                    if q in subscribers:
                        subscribers.remove(q)
        else:
            self.send_response(404)
            self.end_headers()


def get_lan_ip():
    """Best-effort guess at this machine's LAN IP, for printing a shareable URL."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is actually sent
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def pick_port_interactively():
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found. Plug in the Nano and try again.")
        sys.exit(1)
    print("Available serial ports:")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p.device} - {p.description}")
    choice = input("Pick a port number: ").strip()
    try:
        return ports[int(choice)].device
    except (ValueError, IndexError):
        print("Invalid choice.")
        sys.exit(1)


def main():
    if len(sys.argv) > 1:
        port_name = sys.argv[1]
    else:
        port_name = pick_port_interactively()

    t = threading.Thread(target=serial_reader, args=(port_name,), daemon=True)
    t.start()
    threading.Thread(target=udp_listener, daemon=True).start()
    threading.Thread(target=weather_loop, daemon=True).start()
    threading.Thread(target=alarm_loop, daemon=True).start()
    threading.Thread(target=matrix_loop, daemon=True).start()
    threading.Thread(target=system_loop, daemon=True).start()
    threading.Thread(target=broadcast_loop, daemon=True).start()
    load_gate_cards()
    make_sample_history()
    hist_event("system", "Dashboard started")
    threading.Thread(target=history_loop, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    print(f"Dashboard running:\n  on this machine : http://localhost:{HTTP_PORT}\n  from other devices: http://{get_lan_ip()}:{HTTP_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")


if __name__ == "__main__":
    main()
