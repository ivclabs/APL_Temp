// APL_Temp firmware: read the DS18B20 and send the temperature to the server.
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <OneWire.h>
#include <DallasTemperature.h>

#include "secrets.h"   // WIFI_SSID, WIFI_PASSWORD, SERVER_URL, API_KEY (not in git)

#define SENSOR_PIN 7                 // GPIO7 -> A3 -> column 20
#define DEVICE_NAME "esp32-c3"
// "Send on change": read often, but only send when something is worth sending.
const unsigned long READ_EVERY_MS = 2000;    // read the sensor every 2 s
const unsigned long HEARTBEAT_MS  = 30000;   // send at least every 30 s, even if nothing changed
const float SEND_IF_CHANGED_C     = 0.3;     // send right away if temp moved this much (sensor noise is ~0.06)

float lastSentTemp = 0;
unsigned long lastSentAt = 0;
bool sentOnce = false;

OneWire oneWire(SENSOR_PIN);
DallasTemperature sensors(&oneWire);

// Connect to WiFi. Gives up after 20 s so the loop can try again later.
bool connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return true;

  Serial.printf("Connecting to WiFi \"%s\"", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("WiFi OK, my IP: %s\n", WiFi.localIP().toString().c_str());
    return true;
  }
  Serial.println("WiFi failed - check name/password and that it is 2.4 GHz");
  return false;
}

// Send one reading as JSON. Returns the HTTP status code (201 = saved).
int sendReading(float tempC) {
  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", API_KEY);
  http.setTimeout(5000);

  char body[80];
  snprintf(body, sizeof(body), "{\"temp_c\": %.2f, \"device\": \"%s\"}", tempC, DEVICE_NAME);

  int code = http.POST(body);
  http.end();
  return code;
}

void setup() {
  Serial.begin(115200);
  delay(2000);
  sensors.begin();
  Serial.printf("Found %d sensor(s)\n", sensors.getDeviceCount());

  WiFi.mode(WIFI_STA);
  connectWiFi();
}

// Fixed schedule: read at 0 s, 2 s, 4 s... no matter how long reading/sending takes.
unsigned long nextReadAt = 0;

void loop() {
  unsigned long now = millis();
  if ((long)(now - nextReadAt) < 0) {
    delay(20);        // not time yet - check again soon
    return;
  }
  nextReadAt += READ_EVERY_MS;
  if ((long)(now - nextReadAt) >= 0) {
    nextReadAt = now + READ_EVERY_MS;   // fell far behind (e.g. slow WiFi) - restart the schedule
  }

  sensors.requestTemperatures();
  float t = sensors.getTempCByIndex(0);

  if (t == DEVICE_DISCONNECTED_C) {
    Serial.println("Sensor not responding - check column 20");
    return;
  }
  if (t == 85.0) {
    // 85.0 is the DS18B20's power-on value, not a real reading.
    Serial.println("Got 85.0 (sensor start-up value) - skipping");
    return;
  }

  // Decide if this reading is worth sending.
  bool changed = sentOnce && fabs(t - lastSentTemp) >= SEND_IF_CHANGED_C;
  bool heartbeatDue = !sentOnce || now - lastSentAt >= HEARTBEAT_MS;
  if (!changed && !heartbeatDue) return;   // nothing new - stay quiet

  const char *why = changed ? "changed" : "heartbeat";

  if (!connectWiFi()) {
    Serial.printf("Temperature: %.2f C (not sent, no WiFi)\n", t);
  } else {
    int code = sendReading(t);
    if (code == 201) {
      lastSentTemp = t;
      lastSentAt = now;
      sentOnce = true;
      Serial.printf("Temperature: %.2f C -> sent OK (%s)\n", t, why);
    } else if (code == 401) {
      Serial.printf("Temperature: %.2f C -> server says wrong API key\n", t);
    } else if (code < 0) {
      Serial.printf("Temperature: %.2f C -> can't reach server (%s)\n",
                    t, HTTPClient::errorToString(code).c_str());
    } else {
      Serial.printf("Temperature: %.2f C -> server answered %d\n", t, code);
    }
  }
}
