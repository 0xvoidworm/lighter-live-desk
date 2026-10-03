# Lighter Live Desk

Local BTC perpetual desk. The engine and dashboard API run as separate processes. The initial configuration is **observe only**; it can display public live data without any key or funds.

## Quick start

```bash
cd ~/lighter-live-desk
cp -n config.example.json config.json
uv sync --frozen --extra dev --python 3.12
cd frontend && npm ci && cd ..
./dev.sh
```

Open `http://127.0.0.1:5173`. The engine is the only order sender. Restarting the browser or dashboard does not create another engine.

Untuk dashboard lokal tanpa Vite, jalankan layanan user menurut RUNBOOK lalu buka
`http://lighter.localhost:8765` (atau `http://127.0.0.1:8765`). Nama `.localhost`
mengarah ke loopback; aplikasi tetap hanya mendengarkan di `127.0.0.1`.

Jika DNS router memberi alamat Binance yang tidak dapat tersambung, ubah
`binance_dns_mode` pada `config.json` dari `system` menjadi `doh`, lalu restart
engine ketika sesi tidak aktif. Mode `doh` memakai Cloudflare dengan fallback
Google hanya untuk `fapi.binance.com` dan `fstream.binance.com`. Host resmi dan
verifikasi sertifikat TLS tetap dipakai; DNS sistem dan koneksi akun Lighter
tetap memakai konfigurasi lokal. Tidak ada IP Binance yang dipatok di kode.

See [RUNBOOK.md](RUNBOOK.md) for user services, keys, funding, stop and recovery. See [PROGRESS.md](PROGRESS.md) for verified status and [AUDIT_2026-09-30.md](AUDIT_2026-09-30.md) for the Binance and order-path evidence. Mainnet orders remain untested.
