"""APL_Temp server: receives temperatures from the ESP32, saves them and checks alarms.

Endpoints:
  POST /api/readings   ESP32 sends {"temp_c": 22.5}   (needs X-API-Key header)
  GET  /api/readings   latest readings as JSON
  GET  /api/alarms     latest alarms as JSON (with the AI explanation when ready)
  GET  /api/history    readings for the chart: ?hours=1|6|24, averaged into max 300 points
  GET  /api/summary    AI-written summary of today
  POST /api/ask        ask the AI a question about the data: {"question": "..."}
  GET  /api/ai         AI status and usage today
  GET  /               web page
"""
import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g, jsonify, render_template_string, request

from alarms import AlarmConfig, check_rules, cooldown_over, parse_time

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

import ai      # noqa: E402  (after load_dotenv so they see their settings)
import notify  # noqa: E402

API_KEY = os.environ.get("API_KEY", "")
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "readings.db"))
ALARMS = AlarmConfig.from_env()
READING_EVERY_S = int(os.environ.get("READING_EVERY_S", 30))  # must match HEARTBEAT_MS on the ESP32

# DS18B20 can measure -55 to +125 °C. Anything outside is a broken reading.
MIN_VALID_C = -55
MAX_VALID_C = 125

SUMMARY_MAX_AGE_MIN = 180   # write a new AI summary at most every 3 hours
MAX_QUESTION_LEN = 300

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# AI calls and emails take a few seconds - run them in the background so the ESP32 never waits.
ai_jobs = ThreadPoolExecutor(max_workers=2)


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
        # Added in step 5: the AI's explanation of the alarm (filled in a few seconds later).
        columns = [r[1] for r in db.execute("PRAGMA table_info(alarms)")]
        if "ai_message" not in columns:
            try:
                db.execute("ALTER TABLE alarms ADD COLUMN ai_message TEXT")
            except sqlite3.OperationalError as e:
                # gunicorn starts 2 workers at once; the other one may have added it first.
                if "duplicate column" not in str(e):
                    raise
        db.execute(
            """CREATE TABLE IF NOT EXISTS summaries (
                   day       TEXT PRIMARY KEY,  -- local date
                   text      TEXT NOT NULL,
                   created   TEXT NOT NULL
               )"""
        )
    ai.init_usage_table(DB_PATH)
    notify.init_table(DB_PATH)


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

        cur = db.execute(
            "INSERT INTO alarms (device, kind, temp_c, message, created) VALUES (?, ?, ?, ?, ?)",
            (device, kind, temp_c, message, now.isoformat(timespec="seconds")),
        )
        new_alarms.append({"id": cur.lastrowid, "kind": kind, "message": message})
        app.logger.warning("ALARM %s: %s", kind, message)

    return new_alarms


def limits_text():
    return (f"high > {ALARMS.high_c} °C, low < {ALARMS.low_c} °C, "
            f"rising >= {ALARMS.rise_c_per_min} °C/min")


def handle_new_alarms(new_alarms, device, temp_c, now):
    """Background job: AI explains each alarm, then ONE email is sent for all of them."""
    since = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute(
            "SELECT created, temp_c FROM readings WHERE device = ? AND created >= ? "
            "ORDER BY id DESC LIMIT 120",
            (device, since),
        ).fetchall()
    recent = list(reversed(rows))

    for alarm in new_alarms:
        try:
            text = ai.explain_alarm(DB_PATH, alarm["kind"], alarm["message"], recent, limits_text())
        except Exception:
            app.logger.exception("AI alarm explanation failed")
            text = None
        alarm["ai_message"] = text
        if text:
            with sqlite3.connect(DB_PATH) as db:
                db.execute("UPDATE alarms SET ai_message = ? WHERE id = ?", (text, alarm["id"]))

    # Email goes out even if the AI failed - then it just has the plain alarm text.
    notify.send_alarm_email(DB_PATH, new_alarms, temp_c, now.astimezone(ai.LOCAL_TZ))


# ---------- Data for the AI (summaries and questions) ----------

def local_day_bounds(day):
    """Local calendar day -> (start, end) as UTC ISO strings, to compare with 'created'."""
    start = datetime.combine(day, time(0), tzinfo=ai.LOCAL_TZ)
    end = start + timedelta(days=1)
    return (start.astimezone(timezone.utc).isoformat(timespec="seconds"),
            end.astimezone(timezone.utc).isoformat(timespec="seconds"))


def day_data_text(db, day, label):
    """Compact text about one day: overall stats, hourly averages and alarms."""
    start, end = local_day_bounds(day)
    rows = db.execute(
        "SELECT created, temp_c FROM readings WHERE created >= ? AND created < ? ORDER BY id",
        (start, end),
    ).fetchall()
    if not rows:
        return f"{label} ({day}): no readings."

    temps = [r[1] for r in rows]
    hottest = max(rows, key=lambda r: r[1])
    coldest = min(rows, key=lambda r: r[1])
    lines = [
        f"{label} ({day}): {len(rows)} readings from {ai.local_time(rows[0][0], '%H:%M')} "
        f"to {ai.local_time(rows[-1][0], '%H:%M')}.",
        f"Average {sum(temps) / len(temps):.2f} °C. "
        f"Highest {hottest[1]:.2f} °C at {ai.local_time(hottest[0], '%H:%M')}. "
        f"Lowest {coldest[1]:.2f} °C at {ai.local_time(coldest[0], '%H:%M')}.",
        "Hourly (local time): average / min / max:",
    ]
    hours = {}
    for created, temp in rows:
        hours.setdefault(ai.local_time(created, "%H:00"), []).append(temp)
    for hour, ts in hours.items():
        lines.append(f"  {hour}  {sum(ts) / len(ts):.2f} / {min(ts):.2f} / {max(ts):.2f} °C")

    alarms = db.execute(
        "SELECT created, kind, message FROM alarms WHERE created >= ? AND created < ? ORDER BY id",
        (start, end),
    ).fetchall()
    lines.append("Alarms:" if alarms else "Alarms: none.")
    for created, kind, message in alarms:
        lines.append(f"  {ai.local_time(created, '%H:%M')}  {kind}: {message}")
    return "\n".join(lines)


def question_data_text(db):
    today = datetime.now(ai.LOCAL_TZ).date()
    latest = db.execute("SELECT created, temp_c FROM readings ORDER BY id DESC LIMIT 10").fetchall()
    return "\n\n".join([
        f"Current local time: {datetime.now(ai.LOCAL_TZ).strftime('%Y-%m-%d %H:%M')}",
        f"Alarm limits: {limits_text()}",
        "Latest readings (oldest first):\n" + ai.format_readings(list(reversed(latest))),
        day_data_text(db, today, "Today so far"),
        day_data_text(db, today - timedelta(days=1), "Yesterday"),
    ])


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

    if new_alarms:
        # Copies, so the background job can add AI text without changing this response.
        ai_jobs.submit(handle_new_alarms, [dict(a) for a in new_alarms], device, temp, now)
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
        "SELECT id, device, kind, temp_c, message, ai_message, created FROM alarms ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/history")
def history():
    """Chart data. Readings are averaged into time buckets so the chart gets at most ~300 points,
    however many readings there are (up to one every 2 s when the temperature changes)."""
    hours = request.args.get("hours", 6, type=int)
    if hours not in (1, 6, 24):
        hours = 6
    bucket_s = hours * 3600 // 300          # 1 h -> 12 s, 6 h -> 72 s, 24 h -> 288 s
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    db = get_db()
    points = db.execute(
        """SELECT AVG(CAST(strftime('%s', created) AS INTEGER)) AS t,
                  AVG(temp_c) AS temp, MIN(temp_c) AS lo, MAX(temp_c) AS hi
           FROM readings WHERE created >= ?
           GROUP BY CAST(strftime('%s', created) AS INTEGER) / ?
           ORDER BY t""",
        (since, bucket_s),
    ).fetchall()
    alarms = db.execute(
        "SELECT CAST(strftime('%s', created) AS INTEGER) AS t, kind, temp_c "
        "FROM alarms WHERE created >= ? ORDER BY id",
        (since,),
    ).fetchall()
    return jsonify(
        hours=hours,
        bucket_s=bucket_s,
        # times in milliseconds since 1970 (what JavaScript's Date uses)
        points=[{"t": int(r["t"] * 1000), "temp": round(r["temp"], 2),
                 "min": r["lo"], "max": r["hi"]} for r in points],
        alarms=[{"t": r["t"] * 1000, "kind": r["kind"], "temp": r["temp_c"]} for r in alarms],
        limits={"high": ALARMS.high_c, "low": ALARMS.low_c},
    )


@app.get("/api/summary")
def get_summary():
    """AI summary of today. Reuses the saved one if it is less than 3 hours old."""
    today = datetime.now(ai.LOCAL_TZ).date()
    db = get_db()
    saved = db.execute("SELECT text, created FROM summaries WHERE day = ?", (today.isoformat(),)).fetchone()
    now = datetime.now(timezone.utc)
    if saved and now - parse_time(saved["created"]) < timedelta(minutes=SUMMARY_MAX_AGE_MIN):
        return jsonify(day=today.isoformat(), text=saved["text"], created=saved["created"])

    text = ai.daily_summary(DB_PATH, "today so far", day_data_text(db, today, "Today so far"))
    if not text:
        if saved:  # AI unavailable - an older summary is better than none
            return jsonify(day=today.isoformat(), text=saved["text"], created=saved["created"])
        return jsonify(error="AI summary not available right now"), 503

    created = now.isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO summaries (day, text, created) VALUES (?, ?, ?) "
        "ON CONFLICT(day) DO UPDATE SET text = excluded.text, created = excluded.created",
        (today.isoformat(), text, created),
    )
    db.commit()
    return jsonify(day=today.isoformat(), text=text, created=created)


@app.post("/api/ask")
def ask():
    question = str((request.get_json(silent=True) or {}).get("question", "")).strip()
    if not question:
        return jsonify(error="Type a question first"), 400
    if len(question) > MAX_QUESTION_LEN:
        return jsonify(error=f"Question too long (max {MAX_QUESTION_LEN} characters)"), 400

    answer = ai.answer_question(DB_PATH, question, question_data_text(get_db()))
    if not answer:
        return jsonify(error="The AI is not available right now (or today's limit is used up)"), 503
    return jsonify(question=question, answer=answer)


@app.get("/api/ai")
def ai_status():
    return jsonify(ai.usage_today(DB_PATH))


# ---------- Web page ----------

PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>APL_Temp</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 600px; margin: 2rem auto; padding: 0 1rem; background: #fcfcfb; color: #0b0b0b; }
    /* Chart */
    .ranges { display: flex; gap: .4rem; margin: .5rem 0; }
    .ranges button { font: inherit; font-size: .9rem; padding: .3rem .8rem; border: 1px solid #e1e0d9;
                     background: #fcfcfb; color: #52514e; border-radius: 6px; cursor: pointer; }
    .ranges button[aria-pressed="true"] { border-color: #0b0b0b; color: #0b0b0b; font-weight: 600; }
    .chart { position: relative; touch-action: pan-y; }
    .chart.loading svg { opacity: .5; }
    .chart svg { display: block; width: 100%; height: 220px; }
    .chart text { font-size: 11px; fill: #52514e; font-variant-numeric: tabular-nums; }
    .tip { position: absolute; pointer-events: none; background: #fff; border: 1px solid #e1e0d9;
           border-radius: 6px; padding: .3rem .5rem; font-size: .8rem; box-shadow: 0 2px 6px rgba(0,0,0,.08);
           white-space: nowrap; }
    .tip b { font-size: .95rem; }
    .tip .muted { display: block; }
    .now { font-size: 3rem; font-weight: bold; margin: .5rem 0; }
    table { width: 100%; border-collapse: collapse; }
    td, th { text-align: left; padding: .3rem; border-bottom: 1px solid #ddd; }
    .muted { color: #777; }
    .live { font-size: .9rem; }
    .dot { display: inline-block; width: .6rem; height: .6rem; border-radius: 50%; background: #1e8e3e; margin-right: .3rem; }
    .offline .dot { background: #d93025; }
    .late .dot { background: #f29900; }
    .alarm { background: #fdecea; border-left: 4px solid #d93025; padding: .5rem .75rem; margin: .4rem 0; }
    .alarm small { color: #777; display: block; }
    /* Older alarms: one short line each, tap to see the full text */
    #older-alarms > summary { cursor: pointer; color: #1a73e8; padding: .5rem 0; font-weight: 600; }
    .alarm-row { background: #fdecea; border-left: 4px solid #d93025; margin: .3rem 0; padding: .45rem .75rem; }
    .alarm-row summary { cursor: pointer; }
    .alarm-row .when { color: #777; }
    .alarm-row[open] summary { margin-bottom: .3rem; }
    .ai { margin-top: .35rem; }
    .ai-box { background: #eef3fd; border-left: 4px solid #1a73e8; padding: .6rem .8rem; }
    #ask-form { display: flex; gap: .5rem; }
    #ask-form input { flex: 1; min-width: 0; padding: .5rem; font-size: 1rem; }
    #ask-form button { padding: .5rem 1rem; font-size: 1rem; }
    tr.new td { animation: flash 2s ease-out; }
    @keyframes flash { from { background: #fff3b0; } to { background: transparent; } }
  </style>
</head>
<body>
  <h1>APL_Temp</h1>
  <div id="now" class="now">–</div>
  <p class="muted">Latest reading: <span id="latest">–</span></p>
  <p id="live" class="live muted"><span class="dot"></span><span id="live-text">Connecting…</span></p>

  <p class="muted">Limits: low &lt; {{ cfg.low_c }} °C · high &gt; {{ cfg.high_c }} °C ·
     rising ≥ {{ cfg.rise_c_per_min }} °C/min</p>

  <h2>Temperature</h2>
  <div class="ranges" role="group" aria-label="Time range">
    <button data-hours="1">1 hour</button>
    <button data-hours="6" aria-pressed="true">6 hours</button>
    <button data-hours="24">24 hours</button>
  </div>
  <div id="chart" class="chart"><p class="muted">Loading chart…</p></div>

  <h2>🤖 Today, by AI</h2>
  <div class="ai-box"><span id="summary">Loading…</span>
    <small id="summary-time" class="muted" style="display:block"></small></div>

  <h2>Alarms</h2>
  <div id="alarms"><p class="muted">Loading…</p></div>
  <details id="older-alarms" hidden>
    <summary id="older-label">Show older alarms</summary>
    <div id="older-list"></div>
  </details>

  <h2>🤖 Ask the AI</h2>
  <form id="ask-form">
    <input id="question" maxlength="300" placeholder="e.g. When was it warmest today?" autocomplete="off">
    <button>Ask</button>
  </form>
  <p id="answer" class="ai-box" hidden></p>
  <p id="ai-usage" class="muted"></p>

  <h2>Latest readings</h2>
  <table>
    <thead><tr><th>Time</th><th>Device</th><th>°C</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>

<script>
// Asks the server for new data every 3 seconds and updates the page without reloading it.
const CHECK_EVERY_MS = 2000;                    // how often we ask the server
const READING_EVERY_S = {{ reading_every_s }};  // ESP32 sends at least this often (sooner if temp changes)
let newestSeen = null;
let latestReadingAt = null;   // time of newest reading, in ms
let serverOk = true;

// Server stores UTC; show it in the viewer's own time zone (e.g. Swedish time).
function localTime(iso) {
  return new Date(iso).toLocaleString("sv-SE", { dateStyle: "short", timeStyle: "medium" });
}

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text;
  return td;
}

function showReadings(rows) {
  if (rows.length === 0) {
    document.getElementById("now").textContent = "–";
    document.getElementById("latest").textContent = "no readings yet, waiting for the ESP32…";
    return;
  }
  document.getElementById("now").textContent = rows[0].temp_c.toFixed(2) + " °C";
  document.getElementById("latest").textContent = localTime(rows[0].created);
  latestReadingAt = new Date(rows[0].created).getTime();

  const body = document.getElementById("rows");
  body.replaceChildren();
  for (const r of rows) {
    const tr = document.createElement("tr");
    // Flash rows that arrived since the last check (not on first load).
    if (newestSeen !== null && r.created > newestSeen) tr.className = "new";
    tr.append(cell(localTime(r.created)), cell(r.device), cell(r.temp_c.toFixed(2)));
    body.append(tr);
  }
  newestSeen = rows[0].created;
}

// Alarms: the latest one in full, older ones folded into a list of short lines.
const ALARMS_SHOWN = 100;
const openAlarms = new Set();   // ids of older alarms the visitor opened - kept open across refreshes
let alarmsKey = null;

function aiWriting(a) {
  return !a.ai_message && Date.now() - new Date(a.created).getTime() < 120000;
}

// Rule message + AI explanation (or "AI is writing…") for one alarm.
function alarmDetails(a, el) {
  el.append(a.message);
  if (a.ai_message || aiWriting(a)) {
    const explain = document.createElement("div");
    explain.className = a.ai_message ? "ai" : "ai muted";
    explain.textContent = a.ai_message ? "🤖 " + a.ai_message : "🤖 AI is writing an explanation…";
    el.append(explain);
  }
}

function showAlarms(alarms) {
  // Only redraw when something changed, so the page doesn't jump while you read.
  const key = JSON.stringify(alarms.map(a => [a.id, a.ai_message, aiWriting(a)]));
  if (key === alarmsKey) return;
  alarmsKey = key;

  const box = document.getElementById("alarms");
  const older = document.getElementById("older-alarms");
  box.replaceChildren();
  if (alarms.length === 0) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No alarms. All good.";
    box.append(p);
    older.hidden = true;
    return;
  }

  // Latest alarm: full card
  const a = alarms[0];
  const div = document.createElement("div");
  div.className = "alarm";
  const b = document.createElement("b");
  b.textContent = a.kind.toUpperCase();
  div.append(b, " – ");
  alarmDetails(a, div);
  const small = document.createElement("small");
  small.textContent = "Latest alarm · " + localTime(a.created);
  div.append(small);
  box.append(div);

  // Older alarms: one line each (type · temperature · time), tap a line to open it
  const rest = alarms.slice(1);
  older.hidden = rest.length === 0;
  const more = alarms.length === ALARMS_SHOWN ? " (latest " + ALARMS_SHOWN + ")" : "";
  document.getElementById("older-label").textContent =
    "Show " + rest.length + " older alarm" + (rest.length === 1 ? "" : "s") + more;
  const list = document.getElementById("older-list");
  list.replaceChildren();
  for (const o of rest) {
    const row = document.createElement("details");
    row.className = "alarm-row";
    row.open = openAlarms.has(o.id);
    row.addEventListener("toggle", () => row.open ? openAlarms.add(o.id) : openAlarms.delete(o.id));
    const sum = document.createElement("summary");
    const kind = document.createElement("b");
    kind.textContent = o.kind.toUpperCase();
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = " · " + localTime(o.created);
    sum.append(kind, " · " + o.temp_c.toFixed(1) + " °C", when);
    const body = document.createElement("div");
    alarmDetails(o, body);
    row.append(sum, body);
    list.append(row);
  }
}

// Status line: count down to the latest time the next reading should arrive.
// It can come sooner: the ESP32 sends right away when the temperature changes.
function showLive() {
  const live = document.getElementById("live");
  const text = document.getElementById("live-text");
  live.classList.remove("offline", "late");

  if (!serverOk) {
    live.classList.add("offline");
    text.textContent = "Can't reach the server – retrying…";
    return;
  }
  if (latestReadingAt === null) {
    text.textContent = "Live · waiting for the first reading…";
    return;
  }
  const secsLeft = Math.ceil((latestReadingAt + READING_EVERY_S * 1000 - Date.now()) / 1000);
  const secsSince = Math.round((Date.now() - latestReadingAt) / 1000);

  if (secsLeft > 0) {
    text.textContent = "Live · next update within " + secsLeft + " s";
  } else if (secsSince < READING_EVERY_S * 3) {
    text.textContent = "Live · next update any moment…";
  } else {
    live.classList.add("late");   // missed 2+ readings
    text.textContent = "No new reading for " + secsSince + " s – is the ESP32 on?";
  }
}

async function update() {
  try {
    const [readings, alarms] = await Promise.all([
      fetch("/api/readings?limit=30").then(r => r.json()),
      fetch("/api/alarms?limit=" + ALARMS_SHOWN).then(r => r.json()),
    ]);
    showReadings(readings);
    showAlarms(alarms);
    serverOk = true;
  } catch (e) {
    serverOk = false;
  }
  showLive();
}

// ---------- AI: summary, questions, usage ----------

async function loadSummary() {
  const el = document.getElementById("summary");
  try {
    const r = await fetch("/api/summary");
    const d = await r.json();
    el.textContent = d.text || d.error;
    document.getElementById("summary-time").textContent =
      d.created ? "Written " + localTime(d.created) + " · updates every few hours" : "";
  } catch (e) {
    el.textContent = "Summary not available right now.";
  }
  loadUsage();
}

async function loadUsage() {
  try {
    const d = await (await fetch("/api/ai")).json();
    document.getElementById("ai-usage").textContent = d.enabled
      ? "AI calls today: " + d.calls + " / " + d.limit + " (" + d.model + ")"
      : "AI is turned off.";
  } catch (e) { /* not important */ }
}

document.getElementById("ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("question");
  const answer = document.getElementById("answer");
  const button = event.target.querySelector("button");
  if (!input.value.trim()) return;
  button.disabled = true;
  answer.hidden = false;
  answer.textContent = "🤖 Thinking…";
  try {
    const r = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: input.value }),
    });
    const d = await r.json();
    answer.textContent = d.answer ? "🤖 " + d.answer : d.error;
  } catch (e) {
    answer.textContent = "Can't reach the server.";
  }
  button.disabled = false;
  loadUsage();
});

// ---------- Chart (plain SVG, no library) ----------
// One line = temperature. Red hairlines = alarm limits. Red dots = alarms.
// Hover / touch shows the exact value. The "Latest readings" table below is the table view.

const SVG_NS = "http://www.w3.org/2000/svg";
const COLOR = { line: "#2a78d6", limit: "#d03b3b", alarm: "#d03b3b", grid: "#e1e0d9",
                surface: "#fcfcfb", cross: "#52514e" };
let chartHours = 6;
let chartData = null;

function el(name, attrs, parent) {
  const node = document.createElementNS(SVG_NS, name);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  if (parent) parent.append(node);
  return node;
}

function niceStep(range) {
  for (const step of [0.5, 1, 2, 5, 10, 20]) if (range / step <= 5) return step;
  return 50;
}

function clock(ms) {
  return new Date(ms).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" });
}

function drawChart() {
  const box = document.getElementById("chart");
  box.classList.remove("loading");
  const d = chartData;
  if (!d || d.points.length === 0) {
    box.replaceChildren();
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No readings in this period.";
    box.append(p);
    return;
  }

  const W = box.clientWidth || 360, H = 220;
  const m = { top: 10, right: 58, bottom: 22, left: 34 };
  const now = Date.now(), t0 = now - d.hours * 3600 * 1000;

  // Y range: the data, plus each limit if it is close enough to matter.
  const temps = d.points.flatMap(p => [p.min, p.max]);
  let lo = Math.min(...temps), hi = Math.max(...temps);
  for (const v of [d.limits.low, d.limits.high]) {
    if (v >= lo - 5 && v <= hi + 5) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  }
  const step = niceStep(Math.max(hi - lo, 1));
  lo = Math.floor((lo - 0.3) / step) * step;
  hi = Math.ceil((hi + 0.3) / step) * step;

  const x = t => m.left + (t - t0) / (now - t0) * (W - m.left - m.right);
  const y = v => m.top + (hi - v) / (hi - lo) * (H - m.top - m.bottom);

  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Temperature over the last ${d.hours} hours, between ${Math.min(...temps).toFixed(1)} and ${Math.max(...temps).toFixed(1)} °C` });

  // Gridlines + y labels
  for (let v = lo; v <= hi + 1e-9; v += step) {
    el("line", { x1: m.left, x2: W - m.right, y1: y(v), y2: y(v), stroke: COLOR.grid, "stroke-width": 1 }, svg);
    el("text", { x: m.left - 6, y: y(v) + 4, "text-anchor": "end" }, svg).textContent = (+v.toFixed(1)) + "°";
  }
  // X labels
  const tick = { 1: 15, 6: 60, 24: 240 }[d.hours] * 60 * 1000;
  for (let t = Math.ceil(t0 / tick) * tick; t <= now; t += tick) {
    el("text", { x: x(t), y: H - 6, "text-anchor": "middle" }, svg).textContent = clock(t);
  }
  // Limit lines (only when inside the visible range), labelled at the right
  for (const [name, v] of [["High", d.limits.high], ["Low", d.limits.low]]) {
    if (v < lo || v > hi) continue;
    el("line", { x1: m.left, x2: W - m.right, y1: y(v), y2: y(v), stroke: COLOR.limit, "stroke-width": 1 }, svg);
    el("text", { x: W - m.right + 6, y: y(v) + 4 }, svg).textContent = `${name} ${v}°`;
  }
  // The temperature line. Break it where the ESP32 was silent (gap > 3 buckets and > 90 s).
  const gapMs = Math.max(3 * d.bucket_s, 90) * 1000;
  let path = "";
  d.points.forEach((p, i) => {
    const jump = i === 0 || p.t - d.points[i - 1].t > gapMs;
    path += (jump ? "M" : "L") + x(p.t).toFixed(1) + "," + y(p.temp).toFixed(1);
  });
  el("path", { d: path, fill: "none", stroke: COLOR.line, "stroke-width": 2,
               "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
  // Latest value label at the line's end
  const last = d.points[d.points.length - 1];
  el("circle", { cx: x(last.t), cy: y(last.temp), r: 4, fill: COLOR.line, stroke: COLOR.surface, "stroke-width": 2 }, svg);
  // Alarm dots
  for (const a of d.alarms) {
    if (a.t < t0) continue;
    el("circle", { cx: x(a.t), cy: y(a.temp), r: 5, fill: COLOR.alarm, stroke: COLOR.surface, "stroke-width": 2 }, svg);
  }

  // Hover / touch: crosshair snaps to the nearest point
  const cross = el("line", { y1: m.top, y2: H - m.bottom, stroke: COLOR.cross, "stroke-width": 1, visibility: "hidden" }, svg);
  const dot = el("circle", { r: 5, fill: COLOR.line, stroke: COLOR.surface, "stroke-width": 2, visibility: "hidden" }, svg);
  const hit = el("rect", { x: m.left, y: 0, width: W - m.left - m.right, height: H, fill: "transparent" }, svg);
  const tip = document.createElement("div");
  tip.className = "tip";
  tip.hidden = true;

  function show(event) {
    const r = svg.getBoundingClientRect();
    const px = (event.clientX - r.left) * (W / r.width);
    const tAt = t0 + (px - m.left) / (W - m.left - m.right) * (now - t0);
    let best = d.points[0];
    for (const p of d.points) if (Math.abs(p.t - tAt) < Math.abs(best.t - tAt)) best = p;
    const near = d.alarms.filter(a => Math.abs(a.t - best.t) <= Math.max(d.bucket_s, 30) * 1000);
    cross.setAttribute("x1", x(best.t)); cross.setAttribute("x2", x(best.t));
    dot.setAttribute("cx", x(best.t)); dot.setAttribute("cy", y(best.temp));
    cross.setAttribute("visibility", "visible"); dot.setAttribute("visibility", "visible");

    tip.replaceChildren();
    const value = document.createElement("b");
    value.textContent = best.temp.toFixed(2) + " °C";
    const when = document.createElement("span");
    when.className = "muted";
    when.textContent = clock(best.t) + (best.max - best.min >= 0.1
      ? ` · range ${best.min.toFixed(1)}–${best.max.toFixed(1)} °C` : "");
    tip.append(value, when);
    for (const a of near) {
      const s = document.createElement("span");
      s.className = "muted";
      s.textContent = "⚠ " + a.kind.toUpperCase() + " alarm";
      tip.append(s);
    }
    tip.hidden = false;
    const left = Math.min(Math.max(x(best.t) * r.width / W - tip.offsetWidth / 2, 0), r.width - tip.offsetWidth);
    tip.style.left = left + "px";
    tip.style.top = "0px";
  }
  function hide() {
    tip.hidden = true;
    cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden");
  }
  hit.addEventListener("pointermove", show);
  hit.addEventListener("pointerdown", show);
  hit.addEventListener("pointerleave", hide);

  box.replaceChildren(svg, tip);
}

async function loadChart(fade) {
  // On a range change, keep the old chart (faded) while loading. Quiet for the 10 s refresh.
  if (fade) document.getElementById("chart").classList.add("loading");
  try {
    chartData = await (await fetch("/api/history?hours=" + chartHours)).json();
  } catch (e) { /* keep the old chart */ }
  drawChart();
}

document.querySelectorAll(".ranges button").forEach(button => {
  button.addEventListener("click", () => {
    chartHours = +button.dataset.hours;
    document.querySelectorAll(".ranges button").forEach(b => b.setAttribute("aria-pressed", b === button));
    loadChart(true);
  });
});
window.addEventListener("resize", drawChart);
loadChart();
setInterval(loadChart, 10000);   // new points every 10 s

loadSummary();
setInterval(loadSummary, 10 * 60 * 1000);   // check for a newer summary every 10 min
update();
setInterval(update, CHECK_EVERY_MS);
setInterval(showLive, 1000);   // tick the countdown every second
// Phones pause pages in the background - check right away when the page is shown again.
document.addEventListener("visibilitychange", () => { if (!document.hidden) update(); });
</script>
</body>
</html>"""


@app.get("/")
def index():
    # The page itself is just a shell; JavaScript fills in the data from the API.
    return render_template_string(PAGE, cfg=ALARMS, reading_every_s=READING_EVERY_S)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
