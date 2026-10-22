// Smart City - Arduino Nano sensor node (simple version)
// Board: Arduino Nano | Keep D0/D1 empty (USB serial)
// Sensors: Rain, Gas, Soil (analog + digital each).
// Temperature now comes from the internet (the Pi fetches it), so no temp sensor here.

// name, analog pin, digital pin, labels for the serial print
const char *NAMES[]  = {"Rain", "Gas", "Soil"};
const char *A_LBL[]  = {"A0",   "A1",  "A3"};
const char *D_LBL[]  = {"D4",   "D8",  "D10"};
const int   A_PIN[]  = {A0,     A1,    A3};
const int   D_PIN[]  = {4,      8,     10};
const int   N = 3;

// average 4 quick readings to smooth noise (about 2 ms per sensor)
int readAvg(int pin) {
  analogRead(pin); delayMicroseconds(500);   // throw away the first read after switching pins
  long sum = 0;
  for (int i = 0; i < 4; i++) { sum += analogRead(pin); delayMicroseconds(500); }
  return sum / 4;
}

// how often a full reading is sent (ms). Smaller = faster dashboard.
const unsigned long SEND_EVERY_MS = 250;
unsigned long lastSend = 0;

void setup() {
  Serial.begin(115200);   // must match BAUD_RATE in serial_dashboard.py
  for (int i = 0; i < N; i++) pinMode(D_PIN[i], INPUT);
}

void loop() {
  if (millis() - lastSend < SEND_EVERY_MS) return;   // no blocking delay, just wait for the next slot
  lastSend = millis();

  Serial.println(F("================================"));

  // Rain, Gas, Soil: analog + digital (Soil is the last line of each block)
  for (int i = 0; i < N; i++) {
    int raw = readAvg(A_PIN[i]);
    Serial.print('['); Serial.print(A_LBL[i]); Serial.print("] ");
    Serial.print(NAMES[i]);
    Serial.print(F("  raw=")); Serial.print(raw);
    Serial.print(F("  volt=")); Serial.print(raw * 5.0 / 1023.0, 2);
    Serial.print(F("V | [")); Serial.print(D_LBL[i]);
    Serial.print(F("] digital="));
    Serial.println(digitalRead(D_PIN[i]) ? F("HIGH") : F("LOW"));
  }

  Serial.println();
}
