"""Tests for the chart data endpoint."""
import sqlite3
from datetime import datetime, timedelta, timezone


def add(server, minutes_ago, temp, seconds=0):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago, seconds=seconds)
    with sqlite3.connect(server.DB_PATH) as db:
        db.execute("INSERT INTO readings (device, temp_c, created) VALUES ('esp32', ?, ?)",
                   (temp, t.isoformat(timespec="seconds")))


def test_history_averages_readings_into_buckets(server):
    # Two readings 2 s apart land in the same 12 s bucket (1 hour view) -> one averaged point.
    add(server, 10, 4.0, seconds=0)
    add(server, 10, 5.0, seconds=-2)
    add(server, 120, 9.9)                                 # older than 1 hour: not included
    d = server.app.test_client().get("/api/history?hours=1").get_json()
    assert d["bucket_s"] == 12
    assert len(d["points"]) == 1
    p = d["points"][0]
    assert p["temp"] == 4.5 and p["min"] == 4.0 and p["max"] == 5.0
    assert d["limits"] == {"high": server.ALARMS.high_c, "low": server.ALARMS.low_c}


def test_history_includes_alarms_and_rejects_odd_ranges(server):
    client = server.app.test_client()
    client.post("/api/readings", json={"temp_c": 30.0}, headers={"X-API-Key": "test-key"})
    d = client.get("/api/history?hours=7").get_json()      # not 1/6/24 -> falls back to 6
    assert d["hours"] == 6
    assert [a["kind"] for a in d["alarms"]] == ["high"]
    assert d["points"][0]["t"] > 1_700_000_000_000          # milliseconds, for JavaScript
