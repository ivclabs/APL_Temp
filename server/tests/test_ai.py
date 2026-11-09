"""Tests for the AI features, using a fake Claude (no cost, no internet)."""
import ai

HEADERS = {"X-API-Key": "test-key"}


def post(client, temp):
    return client.post("/api/readings", json={"temp_c": temp}, headers=HEADERS)


def test_alarm_gets_ai_explanation(server, fake_ai):
    client = server.app.test_client()
    post(client, 22.0)
    post(client, 30.0)                                   # too hot -> alarm
    alarm = client.get("/api/alarms").get_json()[0]
    assert alarm["kind"] == "high"
    assert alarm["ai_message"] == "Fake AI answer"
    sent = fake_ai.requests[0]["messages"][0]["content"]
    assert "30.00 °C" in sent                            # the AI saw the real readings


def test_ai_off_alarm_still_works(server):
    client = server.app.test_client()
    post(client, 30.0)
    alarm = client.get("/api/alarms").get_json()[0]
    assert alarm["kind"] == "high" and alarm["ai_message"] is None


def test_daily_limit_stops_ai_calls(server, fake_ai, monkeypatch):
    monkeypatch.setattr(ai, "DAILY_LIMIT", 2)
    client = server.app.test_client()
    answers = [client.post("/api/ask", json={"question": "How warm is it?"}) for _ in range(3)]
    assert [a.status_code for a in answers] == [200, 200, 503]
    assert len(fake_ai.requests) == 2                    # third call never reached the AI
    assert client.get("/api/ai").get_json()["calls"] == 2


def test_refusal_gives_no_answer(server, fake_ai):
    fake_ai.stop_reason = "refusal"
    reply = server.app.test_client().post("/api/ask", json={"question": "Hi"})
    assert reply.status_code == 503


def test_ask_validates_question(server, fake_ai):
    client = server.app.test_client()
    assert client.post("/api/ask", json={"question": "  "}).status_code == 400
    assert client.post("/api/ask", json={"question": "x" * 301}).status_code == 400
    assert fake_ai.requests == []


def test_ask_answers_with_data(server, fake_ai):
    client = server.app.test_client()
    post(client, 21.5)
    reply = client.post("/api/ask", json={"question": "When was it warmest?"}).get_json()
    assert reply["answer"] == "Fake AI answer"
    sent = fake_ai.requests[0]["messages"][0]["content"]
    assert "When was it warmest?" in sent and "21.50 °C" in sent


def test_summary_is_reused_not_rewritten(server, fake_ai):
    client = server.app.test_client()
    post(client, 21.5)
    first = client.get("/api/summary").get_json()
    second = client.get("/api/summary").get_json()
    assert first["text"] == second["text"] == "Fake AI answer"
    assert len(fake_ai.requests) == 1                    # second request used the saved summary
