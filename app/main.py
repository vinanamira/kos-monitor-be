import asyncio
import logging
import urllib.request
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import router
from .config import Settings, get_settings
from .database import Base, make_engine, make_session_factory
from .mqtt_bridge import MqttBridge
from .processor import NullPublisher, Processor, Publisher
from .validation import TelemetryValidator, translate_errors

DESCRIPTION = """
Backend KosMonitor: **gerbang validasi** antara ESP32 dan dashboard.

```
ESP32 ──► {prefix}/telemetry ──► BACKEND (validasi + simpan) ──► {prefix}/validated/telemetry ──► Dashboard
                                        └─ ditolak ──► {prefix}/validated/rejected + log DB
```

Coba validasinya langsung lewat **POST /api/telemetry** (pilih contoh payload di dropdown *Examples*).
"""


log = logging.getLogger("kosmonitor")


def _ping(url: str) -> None:
    with urllib.request.urlopen(url, timeout=15) as response:
        response.read(0)


async def keep_alive(url: str, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await asyncio.to_thread(_ping, url)
        except Exception as exc:
            log.warning("Keep-alive ke %s gagal: %s", url, exc)


def create_app(settings: Optional[Settings] = None, publisher: Optional[Publisher] = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    validator = TelemetryValidator(settings)

    bridge = None
    if publisher is None:
        if settings.mqtt_enabled:
            bridge = MqttBridge(settings)
            publisher = bridge
        else:
            publisher = NullPublisher()

    processor = Processor(settings, session_factory, validator, publisher)
    if bridge is not None:
        bridge.on_telemetry = lambda raw: processor.handle_telemetry(raw, source="mqtt")
        bridge.on_lamp_state = processor.handle_lamp_state
        bridge.on_command_request = processor.handle_command_request

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Base.metadata.create_all(engine)
        if bridge is not None:
            bridge.start()
        pinger = None
        if settings.keep_alive_target:
            log.info("Keep-alive aktif: ping %s tiap %g s", settings.keep_alive_target, settings.keep_alive_interval_s)
            pinger = asyncio.create_task(keep_alive(settings.keep_alive_target, settings.keep_alive_interval_s))
        yield
        if pinger is not None:
            pinger.cancel()
        if bridge is not None:
            bridge.stop()
        engine.dispose()

    app = FastAPI(title=settings.app_name, description=DESCRIPTION, version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.processor = processor

    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, exc: RequestValidationError):
        errors = translate_errors(exc.errors())
        return JSONResponse(
            status_code=422,
            content={"status": "rejected", "accepted": False, "errors": [e.model_dump() for e in errors]},
        )

    app.include_router(router)
    return app


app = create_app()
