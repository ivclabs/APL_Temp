"""APL_Temp server: receives temperatures from the ESP32, saves them and checks alarms.

Endpoints:
  POST /api/readings   ESP32 sends {"temp_c": 22.5}   (needs X-API-Key header)
  GET  /api/readings   latest readings as JSON
  GET  /api/alarms     latest alarms as JSON (with the AI explanation when ready)
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
        "SELECT device, kind, temp_c, message, ai_message, created FROM alarms ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


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
    body { font-family: system-ui, sans-serif; max-width: 600px; margin: 2rem auto; padding: 0 1rem; }
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

  <h2>🤖 Today, by AI</h2>
  <div class="ai-box"><span id="summary">Loading…</span>
    <small id="summary-time" class="muted" style="display:block"></small></div>

  <h2>Alarms</h2>
  <div id="alarms"><p class="muted">Loading…</p></div>

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

function showAlarms(alarms) {
  const box = document.getElementById("alarms");
  box.replaceChildren();
  if (alarms.length === 0) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No alarms. All good.";
    box.append(p);
    return;
  }
  for (const a of alarms) {
    const div = document.createElement("div");
    div.className = "alarm";
    const b = document.createElement("b");
    b.textContent = a.kind.toUpperCase();
    const small = document.createElement("small");
    small.textContent = localTime(a.created);
    div.append(b, " – " + a.message);
    const explain = document.createElement("div");
    explain.className = "ai";
    if (a.ai_message) {
      explain.textContent = "🤖 " + a.ai_message;
      div.append(explain);
    } else if (Date.now() - new Date(a.created).getTime() < 120000) {
      explain.className = "ai muted";
      explain.textContent = "🤖 AI is writing an explanation…";
      div.append(explain);
    }
    div.append(small);
    box.append(div);
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
      fetch("/api/alarms?limit=10").then(r => r.json()),
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
