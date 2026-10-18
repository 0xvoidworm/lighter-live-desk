```text
╭──────────────────────────────────────────────────────────────────────╮
│   l i g h t e r - l i v e - d e s k                                  │
│   [TRADING] BTC perpetual desk lokal — observe first                 │
╰──────────────────────────────────────────────────────────────────────╯
```

Local BTC perpetual desk. The engine and dashboard API run as separate processes. The initial configuration is **observe only**; it can display public live data without any key or funds.

**Ringkas**

- Engine (Python, `uv`) + dashboard API + frontend (Vite) — dijalankan dengan `./dev.sh`, dibuka di `http://127.0.0.1:5173`. Engine adalah satu-satunya pengirim order.
- `config.json`, `secrets.json`, `state/`, dan rekaman pasar **tidak pernah** ikut repo; key tidak dicatat dan tidak di-commit.
- DNS Binance bisa dipaksa ke `doh` bila DNS router bermasalah (Cloudflare, fallback Google, hanya untuk `fapi`/`fstream` Binance).
- Status & bukti: `PROGRESS.md`, `AUDIT_2026-09-30.md`, `RUNBOOK.md`. **Order mainnet belum pernah diuji.**

## Disclaimer

Riset pribadi — bukan nasihat finansial, bukan layanan sinyal, dan bukan jaminan
profit. Engine hanya bisa mengirim order nyata setelah live preflight di
`RUNBOOK.md` terpenuhi dan pengguna mengaktifkannya secara sadar; restart selalu
dimulai dari RECONCILING dan tidak melanjutkan entri sendiri. Pemakaian
sepenuhnya risiko pengguna; lisensi MIT berarti tanpa garansi apa pun.

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
