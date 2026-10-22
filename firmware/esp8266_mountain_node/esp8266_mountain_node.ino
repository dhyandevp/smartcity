// Smart City - NodeMCU ESP8266 mountain node (UDP broadcast version)
// Board: "NodeMCU 1.0 (ESP-12E Module)"
//
// Sends motion + vibration to EVERYONE on the wifi once a second (UDP broadcast).
// The Raspberry Pi listens for it, so you never need the Pi's IP address.
//
// PIR OUT   -> D5   (PIR VCC -> VIN)
// SW-420 DO -> D6   (SW-420 VCC -> 3V3)
// LDR       -> A0   (LDR module AO, VCC -> 3V3.  Or bare LDR: 3V3 -> LDR -> A0 -> 10k -> G)
// all GND -> G.  NEVER put more than 3.3V on A0.

#include <ESP8266WiFi.h>
#include <WiFiUdp.h>

// ======== FILL THESE IN (same wifi the Pi is on) ========
const char *WIFI_SSID = "rishi";
const char *WIFI_PASS = "12345678";
// ========================================================

const unsigned int UDP_PORT = 8766;   // the Pi listens on this port

const int PIN_MOTION    = D5;   // PIR OUT
const int PIN_VIBRATION = D6;   // SW-420 DO
const int PIN_LIGHT     = A0;   // LDR (the NodeMCU has only this one analog pin)

// Level that means "triggered". Flip to LOW if a card shows backwards.
const int MOTION_ACTIVE    = HIGH;
const int VIBRATION_ACTIVE = HIGH;

// ---- Light (LDR) ----
// The sketch turns the raw reading (0-1023) into 0-100 %  (0 = dark, 100 = bright).
// Calibrate: read the raw value (printed in the Serial Monitor) in your darkest and
// brightest conditions and put them here.
const int  LIGHT_DARK_RAW   = 20;     // raw value in the dark
const int  LIGHT_BRIGHT_RAW = 900;    // raw value in bright light
const bool LIGHT_INVERT     = true;   // true = number goes DOWN when it gets brighter (flipped for this LDR module)

WiFiUDP udp;
bool motion = false, vibration = false, lastMotion = false, lastVibration = false;
float lightSmooth = -1;          // smoothed raw LDR value
int light = 0, lastLight = -100; // percent
unsigned long vibrationUntil = 0, lastSend = 0;

void setup() {
  Serial.begin(115200);
  pinMode(PIN_MOTION, INPUT);
  pinMode(PIN_VIBRATION, INPUT);

  WiFi.mode(WIFI_STA);
  WiFi.setSleepMode(WIFI_NONE_SLEEP);   // no wifi power-saving naps = lower latency
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Connecting");
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) { delay(300); Serial.print('.'); }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("\nConnected, my IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.print("\nWifi failed (status ");
    Serial.print(WiFi.status());
    Serial.println("): check name/password and that it is 2.4 GHz. Retrying in background.");
  }
}

void loop() {
  motion = (digitalRead(PIN_MOTION) == MOTION_ACTIVE);

  // vibration blips are short, so hold the state for 0.6 s
  if (digitalRead(PIN_VIBRATION) == VIBRATION_ACTIVE) vibrationUntil = millis() + 600;
  vibration = (millis() < vibrationUntil);

  // LDR: smooth it so it doesn't flicker, then convert to 0-100 %
  int rawLight = analogRead(PIN_LIGHT);
  lightSmooth = (lightSmooth < 0) ? rawLight : lightSmooth * 0.8 + rawLight * 0.2;
  float pct = (lightSmooth - LIGHT_DARK_RAW) * 100.0 / (LIGHT_BRIGHT_RAW - LIGHT_DARK_RAW);
  pct = constrain(pct, 0, 100);
  light = (int)(LIGHT_INVERT ? 100 - pct : pct);

  bool changed = (motion != lastMotion) || (vibration != lastVibration) ||
                 (abs(light - lastLight) >= 3);   // only send light when it really changes
  if ((changed && millis() - lastSend > 40) || millis() - lastSend >= 1000) {
    lastMotion = motion; lastVibration = vibration; lastLight = light; lastSend = millis();

    if (WiFi.status() == WL_CONNECTED) {
      char body[64];
      snprintf(body, sizeof(body), "{\"motion\":%d,\"vibration\":%d,\"light\":%d}",
               motion, vibration, light);

      // broadcast address of this network (works for any subnet) + the generic one
      uint32_t ip   = (uint32_t)WiFi.localIP();
      uint32_t mask = (uint32_t)WiFi.subnetMask();
      IPAddress directed(ip | ~mask);
      IPAddress generic(255, 255, 255, 255);

      udp.beginPacket(directed, UDP_PORT);
      udp.write((const uint8_t *)body, strlen(body));
      udp.endPacket();
      udp.beginPacket(generic, UDP_PORT);
      udp.write((const uint8_t *)body, strlen(body));
      udp.endPacket();

      Serial.print(body);
      Serial.print("  (LDR raw ");
      Serial.print(rawLight);
      Serial.print(")  -> broadcast to ");
      Serial.println(directed);
    }
  }
  delay(10);
}
