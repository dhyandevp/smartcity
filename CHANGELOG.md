# Changelog

What changed in the Smart City model, newest first.

## 6 Nov 2026 · Repo refresh
- New banner, system diagram and fresh screenshots of every card
- README rewritten: result, features by area, screens gallery, quick start with the health check
- Dashboard guide now explains City Risk, traffic density, the tsunami alert and the Raspberry Pi card
- Seismic card shows *Moving* / *Still* for the sea (the old *Moving 3.5 cm* text did not fit)
- Added the story film: [`docs/smart-city-film.html`](docs/smart-city-film.html)

## 3 Nov 2026 · After the district fair
- **City Risk**: one 0-100 score for the whole city from tsunami, landslide, flood, air and heat
- **Traffic density**: every vehicle the traffic ultrasonic sees is logged, with vehicles a minute,
  road density %, a 30-minute chart and a vehicle log
- **No more false "offline"**: the live stream sends at most 5 updates a second and never drops a
  slow phone; nodes only count as offline after 10 s of silence; the page reconnects by itself
- **Tsunami rule**: water **moving** 2 cm (ultrasonic) + shake 50, instead of water 4 cm from the sensor
- Live temperature now for **Thaliparamba**, Kannur (dashboard and OLED)
- Seismic node optimized: reacts to water movement at once, less RAM, fewer flash writes

## 31 Oct – 3 Nov 2026 · Kannur District Sasthrolsavam, Thaliparamba
- **Joint 3rd, A Grade** in Science Fair HSS/VHSS · Working Model, out of 126 teams

## 2 Nov 2026 · Parking gate and health check
- **NFC parking gate**: RC522 reader + servo barrier on the seismic node, its own dashboard card,
  cards saved from the dashboard and kept on the node (works with the Pi off)
- **Raspberry Pi card**: temperature, power, load, memory, wifi, up time, SD card, restart button
- `check_smartcity.sh`: one command checks the Pi, wifi, files, service, Nano, dashboard and every node
- Troubleshooting for a new Pi board that will not join the hotspot, and `ssh -t` for sudo

## 24 Oct 2026 · Fair day
- First tsunami alert rule (sea level high + strong shaking)
- 8-minute presentation, English and Malayalam

## 23 Oct 2026
- Every alarm plays `alarm.mp3`
- Air quality decided by gas level (50 % on, 45 % off), warning colours
- Motor control feature removed from the whole project

## 22 Oct 2026 · First commit
- Raspberry Pi dashboard, firmware for the Nano and the NodeMCU nodes, and the docs
- Seismic node moved from an ESP32 to a NodeMCU ESP8266, talking straight to the Pi
- Live gas trend in the Air quality tile
