// Smart City - NodeMCU 0.96" OLED (SSD1306, 128x64, I2C) city status display
// Board: "NodeMCU 1.0 (ESP-12E Module)"   (Tools -> Upload Speed -> 921600)
// NO extra library needed: only the built-in Wire.
//
// Rotates every few seconds:  WEATHER (sun / rain cloud)  ->  TEMPERATURE (big number)
//                             ->  AIR QUALITY (Good / POOR, gas detected)
// Disaster alarm on: flashing danger sign + DANGER.  Earthquake: flashing EARTHQUAKE.   No data from the Pi: "Waiting for Pi".
//
// ---- Wiring ----
//   OLED VCC -> NodeMCU 3V3
//   OLED GND -> G
//   OLED SCL -> D1
//   OLED SDA -> D2
// The Pi finds this board by itself and sends it the city status every second.

#include <ESP8266WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>

// ======== FILL THESE IN (same wifi the Pi is on) ========
const char *WIFI_SSID = "YOUR_WIFI_NAME";
const char *WIFI_PASS = "YOUR_WIFI_PASSWORD";
// ========================================================

const char *PLACE = "Koothuparamba";     // shown under the temperature
const unsigned int CMD_PORT    = 8769;   // the Pi sends the city status here
const unsigned int STATUS_PORT = 8766;   // we say "I'm here" here

const uint8_t OLED_ADDR = 0x3C;          // the back of your board says 0x3C
const bool FLIP_SCREEN = false;          // true = turn the picture upside down
const unsigned long SHOW_MS = 5000;      // how long each screen stays (5 s)

// =====================================================================
//  Tiny SSD1306 driver + drawing (replaces the Adafruit libraries)
// =====================================================================
uint8_t fb[1024];                        // 128 x 64 pixels, 1 bit each

const uint8_t FONT5x7[] PROGMEM = {      // ASCII 32..126, 5 columns per character
  0x00, 0x00, 0x00, 0x00, 0x00,   //  
  0x00, 0x00, 0x5F, 0x00, 0x00,   // !
  0x00, 0x07, 0x00, 0x07, 0x00,   // "
  0x14, 0x7F, 0x14, 0x7F, 0x14,   // #
  0x24, 0x2A, 0x7F, 0x2A, 0x12,   // $
  0x23, 0x13, 0x08, 0x64, 0x62,   // %
  0x36, 0x49, 0x56, 0x20, 0x50,   // &
  0x00, 0x08, 0x07, 0x03, 0x00,   // '
  0x00, 0x1C, 0x22, 0x41, 0x00,   // (
  0x00, 0x41, 0x22, 0x1C, 0x00,   // )
  0x2A, 0x1C, 0x7F, 0x1C, 0x2A,   // *
  0x08, 0x08, 0x3E, 0x08, 0x08,   // +
  0x00, 0x80, 0x70, 0x30, 0x00,   // ,
  0x08, 0x08, 0x08, 0x08, 0x08,   // -
  0x00, 0x00, 0x60, 0x60, 0x00,   // .
  0x20, 0x10, 0x08, 0x04, 0x02,   // /
  0x3E, 0x51, 0x49, 0x45, 0x3E,   // 0
  0x00, 0x42, 0x7F, 0x40, 0x00,   // 1
  0x72, 0x49, 0x49, 0x49, 0x46,   // 2
  0x21, 0x41, 0x49, 0x4D, 0x33,   // 3
  0x18, 0x14, 0x12, 0x7F, 0x10,   // 4
  0x27, 0x45, 0x45, 0x45, 0x39,   // 5
  0x3C, 0x4A, 0x49, 0x49, 0x31,   // 6
  0x41, 0x21, 0x11, 0x09, 0x07,   // 7
  0x36, 0x49, 0x49, 0x49, 0x36,   // 8
  0x46, 0x49, 0x49, 0x29, 0x1E,   // 9
  0x00, 0x00, 0x14, 0x00, 0x00,   // :
  0x00, 0x40, 0x34, 0x00, 0x00,   // ;
  0x00, 0x08, 0x14, 0x22, 0x41,   // <
  0x14, 0x14, 0x14, 0x14, 0x14,   // =
  0x00, 0x41, 0x22, 0x14, 0x08,   // >
  0x02, 0x01, 0x59, 0x09, 0x06,   // ?
  0x3E, 0x41, 0x5D, 0x59, 0x4E,   // @
  0x7C, 0x12, 0x11, 0x12, 0x7C,   // A
  0x7F, 0x49, 0x49, 0x49, 0x36,   // B
  0x3E, 0x41, 0x41, 0x41, 0x22,   // C
  0x7F, 0x41, 0x41, 0x41, 0x3E,   // D
  0x7F, 0x49, 0x49, 0x49, 0x41,   // E
  0x7F, 0x09, 0x09, 0x09, 0x01,   // F
  0x3E, 0x41, 0x41, 0x51, 0x73,   // G
  0x7F, 0x08, 0x08, 0x08, 0x7F,   // H
  0x00, 0x41, 0x7F, 0x41, 0x00,   // I
  0x20, 0x40, 0x41, 0x3F, 0x01,   // J
  0x7F, 0x08, 0x14, 0x22, 0x41,   // K
  0x7F, 0x40, 0x40, 0x40, 0x40,   // L
  0x7F, 0x02, 0x1C, 0x02, 0x7F,   // M
  0x7F, 0x04, 0x08, 0x10, 0x7F,   // N
  0x3E, 0x41, 0x41, 0x41, 0x3E,   // O
  0x7F, 0x09, 0x09, 0x09, 0x06,   // P
  0x3E, 0x41, 0x51, 0x21, 0x5E,   // Q
  0x7F, 0x09, 0x19, 0x29, 0x46,   // R
  0x26, 0x49, 0x49, 0x49, 0x32,   // S
  0x03, 0x01, 0x7F, 0x01, 0x03,   // T
  0x3F, 0x40, 0x40, 0x40, 0x3F,   // U
  0x1F, 0x20, 0x40, 0x20, 0x1F,   // V
  0x3F, 0x40, 0x38, 0x40, 0x3F,   // W
  0x63, 0x14, 0x08, 0x14, 0x63,   // X
  0x03, 0x04, 0x78, 0x04, 0x03,   // Y
  0x61, 0x59, 0x49, 0x4D, 0x43,   // Z
  0x00, 0x7F, 0x41, 0x41, 0x41,   // [
  0x02, 0x04, 0x08, 0x10, 0x20,   // backslash
  0x00, 0x41, 0x41, 0x41, 0x7F,   // ]
  0x04, 0x02, 0x01, 0x02, 0x04,   // ^
  0x40, 0x40, 0x40, 0x40, 0x40,   // _
  0x00, 0x03, 0x07, 0x08, 0x00,   // `
  0x20, 0x54, 0x54, 0x78, 0x40,   // a
  0x7F, 0x28, 0x44, 0x44, 0x38,   // b
  0x38, 0x44, 0x44, 0x44, 0x28,   // c
  0x38, 0x44, 0x44, 0x28, 0x7F,   // d
  0x38, 0x54, 0x54, 0x54, 0x18,   // e
  0x00, 0x08, 0x7E, 0x09, 0x02,   // f
  0x18, 0xA4, 0xA4, 0x9C, 0x78,   // g
  0x7F, 0x08, 0x04, 0x04, 0x78,   // h
  0x00, 0x44, 0x7D, 0x40, 0x00,   // i
  0x20, 0x40, 0x40, 0x3D, 0x00,   // j
  0x7F, 0x10, 0x28, 0x44, 0x00,   // k
  0x00, 0x41, 0x7F, 0x40, 0x00,   // l
  0x7C, 0x04, 0x78, 0x04, 0x78,   // m
  0x7C, 0x08, 0x04, 0x04, 0x78,   // n
  0x38, 0x44, 0x44, 0x44, 0x38,   // o
  0xFC, 0x18, 0x24, 0x24, 0x18,   // p
  0x18, 0x24, 0x24, 0x18, 0xFC,   // q
  0x7C, 0x08, 0x04, 0x04, 0x08,   // r
  0x48, 0x54, 0x54, 0x54, 0x24,   // s
  0x04, 0x04, 0x3F, 0x44, 0x24,   // t
  0x3C, 0x40, 0x40, 0x20, 0x7C,   // u
  0x1C, 0x20, 0x40, 0x20, 0x1C,   // v
  0x3C, 0x40, 0x30, 0x40, 0x3C,   // w
  0x44, 0x28, 0x10, 0x28, 0x44,   // x
  0x4C, 0x90, 0x90, 0x90, 0x7C,   // y
  0x44, 0x64, 0x54, 0x4C, 0x44,   // z
  0x00, 0x08, 0x36, 0x41, 0x00,   // {
  0x00, 0x00, 0x77, 0x00, 0x00,   // |
  0x00, 0x41, 0x36, 0x08, 0x00,   // }
  0x02, 0x01, 0x02, 0x04, 0x02,   // ~
};

void oledCmd(uint8_t c) {
  Wire.beginTransmission(OLED_ADDR);
  Wire.write(0x00);
  Wire.write(c);
  Wire.endTransmission();
}

bool oledBegin() {
  Wire.beginTransmission(OLED_ADDR);
  bool found = (Wire.endTransmission() == 0);
  const uint8_t init[] = {0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40, 0x8D, 0x14, 0x20, 0x00,
                          (uint8_t)(FLIP_SCREEN ? 0xA0 : 0xA1), (uint8_t)(FLIP_SCREEN ? 0xC0 : 0xC8),
                          0xDA, 0x12, 0x81, 0xCF, 0xD9, 0xF1, 0xDB, 0x40, 0xA4, 0xA6, 0x2E, 0xAF};
  for (uint8_t i = 0; i < sizeof(init); i++) oledCmd(init[i]);
  return found;
}

void oledShow() {
  oledCmd(0x21); oledCmd(0); oledCmd(127);   // columns 0..127
  oledCmd(0x22); oledCmd(0); oledCmd(7);     // pages 0..7
  for (int i = 0; i < 1024; i += 16) {
    Wire.beginTransmission(OLED_ADDR);
    Wire.write(0x40);
    for (int k = 0; k < 16; k++) Wire.write(fb[i + k]);
    Wire.endTransmission();
  }
}

void clearFb() { memset(fb, 0, sizeof(fb)); }

void px(int x, int y, bool on) {
  if (x < 0 || y < 0 || x > 127 || y > 63) return;
  if (on) fb[x + (y / 8) * 128] |= (1 << (y & 7));
  else    fb[x + (y / 8) * 128] &= ~(1 << (y & 7));
}

void fillRect(int x, int y, int w, int h, bool on) {
  for (int j = y; j < y + h; j++) for (int i = x; i < x + w; i++) px(i, j, on);
}

void line(int x0, int y0, int x1, int y1, bool on) {
  int dx = abs(x1 - x0), sx = x0 < x1 ? 1 : -1, dy = -abs(y1 - y0), sy = y0 < y1 ? 1 : -1, err = dx + dy;
  while (true) {
    px(x0, y0, on);
    if (x0 == x1 && y0 == y1) break;
    int e2 = 2 * err;
    if (e2 >= dy) { err += dy; x0 += sx; }
    if (e2 <= dx) { err += dx; y0 += sy; }
  }
}

void circle(int cx, int cy, int r, bool on) {
  int x = r, y = 0, err = 1 - r;
  while (x >= y) {
    px(cx + x, cy + y, on); px(cx + y, cy + x, on); px(cx - y, cy + x, on); px(cx - x, cy + y, on);
    px(cx - x, cy - y, on); px(cx - y, cy - x, on); px(cx + y, cy - x, on); px(cx + x, cy - y, on);
    y++;
    if (err < 0) err += 2 * y + 1; else { x--; err += 2 * (y - x) + 1; }
  }
}

void fillCircle(int cx, int cy, int r, bool on) {
  for (int y = -r; y <= r; y++)
    for (int x = -r; x <= r; x++)
      if (x * x + y * y <= r * r + r) px(cx + x, cy + y, on);
}

void fillTriangle(int x0, int y0, int x1, int y1, int x2, int y2, bool on) {
  int minX = min(x0, min(x1, x2)), maxX = max(x0, max(x1, x2));
  int minY = min(y0, min(y1, y2)), maxY = max(y0, max(y1, y2));
  for (int y = minY; y <= maxY; y++)
    for (int x = minX; x <= maxX; x++) {
      long a = (long)(x1 - x0) * (y - y0) - (long)(y1 - y0) * (x - x0);
      long b = (long)(x2 - x1) * (y - y1) - (long)(y2 - y1) * (x - x1);
      long c = (long)(x0 - x2) * (y - y2) - (long)(y0 - y2) * (x - x2);
      if ((a >= 0 && b >= 0 && c >= 0) || (a <= 0 && b <= 0 && c <= 0)) px(x, y, on);
    }
}

// text: size 1 = 6x8 pixels per character, size 2 = 12x16, size 3 = 18x24
void text(int x, int y, int size, const char *s, bool on = true) {
  for (; *s; s++, x += 6 * size) {
    char ch = (*s < 32 || *s > 126) ? '?' : *s;
    for (int col = 0; col < 5; col++) {
      uint8_t bits = pgm_read_byte(&FONT5x7[(ch - 32) * 5 + col]);
      for (int row = 0; row < 8; row++)
        if (bits & (1 << row)) fillRect(x + col * size, y + row * size, size, size, on);
    }
  }
}

// =====================================================================
//  City status screens
// =====================================================================
WiFiUDP rx, tx;
int rain = -1, gas = -1, alarmOn = 0, quakeOn = 0;    // -1 = unknown
bool haveTemp = false;
float temp = 0;
unsigned long lastData = 0, lastHello = 0, lastDraw = 0;

enum { ST_WEATHER, ST_TEMP, ST_AIR };
int stage = ST_WEATHER;
unsigned long stageAt = 0;

void header(const char *title) {
  text(0, 0, 1, title);
  fillRect(0, 11, 128, 1, true);
}

void drawSun(int cx, int cy) {
  const int8_t ix[8] = {0, 9, 13, 9, 0, -9, -13, -9}, iy[8] = {-13, -9, 0, 9, 13, 9, 0, -9};
  const int8_t ox[8] = {0, 13, 19, 13, 0, -13, -19, -13}, oy[8] = {-19, -13, 0, 13, 19, 13, 0, -13};
  fillCircle(cx, cy, 9, true);
  for (int i = 0; i < 8; i++) line(cx + ix[i], cy + iy[i], cx + ox[i], cy + oy[i], true);
}

void drawRainCloud(int x, int y, int phase) {
  fillCircle(x + 12, y + 14, 8, true);
  fillCircle(x + 24, y + 9, 11, true);
  fillCircle(x + 36, y + 14, 8, true);
  fillRect(x + 6, y + 13, 36, 10, true);
  for (int i = 0; i < 4; i++) {
    int dx = x + 8 + i * 10, dy = y + 27 + ((i + phase) % 2) * 6;
    line(dx, dy, dx - 2, dy + 5, true);
  }
}

void drawTick(int x, int y) {
  circle(x + 18, y + 18, 18, true);
  for (int t = 0; t < 3; t++) {
    line(x + 8, y + 18 + t, x + 15, y + 25 + t, true);
    line(x + 15, y + 25 + t, x + 29, y + 11 + t, true);
  }
}

void drawWarning(int x, int y) {
  fillTriangle(x + 18, y, x, y + 34, x + 36, y + 34, true);
  fillRect(x + 16, y + 11, 4, 13, false);
  fillRect(x + 16, y + 27, 4, 4, false);
}

void screenWeather(unsigned long now) {
  header("WEATHER");
  if (rain) {
    drawRainCloud(4, 18, (now / 300) % 2);
    text(60, 22, 2, "Rain");
    text(60, 44, 1, "raining now");
  } else {
    drawSun(26, 38);
    text(60, 22, 2, "Dry");
    text(60, 44, 1, "no rain");
  }
}

void screenTemp() {
  header("TEMPERATURE");
  char buf[12];
  dtostrf(temp, 0, 1, buf);                       // e.g. "29.7"
  int w = strlen(buf) * 18;                       // size 3 = 18 px per character
  int x = (128 - w - 20) / 2;
  text(x, 20, 3, buf);
  circle(x + w + 4, 22, 3, true);
  text(x + w + 9, 20, 2, "C");
  int pxs = (128 - (int)strlen(PLACE) * 6) / 2;
  text(pxs < 0 ? 0 : pxs, 54, 1, PLACE);
}

void screenAir(unsigned long now) {
  header("AIR QUALITY");
  if (gas) {
    if ((now / 400) % 2) drawWarning(6, 20);
    text(52, 22, 2, "POOR");
    text(52, 44, 1, "gas detected");
  } else {
    drawTick(6, 18);
    text(52, 22, 2, "Good");
    text(52, 44, 1, "air is safe");
  }
}

const uint8_t QUAKE_WAVE[33] = {1, 2, 1, 4, 2, 5, 5, 5, 8, 4, 10, 2, 11, 1, 10, 4, 8, 5, 5, 5, 3, 4, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1};

void screenQuake(unsigned long now) {
  bool on = (now / 300) % 2;
  if (on) fillRect(0, 0, 128, 64, true);
  text(4, 4, 2, "EARTHQUAKE", !on);
  for (int i = 0; i < 32; i++) {                 // seismograph zig-zag
    int y0 = 34 + (i % 2 ? QUAKE_WAVE[i] : -QUAKE_WAVE[i]);
    int y1 = 34 + ((i + 1) % 2 ? QUAKE_WAVE[i + 1] : -QUAKE_WAVE[i + 1]);
    line(i * 4, y0, i * 4 + 4, y1, !on);
  }
  text(19, 52, 1, "Take cover now!", !on);
}

void screenAlarm(unsigned long now) {
  bool on = (now / 300) % 2;
  if (on) fillRect(0, 0, 128, 64, true);
  fillTriangle(22, 4, 42, 44, 2, 44, !on);       // danger triangle
  fillRect(20, 16, 4, 16, on);                    // "!" cut out of the triangle
  fillRect(20, 36, 4, 4, on);
  text(50, 18, 2, "DANGER", !on);
  text(10, 52, 1, "Potential disaster", !on);
}

void screenWaiting(unsigned long now) {
  header("SMART CITY");
  text(4, 26, 1, WiFi.status() == WL_CONNECTED ? "Waiting for Pi" : "Connecting wifi");
  int dots = (now / 400) % 4;
  for (int i = 0; i < dots; i++) fillCircle(100 + i * 7, 29, 2, true);
  if (WiFi.status() == WL_CONNECTED) text(4, 48, 1, WiFi.localIP().toString().c_str());
}

bool stageReady(int s) {
  if (s == ST_WEATHER) return rain >= 0;
  if (s == ST_TEMP) return haveTemp;
  return gas >= 0;
}

void nextStage() {
  for (int i = 1; i <= 3; i++) {
    int s = (stage + i) % 3;
    if (stageReady(s)) { stage = s; stageAt = millis(); return; }
  }
  stageAt = millis();
}

const char *curScreen = "wait";          // which screen is showing (sent to the Pi for the dashboard preview)
const char *sentScreen = "";
void sayHello();

void render() {
  unsigned long now = millis();
  clearFb();
  if (lastData == 0 || now - lastData > 5000) {
    screenWaiting(now); curScreen = "wait";
  } else if (quakeOn) {
    screenQuake(now); curScreen = "quake";
  } else if (alarmOn) {
    screenAlarm(now); curScreen = "alarm";
  } else {
    if (!stageReady(stage) || now - stageAt >= SHOW_MS) nextStage();
    if (!stageReady(stage)) { screenWaiting(now); curScreen = "wait"; }
    else if (stage == ST_WEATHER) { screenWeather(now); curScreen = "weather"; }
    else if (stage == ST_TEMP) { screenTemp(); curScreen = "temp"; }
    else { screenAir(now); curScreen = "air"; }
  }
  oledShow();
  if (strcmp(curScreen, sentScreen) != 0) sayHello();   // tell the Pi straight away when the screen changes
}

// ---------- messages from the Pi:  {"rain":1,"temp":29.7,"gas":0,"alarm":0} ----------
int readFlag(const char *buf, const char *key) {
  const char *p = strstr(buf, key);
  if (!p) return -1;
  p += strlen(key);
  if (*p == '1' || *p == 't') return 1;
  if (*p == '0' || *p == 'f') return 0;
  return -1;
}

void handleData(const char *buf) {
  rain = readFlag(buf, "\"rain\":");
  gas = readFlag(buf, "\"gas\":");
  alarmOn = readFlag(buf, "\"alarm\":") == 1;
  quakeOn = readFlag(buf, "\"quake\":") == 1;
  const char *p = strstr(buf, "\"temp\":");
  if (p && p[7] != 'n') { temp = atof(p + 7); haveTemp = true; }
  else haveTemp = false;
  lastData = millis();
}

void sayHello() {
  lastHello = millis();
  if (WiFi.status() != WL_CONNECTED) return;
  char body[48];
  snprintf(body, sizeof(body), "{\"matrix\":\"ok\",\"screen\":\"%s\"}", curScreen);
  sentScreen = curScreen;
  uint32_t ip   = (uint32_t)WiFi.localIP();
  uint32_t mask = (uint32_t)WiFi.subnetMask();
  IPAddress directed(ip | ~mask);
  IPAddress generic(255, 255, 255, 255);
  tx.beginPacket(directed, STATUS_PORT);
  tx.write((const uint8_t *)body, strlen(body));
  tx.endPacket();
  tx.beginPacket(generic, STATUS_PORT);
  tx.write((const uint8_t *)body, strlen(body));
  tx.endPacket();
}

void setup() {
  Serial.begin(115200);
  Wire.begin(D2, D1);                               // SDA = D2, SCL = D1
  Wire.setClock(400000);
  if (!oledBegin()) Serial.println("\nOLED not found: check wiring (SCL D1, SDA D2) and the address 0x3C");
  clearFb();
  header("SMART CITY");
  text(4, 26, 1, "Starting...");
  oledShow();

  WiFi.mode(WIFI_STA);
  WiFi.setSleepMode(WIFI_NONE_SLEEP);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Connecting");
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(300);
    Serial.print('.');
    render();
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("\nConnected, my IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nWifi failed: check name/password and that it is 2.4 GHz. Retrying in background.");
  }
  rx.begin(CMD_PORT);
  stageAt = millis();
}

void loop() {
  int n = rx.parsePacket();
  if (n > 0) {
    char buf[96];
    int len = rx.read(buf, sizeof(buf) - 1);
    buf[len > 0 ? len : 0] = 0;
    handleData(buf);
  }
  if (millis() - lastHello >= 1000) sayHello();
  if (millis() - lastDraw >= 100) { lastDraw = millis(); render(); }
  delay(2);
}
