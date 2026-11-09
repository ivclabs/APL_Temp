# APL_Temp – AI-powered temperature monitor

Final project for the APL practice period. A DS18B20 temperature sensor on an
ESP32-C3 sends readings to a web server on AWS. The server checks thresholds,
sends alerts, and uses Generative AI (AWS Bedrock) to explain alerts, summarize
data, and answer questions about it.

```
ESP32 + DS18B20 ──HTTP──> EC2 web server (Flask + DB, threshold rules) <──> GenAI (Bedrock)
                                   │
                                   ├──> Dashboard (public IP)
                                   └──> Email alerts (SNS)
```

## Status
- [x] Hardware built – sensor reads correct temperatures
- [ ] Alarm logic on the ESP32
- [ ] WiFi + send readings to AWS
- [ ] GenAI layer (Bedrock)
- [ ] Dashboard + email alerts

## Hardware
- Olimex ESP32-C3-DevKit-Lipo
- DS18B20 waterproof probe (data on GPIO7)
- 4.7 kΩ pull-up resistor between DATA and 3.3 V

## Folders
- `temp-sensor/` – ESP32 firmware (PlatformIO, Arduino framework)

## Secrets
WiFi passwords and keys go in `temp-sensor/include/secrets.h`, which is ignored
by git. Copy `secrets.h.example` to get started.
