# Klien pembanding transport Binance public

Klien Rust independen, read-only, untuk tepat satu pengukuran maksimal 15 menit.
Tidak mengimpor engine, signer/SDK trading, membaca key, atau mengirim order.
URL/subscription mengikuti public BTCUSDT bookTicker dan depth 100ms aplikasi.
Tidak membuat snapshot REST atau menyatakan book valid untuk keputusan trading.

```bash
cargo build --release --locked
./target/release/binance-ws-compare OUTPUT ENGINE_PID 900
```

`OUTPUT` harus baru: marker `started.json` memakai create_new, sehingga output
pengukuran tidak bisa ditimpa/dipakai ulang. Duration 1–900 detik termasuk DNS,
handshake, disconnect dan backoff. Tidak memperpanjang deadline. Satu proses
minimal, satu socket public pada suatu waktu, TLS native roots dengan validasi
hostname, IPv4 DoH A Cloudflare/Google, proxy dimatikan. Jawaban A dicatat dan
dideduplikasi/diurutkan; urutan pemilihan IP berbeda dari resolver aplikasi.

Client heartbeat 20 detik idle/pong 10 detik menyerupai aiohttp. Ping server
dijawab satu matching pong otomatis; tidak mengirim ulang subscription JSON.
Transport/control frame tidak menyegarkan harga. Sequence pu/u yang terputus
dicatat tanpa membuat book lokal atau mengisi gap.

Status engine dibaca lewat localhost sekali per detik, bukan endpoint exchange.
PID/order-OFF/session-closed diperiksa sebelum dan selama pengukuran. Perubahan
itu menghentikan klien diagnostik saja; engine dan worker risiko tidak disentuh.
Socket `/proc/PID/net/tcp{,6}` difilter berdasarkan inode milik engine; tuple
socket bukan bukti hostname yang pasti. Satu snapshot bisa melewatkan socket
yang berumur kurang dari interval sampling.

Artefak: `started.json`, `trace.jsonl`, `finished.json`. Trace mencatat epoch dan
elapsed monotonic, koneksi, DNS/peer, handshake, message/update IDs, server/client
ping/pong, close/error dan backoff; app_snapshot berisi telemetry yang sudah
dikonsumsi engine. Tidak mengukur latensi fill, tidak membuka sesi trading.
Simpan rekaman besar di Workspace/outputs, commit ringkasan/hash yang aman.

Rekonstruksi rekaman persiapan lama terintegrasi dengan analyzer Python repo
yang sudah ada, agar kompatibel dengan schema/SQLite telemetry engine:

```bash
.venv/bin/python scripts/analyze_bounded_preparation.py RECORDING OUTPUT_JSON --reconnects
```

```bash
rust-workbench check
```

Regresi lokal mencakup DNS nonpublic, status izin/PID, backoff cap, dan ping/pong
matching tanpa duplikasi. Server test hanya localhost, tanpa exchange/SDK/key.
