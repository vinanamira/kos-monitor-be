from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

SwitchState = Literal["ON", "OFF"]
Status = Literal["valid", "warning", "rejected"]


def _normalize_switch(value):
    return value.strip().upper() if isinstance(value, str) else value


class TelemetryPayload(BaseModel):
    """Kontrak data telemetry dari ESP32.

    strict=True: angka harus dikirim sebagai angka JSON ("220" string ditolak).
    allow_inf_nan=False: NaN / Infinity ditolak.
    Field yang tidak dikenal diabaikan.
    """

    model_config = ConfigDict(strict=True, extra="ignore")

    voltage_v: float = Field(allow_inf_nan=False, description="Tegangan (V)")
    current_a: float = Field(allow_inf_nan=False, description="Arus (A)")
    power_w: float = Field(allow_inf_nan=False, description="Daya nyata (W)")
    power_factor: Optional[float] = Field(default=None, allow_inf_nan=False, description="Faktor daya 0-1")
    load_percent: Optional[float] = Field(default=None, allow_inf_nan=False, description="Beban (%)")
    adc: Optional[int] = Field(default=None, description="Nilai ADC mentah (bilangan bulat)")
    led1: Optional[SwitchState] = None
    led2: Optional[SwitchState] = None

    @field_validator("led1", "led2", mode="before")
    @classmethod
    def _normalize_leds(cls, value):
        return _normalize_switch(value)


class LampCommandIn(BaseModel):
    model_config = ConfigDict(strict=True)

    action: SwitchState = Field(description="ON atau OFF")

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, value):
        return _normalize_switch(value)


class Issue(BaseModel):
    field: str
    code: str
    message: str


class ProcessResult(BaseModel):
    status: Status
    accepted: bool
    id: Optional[int] = Field(description="ID data tersimpan (telemetry atau penolakan)")
    received_at: datetime
    errors: list[Issue] = []
    warnings: list[Issue] = []
    data: Optional[dict] = None


class TelemetryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    received_at: datetime
    source: str
    status: Status
    voltage_v: float
    current_a: float
    power_w: float
    power_factor: Optional[float]
    load_percent: Optional[float]
    adc: Optional[int]
    led1: Optional[str]
    led2: Optional[str]
    warnings: list[Issue]


class RejectedOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    received_at: datetime
    source: str
    kind: str
    topic: str
    raw_payload: str
    errors: list[Issue]


class LampStateOut(BaseModel):
    lamp: int
    state: Optional[str]
    updated_at: Optional[datetime]


class CommandResult(BaseModel):
    lamp: int
    action: SwitchState
    topic: str
    sent_at: datetime
