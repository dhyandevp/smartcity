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
| `Failed to connect ... Timed out waiting for packet header` (ESP32) | Hold **BOOT** until the upload starts |
| `Failed to communicate with the flash chip` (ESP32) | Usually a faulty board: try another ESP32 |
| `D1 was not declared` (ESP32) | Use the GPIO numbers the sketch already uses, and the board **ESP32 Dev Module** |
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
