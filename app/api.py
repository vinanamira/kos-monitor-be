from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import LAMP_IDS, WIB, Settings
from .database import from_db_time, to_db_time, utc_now
from .models import LampEvent, RejectedMessage, TelemetryReading
from .processor import CommandError, Processor
from .schemas import (
    CommandResult,
    LampCommandIn,
    LampStateOut,
    ProcessResult,
    RejectedOut,
    TelemetryOut,
    TelemetryPayload,
)

router = APIRouter()


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        yield db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_processor(request: Request) -> Processor:
    return request.app.state.processor


def _telemetry_out(row: TelemetryReading) -> TelemetryOut:
    out = TelemetryOut.model_validate(row)
    return out.model_copy(update={"received_at": from_db_time(row.received_at)})


def _day_range(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=WIB)
    return to_db_time(start), to_db_time(start + timedelta(days=1))


# ---------------------------------------------------------------------- sistem
@router.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@router.get("/health", tags=["sistem"], summary="Status backend, MQTT, dan perangkat")
def health(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    processor: Processor = Depends(get_processor),
):
    if not settings.mqtt_enabled:
        mqtt_status = "disabled"
    else:
        mqtt_status = "connected" if processor.publisher.connected else "disconnected"

    # Hanya data dari perangkat (MQTT); data uji lewat HTTP tidak dihitung.
    last = db.scalar(select(func.max(TelemetryReading.received_at)).where(TelemetryReading.source == "mqtt"))
    device, age = "unknown", None
    if last is not None:
        age = round((utc_now() - from_db_time(last)).total_seconds(), 1)
        device = "online" if age <= settings.device_offline_after_s else "offline"

    return {
        "status": "ok",
        "mqtt": mqtt_status,
        "broker": f"{settings.mqtt_broker}:{settings.mqtt_port}",
        "device": device,
        "last_telemetry_at": from_db_time(last).isoformat() if last else None,
        "last_telemetry_age_s": age,
        "topics": settings.topic_map(),
    }


# ---------------------------------------------------------------------- telemetry
_EXAMPLES = {
    "valid": {
        "summary": "Data valid",
        "value": {"voltage_v": 220.4, "current_a": 1.25, "power_w": 247.95, "power_factor": 0.9, "load_percent": 41.7, "adc": 1708},
    },
    "peringatan": {
        "summary": "Lolos dengan peringatan (tegangan drop + melebihi daya kontrak)",
        "value": {"voltage_v": 190, "current_a": 7.5, "power_w": 1353.75, "power_factor": 0.95},
    },
    "di_luar_rentang": {
        "summary": "Ditolak: tegangan & faktor daya mustahil",
        "value": {"voltage_v": 999, "current_a": 1.2, "power_w": 250, "power_factor": 1.7},
    },
    "tidak_konsisten": {
        "summary": "Ditolak: daya tidak cocok dengan V×I×PF",
        "value": {"voltage_v": 220, "current_a": 1.25, "power_w": 900, "power_factor": 0.9},
    },
    "tipe_salah": {
        "summary": "Ditolak: angka dikirim sebagai teks, field wajib hilang",
        "value": {"voltage_v": "220", "power_w": 250},
    },
}


@router.post(
    "/api/telemetry",
    tags=["telemetry"],
    summary="Kirim telemetry lewat HTTP (uji validasi tanpa ESP32)",
    description=(
        "Melewati aturan validasi yang **sama persis** dengan data dari MQTT. "
        "Data valid disimpan dan diteruskan ke dashboard; data tidak valid ditolak (422) beserta alasannya."
    ),
    status_code=201,
    response_model=ProcessResult,
    responses={422: {"model": ProcessResult, "description": "Data ditolak validasi"}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": TelemetryPayload.model_json_schema(), "examples": _EXAMPLES}
            },
        }
    },
)
async def ingest_telemetry(request: Request, processor: Processor = Depends(get_processor)):
    raw = await request.body()
    result = await run_in_threadpool(processor.handle_telemetry, raw, source="http", topic="POST /api/telemetry")
    return JSONResponse(status_code=201 if result.accepted else 422, content=result.model_dump(mode="json"))


@router.get("/api/telemetry/latest", tags=["telemetry"], response_model=TelemetryOut, summary="Data valid terakhir")
def latest_telemetry(db: Session = Depends(get_db)):
    row = db.scalars(select(TelemetryReading).order_by(TelemetryReading.id.desc()).limit(1)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Belum ada telemetry yang lolos validasi")
    return _telemetry_out(row)


@router.get("/api/telemetry", tags=["telemetry"], response_model=list[TelemetryOut], summary="Riwayat data valid (terbaru dulu)")
def list_telemetry(
    limit: int = Query(100, ge=1, le=1000),
    status: Optional[Literal["valid", "warning"]] = Query(None, description="Filter status"),
    db: Session = Depends(get_db),
):
    query = select(TelemetryReading).order_by(TelemetryReading.id.desc()).limit(limit)
    if status:
        query = query.where(TelemetryReading.status == status)
    return [_telemetry_out(row) for row in db.scalars(query)]


@router.get("/api/telemetry/summary", tags=["telemetry"], summary="Total energi (kWh) & estimasi biaya per hari")
def telemetry_summary(
    day: Optional[date] = Query(None, alias="date", description="Tanggal WIB (YYYY-MM-DD), default hari ini"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    day = day or datetime.now(WIB).date()
    start, end = _day_range(day)
    rows = db.execute(
        select(TelemetryReading.received_at, TelemetryReading.power_w)
        .where(TelemetryReading.received_at >= start, TelemetryReading.received_at < end)
        .order_by(TelemetryReading.received_at)
    ).all()

    # Integrasi trapesium daya terhadap waktu. Celah panjang (perangkat offline) dilewati.
    energy_wh = 0.0
    for (t0, p0), (t1, p1) in zip(rows, rows[1:]):
        dt = (t1 - t0).total_seconds()
        if 0 < dt <= settings.energy_max_gap_s:
            energy_wh += (p0 + p1) / 2 * dt / 3600

    powers = [row.power_w for row in rows]
    rejected = db.scalar(
        select(func.count())
        .select_from(RejectedMessage)
        .where(RejectedMessage.kind == "telemetry", RejectedMessage.received_at >= start, RejectedMessage.received_at < end)
    )
    energy_kwh = energy_wh / 1000
    return {
        "date": day.isoformat(),
        "readings": len(rows),
        "rejected": rejected,
        "energy_kwh": round(energy_kwh, 4),
        "cost_rp": round(energy_kwh * settings.tarif_per_kwh),
        "tarif_per_kwh": settings.tarif_per_kwh,
        "avg_power_w": round(sum(powers) / len(powers), 2) if powers else None,
        "max_power_w": max(powers) if powers else None,
    }


# ---------------------------------------------------------------------- validasi
@router.get("/api/rejections", tags=["validasi"], response_model=list[RejectedOut], summary="Log data yang ditolak (terbaru dulu)")
def list_rejections(
    limit: int = Query(50, ge=1, le=500),
    kind: Optional[Literal["telemetry", "lamp_state", "lamp_command"]] = Query(None),
    db: Session = Depends(get_db),
):
    query = select(RejectedMessage).order_by(RejectedMessage.id.desc()).limit(limit)
    if kind:
        query = query.where(RejectedMessage.kind == kind)
    rows = db.scalars(query).all()
    return [RejectedOut.model_validate(r).model_copy(update={"received_at": from_db_time(r.received_at)}) for r in rows]


@router.get("/api/validation/stats", tags=["validasi"], summary="Ringkasan hasil validasi telemetry")
def validation_stats(db: Session = Depends(get_db)):
    by_status = dict(
        db.execute(select(TelemetryReading.status, func.count()).group_by(TelemetryReading.status)).all()
    )
    rejected_errors = db.scalars(select(RejectedMessage.errors).where(RejectedMessage.kind == "telemetry")).all()
    by_code = Counter(err["code"] for errors in rejected_errors for err in errors)

    valid, warning, rejected = by_status.get("valid", 0), by_status.get("warning", 0), len(rejected_errors)
    total = valid + warning + rejected
    return {
        "total": total,
        "valid": valid,
        "warning": warning,
        "rejected": rejected,
        "acceptance_rate_pct": round((valid + warning) / total * 100, 1) if total else None,
        "rejected_by_code": dict(by_code.most_common()),
    }


@router.get("/api/validation/rules", tags=["validasi"], summary="Aturan validasi yang sedang berlaku")
def validation_rules(settings: Settings = Depends(get_settings)):
    s = settings
    return {
        "layers": [
            {
                "layer": 1,
                "name": "Ukuran & encoding",
                "action": "tolak",
                "rules": [f"payload tidak kosong, maksimal {s.max_payload_bytes} byte", "teks UTF-8"],
            },
            {
                "layer": 2,
                "name": "Format (schema)",
                "action": "tolak",
                "rules": [
                    "objek JSON",
                    "wajib: voltage_v, current_a, power_w",
                    "opsional: power_factor, load_percent, adc, led1, led2",
                    "angka harus bertipe number (bukan string), bukan NaN/Infinity",
                    "adc bilangan bulat; led1/led2 bernilai ON/OFF",
                ],
            },
            {
                "layer": 3,
                "name": "Rentang fisik",
                "action": "tolak",
                "rules": [
                    f"voltage_v {s.voltage_min_v:g}–{s.voltage_max_v:g} V",
                    f"current_a 0–{s.current_max_a:g} A",
                    f"power_w 0–{s.power_max_w:g} W",
                    "power_factor 0–1",
                    "load_percent 0–100 %",
                    f"adc 0–{s.adc_max}",
                ],
            },
            {
                "layer": 4,
                "name": "Konsistensi antar-field",
                "action": "tolak",
                "rules": [
                    f"power_w = V×I×PF (toleransi ±{s.power_tolerance_pct:g}%, minimal ±{s.power_tolerance_abs_w:g} W)",
                    "tanpa power_factor: power_w ≤ V×I",
                ],
            },
            {
                "layer": 5,
                "name": "Laju pesan",
                "action": "tolak",
                "rules": [f"jarak antar pesan dari sumber yang sama minimal {s.min_interval_s:g} s"],
            },
            {
                "layer": 6,
                "name": "Peringatan",
                "action": "lolos, ditandai status 'warning'",
                "rules": [
                    f"tegangan di luar toleransi PLN {s.voltage_nominal_min_v:g}–{s.voltage_nominal_max_v:g} V",
                    f"beban V×I melebihi daya kontrak {s.daya_kontrak_va:g} VA",
                    f"perubahan daya ≥ {s.spike_power_delta_w:g} W dibanding data sebelumnya",
                ],
            },
        ],
        "lamp": ["status & perintah lampu hanya 'ON' atau 'OFF' (tidak peka huruf besar-kecil)"],
    }


# ---------------------------------------------------------------------- lampu
@router.get("/api/lamps", tags=["lampu"], response_model=list[LampStateOut], summary="Status lampu terakhir")
def list_lamps(db: Session = Depends(get_db)):
    result = []
    for n in LAMP_IDS:
        candidates = []
        event = db.scalars(
            select(LampEvent).where(LampEvent.lamp == n, LampEvent.kind == "state").order_by(LampEvent.id.desc()).limit(1)
        ).first()
        if event:
            candidates.append((event.received_at, event.state))

        column = getattr(TelemetryReading, f"led{n}")
        from_telemetry = db.execute(
            select(TelemetryReading.received_at, column).where(column.is_not(None)).order_by(TelemetryReading.id.desc()).limit(1)
        ).first()
        if from_telemetry:
            candidates.append((from_telemetry[0], from_telemetry[1]))

        if candidates:
            updated_at, state = max(candidates)
            result.append(LampStateOut(lamp=n, state=state, updated_at=from_db_time(updated_at)))
        else:
            result.append(LampStateOut(lamp=n, state=None, updated_at=None))
    return result


@router.post(
    "/api/lamps/{lamp}/command",
    tags=["lampu"],
    status_code=202,
    response_model=CommandResult,
    responses={503: {"description": "MQTT belum terhubung"}},
    summary="Kirim perintah ON/OFF ke lampu (tervalidasi)",
)
async def lamp_command(
    body: LampCommandIn,
    lamp: int = Path(ge=min(LAMP_IDS), le=max(LAMP_IDS)),
    processor: Processor = Depends(get_processor),
):
    try:
        return await run_in_threadpool(processor.send_lamp_command, lamp, body.action, source="http")
    except CommandError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
