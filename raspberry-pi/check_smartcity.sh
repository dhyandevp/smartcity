#!/usr/bin/env bash
# Smart City - full health check for the Raspberry Pi
#
#   Copy:  scp check_smartcity.sh admin@admin.local:~/
#   Run:   ssh -t admin@admin.local "sudo bash check_smartcity.sh"
#
# Prints a green / orange / red list:
#   1. Pi       : model, power (under-voltage), temperature, SD card space, clock
#   2. Wifi     : connected to the hotspot, IP address, phone reachable, internet
#   3. Files    : dashboard is the latest version, alarm.mp3, no duplicate or leftover copies
#   4. Service  : installed, auto-start on, running exactly ONCE, not crash-looping
#   5. Nano     : USB serial port present and sending rain / gas / soil
#   6. Dashboard: page, live data, alarm sound, history all answer
#   7. Nodes    : listens 15 s - each node Online / Silent, and warns if two boards
#                 claim the same job (duplicate) or a node floods the Pi
#   8. Extras   : pi_optimize.sh tune-up (watchdog, wifi checker) - optional
# Nothing is changed on the Pi: this only looks.

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo:   sudo bash check_smartcity.sh"
  exit 1
fi
exec python3 - "$@" <<'PYTHON'
import glob, hashlib, json, os, py_compile, re, shutil, socket, struct, subprocess, sys, time, urllib.request

# ---------------------------------------------------------------- settings
USER = os.environ.get("SUDO_USER") or "admin"
if USER == "root":
    USER = "admin"
HOME = os.path.expanduser("~" + USER)
APP_DIR = os.path.join(HOME, "smartcity")
DASH = os.path.join(APP_DIR, "serial_dashboard.py")
SERVICE = "smartcity-dashboard"
UNIT = "/etc/systemd/system/%s.service" % SERVICE
SERIAL_PORT = "/dev/ttyUSB0"
HTTP = "http://127.0.0.1:8765"
WIFI_NAME = "rishi"
LISTEN_SEC = 15
# features the latest dashboard must have (older copies miss some of these)
FEATURES = {"tsunami rule (sea moves 2 cm + shake 50)": "SEA_MOVE_CM",
            "parking gate (NFC + servo)": "def gate_ack",
            "gas by level (50 / 45 %)": "GAS_ALERT_PCT",
            "seismic answer port 8770": "SEISMIC_ACK_PORT"}

# ---------------------------------------------------------------- output
TTY = sys.stdout.isatty()
def col(code, t): return "\033[%sm%s\033[0m" % (code, t) if TTY else t
counts = {"ok": 0, "warn": 0, "bad": 0}
problems = []
def ok(t, fix=""): counts["ok"] += 1; print("  " + col("32", "✔") + "  " + t)
def warn(t, fix=""):
    counts["warn"] += 1; print("  " + col("33", "!") + "  " + t + (col("2", "   → " + fix) if fix else ""))
    problems.append(("!", t, fix))
def bad(t, fix=""):
    counts["bad"] += 1;  print("  " + col("31", "✘") + "  " + t + (col("2", "   → " + fix) if fix else ""))
    problems.append(("✘", t, fix))
def info(t): print("     " + col("2", t))
def head(t): print("\n" + col("1", t))

def run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except Exception:
        return 1, ""
def have(prog): return shutil.which(prog) is not None
def read(p, default=""):
    try:
        with open(p, "r", errors="replace") as f: return f.read()
    except OSError: return default
def http(path, timeout=4):
    try:
        with urllib.request.urlopen(HTTP + path, timeout=timeout) as r:
            return r.status, r.read()
    except Exception as e:
        return None, str(e).encode()

print(col("1", "Smart City health check") + col("2", "  ·  " + time.strftime("%d %b %Y %H:%M:%S") + "  ·  user " + USER))

# ================================================================ 1. Pi
head("1. Raspberry Pi")
model = read("/proc/device-tree/model").strip("\x00\n ") or "unknown"
(ok if "Raspberry Pi" in model else warn)("Board: " + model)
osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?', read("/etc/os-release"), re.M))
info("OS: " + osr.get("PRETTY_NAME", "?") + "  ·  hostname " + socket.gethostname())
if have("vcgencmd"):
    _, t = run("vcgencmd get_throttled")
    m = re.search(r"0x([0-9a-fA-F]+)", t)
    if m:
        v = int(m.group(1), 16)
        if v & 0x1: bad("Power: UNDER-VOLTAGE right now", "use a proper 5V 2.5A charger and a short thick cable")
        elif v & 0x10000: warn("Power: under-voltage happened since boot", "use a better charger (wifi drops and resets come from this)")
        else: ok("Power: good, no under-voltage")
        if v & 0x4: warn("Pi is slowing itself down (throttled) right now", "power or heat problem")
    _, t = run("vcgencmd measure_temp")
    m = re.search(r"([\d.]+)", t)
    if m:
        c = float(m.group(1))
        (ok if c < 70 else warn if c < 80 else bad)("Temperature: %.0f °C" % c, "" if c < 70 else "give it air / a heatsink")
else:
    warn("Power / temperature not checked (vcgencmd missing)")
du = shutil.disk_usage("/")
free_gb = du.free / 1e9
(ok if free_gb > 1 else warn if free_gb > 0.3 else bad)("SD card: %.1f GB free of %.1f GB" % (free_gb, du.total / 1e9),
                                                     "" if free_gb > 1 else "delete old files / logs")
if os.statvfs("/").f_flag & os.ST_RDONLY:
    bad("SD card is READ-ONLY (damaged / corrupted?)", "reboot; if it stays, re-flash the card")
up = float(read("/proc/uptime", "0").split()[0] or 0)
info("Up for %dh %02dm" % (up // 3600, up % 3600 // 60))
if time.localtime().tm_year < 2025:
    warn("Clock is wrong (year %d)" % time.localtime().tm_year, "needs internet once to set the time")
tz = time.strftime("%z")
(ok if tz == "+0530" else warn)("Time zone: " + time.strftime("%Z") + " (" + tz + ")", "" if tz == "+0530" else "sudo timedatectl set-timezone Asia/Kolkata")

# ================================================================ 2. Wifi
head("2. Wifi")
ssid = ""
if have("iwgetid"): ssid = run("iwgetid -r")[1]
if not ssid and have("nmcli"):
    for line in run("nmcli -t -f active,ssid dev wifi")[1].splitlines():
        if line.startswith("yes:"): ssid = line[4:]
_, ipa = run("ip -4 -o addr show wlan0")
m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", ipa)
wlan_ip = m.group(1) if m else ""
if ssid == WIFI_NAME: ok('Connected to the hotspot "%s"' % ssid)
elif ssid: warn('Connected to "%s", not "%s"' % (ssid, WIFI_NAME), "all nodes must be on the same wifi as the Pi")
else: bad("Wifi not connected", "hotspot on, 2.4 GHz, same name/password")
(ok if wlan_ip else bad)("Pi address: " + (wlan_ip or "none"), "" if wlan_ip else "no IP from the hotspot")
_, eth = run("ip -4 -o addr show eth0")
if "inet " in eth: warn("Ethernet cable is also connected", "fine, but nodes only find the Pi over the hotspot")
_, rt = run("ip route show default")
m = re.search(r"via (\S+)", rt)
gw = m.group(1) if m else ""
if gw:
    rc, _ = run("ping -c 2 -W 2 " + gw)
    (ok if rc == 0 else warn)("Phone hotspot answers (" + gw + ")", "" if rc == 0 else "weak wifi signal?")
rc, _ = run("ping -c 2 -W 3 1.1.1.1")
(ok if rc == 0 else warn)("Internet " + ("works" if rc == 0 else "not reachable"),
                          "" if rc == 0 else "only the live temperature needs it; turn on mobile data")
pw = run("iw dev wlan0 get power_save")[1] if have("iw") else ""
if "on" in pw.lower(): warn("Wifi power-saving is ON", "run pi_optimize.sh (it switches it off)")
elif pw: ok("Wifi power-saving off")

# ================================================================ 3. Files
head("3. Files")
if os.path.isfile(DASH):
    src = read(DASH)
    ok("serial_dashboard.py found (%d KB)" % (len(src.encode()) // 1024))
    try:
        py_compile.compile(DASH, cfile="/tmp/_sc_check.pyc", doraise=True); ok("serial_dashboard.py has no errors")
    except py_compile.PyCompileError as e:
        bad("serial_dashboard.py is broken: " + str(e).splitlines()[-1][:80], "copy it again from D:\\Smartcity\\raspberry-pi")
    missing = [n for n, key in FEATURES.items() if key not in src]
    if missing: bad("OLD dashboard version, missing: " + ", ".join(missing), "copy the new serial_dashboard.py and restart")
    else: ok("Latest version (tsunami rule, parking gate, gas level)")
    info("fingerprint " + hashlib.sha256(src.encode()).hexdigest()[:12])
else:
    bad("serial_dashboard.py NOT found in " + APP_DIR, "scp D:\\Smartcity\\raspberry-pi\\serial_dashboard.py admin@admin.local:~/smartcity/")
mp3 = os.path.join(APP_DIR, "alarm.mp3")
sz = os.path.getsize(mp3) if os.path.isfile(mp3) else 0
magic = open(mp3, "rb").read(3) if sz else b""
if sz > 10000 and (magic == b"ID3" or magic[:1] == b"\xff"):
    ok("alarm.mp3 found (%d KB)" % (sz // 1024))
elif sz: bad("alarm.mp3 looks damaged (%d bytes)" % sz, "copy alarm.mp3 again")
else: bad("alarm.mp3 missing (alarms will only beep)", "scp D:\\Smartcity\\raspberry-pi\\alarm.mp3 admin@admin.local:~/smartcity/")
cards = os.path.join(APP_DIR, "gate_cards.json")
if os.path.isfile(cards):
    try:
        n = len(json.loads(read(cards)))
        ok("Parking gate: %d saved card(s)" % n)
    except ValueError:
        bad("gate_cards.json is damaged", "delete it, then press Allow again for each card")
else:
    info("Parking gate: no saved cards yet (gate_cards.json is made when you press Allow)")
# duplicates / leftovers
copies = []
for root in ("/home", "/root", "/opt", "/usr/local", "/srv"):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith(".") or d in (".local",)]
        if dp.count(os.sep) > 6: dn[:] = []
        for f in fn:
            if f == "serial_dashboard.py" or (f.startswith("serial_dashboard") and f.endswith(".py")):
                p = os.path.join(dp, f)
                if os.path.realpath(p) != os.path.realpath(DASH): copies.append(p)
if copies:
    warn("Other copies of the dashboard: " + ", ".join(copies), "not used by the service; delete them so nobody runs the wrong one")
else: ok("No duplicate dashboard copies")
left = [p for p in (os.path.join(HOME, "smartcity-dashboard.service"),) if os.path.exists(p)]
for p in glob.glob(os.path.join(APP_DIR, "*")):
    b = os.path.basename(p)
    if b not in ("serial_dashboard.py", "alarm.mp3", "gate_cards.json", "__pycache__") and not b.startswith("gate_cards.json"):
        left.append(p)
if left: warn("Extra files lying around: " + ", ".join(os.path.basename(x) for x in left), "harmless; tidy up if you like")
else: ok("No leftover files")

# ================================================================ 4. Service
head("4. Auto-start service")
unit = read(UNIT)
if not unit:
    bad("Service not installed", "sudo mv ~/smartcity-dashboard.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now smartcity-dashboard")
else:
    m = re.search(r"^ExecStart=(.*)$", unit, re.M)
    exe = m.group(1) if m else ""
    u = re.search(r"^User=(\S+)", unit, re.M)
    good = DASH in exe and (u and u.group(1) == USER)
    (ok if good else bad)("Service file points to " + (exe or "?") + (" as " + u.group(1) if u else ""),
                          "" if good else "path/user wrong: copy smartcity-dashboard.service again")
    if SERIAL_PORT not in exe: warn("Service does not use " + SERIAL_PORT)
    if have("systemctl"):
        en = run("systemctl is-enabled " + SERVICE)[1]
        (ok if en == "enabled" else bad)("Starts at boot: " + (en or "?"), "" if en == "enabled" else "sudo systemctl enable " + SERVICE)
        act = run("systemctl is-active " + SERVICE)[1]
        (ok if act == "active" else bad)("Running: " + (act or "?"), "" if act == "active" else "journalctl -u %s -n 30   (shows why)" % SERVICE)
        rs = run("systemctl show -p NRestarts --value " + SERVICE)[1]
        since = run("systemctl show -p ActiveEnterTimestamp --value " + SERVICE)[1]
        if rs.isdigit() and int(rs) > 3: warn("It crashed and restarted %s times" % rs, "journalctl -u %s -n 50" % SERVICE)
        elif rs.isdigit(): ok("Not crashing (%s restarts)" % rs)
        if since: info("running since " + since)
        # other services / cron / autostart that also start the dashboard = duplicate
        others = []
        for f in glob.glob("/etc/systemd/system/*.service") + glob.glob("/lib/systemd/system/*.service") + \
                 glob.glob(os.path.join(HOME, ".config/systemd/user/*.service")):
            if os.path.basename(f) != SERVICE + ".service" and "serial_dashboard" in read(f): others.append(f)
        for f in ["/etc/rc.local", "/etc/crontab"] + glob.glob("/etc/cron.d/*") + glob.glob("/var/spool/cron/crontabs/*") + \
                 glob.glob(os.path.join(HOME, ".config/autostart/*")) + glob.glob("/etc/xdg/autostart/*") + \
                 [os.path.join(HOME, ".bashrc"), os.path.join(HOME, ".profile")]:
            if "serial_dashboard" in read(f): others.append(f)
        if others: bad("The dashboard is ALSO started from: " + ", ".join(others), "remove those lines - it must only start from the service")
        else: ok("Started from one place only (no duplicate service/cron/autostart)")
# running processes
procs = []
for p in glob.glob("/proc/[0-9]*/cmdline"):
    c = read(p).replace("\x00", " ").strip()
    if "serial_dashboard" in c and "check_smartcity" not in c: procs.append((p.split("/")[2], c))
if len(procs) == 1: ok("Exactly one dashboard running (pid %s)" % procs[0][0])
elif not procs: bad("Dashboard is not running", "sudo systemctl restart " + SERVICE)
else:
    bad("%d dashboards running at once (DUPLICATE)" % len(procs), "sudo pkill -f serial_dashboard.py && sudo systemctl restart " + SERVICE)
    for pid, c in procs: info("pid %s: %s" % (pid, c[:90]))
if have("ss"):
    for proto, port in (("t", 8765), ("u", 8766)):
        out = run("ss -%sln -p 'sport = :%d'" % (proto, port))[1]
        users = set(re.findall(r'"([^"]+)",pid=(\d+)', out))
        name = "web page %d" % port if proto == "t" else "node port %d" % port
        if not users: bad("Nothing listening on " + name)
        elif len({pid for _, pid in users}) > 1: bad("Two programs on " + name + ": " + ", ".join("%s(%s)" % u for u in users))
        else: ok(name.capitalize() + " open")

# ================================================================ 5. Nano
head("5. Arduino Nano (rain · gas · soil)")
if os.path.exists(SERIAL_PORT):
    byid = [os.path.basename(p) for p in glob.glob("/dev/serial/by-id/*") if os.path.realpath(p) == os.path.realpath(SERIAL_PORT)]
    ok(SERIAL_PORT + " plugged in" + ("  (" + byid[0][:50] + ")" if byid else ""))
else:
    ports = glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
    bad(SERIAL_PORT + " not found" + (" (found " + ", ".join(ports) + " instead)" if ports else ""),
        "plug the Nano into the Pi" if not ports else "edit the service to use " + ports[0])
extra = [p for p in glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*") if p != SERIAL_PORT]
if extra: warn("Other USB serial devices: " + ", ".join(extra), "only the Nano should be plugged into the Pi")
try:
    import serial  # noqa
    ok("python3-serial installed")
except ImportError:
    bad("python3-serial missing", "sudo apt install -y python3-serial")

# ================================================================ 6. Dashboard
head("6. Dashboard web page")
st, body = http("/")
(ok if st == 200 and b"Smart City" in body else bad)("Page opens: http://%s:8765" % (wlan_ip or "admin.local"),
                                                     "" if st == 200 else "dashboard not answering")
st, a = http("/alarm.mp3")
(ok if st == 200 and len(a) > 10000 else bad)("Alarm sound served" if st == 200 else "Alarm sound NOT served", "" if st == 200 else "alarm.mp3 must be next to serial_dashboard.py")
st, h = http("/history")
(ok if st == 200 else bad)("History answers" if st == 200 else "History not answering")
st, d = http("/data")
data = {}
if st == 200:
    try: data = json.loads(d)
    except ValueError: pass
if data:
    ok("Live data answers")
    sn = data.get("sensors") or {}
    if data.get("connected") and all(k in sn for k in ("Rain", "Gas", "Soil")):
        ok("Nano sending: rain %s · gas %s · soil %s" % (sn["Rain"]["raw"], sn["Gas"]["raw"], sn["Soil"]["raw"]))
    elif data.get("connected"): warn("Nano connected but missing: " + ", ".join(k for k in ("Rain", "Gas", "Soil") if k not in sn), "check those sensors' wires on the Nano")
    else: bad("Dashboard can't read the Nano", "Nano plugged in? Arduino Serial Monitor closed? baud 115200")
    w = data.get("weather")
    (ok if w and w.get("tempC") is not None else warn)("Live temperature: %s °C" % w["tempC"] if w and w.get("tempC") is not None else "No live temperature yet",
                                                       "" if w else "needs internet; it retries by itself")
else:
    bad("Live data (/data) not answering")

# ================================================================ 7. Nodes
head("7. Wifi nodes (listening %d s, keep everything switched on)" % LISTEN_SEC)
def kind(m):
    if not isinstance(m, dict): return "unknown"
    if "traffic" in m: return "Traffic"
    if m.get("gate") == "scan": return "Gate tap"
    if "seismic" in m: return "Seismic + gate"
    if m.get("matrix") == "ok": return "Display (OLED)"
    if "motion" in m and "vibration" in m: return "Mountain"
    return "unknown"
seen = {}          # kind -> {ip: count}
sniff_ok = True
try:
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003))
    s.settimeout(0.5)
except (OSError, AttributeError):
    sniff_ok = False
snaps = []
t_end = time.time() + LISTEN_SEC
next_poll = 0
while time.time() < t_end:
    if time.time() >= next_poll:
        next_poll = time.time() + 3
        st, d = http("/data", 2)
        if st == 200:
            try: snaps.append(json.loads(d))
            except ValueError: pass
    if not sniff_ok:
        time.sleep(0.5); continue
    try:
        pkt, addr = s.recvfrom(2048)
    except socket.timeout:
        continue
    if addr[2] == 4: continue                          # our own outgoing copy
    off = 14
    if len(pkt) < off + 28 or pkt[off - 2:off] != b"\x08\x00": continue
    ihl = (pkt[off] & 15) * 4
    if pkt[off + 9] != 17: continue
    src = socket.inet_ntoa(pkt[off + 12:off + 16])
    dport = struct.unpack("!H", pkt[off + ihl + 2:off + ihl + 4])[0]
    if dport != 8766: continue
    try: msg = json.loads(pkt[off + ihl + 8:].decode("utf-8", "replace"))
    except ValueError: msg = None
    k = kind(msg)
    seen.setdefault(k, {}).setdefault(src, 0)
    seen[k][src] += 1
print("     " + col("2", "(sniffing the node port %s)" % ("on" if sniff_ok else "not possible here, using the dashboard's view only")))
last = snaps[-1] if snaps else {}
def age_of(key):
    v = last.get(key)
    return None if not v else v.get("ageSec")
NODES = [("Mountain", "mountain", 6, "motion · vibration · light"),
         ("Traffic", "traffic", 6, "traffic lights + ultrasonic"),
         ("Seismic + gate", "seismic", 3, "sea movement · shake · NFC gate"),
         ("Display (OLED)", "matrix", 6, "status screen")]
for name, key, fresh, what in NODES:
    a = age_of(key)
    ips = seen.get(name, {})
    rate = sum(ips.values()) / LISTEN_SEC if ips else 0
    if a is not None and a < fresh:
        msg = "%s online (%s)" % (name, what)
        if ips: msg += "  ·  %s  ·  %.1f msg/s" % (", ".join(ips), rate)
        ok(msg)
    elif a is not None:
        bad("%s went SILENT %ds ago" % (name, a), "check its power and wifi (Serial Monitor 115200)")
    else:
        bad("%s never heard from" % name, "power it on; same hotspot; check the wifi name/password in its sketch")
    if len(ips) > 1:
        bad("DUPLICATE: %d boards are acting as %s: %s" % (len(ips), name, ", ".join(ips)),
            "two boards have the same sketch - unplug the extra one")
    if rate > 12:
        warn("%s sends a lot (%.0f msg/s)" % (name, rate), "normal max is about 5/s")
g = last.get("gate") or {}
if g.get("ageSec") is not None and g["ageSec"] < 3:
    (ok if g.get("nfc") else bad)("Parking gate card reader " + ("OK" if g.get("nfc") else "NOT FOUND"),
                                  "" if g.get("nfc") else "RC522 on 3V3, SDA→D8 SCK→D5 MOSI→D7 MISO→D6 RST→D0")
    nc, pc = g.get("nodeCards", 0), len(g.get("cards", []))
    (ok if nc == pc else warn)("Gate has %d of %d saved cards" % (nc, pc), "" if nc == pc else "wait 5 s - the Pi sends them every 2 s")
    info("gate is " + ("OPEN" if g.get("open") else "closed"))
if seen.get("unknown"):
    warn("Unknown messages from " + ", ".join(seen["unknown"]), "an old sketch or another device is sending to port 8766")
if last.get("alarm", {}).get("active"): warn("Mountain DISASTER alarm is active right now")
if last.get("quake", {}).get("active"): warn("TSUNAMI alert is active right now")

# ================================================================ 8. Extras
head("8. Tune-up extras (optional, from pi_optimize.sh)")
cfg = read("/boot/firmware/config.txt") or read("/boot/config.txt")
if "smartcity-optimize" in cfg:
    ok("pi_optimize.sh applied")
    if have("systemctl"):
        t = run("systemctl is-active smartcity-wifi-check.timer")[1]
        (ok if t == "active" else warn)("Wifi auto-reset checker " + t, "" if t == "active" else "run pi_optimize.sh again")
        wd = run("systemctl show -p RuntimeWatchdogUSec --value")[1]
        (ok if wd and wd not in ("0", "infinity") else warn)("Hardware watchdog " + ("on (%s)" % wd if wd and wd not in ("0", "infinity") else "off"))
else:
    info("not applied - optional:  sudo bash pi_optimize.sh && sudo reboot")

# ================================================================ summary
print()
line = "%d ok   %d warning%s   %d problem%s" % (counts["ok"], counts["warn"], "" if counts["warn"] == 1 else "s",
                                               counts["bad"], "" if counts["bad"] == 1 else "s")
if counts["bad"]:
    print(col("1;31", "✘ NOT READY  ·  " + line))
elif counts["warn"]:
    print(col("1;33", "! READY, with warnings  ·  " + line))
else:
    print(col("1;32", "✔ ALL GOOD - ready for the fair  ·  " + line))
if problems:
    print(col("1", "\nFix these:"))
    for sym, t, fix in problems:
        print("  %s %s%s" % (sym, t, ("\n      → " + fix) if fix else ""))
sys.exit(1 if counts["bad"] else 0)
PYTHON
