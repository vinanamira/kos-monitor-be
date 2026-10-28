import json
from datetime import datetime, timedelta

from app.config import WIB

from .conftest import VALID


# --------------------------------------------------------------------- telemetry
def test_valid_telemetry_stored_and_forwarded(client, publisher, settings):
    response = client.post("/api/telemetry", json=VALID)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "valid" and body["accepted"] is True

    forwarded = publisher.json_on(settings.topic_telemetry_validated)
    assert len(forwarded) == 1
    assert forwarded[0]["voltage_v"] == 220.4
    assert forwarded[0]["status"] == "valid"
    assert forwarded[0]["id"] == body["id"]

    latest = client.get("/api/telemetry/latest").json()
    assert latest["id"] == body["id"]
    assert latest["received_at"].endswith("+07:00")


def test_invalid_telemetry_never_reaches_dashboard(client, publisher, settings):
    response = client.post("/api/telemetry", json={**VALID, "power_w": 900})
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "inconsistent"

    assert publisher.on(settings.topic_telemetry_validated) == []
    rejected = publisher.json_on(settings.topic_rejected)
    assert len(rejected) == 1 and rejected[0]["kind"] == "telemetry"

    assert client.get("/api/telemetry/latest").status_code == 404
    log = client.get("/api/rejections").json()
    assert log[0]["errors"][0]["code"] == "inconsistent"
    assert json.loads(log[0]["raw_payload"])["power_w"] == 900


def test_warning_telemetry_forwarded_with_warnings(client, publisher, settings):
    response = client.post("/api/telemetry", json={"voltage_v": 190, "current_a": 1, "power_w": 171, "power_factor": 0.9})
    assert response.status_code == 201
    assert response.json()["status"] == "warning"
    forwarded = publisher.json_on(settings.topic_telemetry_validated)[0]
    assert forwarded["status"] == "warning"
    assert "toleransi PLN" in forwarded["warnings"][0]


def test_mqtt_and_http_share_same_validation(processor, publisher, settings):
    assert not processor.handle_telemetry(json.dumps({**VALID, "voltage_v": 999}), source="mqtt").accepted
    assert processor.handle_telemetry(json.dumps(VALID).encode(), source="mqtt").accepted
    assert len(publisher.on(settings.topic_telemetry_validated)) == 1


def test_history_and_status_filter(client):
    client.post("/api/telemetry", json=VALID)
    client.post("/api/telemetry", json={"voltage_v": 190, "current_a": 1, "power_w": 171, "power_factor": 0.9})
    client.post("/api/telemetry", json={"voltage_v": "rusak"})

    history = client.get("/api/telemetry").json()
    assert [row["status"] for row in history] == ["warning", "valid"]
    assert len(client.get("/api/telemetry", params={"status": "warning"}).json()) == 1


def test_validation_stats(client):
    client.post("/api/telemetry", json=VALID)
    client.post("/api/telemetry", json={**VALID, "voltage_v": 999})
    client.post("/api/telemetry", json={**VALID, "power_w": 900})
    client.post("/api/telemetry", content=b"bukan json")

    stats = client.get("/api/validation/stats").json()
    assert stats["total"] == 4
    assert stats["valid"] == 1 and stats["rejected"] == 3
    assert stats["acceptance_rate_pct"] == 25.0
    assert stats["rejected_by_code"] == {"out_of_range": 1, "inconsistent": 1, "json_invalid": 1}


def test_energy_summary(processor, client):
    base = datetime(2026, 10, 28, 10, 0, tzinfo=WIB)
    steady = json.dumps({"voltage_v": 200, "current_a": 5, "power_w": 1000, "power_factor": 1})
    for offset in (0, 30, 60, 600):  # celah 540 s terakhir (offline) tidak dihitung
        processor.handle_telemetry(steady, source="mqtt", now=base + timedelta(seconds=offset))

    summary = client.get("/api/telemetry/summary", params={"date": "2026-10-28"}).json()
    assert summary["readings"] == 4
    assert summary["energy_kwh"] == round(1000 * 60 / 3600 / 1000, 4)  # 1000 W selama 60 s
    assert summary["cost_rp"] == round(1000 * 60 / 3600 / 1000 * 1444.70)
    assert summary["max_power_w"] == 1000

    assert client.get("/api/telemetry/summary", params={"date": "2026-10-27"}).json()["readings"] == 0


def test_validation_rules_listed(client):
    layers = client.get("/api/validation/rules").json()["layers"]
    assert [layer["layer"] for layer in layers] == [1, 2, 3, 4, 5, 6]


# --------------------------------------------------------------------- lampu
def test_lamp_state_validated(processor, publisher, settings, client):
    assert processor.handle_lamp_state(1, b"on") == "ON"
    assert processor.handle_lamp_state(2, b"kedip") is None

    assert publisher.messages[0] == (settings.topic_lamp_state_validated(1), "ON", True)
    assert publisher.on(settings.topic_lamp_state_validated(2)) == []
    assert client.get("/api/rejections", params={"kind": "lamp_state"}).json()[0]["topic"].endswith("led2/state")

    lamps = {row["lamp"]: row["state"] for row in client.get("/api/lamps").json()}
    assert lamps == {1: "ON", 2: None}


def test_lamp_state_from_telemetry(client):
    client.post("/api/telemetry", json={**VALID, "led2": "off"})
    lamps = {row["lamp"]: row["state"] for row in client.get("/api/lamps").json()}
    assert lamps[2] == "OFF"


def test_lamp_command_sent(client, publisher, settings):
    response = client.post("/api/lamps/1/command", json={"action": "on"})
    assert response.status_code == 202
    assert response.json()["action"] == "ON"
    assert publisher.on(settings.topic_lamp_cmd(1)) == ["ON"]


def test_invalid_lamp_command_rejected(client, publisher):
    bad_lamp = client.post("/api/lamps/3/command", json={"action": "ON"})
    bad_action = client.post("/api/lamps/1/command", json={"action": "KEDIP"})
    assert bad_lamp.status_code == bad_action.status_code == 422
    assert bad_lamp.json()["errors"][0] == {"field": "lamp", "code": "less_than_equal", "message": "harus ≤ 2"}
    assert bad_action.json()["errors"][0]["field"] == "action"
    assert publisher.messages == []


def test_lamp_command_when_mqtt_down(client, publisher):
    publisher.connected = False
    response = client.post("/api/lamps/1/command", json={"action": "ON"})
    assert response.status_code == 503


def test_command_request_topic_validated(processor, publisher, settings, client):
    processor.handle_command_request(2, b"OFF")
    processor.handle_command_request(2, b"meledak")
    assert publisher.on(settings.topic_lamp_cmd(2)) == ["OFF"]
    assert len(client.get("/api/rejections", params={"kind": "lamp_command"}).json()) == 1


# --------------------------------------------------------------------- sistem
def test_health(client, processor):
    health = client.get("/health").json()
    assert health["mqtt"] == "disabled"
    assert health["device"] == "unknown"

    # data uji lewat HTTP tidak membuat perangkat terlihat online
    client.post("/api/telemetry", json=VALID)
    assert client.get("/health").json()["device"] == "unknown"

    processor.handle_telemetry(json.dumps(VALID), source="mqtt")
    assert client.get("/health").json()["device"] == "online"
