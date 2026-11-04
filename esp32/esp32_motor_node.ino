// ESP32 motor safety node - wired to Raspberry Pi over USB serial (UART0, GPIO1/3).
// Pin assignments are IDENTICAL to the original sketch. Wi-Fi/HTTP removed.
// Flash one ESP32 per motor and give each a UNIQUE MOTOR_ID.
#include <ArduinoJson.h>
#include <DHT.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <Preferences.h>

// ================= PI LINK =================
const char* MOTOR_ID = "motor_1";   // <-- CHANGE PER ESP32: motor_1, motor_2, motor_3 ...
// Protocol (one JSON per line, 115200 baud):
//   ESP -> Pi : @T{telemetry}   every 2.5 s, and right after any command
//   Pi -> ESP : @C{"cmd":"shutdown"|"resume"|"hello"}  and/or  @C{"cfg":{...thresholds...}}
// Any other serial text (debug prints) is ignored by the Pi.

// ================= BUTTONS (GND-wired) =================
#define BTN_MENU 32
#define BTN_UP   16
#define BTN_DOWN 17
// ================= SENSOR INPUTS =================
#define IR_SENSOR_PIN 36
const bool IR_DETECTED_LOW = true;
// ================= LCD =================
#define LCD_SDA 21
#define LCD_SCL 22
LiquidCrystal_I2C lcd(0x27, 16, 2);   // try 0x3F if blank
// ================= SENSOR / OUTPUT PINS =================
#define DHT_PIN       4
#define DHT_TYPE      DHT11
#define VIBRATION_PIN 5
#define SMOKE_PIN     34
#define SOUND_PIN     35
#define PIR_PIN       27
#define RELAY_PIN     26
#define BUZZER_PIN    25
#define LED_GREEN     14
#define LED_YELLOW    12
#define LED_RED       13
DHT dht(DHT_PIN, DHT_TYPE);

const bool RELAY_ACTIVE_LOW = true;
const bool PIR_ACTIVE_HIGH = true;

// ---- RELAY WIRING (choose ONE, then wire the motor accordingly) ----
// false = motor on the relay's NC contact. Coil is energised ONLY to cut power. (matches your current wiring)
// true  = FAIL-SAFE: motor on the relay's NO contact. Coil is energised while the motor may run,
//         so power is also cut if the ESP32 loses power or crashes.
const bool RELAY_FAILSAFE = false;
const unsigned long RELAY_MIN_OFF_MS = 5000;    // after a power cut, Resume is refused for this long (no relay chatter)

// ---- SHUTDOWN BEHAVIOUR ----
const unsigned long WARMUP_MS = 20000;          // after boot: sensors settle (gas sensors read high while heating); only the cover interlock acts
const unsigned long HAZARD_HOLD_MS = 5000;      // 3+ alarms must persist this long before auto-shutdown (use 60000 for 1 minute)
const unsigned long COVER_DEBOUNCE_MS = 400;    // cover signal must be stable this long
const float TEMP_OFFSET_C = 0.0;                // calibration of the DHT sensor (added to readings)
const float HUM_OFFSET = 0.0;

Preferences safetyPrefs;
bool thresholdEditMode = false;
int selectedThreshold = 0; // 0=temp 1=humidity 2=smoke 3=sound 4=vibration
unsigned long menuPressStarted = 0;
bool menuLongPressHandled = false;
const unsigned long MENU_LONG_PRESS_MS = 1200;

// ---- prediction history ----
const unsigned long HISTORY_INTERVAL = 10000;
const int PREDICTION_WINDOW = 6;
unsigned long lastHistoryRecord = 0;
struct HistorySample { float temperature; float humidity; int smoke; int sound; int vibration; bool dhtValid; };
HistorySample history[PREDICTION_WINDOW];
int historyCount = 0, historyWriteIndex = 0;
bool predictedHazard = false;
String predictionDetails = "Insufficient history";

// ---- state / thresholds ----
bool shutdownActive = false, autoShutdown = true, vibrationTrip = true, motionTrip = false;
float temperatureMax = 60.0, humidityMax = 85.0;
int smokeMax = 4000, soundMax = 4000, vibrationMax = 10;
float temperatureC = 0, humidity = 0;
int smokeValue = 0, soundValue = 0;
bool dhtValid = false, vibrationDetected = false, motionDetected = false, irObjectDetected = false;
volatile unsigned long vibrationEdgeCount = 0;
unsigned long lastVibrationRateUpdate = 0;
int vibrationRate = 0;
String shutdownReason = "Normal";
String systemStatus = "SAFE";
bool coverClosed = false, coverRaw = false;     // IR sensor = motor cover detector: object seen = cover in place
unsigned long coverRawSince = 0, hazardSince = 0, lastTripAt = 0;
bool relayCut = false;

unsigned long lastSensorRead = 0, lastLCDUpdate = 0;
const unsigned long SENSOR_INTERVAL = 2500;   // also the telemetry interval
const unsigned long LCD_INTERVAL = 1000;

void evaluateSafety();
void sendTelemetry();

// ================= LCD =================
void printLCDLine(byte row, String m) {
  if (m.length() > 16) m = m.substring(0, 16);
  while (m.length() < 16) m += ' ';
  lcd.setCursor(0, row); lcd.print(m);
}
void showLCDMessage(String a, String b) { printLCDLine(0, a); printLCDLine(1, b); }

void updateLCD() {
  static byte screen = 0;
  static unsigned long lastScreenChange = 0;
  if (millis() - lastScreenChange >= 3000) { screen = (screen + 1) % 5; lastScreenChange = millis(); }
  if (shutdownActive) { showLCDMessage("!! SHUTDOWN !!", shutdownReason); return; }
  if (thresholdEditMode) {
    String name, value;
    switch (selectedThreshold) {
      case 0: name = "Temp limit";    value = String(temperatureMax, 1) + " C"; break;
      case 1: name = "Humidity max";  value = String(humidityMax, 1) + " %"; break;
      case 2: name = "Smoke max";     value = String(smokeMax); break;
      case 3: name = "Sound max";     value = String(soundMax); break;
      default: name = "Vib max/sec";  value = String(vibrationMax); break;
    }
    showLCDMessage(name, value + " +/- MENU");
    return;
  }
  switch (screen) {
    case 0:
      if (dhtValid) showLCDMessage("Temp:" + String(temperatureC, 1) + " C", "Humidity:" + String(humidity, 1) + "%");
      else showLCDMessage("DHT11 ERROR", "Check sensor wire");
      break;
    case 1: showLCDMessage("Smoke ADC:" + String(smokeValue), "Sound ADC:" + String(soundValue)); break;
    case 2: showLCDMessage(String("Vib rate:") + String(vibrationRate) + "/s", String("Limit:") + String(vibrationMax) + " /s"); break;
    case 3: showLCDMessage("Status: " + systemStatus, "Relay: " + String(shutdownActive ? "TRIPPED" : "NORMAL")); break;
    case 4:
      showLCDMessage(coverClosed ? "COVER: CLOSED" : "COVER: OPEN!",
                     coverClosed ? "Motor may run" : "Motor locked out");
      break;
  }
}

// ================= PERSISTENT THRESHOLDS =================
void saveThresholdsToNVS() {
  safetyPrefs.putFloat("tempMax", temperatureMax);
  safetyPrefs.putFloat("humidMax", humidityMax);
  safetyPrefs.putInt("smokeMax", smokeMax);
  safetyPrefs.putInt("soundMax", soundMax);
  safetyPrefs.putInt("vibMax", vibrationMax);
}
void loadThresholdsFromNVS() {
  safetyPrefs.begin("safety", false);
  temperatureMax = safetyPrefs.getFloat("tempMax", 60.0f);
  humidityMax = safetyPrefs.getFloat("humidMax", 85.0f);
  smokeMax = safetyPrefs.getInt("smokeMax", 1800);
  soundMax = safetyPrefs.getInt("soundMax", 2200);
  vibrationMax = safetyPrefs.getInt("vibMax", 10);
  temperatureMax = constrain(temperatureMax, 0.0f, 80.0f);
  humidityMax = constrain(humidityMax, 0.0f, 100.0f);
  smokeMax = constrain(smokeMax, 0, 4095);
  soundMax = constrain(soundMax, 0, 4095);
  vibrationMax = constrain(vibrationMax, 1, 1000);
  saveThresholdsToNVS();
}
void adjustSelectedThreshold(int d) {
  switch (selectedThreshold) {
    case 0: temperatureMax = constrain(temperatureMax + d * 1.0f, 0.0f, 80.0f); break;
    case 1: humidityMax = constrain(humidityMax + d * 1.0f, 0.0f, 100.0f); break;
    case 2: smokeMax = constrain(smokeMax + d * 50, 0, 4095); break;
    case 3: soundMax = constrain(soundMax + d * 50, 0, 4095); break;
    default: vibrationMax = constrain(vibrationMax + d, 1, 1000); break;
  }
  saveThresholdsToNVS();   // Pi sees the new values in the next telemetry line
  evaluateSafety();
}

// ================= BUTTONS =================
void handleThresholdButtons() {
  const unsigned long DEBOUNCE_MS = 35;
  static bool menuStable = HIGH, upStable = HIGH, downStable = HIGH;
  static bool menuRawLast = HIGH, upRawLast = HIGH, downRawLast = HIGH;
  static unsigned long menuAt = 0, upAt = 0, downAt = 0;
  bool menuRaw = digitalRead(BTN_MENU), upRaw = digitalRead(BTN_UP), downRaw = digitalRead(BTN_DOWN);
  unsigned long now = millis();
  if (menuRaw != menuRawLast) { menuRawLast = menuRaw; menuAt = now; }
  if (upRaw != upRawLast) { upRawLast = upRaw; upAt = now; }
  if (downRaw != downRawLast) { downRawLast = downRaw; downAt = now; }
  bool menuPressed = false, menuReleased = false, upPressed = false, downPressed = false;
  if (menuRaw != menuStable && now - menuAt >= DEBOUNCE_MS) { menuStable = menuRaw; menuPressed = (menuStable == LOW); menuReleased = (menuStable == HIGH); }
  if (upRaw != upStable && now - upAt >= DEBOUNCE_MS) { upStable = upRaw; upPressed = (upStable == LOW); }
  if (downRaw != downStable && now - downAt >= DEBOUNCE_MS) { downStable = downRaw; downPressed = (downStable == LOW); }

  if (menuPressed) { menuPressStarted = now; menuLongPressHandled = false; }
  if (menuStable == LOW && !menuLongPressHandled && menuPressStarted != 0 && now - menuPressStarted >= MENU_LONG_PRESS_MS) {
    thresholdEditMode = false; menuLongPressHandled = true;
    showLCDMessage("Thresholds saved", "Long MENU=exit");
  }
  if (menuReleased && !menuLongPressHandled) {
    if (!thresholdEditMode) { thresholdEditMode = true; selectedThreshold = 0; }
    else selectedThreshold = (selectedThreshold + 1) % 5;
  }
  if (thresholdEditMode && upPressed) adjustSelectedThreshold(+1);
  if (thresholdEditMode && downPressed) adjustSelectedThreshold(-1);
}

// ================= OUTPUTS =================
// cutPower=true  -> motor has NO power.   cutPower=false -> motor is allowed to run.
void setRelay(bool cutPower) {
  bool coil = RELAY_FAILSAFE ? !cutPower : cutPower;     // energise the coil?
  bool pinHigh = RELAY_ACTIVE_LOW ? !coil : coil;        // pin level that does it
  digitalWrite(RELAY_PIN, pinHigh ? HIGH : LOW);
  if (cutPower && !relayCut) lastTripAt = millis();
  relayCut = cutPower;
}
void applyOutputs() {
  setRelay(shutdownActive);
  digitalWrite(BUZZER_PIN, shutdownActive ? HIGH : LOW);
  bool redOn = shutdownActive || systemStatus == "DANGER" || systemStatus == "SHUTDOWN";
  bool yellowOn = !redOn && systemStatus == "WARNING";
  bool greenOn = !redOn && !yellowOn && systemStatus == "SAFE";
  digitalWrite(LED_RED, redOn ? HIGH : LOW);
  digitalWrite(LED_YELLOW, yellowOn ? HIGH : LOW);
  digitalWrite(LED_GREEN, greenOn ? HIGH : LOW);
}

// ================= SENSORS =================
void IRAM_ATTR vibrationEdgeISR() { vibrationEdgeCount++; }

void readSensors() {
  float t = dht.readTemperature(), h = dht.readHumidity();
  dhtValid = !isnan(t) && !isnan(h) && t > -40.0f && t < 80.0f && h >= 0.0f && h <= 100.0f;   // out-of-range = bad reading
  if (dhtValid) { temperatureC = t + TEMP_OFFSET_C; humidity = constrain(h + HUM_OFFSET, 0.0f, 100.0f); }
  smokeValue = analogRead(SMOKE_PIN);
  soundValue = analogRead(SOUND_PIN);
  unsigned long now = millis();
  if (now - lastVibrationRateUpdate >= 1000) {
    noInterrupts(); unsigned long edges = vibrationEdgeCount; vibrationEdgeCount = 0; interrupts();
    unsigned long elapsedMs = now - lastVibrationRateUpdate;
    edges = min(edges, 100000UL);
    vibrationRate = elapsedMs > 0 ? (int)((edges * 1000UL) / elapsedMs) : 0;
    lastVibrationRateUpdate = now;
  }
  vibrationDetected = (vibrationRate >= vibrationMax);
  int pirState = digitalRead(PIR_PIN);
  irObjectDetected = coverClosed;                        // debounced cover state (see checkCover)
  motionDetected = PIR_ACTIVE_HIGH ? (pirState == HIGH) : (pirState == LOW);
}

// ================= SAFETY (unchanged logic - runs fully on the ESP32, even if the Pi is gone) =================
const float TEMPERATURE_WARNING_DELTA_C = 10.0;
const float HUMIDITY_WARNING_DELTA = 5.0;
const int SMOKE_WARNING_DELTA = 300;
const int SOUND_WARNING_DELTA = 300;

bool tempDanger() { return dhtValid && temperatureC >= temperatureMax; }
bool humidityDanger() { return dhtValid && humidity >= humidityMax; }
bool smokeDanger() { return smokeValue >= smokeMax; }
bool soundDanger() { return soundValue >= soundMax; }
bool vibrationDanger() { return vibrationTrip && vibrationDetected; }
bool motionDanger() { return motionTrip && motionDetected; }

int countActiveHazardSensors() {
  int c = 0;
  if (tempDanger()) c++;
  if (humidityDanger()) c++;
  if (smokeDanger()) c++;
  if (soundDanger()) c++;
  if (vibrationDanger()) c++;
  if (motionDanger()) c++;
  return c;
}
String getHazardReason() {
  String r = "";
  if (tempDanger()) r += "Temp ";
  if (humidityDanger()) r += "Humidity ";
  if (smokeDanger()) r += "Smoke ";
  if (soundDanger()) r += "Sound ";
  if (vibrationDanger()) r += "Vibration ";
  if (motionDanger()) r += "Motion ";
  r.trim();
  return r.length() == 0 ? "Normal" : r;
}
bool warningPresent() {
  if (predictedHazard) return true;
  if (tempDanger() || (dhtValid && temperatureC >= temperatureMax - TEMPERATURE_WARNING_DELTA_C)) return true;
  if (humidityDanger() || (dhtValid && humidity >= humidityMax - HUMIDITY_WARNING_DELTA)) return true;
  if (smokeDanger() || smokeValue >= smokeMax - SMOKE_WARNING_DELTA) return true;
  if (soundDanger() || soundValue >= soundMax - SOUND_WARNING_DELTA) return true;
  if (vibrationDanger() || motionDanger()) return true;
  return false;
}
void evaluateSafety() {
  unsigned long now = millis();
  if (countActiveHazardSensors() >= 3 && now >= WARMUP_MS) { if (hazardSince == 0) hazardSince = now; }
  else hazardSince = 0;
  bool hazardConfirmed = (hazardSince != 0) && (now - hazardSince >= HAZARD_HOLD_MS);

  if (!coverClosed) {                    // INTERLOCK: the motor may never run with the cover off (immediate, always active)
    shutdownActive = true;
    shutdownReason = "Cover open";
  } else {
    if (shutdownActive && shutdownReason == "Cover open") shutdownReason = "Cover ok:Resume";
    if (hazardConfirmed) {
      shutdownReason = getHazardReason();
      if (autoShutdown) shutdownActive = true;
    }
  }
  if (shutdownActive) systemStatus = "SHUTDOWN";     // latched until a valid Resume
  else if (hazardConfirmed) systemStatus = "DANGER";
  else {
    bool w = warningPresent();
    shutdownReason = w ? "Sensor warning" : "Normal";
    systemStatus = w ? "WARNING" : "SAFE";
  }
  applyOutputs();
}

// Cover detector, checked every loop for a fast trip. Pin unchanged (IR_SENSOR_PIN).
void checkCover() {
  int v = digitalRead(IR_SENSOR_PIN);
  bool raw = IR_DETECTED_LOW ? (v == LOW) : (v == HIGH);       // true = cover in place
  unsigned long now = millis();
  if (raw != coverRaw) { coverRaw = raw; coverRawSince = now; }
  if (coverRaw != coverClosed && now - coverRawSince >= COVER_DEBOUNCE_MS) {
    coverClosed = coverRaw;
    irObjectDetected = coverClosed;
    evaluateSafety();
    sendTelemetry();
  }
}

// ================= HISTORY / PREDICTION (unchanged) =================
void addHistorySample() {
  HistorySample s;
  s.temperature = temperatureC; s.humidity = humidity; s.smoke = smokeValue;
  s.sound = soundValue; s.vibration = vibrationRate; s.dhtValid = dhtValid;
  history[historyWriteIndex] = s;
  historyWriteIndex = (historyWriteIndex + 1) % PREDICTION_WINDOW;
  if (historyCount < PREDICTION_WINDOW) historyCount++;
}
bool predictCrossing(float current, float threshold, float oldest, bool valid, float &secondsToCross) {
  if (!valid || historyCount < PREDICTION_WINDOW || current >= threshold) return false;
  float slope = (current - oldest) / ((PREDICTION_WINDOW - 1) * 10.0f);
  if (slope <= 0.0001f) return false;
  secondsToCross = (threshold - current) / slope;
  return secondsToCross > 0 && secondsToCross <= 60.0f;
}
void updatePrediction() {
  predictedHazard = false;
  predictionDetails = "No near-term crossing predicted";
  if (historyCount < PREDICTION_WINDOW) {
    predictionDetails = "Collecting history " + String(historyCount) + "/" + String(PREDICTION_WINDOW);
    return;
  }
  HistorySample o = history[historyWriteIndex];
  float sec = 0; String d = "";
  if (predictCrossing(temperatureC, temperatureMax, o.temperature, dhtValid && o.dhtValid, sec)) { predictedHazard = true; d += "Temperature may reach limit in ~" + String((int)sec) + "s; "; }
  if (predictCrossing(humidity, humidityMax, o.humidity, dhtValid && o.dhtValid, sec)) { predictedHazard = true; d += "Humidity may reach limit in ~" + String((int)sec) + "s; "; }
  if (predictCrossing((float)smokeValue, (float)smokeMax, (float)o.smoke, true, sec)) { predictedHazard = true; d += "Smoke may reach limit in ~" + String((int)sec) + "s; "; }
  if (predictCrossing((float)soundValue, (float)soundMax, (float)o.sound, true, sec)) { predictedHazard = true; d += "Sound may reach limit in ~" + String((int)sec) + "s; "; }
  if (predictCrossing((float)vibrationRate, (float)vibrationMax, (float)o.vibration, true, sec)) { predictedHazard = true; d += "Vibration activity may reach limit in ~" + String((int)sec) + "s; "; }
  if (predictedHazard) { d.trim(); predictionDetails = d; }
}

// ================= PI LINK =================
void sendTelemetry() {
  DynamicJsonDocument doc(1280);
  doc["id"] = MOTOR_ID;
  doc["uptime_ms"] = millis();
  doc["temperature"] = dhtValid ? temperatureC : 0.0;
  doc["humidity"] = dhtValid ? humidity : 0.0;
  doc["dht_valid"] = dhtValid;
  doc["smoke"] = smokeValue;
  doc["sound"] = soundValue;
  doc["vibration"] = vibrationRate;
  doc["vibration_detected"] = vibrationDetected;
  doc["motion"] = motionDetected;
  doc["ir_object"] = irObjectDetected;
  doc["cover_closed"] = coverClosed;
  doc["relay_cut"] = relayCut;
  doc["warming_up"] = millis() < WARMUP_MS;
  doc["status"] = systemStatus;
  doc["shutdown"] = shutdownActive;
  doc["shutdown_reason"] = shutdownReason;
  doc["relay_normal"] = !shutdownActive;
  doc["active_sensors"] = countActiveHazardSensors();
  doc["predicted_hazard"] = predictedHazard;
  doc["prediction_details"] = predictionDetails;
  doc["temperature_max"] = temperatureMax;
  doc["humidity_max"] = humidityMax;
  doc["smoke_max"] = smokeMax;
  doc["sound_max"] = soundMax;
  doc["vibration_max"] = vibrationMax;
  doc["vibration_trip"] = vibrationTrip;
  doc["motion_trip"] = motionTrip;
  doc["auto_shutdown"] = autoShutdown;
  Serial.print("@T");
  serializeJson(doc, Serial);
  Serial.println();
}

void sendEvent(const char* msg) {
  DynamicJsonDocument doc(256);
  doc["id"] = MOTOR_ID;
  doc["msg"] = msg;
  Serial.print("@E");
  serializeJson(doc, Serial);
  Serial.println();
}

void processCommand(const char* json) {
  DynamicJsonDocument doc(768);
  if (deserializeJson(doc, json)) return;
  String cmd = doc["cmd"] | "";
  cmd.toLowerCase();
  if (cmd == "shutdown") {
    shutdownActive = true; shutdownReason = "Manual shutdown"; systemStatus = "SHUTDOWN";
    applyOutputs();
    sendEvent("Manual shutdown executed");
  } else if (cmd == "resume" || cmd == "reset") {
    if (!coverClosed) {
      sendEvent("Resume rejected: cover is open");
    } else if (millis() - lastTripAt < RELAY_MIN_OFF_MS) {
      sendEvent("Resume rejected: wait a few seconds after a shutdown");
    } else if (countActiveHazardSensors() >= 3) {
      shutdownActive = true; shutdownReason = getHazardReason();
      evaluateSafety();
      sendEvent("Resume rejected: hazard still present");
    } else {
      shutdownActive = false; shutdownReason = "Normal"; hazardSince = 0;
      evaluateSafety();
      sendEvent("Resume accepted");
    }
  }
  if (doc.containsKey("cfg")) {
    JsonObject c = doc["cfg"];
    if (c.containsKey("temperature_max")) temperatureMax = constrain(c["temperature_max"].as<float>(), 0.0f, 80.0f);
    if (c.containsKey("humidity_max"))    humidityMax    = constrain(c["humidity_max"].as<float>(), 0.0f, 100.0f);
    if (c.containsKey("smoke_max"))       smokeMax       = constrain(c["smoke_max"].as<int>(), 0, 4095);
    if (c.containsKey("sound_max"))       soundMax       = constrain(c["sound_max"].as<int>(), 0, 4095);
    if (c.containsKey("vibration_max"))   vibrationMax   = constrain(c["vibration_max"].as<int>(), 1, 1000);
    if (c.containsKey("vibration_trip"))  vibrationTrip  = c["vibration_trip"].as<bool>();
    if (c.containsKey("motion_trip"))     motionTrip     = c["motion_trip"].as<bool>();
    if (c.containsKey("auto_shutdown"))   autoShutdown   = c["auto_shutdown"].as<bool>();
    saveThresholdsToNVS();
    evaluateSafety();
  }
  sendTelemetry();   // immediate acknowledgement (also answers "hello")
}

void handleSerialLink() {
  static char buf[640];
  static size_t n = 0;
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n') {
      buf[n] = 0;
      if (n > 2 && buf[0] == '@' && buf[1] == 'C') processCommand(buf + 2);
      n = 0;
    } else if (ch != '\r') {
      if (n < sizeof(buf) - 1) buf[n++] = ch; else n = 0;
    }
  }
}

// ================= SETUP / LOOP =================
void setup() {
  Serial.begin(115200);
  delay(300);
  analogReadResolution(12);
  pinMode(VIBRATION_PIN, INPUT);
  attachInterrupt(digitalPinToInterrupt(VIBRATION_PIN), vibrationEdgeISR, CHANGE);
  lastVibrationRateUpdate = millis();
  pinMode(PIR_PIN, INPUT);
  pinMode(IR_SENSOR_PIN, INPUT);
  { int v = digitalRead(IR_SENSOR_PIN); coverRaw = coverClosed = IR_DETECTED_LOW ? (v == LOW) : (v == HIGH); coverRawSince = millis(); }
  pinMode(BTN_MENU, INPUT_PULLUP);
  pinMode(BTN_UP, INPUT_PULLUP);
  pinMode(BTN_DOWN, INPUT_PULLUP);
  loadThresholdsFromNVS();
  pinMode(RELAY_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(LED_GREEN, OUTPUT);
  pinMode(LED_YELLOW, OUTPUT);
  pinMode(LED_RED, OUTPUT);
  shutdownActive = false;
  setRelay(true);      // power stays CUT until the first safety evaluation below allows the motor
  digitalWrite(BUZZER_PIN, LOW);
  digitalWrite(LED_GREEN, HIGH);
  digitalWrite(LED_YELLOW, LOW);
  digitalWrite(LED_RED, LOW);
  dht.begin();
  Wire.begin(LCD_SDA, LCD_SCL);
  lcd.init();
  lcd.backlight();
  showLCDMessage("Motor Safety", MOTOR_ID);
  delay(1500);
  readSensors();
  evaluateSafety();
  updateLCD();
  sendTelemetry();
}

void loop() {
  unsigned long now = millis();
  handleThresholdButtons();
  handleSerialLink();
  checkCover();
  static unsigned long lastEval = 0;
  static bool lastShut = false;
  if (now - lastEval >= 250) { lastEval = now; evaluateSafety(); }
  if (shutdownActive != lastShut) { lastShut = shutdownActive; sendTelemetry(); }

  if (now - lastSensorRead >= SENSOR_INTERVAL) {
    lastSensorRead = now;
    readSensors();
    evaluateSafety();
    sendTelemetry();
  }
  if (now - lastHistoryRecord >= HISTORY_INTERVAL) {
    lastHistoryRecord = now;
    addHistorySample();
    updatePrediction();
    if (!shutdownActive && countActiveHazardSensors() < 3) systemStatus = warningPresent() ? "WARNING" : "SAFE";
    applyOutputs();
  }
  if (now - lastLCDUpdate >= LCD_INTERVAL) {
    lastLCDUpdate = now;
    updateLCD();
  }
  delay(10);
}
