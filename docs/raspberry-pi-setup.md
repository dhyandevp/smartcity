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
ssh admin@admin.local
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
ssh admin@admin.local "sudo systemctl restart smartcity-dashboard"
```

Then refresh the dashboard page.

## 5. Optional tune-up

`pi_optimize.sh` makes the Pi more reliable for a long exhibition day:
the dashboard restarts by itself if it crashes, a hardware watchdog reboots a frozen Pi,
logs are kept in RAM (fewer SD card writes), wifi power-saving is off with an automatic
wifi reset, and unused services are switched off for a faster boot.

```bash
scp raspberry-pi/pi_optimize.sh admin@admin.local:~/
ssh admin@admin.local "sudo bash pi_optimize.sh && sudo reboot"
```

Undo everything with `sudo bash pi_optimize.sh --undo`.

## Settings you may want to change

At the top of `serial_dashboard.py`:

| Setting | Default | Meaning |
|---|---|---|
| `HTTP_PORT` | `8765` | Dashboard web port |
| `WEATHER_PLACE`, `WEATHER_LAT`, `WEATHER_LON` | Koothuparamba | Where the live temperature comes from |
| `ALARM_BOTH_SEC` / `ALARM_CLEAR_SEC` | 0.5 s / 30 s | Mountain alarm: trigger time / auto-stop after quiet |
| `QUAKE_MOVE_CM` | 4.0 cm | Ground movement that counts as "big" |
| `QUAKE_BOTH_SEC` / `QUAKE_CLEAR_SEC` | 0.5 s / 30 s | Earthquake: trigger time / auto-stop after calm |
| `CITY_UTC_OFFSET_MIN` | 330 | Time zone used for the history (India, UTC+5:30) |
