"""Tests for the alarm rules. Run with:  venv/bin/pytest -q"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from alarms import AlarmConfig, check_rules, cooldown_over, rise_rate

CFG = AlarmConfig()  # defaults: high 28, low 15, rising 1 °C/min, cooldown 15 min
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def ago(minutes):
    return NOW - timedelta(minutes=minutes)


def kinds(alarms):
    return [kind for kind, _ in alarms]


# --- high / low ---

def test_normal_temperature_gives_no_alarm():
    assert check_rules(22.0, NOW, [], CFG) == []


def test_too_hot():
    assert kinds(check_rules(29.0, NOW, [], CFG)) == ["high"]


def test_exactly_on_the_limit_is_not_an_alarm():
    assert check_rules(28.0, NOW, [], CFG) == []


def test_too_cold():
    assert kinds(check_rules(14.0, NOW, [], CFG)) == ["low"]


# --- rising fast ---

def test_slow_rise_gives_no_alarm():
    history = [(ago(4), 22.0), (ago(2), 22.5)]
    assert check_rules(23.0, NOW, history, CFG) == []   # 1 °C in 4 min = 0.25 °C/min


def test_fast_rise_gives_alarm():
    history = [(ago(2), 22.0)]
    assert kinds(check_rules(25.0, NOW, history, CFG)) == ["rising"]   # 1.5 °C/min


def test_sudden_jump_after_calm_period_is_caught():
    # Flat for 4 minutes, then jumps 3 °C in the last minute.
    history = [(ago(5), 22.0), (ago(3), 22.0), (ago(1), 22.0)]
    assert kinds(check_rules(25.0, NOW, history, CFG)) == ["rising"]


def test_readings_too_recent_are_not_used_for_speed():
    # Only a 30 s old reading: too short to judge speed reliably.
    assert rise_rate(25.0, NOW, [(ago(0.5), 22.0)], CFG) is None


def test_hot_and_rising_gives_both_alarms():
    history = [(ago(2), 24.0)]
    assert kinds(check_rules(29.0, NOW, history, CFG)) == ["high", "rising"]


# --- cooldown (no spam) ---

def test_first_alarm_is_always_allowed():
    assert cooldown_over(None, NOW, CFG)


def test_same_alarm_10_min_later_is_blocked():
    assert not cooldown_over(ago(10), NOW, CFG)


def test_same_alarm_20_min_later_is_allowed():
    assert cooldown_over(ago(20), NOW, CFG)


# --- whole server: send readings through the API ---

def test_api_saves_alarm_once_then_stays_quiet():
    db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    os.environ["DB_PATH"] = db_file
    os.environ["API_KEY"] = "test-key"
    import app as server
    server.DB_PATH = db_file
    server.API_KEY = "test-key"
    server.init_db()
    client = server.app.test_client()
    headers = {"X-API-Key": "test-key"}

    first = client.post("/api/readings", json={"temp_c": 30.0}, headers=headers).get_json()
    second = client.post("/api/readings", json={"temp_c": 30.5}, headers=headers).get_json()

    assert [a["kind"] for a in first["alarms"]] == ["high"]
    assert second["alarms"] == []                 # cooldown: no spam
    assert len(client.get("/api/alarms").get_json()) == 1
    os.remove(db_file)
