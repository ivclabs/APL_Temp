"""Tests for alarm emails, using a fake SNS (no real emails)."""
import notify

HEADERS = {"X-API-Key": "test-key"}


def post(client, temp):
    return client.post("/api/readings", json={"temp_c": temp}, headers=HEADERS)


def test_one_email_for_alarms_from_the_same_reading(server, fake_ai, fake_sns, monkeypatch):
    # Pretend the earlier reading was 2 minutes ago, so "rising" can fire together with "high".
    from datetime import datetime, timedelta, timezone
    import sqlite3
    client = server.app.test_client()
    post(client, 22.0)
    two_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(timespec="seconds")
    with sqlite3.connect(server.DB_PATH) as db:
        db.execute("UPDATE readings SET created = ?", (two_min_ago,))

    post(client, 30.0)                                    # high + rising at once

    assert len(fake_sns.sent) == 1
    email = fake_sns.sent[0]
    assert email["Subject"].startswith("APL_Temp alarm: HIGH + RISING")
    assert "AI explanation: Fake AI answer" in email["Message"]
    assert "http://example.test" in email["Message"]


def test_email_sent_even_when_ai_is_off(server, fake_sns):
    post(server.app.test_client(), 30.0)
    assert len(fake_sns.sent) == 1
    assert "AI explanation" not in fake_sns.sent[0]["Message"]
    assert "above the limit" in fake_sns.sent[0]["Message"]


def test_no_email_without_topic(server, monkeypatch):
    monkeypatch.setattr(notify, "TOPIC_ARN", "")
    post(server.app.test_client(), 30.0)                  # must not crash


def test_no_new_email_during_cooldown(server, fake_sns):
    client = server.app.test_client()
    post(client, 30.0)
    post(client, 31.0)                                    # same alarm 15-min cooldown
    assert len(fake_sns.sent) == 1


def test_daily_email_limit(server, fake_sns, monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(notify, "DAILY_LIMIT", 2)
    alarm = [{"kind": "high", "message": "too hot"}]
    results = [notify.send_alarm_email(server.DB_PATH, alarm, 30.0, datetime.now()) for _ in range(3)]
    assert results == [True, True, False]
    assert len(fake_sns.sent) == 2


def test_subject_fits_sns_limit():
    alarms = [{"kind": "rising" * 10, "message": "x"}] * 5
    subject, _ = notify.build_email(alarms, 30.0, "2026-11-09 12:00:00")
    assert len(subject) <= 100
