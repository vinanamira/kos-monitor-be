from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class TelemetryReading(Base):
    """Telemetry yang LOLOS validasi (status valid / warning)."""

    __tablename__ = "telemetry_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    source: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), index=True)
    voltage_v: Mapped[float] = mapped_column(Float)
    current_a: Mapped[float] = mapped_column(Float)
    power_w: Mapped[float] = mapped_column(Float)
    power_factor: Mapped[Optional[float]] = mapped_column(Float)
    load_percent: Mapped[Optional[float]] = mapped_column(Float)
    adc: Mapped[Optional[int]] = mapped_column(Integer)
    led1: Mapped[Optional[str]] = mapped_column(String(3))
    led2: Mapped[Optional[str]] = mapped_column(String(3))
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    raw_payload: Mapped[str] = mapped_column(Text)


class RejectedMessage(Base):
    """Pesan yang DITOLAK beserta alasannya (jejak audit)."""

    __tablename__ = "rejected_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    source: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16), index=True)  # "telemetry" | "lamp_state" | "lamp_command"
    topic: Mapped[str] = mapped_column(String(255))
    raw_payload: Mapped[str] = mapped_column(Text)
    errors: Mapped[list] = mapped_column(JSON)


class LampEvent(Base):
    """Riwayat status lampu (dari ESP32) dan perintah lampu (ke ESP32)."""

    __tablename__ = "lamp_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    lamp: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # "state" | "command"
    state: Mapped[str] = mapped_column(String(3))
    source: Mapped[str] = mapped_column(String(16))
