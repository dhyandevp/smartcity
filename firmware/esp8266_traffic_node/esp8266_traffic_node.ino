// Smart City - NodeMCU ESP8266 traffic signal node (red / yellow / green + HC-SR04 ultrasonic)
// Board: "NodeMCU 1.0 (ESP-12E Module)"   (Tools -> Upload Speed -> 921600 for fast uploads)
//
// MANUAL mode: the dashboard turns each light on or off by itself.
// AUTO mode:   the ultrasonic sensor controls the lights:
//                nothing near      -> GREEN
//                closer than 20 cm -> YELLOW
//                closer than 10 cm -> RED
//              and it goes back to green when the object moves away.
// The Pi finds this board by itself (it broadcasts its state on the wifi).
//
// ---- Wiring ----
//   Traffic light module:  G -> D1,  Y -> D2,  R -> D7,  GND -> G
//   (Red is on D7, not D3: a module pulls D3 LOW at power-up and the NodeMCU
//    would not start the sketch.)
//
//   HC-SR04 VCC  -> NodeMCU VIN (5V, when powered by USB)
//   HC-SR04 GND  -> G
//   HC-SR04 Trig -> D5
//   HC-SR04 Echo -> 1k resistor -> D6, and D6 -> 2k resistor -> G
//   The Echo pin sends 5V. The two resistors bring it down to about 3.3V, which is
//   what the NodeMCU can take. Do not connect Echo straight to D6.
//   (HC-SR04P / 3.3V version powered from 3V3: no resistors needed.)

#include <ESP8266WiFi.h>
#include <WiFiUdp.h>

// ======== FILL THESE IN (same wifi the Pi is on) ========
const char *WIFI_SSID = "rishi";
const char *WIFI_PASS = "12345678";
// ========================================================

const unsigned int CMD_PORT    = 8768;   // the Pi sends commands here
const unsigned int STATUS_PORT = 8766;   // we broadcast our state here

const int PIN_GREEN  = D1;
const int PIN_YELLOW = D2;
const int PIN_RED    = D7;
const int PIN_TRIG   = D5;
const int PIN_ECHO   = D6;

// ---- auto mode distances ----
const float YELLOW_CM = 20.0;            // closer than this -> yellow
const float RED_CM    = 10.0;            // closer than this -> red
const float HYST_CM   = 1.5;             // small margin so the light doesn't flicker at the edge
const unsigned long CALM_MS = 600;       // object must stay away this long before stepping back to green

const bool START_AUTO      = false;      // true = start in auto mode after power-up
const bool LED_ACTIVE_HIGH = true;       // set false if your module lights up on LOW

WiFiUDP rx;      // receives commands
WiFiUDP tx;      // sends status
bool autoMode = START_AUTO;
bool lamp[3] = {false, false, true};     // red, yellow, green (starts on green)
int level = 0;                           // auto mode: 0 = green, 1 = yellow, 2 = red
float distCm = -1;                       // -1 = nothing in range
float hist[3] = {-1, -1, -1};            // last 3 readings, for a median filter
int histPos = 0;
unsigned long calmSince = 0, lastPing = 0, lastStatus = 0;
bool statusDue = true;

float smaller(float a, float b) { return a < b ? a : b; }
float bigger(float a, float b)  { return a > b ? a : b; }

void out(int pin, bool on) { digitalWrite(pin, (on == LED_ACTIVE_HIGH) ? HIGH : LOW); }

void applyLamps() {
  out(PIN_RED, lamp[0]);
  out(PIN_YELLOW, lamp[1]);
  out(PIN_GREEN, lamp[2]);
  statusDue = true;
}

void setAutoLamps() {
  lamp[0] = (level == 2);
  lamp[1] = (level == 1);
  lamp[2] = (level == 0);
  applyLamps();
}

// one HC-SR04 reading in cm, or -1 if nothing echoed back (nothing within about 4 m)
float pingCm() {
  digitalWrite(PIN_TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(PIN_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);
  unsigned long us = pulseIn(PIN_ECHO, HIGH, 25000UL);
  if (us == 0) return -1;
  return us / 58.0;
}

// median of the last 3 readings, so one bad reading can't flip the light
float filteredCm(float fresh) {
  hist[histPos] = fresh;
  histPos = (histPos + 1) % 3;
  float a = hist[0] < 0 ? 9999 : hist[0];
  float b = hist[1] < 0 ? 9999 : hist[1];
  float c = hist[2] < 0 ? 9999 : hist[2];
  float m = bigger(smaller(a, b), smaller(bigger(a, b), c));
  return m >= 9999 ? -1 : m;
}

// which level the distance asks for (with a small margin when stepping back)
int targetLevel(float cm, int current) {
  if (cm < 0) return 0;
  int t = (cm < RED_CM) ? 2 : (cm < YELLOW_CM) ? 1 : 0;
  if (t < current) {
    if (current == 2 && cm < RED_CM + HYST_CM) t = 2;
    else if (t == 0 && cm < YELLOW_CM + HYST_CM) t = 1;
  }
  return t;
}

void autoStep() {
  int t = targetLevel(distCm, level);
  if (t > level) {                         // getting closer: change right away
    level = t;
    calmSince = 0;
    setAutoLamps();
  } else if (t < level) {                  // moving away: wait CALM_MS first
    if (calmSince == 0) calmSince = millis();
    if (millis() - calmSince >= CALM_MS) {
      level = t;
      calmSince = 0;
      setAutoLamps();
    }
  } else {
    calmSince = 0;
  }
}

void handleCmd(const char *buf) {
  if (strstr(buf, "\"mode\"")) {
    if (strstr(buf, "\"auto\"") && !autoMode) {
      autoMode = true;
      level = 0;
      calmSince = 0;
      setAutoLamps();
      autoStep();
      Serial.println("mode: AUTO");
    } else if (strstr(buf, "\"manual\"") && autoMode) {
      autoMode = false;                    // lights stay as they are
      Serial.println("mode: MANUAL");
    }
    statusDue = true;
    return;
  }
  if (strstr(buf, "\"light\"")) {
    if (autoMode) return;                  // the sensor is in charge in auto mode
    int i = strstr(buf, "\"red\"") ? 0 : strstr(buf, "\"yellow\"") ? 1 : strstr(buf, "\"green\"") ? 2 : -1;
    if (i < 0) return;
    lamp[i] = (strstr(buf, "\"on\":1") != NULL);
    applyLamps();
    Serial.print("light ");
    Serial.print(i == 0 ? "RED" : i == 1 ? "YELLOW" : "GREEN");
    Serial.println(lamp[i] ? " on" : " off");
  }
}

void sendStatus() {
  lastStatus = millis();
  statusDue = false;
  if (WiFi.status() != WL_CONNECTED) return;
  int cm = distCm < 0 ? -1 : (int)(distCm + 0.5);
  char body[80];
  snprintf(body, sizeof(body), "{\"traffic\":\"%s\",\"r\":%d,\"y\":%d,\"g\":%d,\"cm\":%d}",
           autoMode ? "auto" : "manual", lamp[0], lamp[1], lamp[2], cm);

  // broadcast address of this network (works for any subnet) + the generic one
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
  pinMode(PIN_RED, OUTPUT);
  pinMode(PIN_YELLOW, OUTPUT);
  pinMode(PIN_GREEN, OUTPUT);
  pinMode(PIN_TRIG, OUTPUT);
  pinMode(PIN_ECHO, INPUT);
  digitalWrite(PIN_TRIG, LOW);
  if (autoMode) setAutoLamps(); else applyLamps();

  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.setSleepMode(WIFI_NONE_SLEEP);      // no wifi power-saving naps = faster response
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Connecting");
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) { delay(300); Serial.print('.'); }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("\nConnected, my IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nWifi failed: check name/password and that it is 2.4 GHz. Retrying in background.");
  }
  rx.begin(CMD_PORT);
}

void loop() {
  // 1. command from the Pi?
  int n = rx.parsePacket();
  if (n > 0) {
    char buf[64];
    int len = rx.read(buf, sizeof(buf) - 1);
    buf[len > 0 ? len : 0] = 0;
    handleCmd(buf);
  }

  // 2. measure the distance about 12 times a second (auto mode keeps working without wifi)
  if (millis() - lastPing >= 80) {
    lastPing = millis();
    distCm = filteredCm(pingCm());
    if (autoMode) autoStep();
  }

  // 3. tell the Pi our state (right away on change, otherwise a few times a second)
  if (statusDue || millis() - lastStatus >= 400) sendStatus();

  delay(2);
}
