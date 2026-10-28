"""Validasi data IoT sebelum boleh tampil di dashboard.

Telemetry melewati lapisan berikut secara berurutan:

  1. Ukuran & encoding   payload tidak kosong, <= MAX_PAYLOAD_BYTES, UTF-8
  2. Format (schema)     JSON objek, field wajib ada, tipe angka, bukan NaN/Infinity
  3. Rentang fisik       nilai masuk akal untuk sensor & instalasi kost
  4. Konsistensi         P = V x I x PF (atau P <= V x I bila PF tidak dikirim)
  5. Laju pesan          tolak banjir pesan dari sumber yang sama
  6. Peringatan          tegangan di luar toleransi PLN, beban melebihi daya kontrak,
                         lonjakan daya -> data tetap lolos tapi ditandai

Lapisan 1-5 menghasilkan `errors` (data DITOLAK, tidak diteruskan ke dashboard).
Lapisan 6 menghasilkan `warnings` (data LOLOS dengan status "warning").
"""

from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Optional, Sequence, Union

from pydantic import ValidationError

from .config import Settings
from .schemas import Issue, Status, TelemetryPayload

_PYDANTIC_MESSAGES = {
    "missing": "wajib ada",
    "float_type": "harus berupa angka",
    "int_type": "harus berupa bilangan bulat",
    "int_from_float": "harus berupa bilangan bulat",
    "int_parsing": "harus berupa bilangan bulat",
    "finite_number": "harus angka terhingga (bukan NaN/Infinity)",
    "literal_error": "harus bernilai 'ON' atau 'OFF'",
    "string_type": "harus berupa teks",
    "model_type": "payload harus berupa objek JSON",
    "model_attributes_type": "payload harus berupa objek JSON",
    "dict_type": "harus berupa objek JSON",
    "json_invalid": "payload bukan JSON yang valid",
    "json_type": "payload bukan JSON yang valid",
}

_BOUND_MESSAGES = {
    "greater_than_equal": ("ge", "≥"),
    "greater_than": ("gt", ">"),
    "less_than_equal": ("le", "≤"),
    "less_than": ("lt", "<"),
}


def translate_errors(errors: Sequence[dict]) -> list[Issue]:
    """Ubah error Pydantic/FastAPI jadi daftar Issue berbahasa Indonesia."""
    issues = []
    for err in errors:
        loc = [str(part) for part in err.get("loc", ()) if part not in ("body", "path", "query")]
        code = err["type"]
        message = _PYDANTIC_MESSAGES.get(code, err["msg"])
        if code in _BOUND_MESSAGES and "ctx" in err:
            key, symbol = _BOUND_MESSAGES[code]
            message = f"harus {symbol} {err['ctx'][key]}"
        issues.append(Issue(field=".".join(loc) or "payload", code=code, message=message))
    return issues


def _fmt(value: float, unit: str = "") -> str:
    text = f"{value:g}"
    return f"{text} {unit}" if unit else text


@dataclass
class ValidationResult:
    status: Status
    payload: Optional[TelemetryPayload] = None
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.status != "rejected"


class TelemetryValidator:
    """Menyimpan sedikit state per sumber (waktu & daya terakhir yang diterima)
    untuk cek laju pesan dan deteksi lonjakan."""

    def __init__(self, settings: Settings):
        self.s = settings
        self._lock = Lock()
        self._last: dict[str, tuple[datetime, float]] = {}

    def reset(self) -> None:
        with self._lock:
            self._last.clear()

    def validate(self, raw: Union[bytes, str], *, source: str, now: datetime) -> ValidationResult:
        # Lapisan 1: ukuran & encoding
        raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
        if not raw_bytes.strip():
            return self._reject("payload", "empty", "payload kosong")
        if len(raw_bytes) > self.s.max_payload_bytes:
            return self._reject(
                "payload",
                "payload_too_large",
                f"ukuran payload {len(raw_bytes)} byte melebihi batas {self.s.max_payload_bytes} byte",
            )
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return self._reject("payload", "encoding", "payload bukan teks UTF-8 yang valid")

        # Lapisan 2: format / schema
        try:
            payload = TelemetryPayload.model_validate_json(text)
        except ValidationError as exc:
            return ValidationResult(status="rejected", errors=translate_errors(exc.errors()))

        # Lapisan 3 & 4: rentang fisik dan konsistensi antar-field
        errors = self._check_ranges(payload)
        if not errors:
            errors = self._check_consistency(payload)
        if errors:
            return ValidationResult(status="rejected", errors=errors)

        with self._lock:
            last = self._last.get(source)

            # Lapisan 5: laju pesan
            if last is not None:
                elapsed = (now - last[0]).total_seconds()
                if elapsed < self.s.min_interval_s:
                    return self._reject(
                        "payload",
                        "rate_limited",
                        f"pesan terlalu cepat ({elapsed:.2f} s sejak data terakhir, "
                        f"minimal {_fmt(self.s.min_interval_s)} s)",
                    )

            # Lapisan 6: peringatan
            warnings = self._check_warnings(payload, previous_power=last[1] if last else None)
            self._last[source] = (now, payload.power_w)

        return ValidationResult(
            status="warning" if warnings else "valid",
            payload=payload,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    def _check_ranges(self, p: TelemetryPayload) -> list[Issue]:
        s = self.s
        rules = [
            ("voltage_v", p.voltage_v, s.voltage_min_v, s.voltage_max_v, "V"),
            ("current_a", p.current_a, 0, s.current_max_a, "A"),
            ("power_w", p.power_w, 0, s.power_max_w, "W"),
            ("power_factor", p.power_factor, 0, 1, ""),
            ("load_percent", p.load_percent, 0, 100, "%"),
            ("adc", p.adc, 0, s.adc_max, ""),
        ]
        issues = []
        for name, value, low, high, unit in rules:
            if value is not None and not (low <= value <= high):
                issues.append(
                    Issue(
                        field=name,
                        code="out_of_range",
                        message=f"{_fmt(value, unit)} di luar rentang {_fmt(low)}–{_fmt(high, unit)}",
                    )
                )
        return issues

    def _check_consistency(self, p: TelemetryPayload) -> list[Issue]:
        s = self.s
        apparent = p.voltage_v * p.current_a

        def tolerance(expected: float) -> float:
            return max(expected * s.power_tolerance_pct / 100, s.power_tolerance_abs_w)

        if p.power_factor is not None:
            expected = apparent * p.power_factor
            if abs(p.power_w - expected) > tolerance(expected):
                return [
                    Issue(
                        field="power_w",
                        code="inconsistent",
                        message=(
                            f"daya {_fmt(p.power_w, 'W')} tidak sesuai V×I×PF = {expected:.2f} W "
                            f"(toleransi ±{_fmt(s.power_tolerance_pct)}%)"
                        ),
                    )
                ]
        elif p.power_w > apparent + tolerance(apparent):
            return [
                Issue(
                    field="power_w",
                    code="inconsistent",
                    message=f"daya nyata {_fmt(p.power_w, 'W')} melebihi daya semu V×I = {apparent:.2f} VA",
                )
            ]
        return []

    def _check_warnings(self, p: TelemetryPayload, previous_power: Optional[float]) -> list[Issue]:
        s = self.s
        warnings = []
        if not (s.voltage_nominal_min_v <= p.voltage_v <= s.voltage_nominal_max_v):
            warnings.append(
                Issue(
                    field="voltage_v",
                    code="voltage_out_of_tolerance",
                    message=(
                        f"tegangan {_fmt(p.voltage_v, 'V')} di luar toleransi PLN "
                        f"{_fmt(s.voltage_nominal_min_v)}–{_fmt(s.voltage_nominal_max_v, 'V')}"
                    ),
                )
            )
        apparent = p.voltage_v * p.current_a
        if apparent > s.daya_kontrak_va:
            warnings.append(
                Issue(
                    field="current_a",
                    code="over_contract_power",
                    message=(
                        f"beban {apparent:.0f} VA melebihi daya kontrak "
                        f"{_fmt(s.daya_kontrak_va, 'VA')} (MCB berisiko trip)"
                    ),
                )
            )
        if previous_power is not None:
            delta = p.power_w - previous_power
            if abs(delta) >= s.spike_power_delta_w:
                warnings.append(
                    Issue(
                        field="power_w",
                        code="power_spike",
                        message=f"lonjakan daya {delta:+.0f} W dibanding data sebelumnya",
                    )
                )
        return warnings

    @staticmethod
    def _reject(field_name: str, code: str, message: str) -> ValidationResult:
        return ValidationResult(status="rejected", errors=[Issue(field=field_name, code=code, message=message)])


def validate_switch_value(raw: Union[bytes, str], field_name: str) -> tuple[Optional[str], list[Issue]]:
    """Status / perintah lampu: hanya 'ON' atau 'OFF' (tidak peka huruf besar-kecil)."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError:
        return None, [Issue(field=field_name, code="encoding", message="payload bukan teks UTF-8 yang valid")]
    value = text.strip().upper()
    if value in ("ON", "OFF"):
        return value, []
    shown = text.strip()[:30]
    return None, [
        Issue(field=field_name, code="literal_error", message=f"nilai '{shown}' tidak dikenal, harus 'ON' atau 'OFF'")
    ]
