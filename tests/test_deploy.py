import asyncio

from app import main as main_module
from app.config import Settings


def test_keep_alive_target():
    base = {"_env_file": None, "mqtt_enabled": False}
    assert Settings(**base).keep_alive_target is None
    assert (
        Settings(**base, render_external_url="https://kos.onrender.com/").keep_alive_target
        == "https://kos.onrender.com/health"
    )
    assert Settings(**base, keep_alive_url="https://a/b", render_external_url="https://x").keep_alive_target == "https://a/b"
    assert Settings(**base, keep_alive_enabled=False, render_external_url="https://x").keep_alive_target is None


def test_keep_alive_survives_ping_errors(monkeypatch):
    calls = []

    def fake_ping(url):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("jaringan putus")

    monkeypatch.setattr(main_module, "_ping", fake_ping)

    async def run():
        task = asyncio.create_task(main_module.keep_alive("https://kos.onrender.com/health", 0.01))
        await asyncio.sleep(0.15)
        task.cancel()

    asyncio.run(run())
    assert len(calls) >= 2
    assert set(calls) == {"https://kos.onrender.com/health"}


def test_root_redirects_to_docs(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/docs"
