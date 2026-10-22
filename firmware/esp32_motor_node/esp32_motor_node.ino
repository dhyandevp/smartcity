// Smart City - ESP32 motor node (2 motors + L293D driver board)
// Board: "ESP32 Dev Module"   (Tools -> Upload Speed -> 921600 for fast uploads)
//
// The dashboard on the Raspberry Pi sends UP / DOWN / STOP over wifi (UDP).
// This board announces itself twice a second (UDP broadcast), so the Pi
// finds it without you ever typing an IP address.
//
// SAFETY: the motors always stop by themselves after RUN_SECONDS, and also
// if the wifi drops. Set RUN_SECONDS to a bit more than your full travel time.
//
// ---- Wiring (L293D driver board header) ----
//   A1 -> GPIO 25        A2 -> GPIO 26        (motor A on terminals MA1 / MA2)
//   B1 -> GPIO 27        B2 -> GPIO 32        (motor B on terminals MB1 / MB2)
//   EN_A + EN_B -> GPIO 33   (join the two EN pins, one wire to GPIO 33)
//   VCC (header)  -> ESP32 VIN / 5V   (driver logic needs 5V, NOT 3V3)
//   GND (header)  -> ESP32 GND
//   +V / - terminals -> motor battery + / -   (4.5V to 7V for small motors)
//   ALL grounds joined: ESP32 GND, driver GND, battery -.
//   Keep GPIO 0, 2, 12, 15 free: they can stop the ESP32 from booting.

#include <WiFi.h>
#include <WiFiUdp.h>

// ======== FILL THESE IN (same wifi the Pi is on) ========
const char *WIFI_SSID = "rishi";
const char *WIFI_PASS = "12345678";
// ========================================================

const unsigned int CMD_PORT    = 8767;   // the Pi sends commands here
const unsigned int STATUS_PORT = 8766;   // we broadcast our state here

const unsigned long RUN_SECONDS      = 10;    // auto-stop after this many seconds
const unsigned long REVERSE_PAUSE_MS = 300;   // pause before switching direction (easier on the motors)

// If a motor turns the wrong way for UP, flip its line to true.
const bool MOTOR_A_INVERT = false;
const bool MOTOR_B_INVERT = false;

const int PIN_EN  = 33;   // EN_A + EN_B
const int PIN_IN1 = 25;   // A1
const int PIN_IN2 = 26;   // A2
const int PIN_IN3 = 27;   // B1
const int PIN_IN4 = 32;   // B2

WiFiUDP rx;      // receives commands
WiFiUDP tx;      // sends status
int dir = 0;                 // 1 = up, -1 = down, 0 = stopped
int pendingDir = 0;          // direction to start after the reverse pause
unsigned long pendingAt = 0, stopAt = 0, lastStatus = 0;
bool statusDue = true;

// 0 = stop, 1 = up, -1 = down
void drive(int d) {
  dir = d;
  if (d == 0) {
    digitalWrite(PIN_EN, LOW);
    digitalWrite(PIN_IN1, LOW);
    digitalWrite(PIN_IN2, LOW);
    digitalWrite(PIN_IN3, LOW);
    digitalWrite(PIN_IN4, LOW);
    return;
  }
  bool aFwd = (d > 0) != MOTOR_A_INVERT;
  bool bFwd = (d > 0) != MOTOR_B_INVERT;
  digitalWrite(PIN_IN1, aFwd ? HIGH : LOW);
  digitalWrite(PIN_IN2, aFwd ? LOW : HIGH);
  digitalWrite(PIN_IN3, bFwd ? HIGH : LOW);
  digitalWrite(PIN_IN4, bFwd ? LOW : HIGH);
  digitalWrite(PIN_EN, HIGH);
  stopAt = millis() + RUN_SECONDS * 1000UL;
}

void command(int d) {
  if (d != 0 && d == pendingDir) return;   // repeat of a command we already accepted
  pendingDir = 0;
  if (d == 0) {
    drive(0);
  } else if (dir == 0 || d == dir) {
    drive(d);                              // start, or same direction again (restarts the timer)
  } else {
    drive(0);                              // reversing: stop first, then start after a short pause
    pendingDir = d;
    pendingAt = millis() + REVERSE_PAUSE_MS;
  }
  statusDue = true;
}

void sendStatus() {
  lastStatus = millis();
  statusDue = false;
  if (WiFi.status() != WL_CONNECTED) return;

  const char *s = (dir > 0) ? "up" : (dir < 0) ? "down" : "stop";
  int left = (dir != 0) ? (int)((stopAt - millis()) / 1000UL) + 1 : 0;
  char body[48];
  snprintf(body, sizeof(body), "{\"motor\":\"%s\",\"left\":%d}", s, left);

  IPAddress directed = WiFi.broadcastIP();   // broadcast address of this network
  IPAddress generic(255, 255, 255, 255);
  tx.beginPacket(directed, STATUS_PORT);
  tx.write((const uint8_t *)body, strlen(body));
  tx.endPacket();
  tx.beginPacket(generic, STATUS_PORT);
  tx.write((const uint8_t *)body, strlen(body));
  tx.endPacket();
}

void setup() {
  // motors off before anything else
  pinMode(PIN_EN, OUTPUT);
  pinMode(PIN_IN1, OUTPUT);
  pinMode(PIN_IN2, OUTPUT);
  pinMode(PIN_IN3, OUTPUT);
  pinMode(PIN_IN4, OUTPUT);
  drive(0);

  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                 // no wifi power-saving naps = faster response
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
  rx.begin(CMD_PORT);
}

void loop() {
  // 1. command from the Pi?
  int n = rx.parsePacket();
  if (n > 0) {
    char buf[48];
    int len = rx.read(buf, sizeof(buf) - 1);
    buf[len > 0 ? len : 0] = 0;
    if (strstr(buf, "\"stop\""))      { command(0);  Serial.println("cmd: STOP"); }
    else if (strstr(buf, "\"up\""))   { command(1);  Serial.println("cmd: UP"); }
    else if (strstr(buf, "\"down\"")) { command(-1); Serial.println("cmd: DOWN"); }
  }

  // 2. safety auto-stop
  if (dir != 0 && (long)(millis() - stopAt) >= 0) {
    drive(0);
    statusDue = true;
    Serial.println("auto-stop (timer)");
  }

  // 3. start the new direction once the reverse pause is over
  if (pendingDir != 0 && (long)(millis() - pendingAt) >= 0) {
    int d = pendingDir;
    pendingDir = 0;
    drive(d);
    statusDue = true;
  }

  // 4. lost wifi: stop the motors
  if (WiFi.status() != WL_CONNECTED) {
    pendingDir = 0;
    if (dir != 0) { drive(0); statusDue = true; Serial.println("wifi lost, motors stopped"); }
  }

  // 5. tell the Pi our state (right away on change, otherwise twice a second)
  if (statusDue || millis() - lastStatus >= 500) sendStatus();

  delay(5);
}
