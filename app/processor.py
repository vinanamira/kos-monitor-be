"""Alur data: terima -> validasi -> simpan -> teruskan ke dashboard.

Dipakai bersama oleh MQTT bridge (data dari ESP32) dan REST API (uji manual via Swagger),
sehingga aturan validasinya selalu sama.
"""

import json
import logging
from datetime import datetime
from typing import Optional, Protocol, Union

from sqlalchemy.orm import sessionmaker

from .config import LAMP_IDS, Settings
from .database import from_db_time, to_db_time, utc_now
from .models import LampEvent, RejectedMessage, TelemetryReading
from .schemas import CommandResult, Issue, ProcessResult
from .validation import TelemetryValidator, validate_switch_value

log = logging.getLogger("kosmonitor.processor")


class Publisher(Protocol):
    @property
    def connected(self) -> bool: ...

    def publish(self, topic: str, payload: str, retain: bool = False) -> bool: ...


class NullPublisher:
    """Dipakai saat MQTT dimatikan (MQTT_ENABLED=false)."""

    connected = False

    def publish(self, topic: str, payload: str, retain: bool = False) -> bool:
        return False


class CommandError(Exception):
    pass


def _as_text(raw: Union[bytes, str], limit: int) -> str:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    return text[:limit]


class Processor:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker,
        validator: TelemetryValidator,
        publisher: Publisher,
    ):
        self.s = settings
        self.session_factory = session_factory
        self.validator = validator
        self.publisher = publisher

    # ------------------------------------------------------------------ telemetry
    def handle_telemetry(
        self,
        raw: Union[bytes, str],
        *,
        source: str,
        topic: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> ProcessResult:
        now = now or utc_now()
        topic = topic or self.s.topic_telemetry_raw
        raw_text = _as_text(raw, self.s.max_payload_bytes)
        result = self.validator.validate(raw, source=source, now=now)

        if not result.accepted:
            rejection_id = self._reject(now, source, topic, "telemetry", raw_text, result.errors)
            return ProcessResult(
                status="rejected",
                accepted=False,
                id=rejection_id,
                received_at=from_db_time(to_db_time(now)),
                errors=result.errors,
            )

        data = result.payload.model_dump()
        with self.session_factory() as db:
            row = TelemetryReading(
                received_at=to_db_time(now),
                source=source,
                status=result.status,
                warnings=[w.model_dump() for w in result.warnings],
                raw_payload=raw_text,
                **data,
            )
            db.add(row)
            db.commit()

        received_at = from_db_time(row.received_at)
        clean = {k: v for k, v in data.items() if v is not None}
        self.publisher.publish(
            self.s.topic_telemetry_validated,
            json.dumps(
                {
                    **clean,
                    "id": row.id,
                    "status": result.status,
                    "warnings": [w.message for w in result.warnings],
                    "received_at": received_at.isoformat(),
                    "source": source,
                }
            ),
        )
        if result.warnings:
            log.info("Telemetry #%s lolos dengan peringatan: %s", row.id, [w.message for w in result.warnings])

        return ProcessResult(
            status=result.status,
            accepted=True,
            id=row.id,
            received_at=received_at,
            warnings=result.warnings,
            data=clean,
        )

    # ------------------------------------------------------------------ lampu
    def handle_lamp_state(self, lamp: int, raw: Union[bytes, str], *, source: str = "mqtt") -> Optional[str]:
        now = utc_now()
        state, errors = validate_switch_value(raw, f"led{lamp}")
        if errors:
            self._reject(now, source, self.s.topic_lamp_state_raw(lamp), "lamp_state", _as_text(raw, 200), errors)
            return None

        self._log_lamp_event(now, lamp, "state", state, source)
        self.publisher.publish(self.s.topic_lamp_state_validated(lamp), state, retain=True)
        return state

    def handle_command_request(self, lamp: int, raw: Union[bytes, str]) -> Optional[CommandResult]:
        """Perintah lampu dari dashboard lewat topic request/ledN/cmd."""
        action, errors = validate_switch_value(raw, f"led{lamp}_cmd")
        if errors:
            self._reject(
                utc_now(), "mqtt", self.s.topic_lamp_cmd_request(lamp), "lamp_command", _as_text(raw, 200), errors
            )
            return None
        try:
            return self.send_lamp_command(lamp, action, source="mqtt")
        except CommandError as exc:
            log.warning("Perintah lampu %s gagal: %s", lamp, exc)
            return None

    def send_lamp_command(self, lamp: int, action: str, *, source: str) -> CommandResult:
        if lamp not in LAMP_IDS:
            raise CommandError(f"lampu {lamp} tidak ada")
        if not self.publisher.connected:
            raise CommandError("MQTT belum terhubung; perintah tidak dikirim")

        topic = self.s.topic_lamp_cmd(lamp)
        if not self.publisher.publish(topic, action):
            raise CommandError("publish ke broker MQTT gagal")

        now = utc_now()
        self._log_lamp_event(now, lamp, "command", action, source)
        return CommandResult(lamp=lamp, action=action, topic=topic, sent_at=from_db_time(to_db_time(now)))

    # ------------------------------------------------------------------ helper
    def _reject(
        self, now: datetime, source: str, topic: str, kind: str, raw_text: str, errors: list[Issue]
    ) -> int:
        with self.session_factory() as db:
            row = RejectedMessage(
                received_at=to_db_time(now),
                source=source,
                kind=kind,
                topic=topic,
                raw_payload=raw_text,
                errors=[e.model_dump() for e in errors],
            )
            db.add(row)
            db.commit()

        messages = [f"{e.field}: {e.message}" for e in errors]
        log.info("Pesan #%s dari %s DITOLAK: %s", row.id, topic, messages)
        self.publisher.publish(
            self.s.topic_rejected,
            json.dumps(
                {
                    "id": row.id,
                    "kind": kind,
                    "topic": topic,
                    "source": source,
                    "received_at": from_db_time(row.received_at).isoformat(),
                    "errors": messages,
                    "raw": raw_text[:200],
                }
            ),
        )
        return row.id

    def _log_lamp_event(self, now: datetime, lamp: int, kind: str, state: str, source: str) -> None:
        with self.session_factory() as db:
            db.add(LampEvent(received_at=to_db_time(now), lamp=lamp, kind=kind, state=state, source=source))
            db.commit()
