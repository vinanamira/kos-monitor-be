from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

WIB = ZoneInfo("Asia/Jakarta")
LAMP_IDS = (1, 2)


class Settings(BaseSettings):
    """Semua konfigurasi bisa ditimpa lewat file .env atau environment variable."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "KosMonitor Backend"
    database_url: str = "sqlite:///./kosmonitor.db"

    # --- MQTT ---
    mqtt_enabled: bool = True
    mqtt_broker: str = "broker.emqx.io"
    mqtt_port: int = 8083
    mqtt_transport: str = "websockets"  # "websockets" atau "tcp"
    mqtt_ws_path: str = "/mqtt"
    topic_prefix: str = "kosdemo/icha-kos"

    # --- Batas keras: di luar ini data DITOLAK ---
    max_payload_bytes: int = 2048
    voltage_min_v: float = 0
    voltage_max_v: float = 300
    current_max_a: float = 30  # rating sensor ACS712-30A / SCT-013-030
    power_max_w: float = 9000
    adc_max: int = 4095  # ADC ESP32 12-bit
    power_tolerance_pct: float = 5.0  # toleransi cek P = V x I x PF
    power_tolerance_abs_w: float = 1.0  # toleransi minimum untuk daya kecil
    min_interval_s: float = 0.2  # jarak minimum antar pesan dari satu sumber

    # --- Batas lunak: data LOLOS tapi diberi peringatan ---
    voltage_nominal_min_v: float = 198  # 220 V -10%
    voltage_nominal_max_v: float = 231  # 220 V +5%
    daya_kontrak_va: float = 1300
    spike_power_delta_w: float = 1000

    # --- Energi & biaya ---
    tarif_per_kwh: float = 1444.70  # tarif PLN R-1 1.300 VA
    energy_max_gap_s: float = 60  # celah lebih lama dianggap perangkat offline
    device_offline_after_s: float = 30

    # --- Keep-alive ---
    # Hosting gratis (mis. Render) mematikan server setelah 15 menit tanpa request,
    # padahal listener MQTT harus jalan terus. Backend lalu ping /health miliknya sendiri.
    keep_alive_enabled: bool = True
    keep_alive_url: Optional[str] = None
    render_external_url: Optional[str] = None  # diisi otomatis oleh Render
    keep_alive_interval_s: float = 600

    @property
    def keep_alive_target(self) -> Optional[str]:
        if not self.keep_alive_enabled:
            return None
        if self.keep_alive_url:
            return self.keep_alive_url
        if self.render_external_url:
            return self.render_external_url.rstrip("/") + "/health"
        return None

    # --- Topic MQTT ---
    @property
    def topic_telemetry_raw(self) -> str:
        return f"{self.topic_prefix}/telemetry"

    @property
    def topic_telemetry_validated(self) -> str:
        return f"{self.topic_prefix}/validated/telemetry"

    @property
    def topic_rejected(self) -> str:
        return f"{self.topic_prefix}/validated/rejected"

    def topic_lamp_state_raw(self, lamp: int) -> str:
        return f"{self.topic_prefix}/led{lamp}/state"

    def topic_lamp_state_validated(self, lamp: int) -> str:
        return f"{self.topic_prefix}/validated/led{lamp}/state"

    def topic_lamp_cmd(self, lamp: int) -> str:
        return f"{self.topic_prefix}/led{lamp}/cmd"

    def topic_lamp_cmd_request(self, lamp: int) -> str:
        return f"{self.topic_prefix}/request/led{lamp}/cmd"

    def topic_map(self) -> dict:
        return {
            "subscribe": {
                "telemetry": self.topic_telemetry_raw,
                **{f"led{n}_state": self.topic_lamp_state_raw(n) for n in LAMP_IDS},
                **{f"led{n}_cmd_request": self.topic_lamp_cmd_request(n) for n in LAMP_IDS},
            },
            "publish": {
                "telemetry_validated": self.topic_telemetry_validated,
                "rejected": self.topic_rejected,
                **{f"led{n}_state_validated": self.topic_lamp_state_validated(n) for n in LAMP_IDS},
                **{f"led{n}_cmd": self.topic_lamp_cmd(n) for n in LAMP_IDS},
            },
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
