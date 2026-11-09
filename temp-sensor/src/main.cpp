#include <OneWire.h>
#include <DallasTemperature.h>

#define SENSOR_PIN 7   // GPIO7 -> A3 -> column 20

OneWire oneWire(SENSOR_PIN);
DallasTemperature sensors(&oneWire);

void setup() {
  Serial.begin(115200);
  delay(2000);
  sensors.begin();
  Serial.printf("Found %d sensor(s)\n", sensors.getDeviceCount());
}

void loop() {
  sensors.requestTemperatures();
  float t = sensors.getTempCByIndex(0);
  if (t == DEVICE_DISCONNECTED_C) {
    Serial.println("Sensor not responding - check column 20");
  } else {
    Serial.printf("Temperature: %.2f C\n", t);
  }
  delay(2000);
}