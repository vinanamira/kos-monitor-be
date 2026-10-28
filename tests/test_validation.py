import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.validation import TelemetryValidator, validate_switch_value

from .conftest import VALID

T0 = datetime(2026, 10, 28, 5, 0, tzinfo=timezone.utc)


@pytest.fixture
def validator():
    return TelemetryValidator(Settings(_env_file=None, mqtt_enabled=False))


def run(validator, payload, now=T0, source="test"):
    raw = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    return validator.validate(raw, source=source, now=now)


def error_codes(result):
    return [(e.field, e.code) for e in result.errors]


def warning_codes(result):
    return [w.code for w in result.warnings]


# --------------------------------------------------------------------- lolos
def test_valid_payload_accepted(validator):
    result = run(validator, VALID)
    assert result.status == "valid"
    assert result.errors == [] and result.warnings == []
    assert result.payload.power_w == 247.95


def test_minimal_payload_accepted(validator):
    assert run(validator, {"voltage_v": 220, "current_a": 0, "power_w": 0}).status == "valid"


def test_unknown_fields_ignored(validator):
    result = run(validator, {**VALID, "uptime_s": 1234})
    assert result.status == "valid"
    assert "uptime_s" not in result.payload.model_dump()


def test_lamp_fields_normalized(validator):
    result = run(validator, {**VALID, "led1": " on ", "led2": "Off"})
    assert (result.payload.led1, result.payload.led2) == ("ON", "OFF")


# --------------------------------------------------------------------- lapisan 1 & 2: format
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", [("payload", "empty")]),
        ("   ", [("payload", "empty")]),
        ("bukan json", [("payload", "json_invalid")]),
        ("[1, 2]", [("payload", "model_type")]),
        ("220", [("payload", "model_type")]),
        (b"\xff\xfe\x00", [("payload", "encoding")]),
    ],
)
def test_malformed_payload_rejected(validator, raw, expected):
    result = run(validator, raw)
    assert result.status == "rejected"
    assert error_codes(result) == expected


def test_oversized_payload_rejected(validator):
    result = run(validator, {**VALID, "padding": "x" * 3000})
    assert error_codes(result) == [("payload", "payload_too_large")]


def test_missing_required_field_rejected(validator):
    payload = {k: v for k, v in VALID.items() if k != "current_a"}
    assert error_codes(run(validator, payload)) == [("current_a", "missing")]


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"voltage_v": "220"}, ("voltage_v", "float_type")),
        ({"voltage_v": None}, ("voltage_v", "float_type")),
        ({"current_a": True}, ("current_a", "float_type")),
        ({"adc": 1708.5}, ("adc", "int_type")),
        ({"adc": "1708"}, ("adc", "int_type")),
        ({"led1": "KEDIP"}, ("led1", "literal_error")),
    ],
)
def test_wrong_type_rejected(validator, override, expected):
    result = run(validator, {**VALID, **override})
    assert result.status == "rejected"
    assert error_codes(result) == [expected]


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_non_finite_number_rejected(validator, literal):
    raw = f'{{"voltage_v": {literal}, "current_a": 1, "power_w": 200}}'
    assert error_codes(run(validator, raw)) == [("voltage_v", "finite_number")]


# --------------------------------------------------------------------- lapisan 3: rentang fisik
@pytest.mark.parametrize(
    "override, field",
    [
        ({"voltage_v": 999}, "voltage_v"),
        ({"voltage_v": -5}, "voltage_v"),
        ({"current_a": 45}, "current_a"),
        ({"current_a": -0.1}, "current_a"),
        ({"power_w": -1}, "power_w"),
        ({"power_w": 12000}, "power_w"),
        ({"power_factor": 1.7}, "power_factor"),
        ({"power_factor": -0.2}, "power_factor"),
        ({"load_percent": 120}, "load_percent"),
        ({"adc": 5000}, "adc"),
        ({"adc": -1}, "adc"),
    ],
)
def test_out_of_range_rejected(validator, override, field):
    result = run(validator, {**VALID, **override})
    assert error_codes(result) == [(field, "out_of_range")]


def test_all_range_errors_reported_together(validator):
    result = run(validator, {**VALID, "voltage_v": 999, "power_factor": 1.7})
    assert error_codes(result) == [("voltage_v", "out_of_range"), ("power_factor", "out_of_range")]
    assert "999 V di luar rentang 0–300 V" == result.errors[0].message


# --------------------------------------------------------------------- lapisan 4: konsistensi
def test_inconsistent_power_rejected(validator):
    result = run(validator, {**VALID, "power_w": 900})
    assert error_codes(result) == [("power_w", "inconsistent")]


def test_power_within_tolerance_accepted(validator):
    # pembulatan firmware masih dalam toleransi 5%
    assert run(validator, {**VALID, "power_w": round(247.95 * 1.04, 2)}).status == "valid"


def test_small_power_uses_absolute_tolerance(validator):
    # V×I×PF = 0.396 W; selisih 0.5 W masih di bawah toleransi minimum 1 W
    payload = {"voltage_v": 220, "current_a": 0.002, "power_w": 0.9, "power_factor": 0.9}
    assert run(validator, payload).status == "valid"


def test_real_power_above_apparent_power_rejected_without_pf(validator):
    payload = {"voltage_v": 220, "current_a": 1, "power_w": 300}
    assert error_codes(run(validator, payload)) == [("power_w", "inconsistent")]


def test_real_power_below_apparent_power_accepted_without_pf(validator):
    assert run(validator, {"voltage_v": 220, "current_a": 1, "power_w": 200}).status == "valid"


# --------------------------------------------------------------------- lapisan 5: laju pesan
def test_flood_rejected_then_recovers(validator):
    assert run(validator, VALID, now=T0).accepted
    flood = run(validator, VALID, now=T0 + timedelta(seconds=0.1))
    assert error_codes(flood) == [("payload", "rate_limited")]
    assert run(validator, VALID, now=T0 + timedelta(seconds=0.3)).accepted


def test_rate_limit_is_per_source(validator):
    assert run(validator, VALID, source="esp32-a").accepted
    assert run(validator, VALID, source="esp32-b").accepted


def test_rejected_message_does_not_reset_rate_window(validator):
    assert run(validator, VALID, now=T0).accepted
    assert not run(validator, {**VALID, "voltage_v": 999}, now=T0 + timedelta(seconds=0.15)).accepted
    assert run(validator, VALID, now=T0 + timedelta(seconds=0.25)).accepted


# --------------------------------------------------------------------- lapisan 6: peringatan
def test_low_voltage_passes_with_warning(validator):
    result = run(validator, {"voltage_v": 190, "current_a": 1, "power_w": 171, "power_factor": 0.9})
    assert result.status == "warning"
    assert warning_codes(result) == ["voltage_out_of_tolerance"]


def test_over_contract_power_passes_with_warning(validator):
    result = run(validator, {"voltage_v": 220, "current_a": 7, "power_w": 1463, "power_factor": 0.95})
    assert result.status == "warning"
    assert warning_codes(result) == ["over_contract_power"]


def test_power_spike_passes_with_warning(validator):
    run(validator, {"voltage_v": 220, "current_a": 0, "power_w": 0}, now=T0)
    result = run(validator, {"voltage_v": 220, "current_a": 5.5, "power_w": 1210}, now=T0 + timedelta(seconds=1))
    assert result.status == "warning"
    assert warning_codes(result) == ["power_spike"]


# --------------------------------------------------------------------- status / perintah lampu
@pytest.mark.parametrize("raw, expected", [("ON", "ON"), ("off", "OFF"), (" On \n", "ON"), (b"OFF", "OFF")])
def test_switch_value_accepted(raw, expected):
    assert validate_switch_value(raw, "led1") == (expected, [])


@pytest.mark.parametrize("raw", ["", "1", "NYALA", b"\xff"])
def test_switch_value_rejected(raw):
    value, errors = validate_switch_value(raw, "led1")
    assert value is None
    assert len(errors) == 1
