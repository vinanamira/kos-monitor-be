import logging
import uuid
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from .config import LAMP_IDS, Settings

log = logging.getLogger("kosmonitor.mqtt")


class MqttBridge:
    """Subscribe topic mentah dari ESP32/dashboard, teruskan ke handler validasi,
    dan publish hasil yang sudah tervalidasi."""

    def __init__(self, settings: Settings):
        self.s = settings
        self._connected = False
        self.on_telemetry: Optional[Callable[[bytes], object]] = None
        self.on_lamp_state: Optional[Callable[[int, bytes], object]] = None
        self.on_command_request: Optional[Callable[[int, bytes], object]] = None

        self._routes: dict[str, Callable[[bytes], object]] = {settings.topic_telemetry_raw: self._telemetry}
        for n in LAMP_IDS:
            self._routes[settings.topic_lamp_state_raw(n)] = lambda raw, n=n: self._lamp_state(n, raw)
            self._routes[settings.topic_lamp_cmd_request(n)] = lambda raw, n=n: self._command_request(n, raw)

        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"kosmonitor_be_{uuid.uuid4().hex[:8]}",
            transport=settings.mqtt_transport,
        )
        if settings.mqtt_transport == "websockets":
            self._client.ws_set_options(path=settings.mqtt_ws_path)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        log.info("Menghubungkan ke MQTT %s:%s ...", self.s.mqtt_broker, self.s.mqtt_port)
        self._client.connect_async(self.s.mqtt_broker, self.s.mqtt_port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    def publish(self, topic: str, payload: str, retain: bool = False) -> bool:
        if not self._connected:
            return False
        info = self._client.publish(topic, payload, qos=0, retain=retain)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    # ------------------------------------------------------------------ callbacks
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("Koneksi MQTT gagal: %s", reason_code)
            return
        self._connected = True
        client.subscribe([(topic, 0) for topic in self._routes])
        log.info("MQTT terhubung, subscribe: %s", list(self._routes))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connected = False
        log.warning("MQTT terputus: %s", reason_code)

    def _on_message(self, client, userdata, message):
        handler = self._routes.get(message.topic)
        if handler is None:
            return
        try:
            handler(message.payload)
        except Exception:
            # Jangan biarkan satu pesan rusak menghentikan loop MQTT.
            log.exception("Gagal memproses pesan dari %s", message.topic)

    def _telemetry(self, raw: bytes):
        if self.on_telemetry:
            self.on_telemetry(raw)

    def _lamp_state(self, lamp: int, raw: bytes):
        if self.on_lamp_state:
            self.on_lamp_state(lamp, raw)

    def _command_request(self, lamp: int, raw: bytes):
        if self.on_command_request:
            self.on_command_request(lamp, raw)
