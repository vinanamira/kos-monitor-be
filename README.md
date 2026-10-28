# KosMonitor Backend

Backend FastAPI yang menjadi **gerbang validasi** antara ESP32 dan dashboard.
Data dari perangkat **tidak langsung** tampil di dashboard: backend memeriksanya dulu,
menyimpan yang valid, lalu meneruskannya. Data yang tidak valid ditolak dan dicatat beserta alasannya.

```
                 kosdemo/icha-kos/telemetry                     kosdemo/icha-kos/validated/telemetry
ESP32 (Wokwi) ───────────────────────────────► BACKEND ─────────────────────────────────────────────► Dashboard
                 kosdemo/icha-kos/led{n}/state  │  ▲  validasi 6 lapisan      .../validated/led{n}/state
                                                │  │  simpan ke DB             .../validated/rejected
                                                │  │
ESP32 ◄──── kosdemo/icha-kos/led{n}/cmd ────────┘  └──── kosdemo/icha-kos/request/led{n}/cmd ◄──────── Dashboard
```

Dashboard apa pun (website, aplikasi, dsb.) bisa mengambil data yang sudah divalidasi lewat topic MQTT
`validated/...` atau lewat REST API. Lihat [Menyambungkan dashboard](#menyambungkan-dashboard).

## Lapisan validasi telemetry

| # | Lapisan | Aturan (default, bisa diubah di `.env`) | Jika gagal |
|---|---------|------------------------------------------|------------|
| 1 | Ukuran & encoding | tidak kosong, ≤ 2048 byte, UTF-8 | ditolak |
| 2 | Format (schema) | objek JSON; wajib `voltage_v`, `current_a`, `power_w`; angka harus bertipe number (`"220"` ditolak), bukan NaN/Infinity; `adc` bilangan bulat; `led1`/`led2` = ON/OFF | ditolak |
| 3 | Rentang fisik | V 0–300, I 0–30 A, P 0–9000 W, PF 0–1, beban 0–100 %, ADC 0–4095 | ditolak |
| 4 | Konsistensi | `power_w = V × I × PF` (±5 %, minimal ±1 W); tanpa PF: `power_w ≤ V × I` | ditolak |
| 5 | Laju pesan | jarak antar pesan dari sumber yang sama ≥ 0,2 s | ditolak |
| 6 | Peringatan | tegangan di luar 198–231 V (toleransi PLN), V×I > daya kontrak 1300 VA, lonjakan daya ≥ 1000 W | **lolos** dengan status `warning` |

Status lampu dan perintah lampu hanya boleh `ON`/`OFF` (tidak peka huruf besar-kecil).

Semua aturan berlaku sama untuk data dari MQTT maupun dari `POST /api/telemetry`.
Daftar aturan yang sedang aktif bisa dilihat di `GET /api/validation/rules`.

## Menjalankan

Butuh Python 3.10+ (Laragon sudah menyertakan Python 3.10).

**Windows (terminal Laragon / PowerShell):**
```powershell
cd C:\laragon\www\kos-monitor-be
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env
uvicorn app.main:app --reload
```

**Linux / WSL:**
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Buka dokumentasi API di <http://127.0.0.1:8000/docs>. Database SQLite `kosmonitor.db` dibuat otomatis.
Untuk memakai MySQL Laragon: `pip install pymysql`, buat database `kosmonitor`, lalu set
`DATABASE_URL=mysql+pymysql://root:@127.0.0.1:3306/kosmonitor` di `.env`.

## Self-hosting di laptop Windows (otomatis nyala saat login)

Backend berjalan di latar belakang tanpa jendela terminal, otomatis start setiap login Windows,
dan dijalankan ulang bila crash. Pemakaian sumber daya kecil (± 70 MB RAM, CPU < 1%).

Pasang sekali (PowerShell, di folder project):
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File autostart.ps1
```

| Perlu apa | Perintah (PowerShell) |
|-----------|----------------------|
| Cek status | `Get-ScheduledTask "KosMonitor Backend"` |
| Hentikan sementara | `Stop-ScheduledTask "KosMonitor Backend"` |
| Jalankan lagi | `Start-ScheduledTask "KosMonitor Backend"` |
| Hapus auto-start | `powershell -ExecutionPolicy Bypass -File autostart.ps1 -Remove` |
| Lihat log | `Get-Content logs\backend.log -Tail 30 -Wait` |

Setelah `git pull` (kode berubah), jalankan *Hentikan* lalu *Jalankan lagi* agar kode baru dipakai.
Backend hanya aktif selama laptop menyala dan terhubung internet.

## Deploy ke Render (gratis)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/vinanamira/kos-monitor-be)

1. Klik tombol di atas, login ke Render pakai akun GitHub.
2. Render membaca `render.yaml` (paket free, region Singapore). Klik **Apply** / **Deploy**.
3. Tunggu build selesai (± 2–5 menit). Backend bisa dibuka di `https://<nama-service>.onrender.com/docs`.

Setiap `git push` ke branch `main` akan otomatis di-deploy ulang.

Catatan paket free Render:
- **Tidur setelah 15 menit tanpa request.** Backend menangani ini sendiri: kalau berjalan di Render,
  backend ping `/health` miliknya tiap 10 menit (`KEEP_ALIVE_*` di `.env.example`).
- **Database SQLite ter-reset** setiap Render melakukan deploy atau restart. Validasi tetap jalan; hanya riwayat
  yang hilang. Kalau riwayat perlu permanen, arahkan `DATABASE_URL` ke database Postgres gratis (mis. Neon)
  dan tambahkan `psycopg[binary]` ke `requirements.txt`.

**Jalankan hanya satu backend dalam satu waktu.** Kalau backend lokal dan backend Render menyala bersamaan,
keduanya meneruskan data yang sama, sehingga dashboard menerima setiap data dua kali. Untuk mencoba backend lokal
saat versi Render menyala, pakai `TOPIC_PREFIX` lain di `.env` lokal.

## Menyambungkan dashboard

**Lewat MQTT** (broker sama dengan ESP32). Dashboard subscribe/publish ke topic berikut, bukan ke topic mentah ESP32:

| Arah | Topic | Isi |
|------|-------|-----|
| backend → dashboard | `kosdemo/icha-kos/validated/telemetry` | JSON telemetry dari ESP32 + `id`, `status` (`valid`/`warning`), `warnings`, `received_at`, `source` |
| backend → dashboard | `kosdemo/icha-kos/validated/led{n}/state` | `ON` / `OFF` (retained) |
| backend → dashboard | `kosdemo/icha-kos/validated/rejected` | JSON data yang ditolak: `topic`, `errors` (alasan), `raw` |
| dashboard → backend | `kosdemo/icha-kos/request/led{n}/cmd` | `ON` / `OFF`; divalidasi lalu diteruskan ke ESP32 |

**Lewat REST API** (bila dashboard bisa menjangkau backend, mis. website di laptop yang sama):
`GET http://localhost:8000/api/telemetry/latest` untuk data terbaru, `GET /api/lamps` untuk status lampu,
`POST /api/lamps/{n}/command` dengan `{"action": "ON"}` untuk kontrol lampu. CORS sudah diizinkan.

## Endpoint API

| Method | Path | Fungsi |
|--------|------|--------|
| GET | `/health` | Status backend, koneksi MQTT, perangkat online/offline |
| POST | `/api/telemetry` | Kirim telemetry lewat HTTP (uji validasi tanpa ESP32) |
| GET | `/api/telemetry/latest` | Data valid terakhir |
| GET | `/api/telemetry?limit=&status=` | Riwayat data valid |
| GET | `/api/telemetry/summary?date=YYYY-MM-DD` | Total kWh & estimasi biaya (Rp) per hari |
| GET | `/api/rejections?kind=` | Log data yang ditolak beserta alasannya |
| GET | `/api/validation/stats` | Jumlah valid / warning / ditolak, persentase lolos, penolakan per jenis |
| GET | `/api/validation/rules` | Aturan validasi yang sedang berlaku |
| GET | `/api/lamps` | Status lampu terakhir |
| POST | `/api/lamps/{lamp}/command` | Kirim `{"action": "ON"}` ke lampu (tervalidasi) |

Contoh respons penolakan (`422`):

```json
{
  "status": "rejected",
  "accepted": false,
  "errors": [
    {"field": "power_w", "code": "inconsistent",
     "message": "daya 900 W tidak sesuai V×I×PF = 247.50 W (toleransi ±5%)"}
  ]
}
```

## Demo validasi

1. Jalankan backend, buka `/docs`, pilih **POST /api/telemetry**, lalu *Try it out*.
2. Pada dropdown *Examples*, coba `valid`, `peringatan`, `di_luar_rentang`, `tidak_konsisten`, dan `tipe_salah`.
3. Data valid langsung muncul di dashboard. Data yang ditolak tidak muncul, tapi tercatat di `GET /api/rejections`.
4. `GET /api/validation/stats` menampilkan rekap hasilnya.

## Tes

```bash
pytest
```

Tes mencakup setiap lapisan validasi (format, tipe, NaN, rentang, konsistensi, laju pesan, peringatan),
alur simpan dan teruskan, validasi status/perintah lampu, dan perhitungan energi.

## Catatan keamanan

`broker.emqx.io` adalah broker publik tanpa autentikasi. Siapa pun yang tahu nama topic bisa
mengirim data palsu, termasuk langsung ke topic `validated/...`. Validasi backend menyaring data yang
tidak masuk akal, tapi tidak bisa membuktikan asal pesan. Untuk produksi, gunakan broker privat
(misal Mosquitto di jaringan kost) dengan username/password dan ACL: hanya ESP32 yang boleh publish
ke topic mentah, dan hanya backend yang boleh publish ke `validated/...`. Endpoint perintah lampu juga
sebaiknya diberi autentikasi.
