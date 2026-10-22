// Smart City - Seismic node (NodeMCU ESP8266)
// Board: "NodeMCU 1.0 (ESP-12E Module)"   esp8266 by ESP8266 Community
//
// Watches for an earthquake:
//   * SW-420 vibration sensor -> counts how hard the ground shakes (interrupt)
//   * HC-SR04 ultrasonic      -> learns the normal distance to a fixed target and reports
//                                how far it suddenly MOVES (interrupt timed, never blocks)
// Talks to the Raspberry Pi over wifi (UDP, no IP needed). The Pi raises
// "Earthquake detected" when big movement AND shaking stay on together for 0.5 s.
//
//   * Fast:     a report goes out the instant shaking or big movement starts
//   * Smooth:   ultrasonic read with interrupts (no pulseIn freezing the loop),
//               5-reading median filter throws away stray spikes
//   * Light:    1 report a second when calm, 5 a second while active (+5 s after)
//   * Sure:     broadcasts until the Pi answers, then sends straight to the Pi;
//               the Serial Monitor says "Pi heard us OK"
//   * Reliable: wifi reconnects by itself, the ESP8266's built-in watchdog reboots it
//               if it ever freezes, keeps reporting vibration if the ultrasonic is unplugged
//
// ---- Wiring ----
//   SW-420 vibration : VCC -> 3V3   GND -> G   DO   -> D5
//   HC-SR04 ultrasonic: VCC -> 3V3   GND -> G   TRIG -> D6   ECHO -> D7
//   (D5/D6/D7 are safe: they don't stop the NodeMCU from booting. Avoid D3, D4, D8.)
//   Still "NO ECHO" on 3.3V? HC-SR04 VCC -> VIN (5V) and ECHO -> 1k -> D7, D7 -> 2k -> G.
// Point the ultrasonic at something flat 10-100 cm away that moves when the "ground" shakes.

#include <ESP8266WiFi.h>
#include <WiFiUdp.h>

// ======== FILL THESE IN (same wifi the Pi is on) ========
const char *WIFI_SSID = "rishi";
const char *WIFI_PASS = "12345678";
// ========================================================

const unsigned int UDP_PORT = 8766;       // the Pi listens on this port
const unsigned int ACK_PORT = 8770;       // the Pi answers us here ("Pi heard us")
const char *PI_IP_FIXED = "";             // optional: the Pi's IP, e.g. "192.168.43.120" (normally learned automatically)

const int PIN_VIBRATION = D5;             // SW-420 DO
const int PIN_TRIG      = D6;
const int PIN_ECHO      = D7;
const int VIBRATION_ACTIVE = HIGH;        // flip to LOW if the card reads backwards

// ---- tuning ----
const unsigned long FAST_MS      = 200;   // report every 0.2 s while active
const unsigned long CALM_MS      = 1000;  // report every 1 s when calm
const unsigned long ACTIVE_HOLD  = 5000;  // stay fast this long after the last activity
const unsigned long PING_MS      = 40;    // ultrasonic ping every 40 ms
const unsigned long ECHO_WAIT_MS = 30;    // no echo after this = missed ping
const float MAX_CM          = 200;        // ignore anything further
const float MOVE_CM         = 4.0;        // "big movement" (same as QUAKE_MOVE_CM on the Pi)
const uint32_t SHAKE_FLIPS  = 2;          // vibration flips that count as "shaking"
const float BASE_LEARN      = 0.03;       // how fast "normal distance" follows slow drift
const unsigned long NEW_NORMAL_MS = 20000; // moved + no shaking this long = new normal
const unsigned long US_LOST_MS    = 3000;  // no echo this long = ultrasonic missing

WiFiUDP udp;

// ---- shared with the interrupts ----
volatile uint32_t vibFlips = 0;           // vibration sensor flips since last report
volatile uint32_t echoRise = 0, echoWidth = 0;
volatile bool echoDone = false;

void IRAM_ATTR onVibration() { vibFlips++; }

void IRAM_ATTR onEcho() {
  uint32_t t = micros();
  if (digitalRead(PIN_ECHO)) {
    echoRise = t;
  } else {
    echoWidth = t - echoRise;
    echoDone = true;
  }
}

// ---- state ----
float window5[5] = {-1, -1, -1, -1, -1};
int windowN = 0;
float cmNow = -1, baseCm = -1, moveMax = 0;
bool vibSeen = false, pingPending = false, echoStuck = false, wasActive = false;
unsigned long pingAt = 0, lastEcho = 0, lastSend = 0, activeUntil = 0, stillSince = 0;
unsigned long lastWifiTry = 0, lastHealth = 0, lastAck = 0, wifiLostAt = 0;
IPAddress piIP;                           // learned from the Pi's answer (or PI_IP_FIXED)
bool piKnown = false, udpReady = false;
uint32_t pings = 0, echoes = 0, flipsThisSec = 0, sentThisSec = 0;

float median5() {
  float v[5];
  int n = 0;
  for (int i = 0; i < 5; i++) if (window5[i] > 0) v[n++] = window5[i];
  if (n == 0) return -1;
  for (int i = 1; i < n; i++)
    for (int j = i; j > 0 && v[j] < v[j - 1]; j--) { float t = v[j]; v[j] = v[j - 1]; v[j - 1] = t; }
  return v[n / 2];
}

// a new reading (or a miss = -1) arrived: filter it and work out the movement
void newReading(float cm, unsigned long now) {
  window5[windowN] = cm;
  windowN = (windowN + 1) % 5;
  bool usOk = lastEcho != 0 && now - lastEcho < US_LOST_MS;
  cmNow = usOk ? median5() : -1;
  if (cmNow <= 0) return;
  if (baseCm < 0) baseCm = cmNow;                    // first good reading = normal distance
  float mv = fabs(cmNow - baseCm);
  if (mv > moveMax) moveMax = mv;
  bool shaking = vibSeen || vibFlips > 0;
  if (mv < 1.5 && !shaking) {                        // calm: follow slow drift
    baseCm += (cmNow - baseCm) * BASE_LEARN;
    stillSince = 0;
  } else if (!shaking) {                             // moved, then stayed still
    if (stillSince == 0) stillSince = now;
    if (now - stillSince > NEW_NORMAL_MS) { baseCm = cmNow; stillSince = 0; }
  } else {
    stillSince = 0;
  }
}

// ---- ultrasonic: start a ping, collect the echo later (never blocks) ----
void ultrasonicTick(unsigned long now) {
  if (pingPending && echoDone) {                     // echo came back
    pingPending = false;
    float cm = echoWidth / 58.0;
    if (cm >= 2 && cm <= MAX_CM) { echoes++; lastEcho = now; newReading(cm, now); }
    else newReading(-1, now);
  } else if (pingPending && now - pingAt > ECHO_WAIT_MS) {   // missed
    pingPending = false;
    newReading(-1, now);
  }
  if (!pingPending && now - pingAt >= PING_MS) {
    pingAt = now;
    pings++;
    if (digitalRead(PIN_ECHO) == HIGH) {             // ECHO must idle LOW
      echoStuck = true;
      newReading(-1, now);
      return;
    }
    echoDone = false;
    pingPending = true;
    digitalWrite(PIN_TRIG, HIGH);
    delayMicroseconds(10);
    digitalWrite(PIN_TRIG, LOW);
  }
}

// ---- networking helpers ----
bool piAnswering(unsigned long now) { return lastAck != 0 && now - lastAck < 5000; }

bool sendTo(IPAddress to, const char *body, int n) {
  if (!udp.beginPacket(to, UDP_PORT)) return false;
  udp.write((const uint8_t *)body, n);
  return udp.endPacket() == 1;
}

// wifi just connected: open our UDP port (the Pi's answers arrive here)
void networkUp() {
  udp.stop();
  udpReady = udp.begin(ACK_PORT) == 1;
  if (PI_IP_FIXED[0] && piIP.fromString(PI_IP_FIXED)) piKnown = true;
  Serial.printf("Connected, my IP: %s  |  UDP %s\n", WiFi.localIP().toString().c_str(), udpReady ? "ready" : "FAILED");
}

// the Pi answers {"ack":"seismic"}: now we know for sure it hears us, and its exact IP
void checkAnswers(unsigned long now) {
  int len = udp.parsePacket();
  while (len > 0) {
    char buf[48];
    int r = udp.read(buf, sizeof(buf) - 1);
    buf[r > 0 ? r : 0] = 0;
    if (strstr(buf, "\"ack\"")) {
      if (!piKnown || piIP != udp.remoteIP()) Serial.printf("Pi found at %s\n", udp.remoteIP().toString().c_str());
      piIP = udp.remoteIP();
      piKnown = true;
      lastAck = now;
    }
    len = udp.parsePacket();
  }
}

// ---- report to the Pi ----
void sendReport(unsigned long now) {
  noInterrupts();
  uint32_t flips = vibFlips;
  vibFlips = 0;
  interrupts();
  flipsThisSec += flips;
  lastSend = now;
  if (WiFi.status() == WL_CONNECTED) {
    char body[100];
    int n = snprintf(body, sizeof(body),
                     "{\"seismic\":1,\"vib\":%d,\"pulses\":%lu,\"cm\":%.1f,\"base\":%.1f,\"move\":%.1f}",
                     vibSeen ? 1 : 0, (unsigned long)flips, cmNow, baseCm, moveMax);
    if (piKnown) sendTo(piIP, body, n);                            // straight to the Pi
    if (!piAnswering(now)) {                                       // not sure the Pi hears us: also shout
      uint32_t ip = (uint32_t)WiFi.localIP(), mask = (uint32_t)WiFi.subnetMask();
      sendTo(IPAddress(ip | ~mask), body, n);                      // this network
      sendTo(IPAddress(255, 255, 255, 255), body, n);              // everyone
    }
    sentThisSec++;
  }
  vibSeen = false;
  moveMax = 0;
}

// ---- once a second: plain-English health check in the Serial Monitor (115200) ----
void healthReport(unsigned long now) {
  if (echoStuck)        Serial.print("ultrasonic: ECHO stays HIGH -> TRIG/ECHO swapped? (TRIG->D6, ECHO->D7)");
  else if (echoes == 0) Serial.print("ultrasonic: NO ECHO / missing -> check wires, flat target 10-100 cm, or VCC on 5V");
  else                  Serial.printf("ultrasonic OK: %.1f cm (normal %.1f, %u/%u pings)", cmNow, baseCm,
                                      (unsigned)echoes, (unsigned)pings);
  Serial.printf("  |  vibration %u flips/s  |  %s, %u reports/s  |  wifi %s  |  ", (unsigned)flipsThisSec,
                now < activeUntil ? "ACTIVE" : "calm", (unsigned)sentThisSec,
                WiFi.status() == WL_CONNECTED ? "ok" : "RECONNECTING");
  if (WiFi.status() != WL_CONNECTED) Serial.println("Pi: waiting for wifi");
  else if (piAnswering(now)) Serial.printf("Pi heard us OK (%s)\n", piIP.toString().c_str());
  else if (piKnown) Serial.printf("Pi NOT answering (sending to %s + broadcast) -> is the dashboard running?\n",
                                  piIP.toString().c_str());
  else Serial.println("Pi NOT answering (broadcasting) -> same wifi? new dashboard running on the Pi?");
  pings = echoes = flipsThisSec = sentThisSec = 0;
  echoStuck = false;
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_VIBRATION, INPUT);
  pinMode(PIN_TRIG, OUTPUT);
  pinMode(PIN_ECHO, INPUT);
  digitalWrite(PIN_TRIG, LOW);
  attachInterrupt(digitalPinToInterrupt(PIN_VIBRATION), onVibration, CHANGE);
  attachInterrupt(digitalPinToInterrupt(PIN_ECHO), onEcho, CHANGE);

  WiFi.persistent(false);                          // don't rewrite flash on every connect
  WiFi.mode(WIFI_STA);
  WiFi.setSleepMode(WIFI_NONE_SLEEP);              // no power-saving naps = faster alerts
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Connecting to wifi");
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 10000) { delay(250); Serial.print('.'); }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) networkUp();
  else Serial.println("No wifi yet (check name/password, 2.4 GHz). Sensors run anyway; retrying in the background.");
  lastWifiTry = millis();
}

void loop() {
  unsigned long now = millis();

  // 1. vibration level right now (flips are counted by the interrupt)
  if (digitalRead(PIN_VIBRATION) == VIBRATION_ACTIVE) vibSeen = true;

  // 2. ultrasonic (non-blocking)
  ultrasonicTick(now);

  // 3. activity: shaking or big movement -> report instantly, then stay fast for a while
  bool active = vibFlips >= SHAKE_FLIPS || vibSeen || moveMax >= MOVE_CM;
  if (active) {
    if (!wasActive && now - lastSend >= 30) sendReport(now);   // first sign: send right now
    wasActive = true;
    activeUntil = now + ACTIVE_HOLD;
  } else if (now >= activeUntil) {
    wasActive = false;
  }
  unsigned long every = now < activeUntil ? FAST_MS : CALM_MS;
  if (now - lastSend >= every) sendReport(now);

  // 4. health check
  if (now - lastHealth >= 1000) { lastHealth = now; healthReport(now); }

  // 5. wifi + Pi: answers from the Pi, find the Pi, and gentle reconnects
  if (WiFi.status() == WL_CONNECTED) {
    if (!udpReady) networkUp();                    // (re)connected: reopen UDP
    wifiLostAt = 0;
    checkAnswers(now);
  } else {
    if (udpReady) { udpReady = false; Serial.println("wifi lost, reconnecting..."); }
    if (wifiLostAt == 0) wifiLostAt = now;
    if (now - wifiLostAt > 20000 && now - lastWifiTry > 20000) {   // auto-reconnect stuck: nudge it
      lastWifiTry = now;
      WiFi.reconnect();
    }
  }
  delay(1);
}
