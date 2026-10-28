import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

VALID = {
    "voltage_v": 220.4,
    "current_a": 1.25,
    "power_w": 247.95,
    "power_factor": 0.9,
    "load_percent": 41.7,
    "adc": 1708,
}


class FakePublisher:
    """Pengganti broker MQTT: mencatat semua pesan yang dipublish."""

    def __init__(self):
        self.connected = True
        self.messages: list[tuple[str, str, bool]] = []

    def publish(self, topic: str, payload: str, retain: bool = False) -> bool:
        if not self.connected:
            return False
        self.messages.append((topic, payload, retain))
        return True

    def on(self, topic: str) -> list[str]:
        return [payload for t, payload, _ in self.messages if t == topic]

    def json_on(self, topic: str) -> list[dict]:
        return [json.loads(p) for p in self.on(topic)]


@pytest.fixture
def settings(tmp_path):
    # min_interval_s=0 supaya tes API bisa kirim beberapa data berturut-turut;
    # pembatasan laju diuji terpisah di test_validation.py.
    return Settings(
        _env_file=None,
        mqtt_enabled=False,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        min_interval_s=0,
    )


@pytest.fixture
def publisher():
    return FakePublisher()


@pytest.fixture
def app(settings, publisher):
    return create_app(settings, publisher)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def processor(app, client):
    return app.state.processor
