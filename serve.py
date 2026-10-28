"""Menjalankan backend untuk self-hosting (dipanggil Task Scheduler saat login Windows).

- Log ditulis ke logs/backend.log (dipindah ke backend.log.old bila > 5 MB saat start).
- Jika server berhenti karena error, dijalankan ulang otomatis.
"""

import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "logs" / "backend.log"
MAX_LOG_BYTES = 5_000_000
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))


def open_log():
    LOG_FILE.parent.mkdir(exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
        LOG_FILE.replace(LOG_FILE.with_name("backend.log.old"))
    return open(LOG_FILE, "a", encoding="utf-8", buffering=1)


def main() -> None:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    # pythonw.exe tidak punya konsol, jadi semua output diarahkan ke file log.
    sys.stdout = sys.stderr = open_log()

    import uvicorn

    delay = 5
    while True:
        started = time.monotonic()
        print(f"[serve] menjalankan backend di http://{HOST}:{PORT}", flush=True)
        try:
            uvicorn.run("app.main:app", host=HOST, port=PORT)
        except KeyboardInterrupt:
            break
        except BaseException:
            traceback.print_exc()

        # Berhenti setelah lama berjalan -> coba lagi cepat; gagal beruntun -> jeda makin panjang.
        delay = 5 if time.monotonic() - started > 60 else min(delay * 2, 300)
        print(f"[serve] backend berhenti, dijalankan ulang dalam {delay} s", flush=True)
        time.sleep(delay)


if __name__ == "__main__":
    main()
