# Raspberry Pi setup

Tested on a Raspberry Pi 3B+ with Raspberry Pi OS, user `admin`, hostname `admin.local`.
Change `admin` in the commands below if your user name is different.

## 1. Install

```bash
sudo apt update
sudo apt install -y python3-serial
mkdir -p ~/smartcity
```

Copy the files from `raspberry-pi/` to the Pi (from your computer):

```bash
scp raspberry-pi/serial_dashboard.py raspberry-pi/alarm.mp3 admin@admin.local:~/smartcity/
```

`alarm.mp3` must sit next to `serial_dashboard.py`.

## 2. Run it once by hand

```bash
python3 ~/smartcity/serial_dashboard.py              # lists the serial ports it can see
python3 ~/smartcity/serial_dashboard.py /dev/ttyUSB0 # start with the Arduino Nano
```

It prints the address to open, for example `http://192.168.x.x:8765`.
`http://admin.local:8765` also works from most phones and laptops.

## 3. Start automatically at boot

```bash
scp raspberry-pi/smartcity-dashboard.service admin@admin.local:~/
ssh -t admin@admin.local
sudo mv ~/smartcity-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smartcity-dashboard
```

Useful commands:

```bash
sudo systemctl status smartcity-dashboard     # is it running?
journalctl -u smartcity-dashboard -f          # live log
sudo systemctl restart smartcity-dashboard    # after copying a new version
```

## 4. Update to a new version

```bash
scp raspberry-pi/serial_dashboard.py admin@admin.local:~/smartcity/
ssh -t admin@admin.local "sudo systemctl restart smartcity-dashboard"
```

(`-t` lets `sudo` ask for the password; without it you get *"a terminal is required"*.)
Then refresh the dashboard page.

## 5. Optional tune-up

`pi_optimize.sh` makes the Pi more reliable for a long exhibition day:
the dashboard restarts by itself if it crashes, a hardware watchdog reboots a frozen Pi,
logs are kept in RAM (fewer SD card writes), wifi power-saving is off with an automatic
wifi reset, and unused services are switched off for a faster boot.

```bash
scp raspberry-pi/pi_optimize.sh admin@admin.local:~/
ssh -t admin@admin.local "sudo bash pi_optimize.sh && sudo reboot"
```

Undo everything with `sudo bash pi_optimize.sh --undo`.

## 6. Health check: is everything working?

`check_smartcity.sh` looks at everything and prints a ✔ / ! / ✘ list with the fix for
each problem. It changes nothing.

```bash
scp raspberry-pi/check_smartcity.sh admin@admin.local:~/
ssh -t admin@admin.local "sudo bash check_smartcity.sh"
```

| Part | What it checks |
|---|---|
| Raspberry Pi | Board, power (under-voltage), temperature, SD card space / read-only, time zone |
| Wifi | Connected to `rishi`, IP address, phone hotspot and internet reachable, power-saving |
| Files | `serial_dashboard.py` is the latest version and has no errors, `alarm.mp3`, `gate_cards.json`, no duplicate copies or leftovers |
| Service | Installed, starts at boot, running, not crash-looping, started from **one** place only, exactly **one** dashboard running |
| Arduino Nano | `/dev/ttyUSB0` present, rain / gas / soil arriving |
| Dashboard | Page, live data, alarm sound and history answer |
| Wifi nodes | Listens 15 s: Mountain, Traffic, Seismic + gate and Display each **online** or **silent**, **duplicate** boards doing the same job, nodes sending too much, parking gate reader and saved cards |
| Extras | `pi_optimize.sh` tune-up (watchdog, wifi checker) |

It ends with **✔ ALL GOOD**, **! READY, with warnings** or **✘ NOT READY** and a *Fix these* list.
Keep every node switched on while it runs.

## Settings you may want to change

At the top of `serial_dashboard.py`:

| Setting | Default | Meaning |
|---|---|---|
| `HTTP_PORT` | `8765` | Dashboard web port |
| `WEATHER_PLACE`, `WEATHER_LAT`, `WEATHER_LON` | Thaliparamba (12.04, 75.36) | Where the live temperature comes from |
| `ALARM_BOTH_SEC` / `ALARM_CLEAR_SEC` | 0.5 s / 30 s | Mountain alarm: trigger time / auto-stop after quiet |
| `SEA_MOVE_CM` / `QUAKE_SHAKE_MIN` | 2.0 cm / 50 | Tsunami alert: water movement that counts / shake level that counts |
| `QUAKE_BOTH_SEC` / `QUAKE_CLEAR_SEC` | 0.5 s / 30 s | Tsunami alert: trigger time / auto-stop after calm |
| `CITY_UTC_OFFSET_MIN` | 330 | Time zone used for the history (India, UTC+5:30) |
| `CAR_NEAR_CM` | 20 | Traffic: something closer than this counts as one vehicle |
| `DENSITY_FULL_PER_MIN` / `DENSITY_WINDOW_MIN` | 12 / 5 | Traffic density: vehicles a minute that count as 100 % / minutes averaged |
