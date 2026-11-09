"""APL_Temp server: receives temperatures from the ESP32 and saves them.

Endpoints:
  POST /api/readings   ESP32 sends {"temp_c": 22.5}   (needs X-API-Key header)
  GET  /api/readings   latest readings as JSON
  GET  /               simple web page with the latest readings
"""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g, jsonify, render_template_string, request

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

API_KEY = os.environ.get("API_KEY", "")
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "readings.db"))

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

    device = str(data.get("device", "esp32"))[:50]
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")

    db = get_db()
    db.execute(
        "INSERT INTO readings (device, temp_c, created) VALUES (?, ?, ?)",
        (device, float(temp), created),
    )
    db.commit()
    return jsonify(ok=True, temp_c=temp, created=created), 201


@app.get("/api/readings")
def list_readings():
    limit = min(request.args.get("limit", 50, type=int), 1000)
    rows = get_db().execute(
        "SELECT device, temp_c, created FROM readings ORDER BY id DESC LIMIT ?",
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
  </style>
</head>
<body>
  <h1>APL_Temp</h1>
  {% if rows %}
    <div class="now">{{ "%.2f"|format(rows[0].temp_c) }} °C</div>
    <p class="muted">Latest reading: {{ rows[0].created }} (UTC) · page updates every 10 s</p>
    <table>
      <tr><th>Time (UTC)</th><th>Device</th><th>°C</th></tr>
      {% for r in rows %}
      <tr><td>{{ r.created }}</td><td>{{ r.device }}</td><td>{{ "%.2f"|format(r.temp_c) }}</td></tr>
      {% endfor %}
    </table>
  {% else %}
    <p>No readings yet. Waiting for the ESP32…</p>
  {% endif %}
</body>
</html>"""


@app.get("/")
def index():
    rows = get_db().execute(
        "SELECT device, temp_c, created FROM readings ORDER BY id DESC LIMIT 30"
    ).fetchall()
    return render_template_string(PAGE, rows=rows)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
