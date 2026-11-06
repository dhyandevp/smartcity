# Dashboard

Open `http://<pi-address>:8765` on any phone, tablet or laptop on the same wifi.
The page works in portrait and landscape and updates instantly when a sensor changes.

![Dashboard](images/dashboard_top.png)

The whole page in one picture: [images/dashboard_page.png](images/dashboard_page.png).

## Top bar

- Greeting for the time of day, and a live clock
- Status of every node: **Controller, Mountain, Traffic, Seismic, Display**
  (green = online, orange = lost, grey = waiting)
- **AI model** chip (jumps to the City AI card) and the **Sound** button. Browsers only
  play sound after one tap on the page, so tap once when the dashboard opens.

## Cards

| Card | Shows |
|---|---|
| City Risk | One 0-100 score for the whole city (Low < 30, Medium < 60, High), built from 5 hazards: tsunami, landslide, flood, air pollution and heat. Shows the main reason it is high. Any active alarm pushes it to 92+ |
| City overview | 8 areas at a glance, plus the air quality and soil moisture tiles |
| City AI | 4 live risk predictions (see below) |
| Rainfall | Rain sensor level and recent bars |
| Temperature | Live temperature for Thaliparamba, Kannur (Open-Meteo, every 5 minutes) |
| Security & Safety | Mountain motion and vibration, when each last triggered |
| Street Lighting | Ambient light level from the LDR |
| Status Display | A live copy of what the OLED in the model is showing |
| Traffic Signal | Manual / Auto, tap a lamp to switch it, live ultrasonic distance |
| Traffic Density | Vehicles a minute (last 5 min), road density % (12 a minute = 100 %, Low / Medium / Heavy), a 30-minute bar chart and a vehicle log. Every vehicle the traffic ultrasonic sees (closer than 20 cm) is logged as **live**; **sample** traffic fills in the rest, busier at 8-10 am and 4-7 pm |
| Seismic Monitor | Live 60 s seismograph, Calm / Light / Moderate / Strong, distance, sea movement and shake level |
| Raspberry Pi | CPU temperature, power (under-voltage), CPU load, memory, wifi name + signal, up time, SD card space, the address to open, and a **Restart dashboard** button (tap twice) |
| Parking Gate | NFC card reader + servo barrier: Open / Closed, last card (Allowed / Denied), opens today, saved cards and recent taps |
| City History | 24 h charts and an event log |

## City Risk

![City Risk](images/city_risk.png)

One number for the whole city, worked out with the City AI every 2 s. Each hazard gets a
0-100 % chance (tsunami from shaking × water movement, landslide and air pollution from the City AI,
flood from rain + wet soil, heat from the temperature). The score is mostly the **biggest** hazard
(75 %) plus a little of the average of all five (25 %), so one serious danger is never hidden by
four calm ones. An active alarm pushes it to at least 92. The box under the bar names the hazard
that is pushing the score up.

## Traffic density

![Traffic density](images/traffic_density.png)

- A vehicle is counted each time something comes closer than **20 cm** to the traffic ultrasonic
  (`CAR_NEAR_CM`), once per pass. It is logged as **live** with its distance.
- Vehicles a minute = vehicles in the last 5 minutes ÷ 5. **12 a minute = 100 %** road density
  (`DENSITY_FULL_PER_MIN`): under 35 % Low, under 70 % Medium, above that Heavy.
- **Sample** traffic (Car, Bike, Auto, Bus) keeps the road looking like a real town: quiet at night,
  busiest 8-10 am and 4-7 pm. Sample entries are always marked *sample*; *Sensor saw* counts only real ones.

## Seismic monitor and tsunami alert

![Seismic monitor during a tsunami](images/seismic_tsunami.png)

![Tsunami alert banner](images/tsunami_alert.png)

## Raspberry Pi

![Raspberry Pi card](images/pi_card.png)

Refreshed every 5 s. **Needs attention** when the CPU is 80 °C or hotter, the power is too low
right now, or wifi is off; **Check** for 70 °C+, a power dip since boot, weak wifi or less than
1 GB free on the SD card. *Restart dashboard* needs two taps and is back in about 3 s.

## Alarms

| Alarm | Fires when | Stops |
|---|---|---|
| **Potential disaster · Mountain** | Motion **and** vibration on together for 0.5 s | 30 s of quiet, or tap **Silence** |
| **Tsunami alert** | Water moving 2 cm or more (ultrasonic) **and** shake level 50+, together for 0.5 s | 30 s of calm, or tap **Silence** |

Both show a red banner, play `alarm.mp3`, flash the matching card and switch the OLED to
its warning screen. After **Silence** the alarm re-arms once the sensors calm down.

Pop-up notifications also appear for rain starting/stopping, poor air quality, dry soil,
high temperature, nodes going offline and every card tapped on the parking gate.
A node only counts as offline after 10 s of silence, and never while the page itself has lost
its link to the Pi (then the header says *Reconnecting* and the page reconnects by itself).

## Parking gate

![Parking gate](images/parking_gate_cards.png)

- Tap a **saved** card on the RC522 reader: the barrier opens for **2 s**, then closes by itself.
- Tap an **unknown** card: the gate stays shut and a peach **New card at the gate** box
  appears. Press **Allow** to save it as *Card 1*, *Card 2*… (up to 10). Unknown cards in
  *Recent taps* have their own **Allow** button too.
- **Remove** a saved card with two taps (*Remove* → *Sure?*).
- Saved cards are stored on the Pi in `raspberry-pi/gate_cards.json` and copied to the node,
  which keeps them in flash, so the gate keeps working if the Pi is switched off.
- Every opening, unknown card, save and removal also goes into the City History event log.

## City AI

![City AI](images/city_ai_card.png)

Every 2 seconds the dashboard turns the live sensor readings (rain, soil, gas and its
trend, vibration, motion, light, temperature, seismic shaking) and the last 24 hours of
history into **weighted risk scores**, then squashes each score into a 0-100 % chance:

- **Landslide risk**: wet rain sensor, recent rain, wet soil, shaking on the mountain or
  the seismic node, an active alarm
- **Air pollution risk**: gas level, rising gas, gas sensor alarm
- **Rain next hour**: raining now, recent rain, afternoon storm hours, heat
- **Traffic next hour**: the same hour yesterday blended with this hour so far

It also suggests an action (for example *Close the mountain road*).
It is a rule-based scoring model, not a trained neural network: the network drawing
is an illustration of the inputs and outputs.

## History

![History](images/history_card.png)

Hourly charts for temperature, rainfall, air quality, soil moisture, ambient light and
traffic, plus an event log. Because the model is not running all day, the history starts
filled with **sample data**, clearly marked with a *Sample data* tag and a *sample* label
on each sample event. Real readings replace the sample hours as the model runs, and the
tag disappears once 24 real hours exist. History is kept in memory, so it restarts with
the dashboard.
