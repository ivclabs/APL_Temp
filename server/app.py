"""APL_Temp server: receives temperatures from the ESP32, saves them and checks alarms.

Endpoints:
  POST /api/readings   ESP32 sends {"temp_c": 22.5}   (needs X-API-Key header)
  GET  /api/readings   latest readings as JSON
  GET  /api/alarms     latest alarms as JSON
  GET  /               simple web page with the latest readings and alarms
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g, jsonify, render_template_string, request

from alarms import AlarmConfig, check_rules, cooldown_over, parse_time

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

API_KEY = os.environ.get("API_KEY", "")
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "readings.db"))
ALARMS = AlarmConfig.from_env()

# DS18B20 can measure -55 to +125 °C. Anything outside is a broken reading.
MIN_VALID_C = -55
MAX_VALID_C = 125

app = Flask(__name__)


# ---------- Database ----------

def get_db():
    """Open one database connection per request."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS readings (
                   id        INTEGER PRIMARY KEY AUTOINCREMENT,
                   device    TEXT NOT NULL,
                   temp_c    REAL NOT NULL,
                   created   TEXT NOT NULL   -- UTC time, ISO format
               )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS alarms (
                   id        INTEGER PRIMARY KEY AUTOINCREMENT,
                   device    TEXT NOT NULL,
                   kind      TEXT NOT NULL,  -- high / low / rising
                   temp_c    REAL NOT NULL,
                   message   TEXT NOT NULL,
                   created   TEXT NOT NULL
               )"""
        )


# ---------- Alarms ----------

def check_alarms(db, device, temp_c, now):
    """Run the alarm rules for a new reading and save any new alarms.

    Must run BEFORE the new reading is saved, so 'history' only has older readings.
    """
    since = (now - timedelta(minutes=ALARMS.rise_window_min)).isoformat(timespec="seconds")
    history = [
        (parse_time(r["created"]), r["temp_c"])
        for r in db.execute(
            "SELECT temp_c, created FROM readings WHERE device = ? AND created >= ? ORDER BY id",
            (device, since),
        )
    ]

    new_alarms = []
    for kind, message in check_rules(temp_c, now, history, ALARMS):
        last = db.execute(
            "SELECT created FROM alarms WHERE device = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (device, kind),
        ).fetchone()
        if not cooldown_over(parse_time(last["created"]) if last else None, now, ALARMS):
            continue  # same alarm fired recently - stay quiet to avoid spam

        db.execute(
            "INSERT INTO alarms (device, kind, temp_c, message, created) VALUES (?, ?, ?, ?, ?)",
            (device, kind, temp_c, message, now.isoformat(timespec="seconds")),
        )
        new_alarms.append({"kind": kind, "message": message})
        app.logger.warning("ALARM %s: %s", kind, message)

    return new_alarms


# ---------- API ----------

@app.post("/api/readings")
def add_reading():
    # Only our ESP32 knows the key, so nobody else can send fake temperatures.
    if not API_KEY or request.headers.get("X-API-Key") != API_KEY:
        return jsonify(error="wrong or missing API key"), 401

    data = request.get_json(silent=True) or {}
    temp = data.get("temp_c")
    if not isinstance(temp, (int, float)) or isinstance(temp, bool):
        return jsonify(error="temp_c must be a number"), 400
    if not MIN_VALID_C <= temp <= MAX_VALID_C:
        return jsonify(error=f"temp_c must be between {MIN_VALID_C} and {MAX_VALID_C}"), 400

    temp = float(temp)
    device = str(data.get("device", "esp32"))[:50]
    now = datetime.now(timezone.utc)
    created = now.isoformat(timespec="seconds")

    db = get_db()
    new_alarms = check_alarms(db, device, temp, now)
    db.execute(
        "INSERT INTO readings (device, temp_c, created) VALUES (?, ?, ?)",
        (device, temp, created),
    )
    db.commit()
    return jsonify(ok=True, temp_c=temp, created=created, alarms=new_alarms), 201


@app.get("/api/readings")
def list_readings():
    limit = min(request.args.get("limit", 50, type=int), 1000)
    rows = get_db().execute(
        "SELECT device, temp_c, created FROM readings ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/alarms")
def list_alarms():
    limit = min(request.args.get("limit", 50, type=int), 1000)
    rows = get_db().execute(
        "SELECT device, kind, temp_c, message, created FROM alarms ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ---------- Web page ----------

PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <title>APL_Temp</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 600px; margin: 2rem auto; padding: 0 1rem; }
    .now { font-size: 3rem; font-weight: bold; margin: .5rem 0; }
    table { width: 100%; border-collapse: collapse; }
    td, th { text-align: left; padding: .3rem; border-bottom: 1px solid #ddd; }
    .muted { color: #777; }
    .alarm { background: #fdecea; border-left: 4px solid #d93025; padding: .5rem .75rem; margin: .4rem 0; }
    .alarm small { color: #777; display: block; }
  </style>
</head>
<body>
  <h1>APL_Temp</h1>
  {% if rows %}
    <div class="now">{{ "%.2f"|format(rows[0].temp_c) }} °C</div>
    <p class="muted">Latest reading: {{ rows[0].created }} (UTC) · page updates every 10 s</p>
  {% else %}
    <p>No readings yet. Waiting for the ESP32…</p>
  {% endif %}

  <p class="muted">Limits: low &lt; {{ cfg.low_c }} °C · high &gt; {{ cfg.high_c }} °C ·
     rising ≥ {{ cfg.rise_c_per_min }} °C/min</p>

  <h2>Alarms</h2>
  {% for a in alarms %}
    <div class="alarm"><b>{{ a.kind|upper }}</b> – {{ a.message }}<small>{{ a.created }} (UTC)</small></div>
  {% else %}
    <p class="muted">No alarms. All good.</p>
  {% endfor %}

  {% if rows %}
  <h2>Latest readings</h2>
  <table>
    <tr><th>Time (UTC)</th><th>Device</th><th>°C</th></tr>
    {% for r in rows %}
    <tr><td>{{ r.created }}</td><td>{{ r.device }}</td><td>{{ "%.2f"|format(r.temp_c) }}</td></tr>
    {% endfor %}
  </table>
  {% endif %}
</body>
</html>"""


@app.get("/")
def index():
    db = get_db()
    rows = db.execute(
        "SELECT device, temp_c, created FROM readings ORDER BY id DESC LIMIT 30"
    ).fetchall()
    alarms = db.execute(
        "SELECT kind, message, created FROM alarms ORDER BY id DESC LIMIT 10"
    ).fetchall()
    return render_template_string(PAGE, rows=rows, alarms=alarms, cfg=ALARMS)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
