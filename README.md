# APL_Temp – AI-powered temperature monitor

**Live: http://56.228.124.110**

Final project for the APL practice period. A DS18B20 temperature sensor on an
ESP32-C3 sends readings to a web server on AWS. The server checks thresholds,
sends alerts, and uses Generative AI (AWS Bedrock) to explain alerts, summarize
data, and answer questions about it.

```
ESP32 + DS18B20 ──HTTP──> nginx ──> Flask app (gunicorn) ──> SQLite      <──> Claude Haiku 4.5 (Bedrock, EU)
                          └──────── EC2, Elastic IP ───────┘                  │
                                                                              └──> Email alerts (SNS)
```

## Status
- [x] Hardware built – sensor reads correct temperatures
- [x] WiFi + send readings (send on change: within 2 s of a 0.3 °C change, otherwise every 30 s)
- [x] Alarm rules on the server: too high, too low, rising too fast (with 15 min cooldown)
- [x] Live web page (updates every 2 s, countdown to next reading)
- [x] Deployed to AWS with Terraform + Ansible
- [x] GenAI with Claude Haiku 4.5 on AWS Bedrock: explains alarms, writes a daily summary, answers questions
- [ ] Email alerts (SNS)
- [ ] Dashboard chart

## Hardware
- Olimex ESP32-C3-DevKit-Lipo
- DS18B20 waterproof probe (data on GPIO7)
- 4.7 kΩ pull-up resistor between DATA and 3.3 V

## Folders
- `temp-sensor/` – ESP32 firmware (PlatformIO, Arduino framework)
- `server/` – Flask app: API, alarm rules, web page, tests
- `infra/` – Terraform (AWS resources) and Ansible (server setup)

## Deployment (AWS)

```
terraform apply
   ├── Security group: port 80 open, SSH only from home IP
   ├── IAM role: server may only read its own API key (no stored AWS keys)
   ├── Parameter Store: API key (encrypted SecureString)
   ├── Elastic IP: fixed address for the ESP32
   └── EC2 t3.micro, Amazon Linux 2023
          └── first boot (user-data) → installs Ansible → runs infra/ansible/playbook.yml
                 ├── Python venv + app, run by gunicorn as a systemd service (own user, not root)
                 └── nginx on port 80 → app
```

- Create: `cd infra && cp terraform.tfvars.example terraform.tfvars` (fill in), then `terraform init && terraform apply`
- Update the running server to the newest code: `ssh` in and run `sudo /opt/apl-temp/infra/update.sh`
- Remove everything: `terraform destroy`

Terraform state is kept in an encrypted, versioned S3 bucket.

## AI (Claude on AWS Bedrock)
- **Alarm explanations:** when an alarm fires, Claude gets the last 30 minutes of readings and writes
  what happened and what to check. Runs in the background, so the ESP32 never waits.
- **Daily summary:** "Today, by AI" on the page, rewritten at most every 3 hours.
- **Ask the AI:** visitors ask questions; Claude answers only from the stored data.
- **Cost guard:** max 50 AI calls per day (`AI_DAILY_LIMIT`); usage and tokens at `/api/ai`.
- **No AI key on the server:** Bedrock calls are signed with the EC2 instance's IAM role.
- If AI is off or fails, the app keeps working with the plain alarm messages.

## Tests
```
cd server && python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/pytest -q
```

## Secrets
Nothing secret is in git:
- ESP32: WiFi password, server URL and API key in `temp-sensor/include/secrets.h` (copy `secrets.h.example`)
- Server (local): `server/.env` (copy `.env.example`)
- Terraform: `infra/terraform.tfvars` (copy `terraform.tfvars.example`)
- On AWS the API key lives in Parameter Store and is read by the server's IAM role
