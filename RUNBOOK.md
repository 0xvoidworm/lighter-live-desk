# Runbook Lighter Live Desk

Status 2026-09-30: mode default `observe`. Integrasi akun dan order belum diuji pada akun nyata; koneksi Binance USD-M dari lingkungan engine gagal sebelum TCP selesai. Bukti per lapisan ada di `AUDIT_2026-09-30.md`. Jangan menyebut dashboard siap live sampai `live_blockers` kosong, hasil `account-check` cocok dengan akun dedicated, dan satu probe telah direkonsiliasi. Bila development runner sudah aktif, hentikan dari terminal yang menjalankannya sebelum memakai unit systemd; single-instance lock menolak engine kedua.

## Jalankan lokal

```bash
cd ~/lighter-live-desk
uv sync --frozen --extra dev --python 3.12
cd frontend && npm ci && npm run build && cd ..
cp -n config.example.json config.json
uv run --frozen lldesk check
./dev.sh
```

Dashboard development: `http://127.0.0.1:5173`; API: `http://127.0.0.1:8765`. Engine adalah proses terpisah. Menutup tab browser tidak menghentikan engine. Jika heartbeat lebih tua dari tiga detik, API menandai `DISCONNECTED`.

Layanan user tanpa root:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/lighter-live-desk-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start lighter-live-desk-engine.service lighter-live-desk-api.service
```

Buka `http://127.0.0.1:8765`. Unit tidak di-*enable* otomatis. Jika nanti diaktifkan saat login, engine selalu mulai dengan entry berhenti dan rekonsiliasi dahulu. Lihat log melalui `journalctl --user -u lighter-live-desk-engine.service -f`; jangan menyalin log berisi data akun ke ruang publik.

Unit memakai `~/lighter-live-desk/.venv/bin/lldesk` yang dibuat oleh `uv sync` di atas. Unit telah dipasang pada user ini dan diuji start/stop; status akhirnya `inactive` dan `disabled`.

## Pendanaan dan key lokal

1. Siapkan akun/subakun Lighter **Standard** khusus eksperimen, kosong dari posisi, order, dan aset lain. Pastikan wilayah, akun, dan akses pengguna memenuhi aturan platform. Konfirmasi jenis akun, margin konservatif, market BTC perpetual, biaya aktual, dan batas min order di UI serta `uv run --frozen lldesk market-check`.
2. Dana pengguna USDT adalah sumber budget, sedangkan collateral perpetual Lighter adalah **USDC**. [Dokumen deposit resmi](https://apidocs.lighter.xyz/docs/deposits-transfers-and-withdrawals) menyebut deposit langsung USDC via Ethereum dengan minimum 1 USDC; rute Universal Deposit Address (UDA) menerima USDT pada jaringan yang didaftarkan (misalnya Ethereum, Arbitrum, Base, Polygon, Solana, Tron) dan mengkreditkan USDC setelah konversi. Minimum UDA untuk akun baru $5 di Ethereum/EVM/Solana/Tron; akun lama $3 di EVM/Solana dan $5 di Tron. Rute legacy USDC CCTP punya minimum lain. Periksa **rute yang benar-benar dipilih** di UI resmi, alamat/jaringan/aset, quote bersih, biaya/slippage dan status deposit sebelum mengirim. API UDA builder memerlukan kunci builder terpisah; aplikasi ini tidak memakainya. Total USDT yang dikeluarkan termasuk biaya konversi/transfer maksimum setara US$50. Aplikasi tidak melakukan swap, bridge, deposit, atau withdrawal.
3. Di UI resmi, buat API key trading untuk akun dedicated itu. Jangan kirim seed phrase/private key melalui chat. Simpan hanya **API private key khusus Lighter** di `~/lighter-live-desk/secrets.json` dengan bentuk `{"api_private_key":"ISI_SENDIRI","api_key_index":N}`. Jalankan `umask 077` di terminal lokal, buat file dengan editor yang tidak menaruh nilainya ke shell history, lalu `chmod 600 secrets.json`. Jangan menjalankan `cat` pada file itu atau menyalin isinya ke tiket/log. File diabaikan Git, hanya dibaca engine, dan harus dimiliki user saat ini. `account-check` menangkap error SDK tanpa menampilkan nilai kunci.
4. Edit `config.json`: set `mode` ke `live_capable`, isi `account_index` dedicated, tetapi biarkan `allow_live_orders: false`. Jalankan `uv run --frozen lldesk account-check`; cocokkan account index, `account_type_code`, `account_trading_mode`, `BTC_margin_mode`, `BTC_initial_margin_fraction`, equity, posisi, aset, order aktif dan gross history inflow dengan UI resmi. Konfigurasikan BTC margin konservatif di UI, tanpa menaikkan leverage untuk memenuhi minimum order. Isi keempat `expected_*` dari nilai akun yang diverifikasi; aplikasi menolak live bila BTC margin setting tidak tersedia atau berubah. Pastikan equity dan **jumlah seluruh inflow historis** tidak melebihi pagu, termasuk biaya sumber dana yang tidak terlihat oleh aplikasi. Riwayat deposit, withdrawal dan transfer harus cocok; bila tidak, hentikan setup dan audit secara manual. Baru setelah pemeriksaan itu dan akses Binance pulih, set `allow_live_orders: true` dan restart engine.
5. Tunggu kedua feed valid, snapshot akun segar, warm-up sekurangnya 10 menit, dan `live_blockers` kosong. Kendali `Mulai sesi live` membuka jendela maksimum 30 menit dan tiga posisi selesai. `Probe sekali` hanya untuk satu roundtrip berukuran minimum legal, ditandai PROBE dan tidak masuk statistik strategi. Jangan ulangi probe untuk mengejar hasil bagus. Pantau order, stop, fill, fee, dan equity sampai ledger cocok.

Kode adapter membaca riwayat deposit, withdrawal dan transfer akun via SDK. Status selain `completed`, aset selain USDC, transfer selain inflow/outflow perps yang dikenali, riwayat terlalu panjang, atau respons tidak tersedia menghentikan entry untuk peninjauan. Jika arus dana baru muncul setelah sesi live pernah dimulai, flag persisten `funding_change_pending` menghentikan entry. Trade yang bukan milik akun, tanpa intent lokal, atau tanpa fee memicu `account_incident_pending`. Keduanya memerlukan audit manual; jangan hapus flag atau database untuk memaksa live. Kelengkapan dan kemungkinan overlap antar riwayat SDK belum dapat diverifikasi tanpa akun nyata.

## Stop, close, dan insiden

- `Hentikan entry` mencegah order baru. `Batalkan entry` mengirim permintaan cancel; tunggu status bursa yang terkonfirmasi. `Minta tutup posisi` mengirim IOC reduce-only dan membutuhkan konfirmasi posisi flat dari bursa. Tombol dapat diklik di dashboard; respons awal hanya `QUEUED`.
- Jika dashboard/engine terputus saat posisi terbuka, lihat posisi dan stop di UI Lighter resmi. Kelola posisi di sana bila engine tidak dapat mengirim exit. Setelah koneksi pulih, restart engine untuk rekonsiliasi; engine tetap memblokir entry. Jangan menganggap cancel/close berhasil hanya dari HTTP 200 atau hash transaksi.
- Exit otomatis memakai maksimal tiga IOC dengan toleransi harga 30, 60, lalu 100 bps setelah status sebelumnya definitif tetapi posisi masih ada. Jika tiga upaya habis atau book Lighter invalid, engine berhenti entry dan menampilkan alasan; operator harus memeriksa/menutup melalui UI resmi. Stop bursa tidak menjamin harga fill pada gap pasar. Posisi kecil di bawah minimum legal mungkin perlu penanganan manual.
- Jika jurnal gagal ditulis, entry berhenti. Jangan hapus database untuk menghilangkan UNKNOWN; cocokkan client ID/order/fill dengan akun resmi lebih dulu.

## Backup dan pemulihan

```bash
cd ~/lighter-live-desk
mkdir -p state/backups
uv run --frozen lldesk backup state/backups/desk-$(date +%Y%m%d-%H%M).db
```

Backup memakai API SQLite agar konsisten dengan WAL. Folder `state/` dan rahasia tidak masuk Git. Untuk restore: hentikan unit engine/API, simpan salinan `state/desk.db` saat ini, ganti dengan backup setelah `PRAGMA integrity_check`, lalu start engine. Rekonsiliasi akun tetap wajib; jangan memulihkan snapshot lama dengan asumsi posisi masih sama. Gunakan izin file 0600 untuk DB dan 0700 untuk direktori state. Rekaman `state/market/*.jsonl.gz` diputar per jam untuk debugging dan dapat dihapus terpisah saat engine mati setelah disalin jika perlu ruang disk.

## Pindah ke VPS kelak

Belum ada deploy. VPS memerlukan verifikasi wilayah/jaringan, sinkronisasi jam, penyimpanan rahasia yang terlindungi, firewall/SSH, backup terenkripsi, dan audit akses. API sekarang sengaja bind `127.0.0.1`; gunakan tunnel SSH tepercaya bila perlu akses jarak jauh, tanpa mengubah bind ke `0.0.0.0` secara langsung. Jangan otomatis mengaktifkan sesi trading setelah boot.
