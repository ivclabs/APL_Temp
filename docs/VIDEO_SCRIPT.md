# Video script (about 5 minutes, English)

Tip: record the screen with OBS (or a similar tool) and film the hardware with your phone.
Read the text calmly. It doesn't have to be word for word.

Before recording:
- The ESP32 is plugged in and the dashboard shows "Live"
- Open tabs: the dashboard, the GitHub repo, `docs/DOCUMENTATION.md`, an email from APL_Temp
- Have a cup of warm water (or your hand) ready to trigger an alarm
- If you film the final freezer test: show the probe cable going through the door seal and the
  dashboard showing about −18 °C, then open the door for a minute and watch the temperature rise

---

## 0:00 – 0:30 Introduction
**Show:** the dashboard on screen.

> "Hi, this is APL_Temp, my final project. It is a temperature monitor for a freezer.
> A sensor sends the temperature to a server on AWS, and Generative AI explains alarms in plain
> language and sends them by email. The site is live at 56.228.124.110."

## 0:30 – 1:15 Hardware
**Show:** the ESP32, the probe and the breadboard (phone camera).

> "The hardware is an Olimex ESP32-C3 with WiFi and a DS18B20 waterproof probe. The probe is
> digital and accurate to half a degree. It needs a 4.7 kilo-ohm pull-up resistor on the data line.
> The ESP32 reads the sensor every 2 seconds. It sends right away if the temperature changes by
> 0.3 degrees, otherwise every 30 seconds, so the server knows it is alive."

## 1:15 – 2:00 Architecture and AWS
**Show:** the diagram in DOCUMENTATION.md (section 2.1), then `infra/main.tf`.

> "The ESP32 sends the readings over HTTP with a secret API key. On AWS, nginx and a Python Flask
> app run on an EC2 server, and the data is stored in SQLite.
> Everything is built with Terraform: the server, the firewall, a fixed IP, the email topic and
> the permissions. On the first boot, Ansible installs the app automatically.
> There are no passwords or AI keys in the code. The server uses an IAM role that may only do
> what it needs."

## 2:00 – 3:30 Live demo (the most important part)
**Show:** the dashboard. Put the probe in warm water or hold it.

> "For the demo I use test limits: high 28 degrees. In the freezer the limits are minus 12 and minus 30.
> Now I warm the probe. Watch the number: it updates within two seconds…
> The chart shows the spike, the red line is the alarm limit, and here is the alarm."

Wait for the 🤖 text to appear (a few seconds).

> "This is the Generative AI part. Claude, running on AWS Bedrock, got the last 30 minutes of
> readings and explains what happened, the most likely cause and what to check first.
> The AI is focused on the freezer: it knows that a freezer should be minus 18 or colder, door openings,
> cooling failure and power cuts, and it can mention tampering with the cooling controller,
> but only when normal causes don't explain the pattern."

**Show:** the email in your inbox.

> "At the same time I get an email with the alarm and the same AI explanation, plus a link to the dashboard."

## 3:30 – 4:15 More AI: summary and questions
**Show:** "Today, by AI", then type in "Ask the AI":
1. `Was the freezer temperature safe today?`
2. `Write me a poem about football` → it refuses

> "The AI also writes a daily summary and answers questions, using only the real data.
> If I ask about something else, it refuses, because the question box is public and must not be misused.
> There is also a daily limit on AI calls, to keep the costs under control."

## 4:15 – 4:45 Code, tests and GitHub
**Show:** the GitHub commit list, then run the tests in a terminal (`venv/bin/pytest -q` → 30 passed).

> "The code is on GitHub. I built it in eight steps with one commit per feature.
> There are 30 automated tests. They use a fake AI and a fake email service, so they are free and fast."

## 4:45 – 5:15 Problems and conclusion
**Show:** the Problems and solutions table in DOCUMENTATION.md.

> "I ran into some problems, for example: the newest Claude model wasn't available for my AWS
> account, so I used Haiku 4.5; two server workers crashed on start-up; and at first the AI blamed
> the sensor when I touched it. Giving the AI knowledge about freezers fixed that.
> What I learned: build in small steps, use infrastructure as code, and give the AI clear context
> and limits. Thank you for watching!"
