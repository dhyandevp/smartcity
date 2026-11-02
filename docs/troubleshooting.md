# Troubleshooting

Problems we actually hit while building the model, and what fixed them.

## A node shows "Waiting" on the dashboard

The Pi has never heard from it.

1. Open the Serial Monitor at **115200** and press the board's reset button.
2. It should print `Connected, my IP: ...`. If it prints `Wifi failed`, check the wifi
   name/password in the sketch and that the hotspot is **2.4 GHz**.
3. The board and the Pi must be on the **same** wifi.
4. A weak USB power bank or long cable can make the board reset: try another power source.

## Upload problems

| Message | Fix |
|---|---|
| `Failed to connect to ESP8266: Timed out` | Use a data USB cable, close the Serial Monitor, pick the right port and try again |
| Sketch path errors (`Invalid argument`) | Move the project out of OneDrive to a plain folder such as `D:\Smartcity` |

## Traffic light: one colour always on

Red was first wired to D3, which a module pulls LOW at power-up, so the NodeMCU did not
start the sketch. Red is on **D7** now.

## Ultrasonic says "no echo" / `-1`

- Check **Trig** and **Echo** are not swapped (the seismic node prints
  `ECHO stays HIGH` when they are).
- Point it at a flat, solid object 10-100 cm away.
- Some HC-SR04 modules do not work on 3.3 V: power from 5V/VIN and put a
  1 kΩ + 2 kΩ divider on Echo.

## Parking gate: "Reader missing" / card does nothing

- The RC522 must be on **3.3V**, never 5V. Check SDA → D8, SCK → D5, MOSI → D7, MISO → D6, RST → D0.
- The Serial Monitor (115200) prints `NFC reader OK` at start and `Card 04A1B2C3 -> ...` for each
  tap. No `Card` line = the reader doesn't see the card: hold it flat on the reader, 1-2 cm away.
- `UNKNOWN, gate stays shut` = the card isn't saved yet: press **Allow** on the dashboard and tap again.
- The servo moves the wrong way: swap `GATE_CLOSED_DEG` and `GATE_OPEN_DEG` in the sketch.
- The board resets when the servo moves: use a better USB charger, or a separate 5V for the servo
  (grounds joined).

## OLED stuck on one screen

The display only shows screens it has data for. If it stays on **Temperature**, the
Pi is not getting rain/gas readings: check the Arduino Nano is plugged into the Pi and
the **Controller** chip says Online.

## 8x8 LED matrix flickering

The first status display used an 8x8 MAX7219 matrix that flickered with all LEDs on;
it was replaced by the 0.96" OLED (`firmware/esp8266_oled_node`).

## No alarm sound

Browsers block sound until the page is tapped once. Tap anywhere, or the
**Tap to enable sound** button, after opening the dashboard.

## Light card shows the opposite

Set `LIGHT_INVERT` in `esp8266_mountain_node.ino` to the other value, and adjust
`LIGHT_DARK_RAW` / `LIGHT_BRIGHT_RAW` using the raw values printed in the Serial Monitor.
