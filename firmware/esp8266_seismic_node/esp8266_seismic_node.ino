// Smart City - Seismic node + Parking gate (NodeMCU ESP8266)
// Board: "NodeMCU 1.0 (ESP-12E Module)"   esp8266 by ESP8266 Community
// Libraries (Sketch > Include Library > Manage Libraries): "MFRC522" by GithubCommunity
//   (Servo, SPI and EEPROM come with the ESP8266 board package)
//
// 1. Watches the sea (tsunami alert):
//   * SW-420 vibration sensor -> counts how hard the ground shakes (interrupt)
//   * HC-SR04 ultrasonic      -> distance to the water (interrupt timed, never blocks)
//   The Pi raises "Tsunami alert" when the water is 4 cm or closer AND the shake level
//   is 50+, together for 0.5 s.
// 2. Parking gate:
//   * RC522 NFC/RFID reader   -> reads the card's ID
//   * Servo                   -> a SAVED card opens the gate (90 deg) for 2 s, then it closes
//   Unknown cards stay shut and show up on the dashboard with an "Allow" button.
//   The saved-card list comes from the Pi and is kept in flash, so the gate still
//   works if the Pi is off.
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
// ---- Wiring ----   (the RC522 needs D5/D6/D7, so the sensors moved to D1/D2/D3)
//   SW-420 vibration  : VCC -> 3V3   GND -> G   DO   -> D1
//   HC-SR04 ultrasonic: VCC -> 3V3   GND -> G   TRIG -> D3   ECHO -> D2
//   RC522 NFC reader  : 3.3V -> 3V3  GND -> G   RST  -> D0   SDA -> D8
//                       SCK -> D5    MISO -> D6  MOSI -> D7   IRQ -> (not connected)
//   Servo (SG90)      : red -> VIN (5V)   brown -> G   orange (signal) -> D4
//   The RC522 must be on 3.3V, NOT 5V.  Servo shakes or the board resets? Power the NodeMCU
//   from a good USB charger, or give the servo its own 5V (join the grounds).
//   Still "NO ECHO" on 3.3V? HC-SR04 VCC -> VIN (5V) and ECHO -> 1k -> D2, D2 -> 2k -> G.

#include <ESP8266WiFi.h>
#include <WiFiUdp.h>
#include <SPI.h>
#include <MFRC522.h>
#include <Servo.h>
#include <EEPROM.h>

// ======== FILL THESE IN (same wifi the Pi is on) ========
const char *WIFI_SSID = "rishi";
const char *WIFI_PASS = "12345678";
// ========================================================

const unsigned int UDP_PORT = 8766;       // the Pi listens on this port
const unsigned int ACK_PORT = 8770;       // the Pi answers us here ("Pi heard us")
const char *PI_IP_FIXED = "";             // optional: the Pi's IP, e.g. "192.168.43.120" (normally learned automatically)

const int PIN_VIBRATION = D1;             // SW-420 DO
const int PIN_TRIG      = D3;
const int PIN_ECHO      = D2;
const int VIBRATION_ACTIVE = HIGH;        // flip to LOW if the card reads backwards

// ---- parking gate ----
const int PIN_RC522_SS  = D8;             // RC522 SDA
const int PIN_RC522_RST = D0;             // RC522 RST
const int PIN_SERVO     = D4;             // servo signal (orange)
const int GATE_CLOSED_DEG = 0;            // servo angle when shut   (swap the two if it opens the wrong way)
const int GATE_OPEN_DEG   = 90;           // servo angle when open
const unsigned long GATE_OPEN_MS = 2000;  // a saved card keeps the gate open this long
const unsigned long SAME_CARD_MS = 1500;  // the same card read again within this time = still the same tap
const unsigned long NFC_EVERY_MS = 80;    // look for a card this often
const int MAX_CARDS = 10;

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

// ---- gate state ----
MFRC522 rfid(PIN_RC522_SS, PIN_RC522_RST);
Servo gateServo;
char cards[MAX_CARDS][21];                // saved card IDs (hex), from the Pi
int cardCount = 0;
bool nfcOk = false, gateOpen = false, gateChanged = false;
unsigned long gateCloseAt = 0, servoOffAt = 0, lastNfc = 0, lastNfcCheck = 0, lastCardAt = 0;
char lastCard[21] = "";
uint32_t scanSeq = 0;

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

void loadCards(const char *list, bool fromPi);

// the Pi answers {"ack":"seismic","cards":"04A1B2C3,..."}: now we know for sure it hears us,
// its exact IP, and the saved parking cards
void checkAnswers(unsigned long now) {
  int len = udp.parsePacket();
  while (len > 0) {
    char buf[300];
    int r = udp.read(buf, sizeof(buf) - 1);
    buf[r > 0 ? r : 0] = 0;
    if (strstr(buf, "\"ack\"")) {
      if (!piKnown || piIP != udp.remoteIP()) Serial.printf("Pi found at %s\n", udp.remoteIP().toString().c_str());
      piIP = udp.remoteIP();
      piKnown = true;
      lastAck = now;
      const char *c = strstr(buf, "\"cards\":\"");
      if (c) {
        c += 9;
        const char *e = strchr(c, '"');
        if (e && e - c < 260) {
          char list[260];
          memcpy(list, c, e - c);
          list[e - c] = 0;
          loadCards(list, true);
        }
      }
    }
    len = udp.parsePacket();
  }
}

// ---- parking gate ----
// saved cards are kept in flash: [0x5A][length][list text]
void saveCardsToFlash(const char *list) {
  int n = strlen(list);
  if (n > 250) return;
  EEPROM.write(0, 0x5A);
  EEPROM.write(1, n);
  for (int i = 0; i < n; i++) EEPROM.write(2 + i, list[i]);
  EEPROM.commit();
}

// "04A1B2C3,DEADBEEF" -> cards[]   (from the Pi: flash is only rewritten when the list really changed)
void loadCards(const char *list, bool fromPi) {
  char old[260] = "";
  for (int i = 0; i < cardCount; i++) { if (i) strcat(old, ","); strcat(old, cards[i]); }
  bool changed = strcmp(old, list) != 0;
  cardCount = 0;
  const char *p = list;
  while (*p && cardCount < MAX_CARDS) {
    const char *e = strchr(p, ',');
    int n = e ? e - p : strlen(p);
    if (n > 0 && n <= 20) { memcpy(cards[cardCount], p, n); cards[cardCount][n] = 0; cardCount++; }
    if (!e) break;
    p = e + 1;
  }
  if (!fromPi) return;
  if (changed) { saveCardsToFlash(list); Serial.printf("Saved cards updated by the Pi: %d card(s)\n", cardCount); }
}

void loadCardsFromFlash() {
  if (EEPROM.read(0) != 0x5A) return;
  int n = EEPROM.read(1);
  if (n > 250) return;
  char list[260];
  for (int i = 0; i < n; i++) list[i] = EEPROM.read(2 + i);
  list[n] = 0;
  loadCards(list, false);
  Serial.printf("Loaded %d saved card(s) from flash\n", cardCount);
}

bool cardSaved(const char *id) {
  for (int i = 0; i < cardCount; i++) if (strcmp(cards[i], id) == 0) return true;
  return false;
}

void moveServo(int deg, unsigned long now) {
  if (!gateServo.attached()) gateServo.attach(PIN_SERVO, 500, 2400);
  gateServo.write(deg);
  servoOffAt = now + 700;                          // let go after the move = no buzzing/jitter
}

void openGate(unsigned long now) {
  if (!gateOpen) { moveServo(GATE_OPEN_DEG, now); Serial.println("Gate OPEN"); gateChanged = true; }
  gateOpen = true;
  gateCloseAt = now + GATE_OPEN_MS;                // another saved tap while open = 2 s more
}

bool readerAlive() {
  byte v = rfid.PCD_ReadRegister(MFRC522::VersionReg);
  return v != 0x00 && v != 0xFF;
}

// tell the Pi about a card (sent twice, the Pi ignores the copy)
void sendScan(const char *id, bool ok, unsigned long now) {
  if (WiFi.status() != WL_CONNECTED) return;
  scanSeq++;
  char body[90];
  int n = snprintf(body, sizeof(body), "{\"gate\":\"scan\",\"uid\":\"%s\",\"ok\":%d,\"n\":%lu}",
                   id, ok ? 1 : 0, (unsigned long)scanSeq);
  for (int k = 0; k < 2; k++) {
    if (piKnown) sendTo(piIP, body, n);
    if (!piAnswering(now)) {
      uint32_t ip = (uint32_t)WiFi.localIP(), mask = (uint32_t)WiFi.subnetMask();
      sendTo(IPAddress(ip | ~mask), body, n);
    }
  }
}

void gateTick(unsigned long now) {
  if (gateOpen && (long)(now - gateCloseAt) >= 0) {        // time's up: close
    gateOpen = false;
    gateChanged = true;
    moveServo(GATE_CLOSED_DEG, now);
    Serial.println("Gate closed");
  }
  if (servoOffAt && (long)(now - servoOffAt) >= 0) { servoOffAt = 0; gateServo.detach(); }

  if (now - lastNfcCheck >= 5000) {                         // reader unplugged / stuck? restart it
    lastNfcCheck = now;
    nfcOk = readerAlive();
    if (!nfcOk) { rfid.PCD_Init(); nfcOk = readerAlive(); }
  }
  if (!nfcOk || now - lastNfc < NFC_EVERY_MS) return;
  lastNfc = now;
  if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) return;

  char id[21];
  int n = 0;
  for (byte i = 0; i < rfid.uid.size && n < 18; i++) n += snprintf(id + n, sizeof(id) - n, "%02X", rfid.uid.uidByte[i]);
  id[n] = 0;
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
  if (strcmp(id, lastCard) == 0 && now - lastCardAt < SAME_CARD_MS) { lastCardAt = now; return; }
  strcpy(lastCard, id);
  lastCardAt = now;

  bool ok = cardSaved(id);
  Serial.printf("Card %s -> %s\n", id, ok ? "ALLOWED, opening the gate" : "UNKNOWN, gate stays shut (press Allow on the dashboard)");
  if (ok) openGate(now);
  sendScan(id, ok, now);
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
    char body[140];
    int n = snprintf(body, sizeof(body),
                     "{\"seismic\":1,\"vib\":%d,\"pulses\":%lu,\"cm\":%.1f,\"base\":%.1f,\"move\":%.1f,"
                     "\"gate\":%d,\"nfc\":%d,\"cards\":%d}",
                     vibSeen ? 1 : 0, (unsigned long)flips, cmNow, baseCm, moveMax,
                     gateOpen ? 1 : 0, nfcOk ? 1 : 0, cardCount);
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
  if (echoStuck)        Serial.print("ultrasonic: ECHO stays HIGH -> TRIG/ECHO swapped? (TRIG->D3, ECHO->D2)");
  else if (echoes == 0) Serial.print("ultrasonic: NO ECHO / missing -> check wires, flat target 10-100 cm, or VCC on 5V");
  else                  Serial.printf("ultrasonic OK: %.1f cm (normal %.1f, %u/%u pings)", cmNow, baseCm,
                                      (unsigned)echoes, (unsigned)pings);
  Serial.printf("  |  vibration %u flips/s  |  %s, %u reports/s  |  gate %s, %s, %d card(s)  |  wifi %s  |  ",
                (unsigned)flipsThisSec, now < activeUntil ? "ACTIVE" : "calm", (unsigned)sentThisSec,
                gateOpen ? "OPEN" : "closed", nfcOk ? "reader OK" : "READER NOT FOUND (check 3V3, SDA->D8, RST->D0)",
                cardCount, WiFi.status() == WL_CONNECTED ? "ok" : "RECONNECTING");
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

  // parking gate: close the barrier, start the reader, load the saved cards
  EEPROM.begin(256);
  loadCardsFromFlash();
  moveServo(GATE_CLOSED_DEG, millis());
  SPI.begin();
  rfid.PCD_Init();
  nfcOk = readerAlive();
  Serial.println(nfcOk ? "NFC reader OK" : "NFC reader NOT FOUND -> check 3V3, SDA->D8, SCK->D5, MOSI->D7, MISO->D6, RST->D0");

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

  // 3. parking gate (card reader + servo); the dashboard hears about open/close straight away
  gateTick(now);
  if (gateChanged && now - lastSend >= 30) { gateChanged = false; sendReport(now); }

  // 4. activity: shaking or big movement -> report instantly, then stay fast for a while
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

  // 5. health check
  if (now - lastHealth >= 1000) { lastHealth = now; healthReport(now); }

  // 6. wifi + Pi: answers from the Pi, find the Pi, and gentle reconnects
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
