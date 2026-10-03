# Runbook Lighter Live Desk

IOC entry/exit harus mengirim order_expiry=0. Percobaan 3 Oktober berhenti
di signing lokal karena adapter memakai default SDK -1; tidak ada broadcast,
fill, stop atau close mainnet. Penolakan lokal yang definitif dicatat REJECTED,
hasil network/API yang tidak pasti tetap UNKNOWN. probe_used tidak direset
dan tidak ada entry kedua sesudah fix. Lihat AUDIT_PROBE_2026-10-03.md.

Izin satu probe memakai `live_entry_scope: "probe_only"` (default). Walaupun
`allow_live_orders=true`, backend menolak START_LIVE dan dispatch ENTRY strategi;
PROBE tetap sekali-pakai melalui flag persisten. Jangan mengubah scope ke
`strategy` untuk tugas uji tunggal. Catat `source_funding_spent_usd` dari jumlah
sumber yang telah dikeluarkan, termasuk biaya. Pada akun ini nilainya 50 dan
sisa budget sumber nol; inflow collateral 49,43 tidak memberi izin deposit 0,57.

Pagu normal tetap 40 dan transaksi proteksi/cancel/close tetap 55 per 60 detik.
Pembacaan urgent probe dibatasi 47 agar menyisakan delapan slot transaksi;
preflight juga menyisakan kapasitas pembacaan setelah entry. Ini lebih rendah
dari batas Standard 60 pada [dokumen rate limit resmi](https://apidocs.lighter.xyz/docs/rate-limits).
Probe menutup normal hanya setelah stop reduce-only terverifikasi dari status
bursa. Stop gagal/tidak pasti meminta close darurat; deadline konfirmasi delapan
detik tetap berlaku meskipun polling akun gagal. Stop tidak dikirim ulang pada
hasil ambigu. Pending probe bertahan sampai cleanup dan audit dana lengkap,
termasuk ketika posisi telah flat sebelum restart.

Status 2026-10-03: akun Standard dan setting BTC Cross 1x sudah diverifikasi
di UI/API nyata. Mode lokal `live_capable` dengan `allow_live_orders=false`;
entry tetap berhenti. Feed publik Binance dan Lighter sudah diverifikasi
bersama. Order/fill, proteksi dan close mainnet belum diuji. Sebelum probe
pertama, gunakan jalur probe khusus seperti catatan di bawah.
Bukti jaringan sebelum perbaikan ada di `AUDIT_2026-09-30.md`; hasil perbaikan ada di
`LOCAL_FIX_2026-09-30.md`. Jangan menyebut dashboard siap live sampai
`live_blockers` kosong, hasil `account-check` cocok dengan akun dedicated, dan
satu probe telah direkonsiliasi. Bila development runner sudah aktif, hentikan
dari terminal yang menjalankannya sebelum memakai unit systemd; single-instance
lock menolak engine kedua.

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

Buka `http://lighter.localhost:8765` atau `http://127.0.0.1:8765`. Unit tidak di-*enable* otomatis. Jika nanti diaktifkan saat login, engine selalu mulai dengan entry berhenti dan rekonsiliasi dahulu. Lihat log melalui `journalctl --user -u lighter-live-desk-engine.service -f`; jangan menyalin log berisi data akun ke ruang publik.

Pilihan DNS hanya di aplikasi: `binance_dns_mode="system"` memakai DNS laptop,
sedangkan `"doh"` mengambil jawaban A lewat HTTPS Cloudflare/Google untuk dua
host feed Binance. TTL dihormati sampai maksimal 60 detik dan alamat nonpublik
ditolak. Mode ini tidak mengganti resolver router, `/etc/hosts`, DNS laptop,
hostname bursa, ataupun pemeriksaan TLS. Konfigurasi laptop ini memakai `doh`
setelah REST dan kedua stream resmi berhasil diuji.

Unit memakai `~/lighter-live-desk/.venv/bin/lldesk` yang dibuat oleh `uv sync` di atas.
Status sebelum perbaikan adalah `inactive` dan `disabled`. Setelah perbaikan,
layanan dijalankan dalam mode observasi tanpa mengubah pengaturan autostart.

## Idle dengan akun tersambung

Dengan `mode=live_capable`, account index/tier/trading mode yang sudah
diverifikasi, dan `allow_live_orders=false`, restart mulai dari RECONCILING.
Sesudah warm-up wajib 600 detik dan pemeriksaan sehat, mesin dapat masuk
IDLE untuk memantau akun/harga. IDLE mensyaratkan akun flat, tidak ada
order/intent/sesi aktif, data segar, jurnal sehat dan tidak ada flag insiden.
Jika syarat itu berubah, IDLE ditahan. Entry tetap berhenti.

API dapat tidak mengembalikan setting margin BTC ketika akun flat. Hal itu
tidak menghalangi pemantauan IDLE; semua guard margin, izin order dan
preflight tetap berlaku sebelum live. Jangan mengisi expected margin
dengan perkiraan, menaikkan leverage, atau mengirim order untuk memaksa IDLE.

Profil akun eksperimen saat ini memakai 8 read request per rekonsiliasi;
intervalnya 12 detik untuk menjaga pagu baca lokal 40/min dan cadangan exit
hingga 55/min. [Batas resmi Standard](https://apidocs.lighter.xyz/docs/rate-limits)
adalah 60 request per rolling minute dan dapat dibagi dengan klien lain.
Batas freshness akun tetap 15 detik. Kesalahan rekonsiliasi tetap menghentikan
entry sampai pemeriksaan berikutnya berhasil; umur data tidak diperbarui
secara palsu dan pagu tidak dinaikkan untuk menyamarkan polling yang gagal.

## Pendanaan dan key lokal

Probe memeriksa ketersediaan dua request transaksi sebelum mengonsumsi flag
sekali-pakai dan menolak mulai ketika rekonsiliasi lain masih berjalan.
Polling rutin tidak mendahului task pengiriman probe yang sedang menunggu.
Saat probe/posisi/order masih aktif, akun, order dan fill dibaca dengan
prioritas exit (profil eksperimen empat GET, termasuk satu query status).
Riwayat dana dari audit lengkap terakhir dipertahankan sementara; pembacaan
posisi ini **tidak** memperbarui `last_reconcile_ok`/umur audit dana dan tidak
dapat mengakhiri probe. Baseline dana/PnL juga menunggu audit lengkap.
Setelah posisi flat serta order/proteksi bersih, riwayat deposit, withdrawal
dan transfer wajib dibaca ulang dengan prioritas exit sebelum probe selesai.
Pagu tetap 40 request biasa dan 55 request prioritas exit per rolling minute.
Audit gagal/kuota habis menjaga probe tertahan dan entry strategi berhenti.

Untuk akun saat ini, pengguna menyimpan BTC **Cross 1x** di UI resmi pada
3 Oktober 2026. Account-check kemudian membaca `BTC_margin_mode: 0` dan
`BTC_initial_margin_fraction: 100.00` dengan posisi/order tetap nol.
Konfigurasi expected margin harus memakai nilai respons akun yang nyata
(`100.00` pada akun ini), bukan menebak `1` dari angka leverage atau memakai
skala basis point milik transaksi signer. Nilai ini berlaku untuk akun yang
diverifikasi; jangan menyalinnya ke akun lain tanpa pemeriksaan ulang.

1. Siapkan akun/subakun Lighter **Standard** khusus eksperimen, kosong dari posisi, order, dan aset lain. Pastikan wilayah, akun, dan akses pengguna memenuhi aturan platform. Konfirmasi jenis akun, margin konservatif, market BTC perpetual, biaya aktual, dan batas min order di UI serta `uv run --frozen lldesk market-check`.
2. Dana pengguna USDT adalah sumber budget, sedangkan collateral perpetual Lighter adalah **USDC**. [Dokumen deposit resmi](https://apidocs.lighter.xyz/docs/deposits-transfers-and-withdrawals) menyebut deposit langsung USDC via Ethereum dengan minimum 1 USDC; rute Universal Deposit Address (UDA) menerima USDT pada jaringan yang didaftarkan (misalnya Ethereum, Arbitrum, Base, Polygon, Solana, Tron) dan mengkreditkan USDC setelah konversi. Minimum UDA untuk akun baru $5 di Ethereum/EVM/Solana/Tron; akun lama $3 di EVM/Solana dan $5 di Tron. Rute legacy USDC CCTP punya minimum lain. Periksa **rute yang benar-benar dipilih** di UI resmi, alamat/jaringan/aset, quote bersih, biaya/slippage dan status deposit sebelum mengirim. API UDA builder memerlukan kunci builder terpisah; aplikasi ini tidak memakainya. Total USDT yang dikeluarkan termasuk biaya konversi/transfer maksimum setara US$50. Aplikasi tidak melakukan swap, bridge, deposit, atau withdrawal.
3. Di UI resmi, buka **Tools > API Keys > Generate API Key** untuk akun dedicated itu. Pilih indeks kosong **4–254**; [dokumentasi API key resmi](https://apidocs.lighter.xyz/docs/api-keys) mencadangkan indeks 0–3 untuk web/mobile, sedangkan 255 hanya untuk query semua key. Jangan menimpa key yang sudah dipakai aplikasi lain. Registrasi memerlukan tanda tangan wallet pemilik akun. Jangan kirim seed phrase/private key melalui chat. Simpan hanya **API private key khusus Lighter** di `~/lighter-live-desk/secrets.json` dengan bentuk `{"api_private_key":"ISI_SENDIRI","api_key_index":N}`. Jalankan `umask 077` di terminal lokal, buat file dengan editor yang tidak menaruh nilainya ke shell history, lalu `chmod 600 secrets.json`. Jangan menjalankan `cat` pada file itu atau menyalin isinya ke tiket/log. File diabaikan Git, hanya dibaca engine, dan harus dimiliki user saat ini. `account-check` menangkap error SDK tanpa menampilkan nilai kunci.
4. Edit `config.json`: set `mode` ke `live_capable`, isi `account_index` dedicated, tetapi biarkan `allow_live_orders: false`. Jalankan `uv run --frozen lldesk account-check`; cocokkan account index, `account_type_code`, `account_trading_mode`, `BTC_margin_mode`, `BTC_initial_margin_fraction`, equity, posisi, aset, order aktif dan gross history inflow dengan UI resmi. Konfigurasikan BTC margin konservatif di UI, tanpa menaikkan leverage untuk memenuhi minimum order. Isi keempat `expected_*` dari nilai akun yang diverifikasi; aplikasi menolak live bila BTC margin setting tidak tersedia atau berubah. Pastikan equity dan **jumlah seluruh inflow historis** tidak melebihi pagu, termasuk biaya sumber dana yang tidak terlihat oleh aplikasi. Riwayat deposit, withdrawal dan transfer harus cocok; bila tidak, hentikan setup dan audit secara manual. Tetap pertahankan `allow_live_orders: false` setelah pemeriksaan akun. Mengaktifkan order memerlukan instruksi jelas pengguna untuk uji transaksi, lalu restart engine; restart tetap tidak membuka entry dengan sendirinya.
5. Untuk satu probe, tunggu status READY, entry strategi berhenti, kedua feed valid, snapshot akun segar, warm-up sekurangnya 10 menit, dan `probe_blockers` kosong. Klik langsung **Probe 1×**; tidak perlu menekan **Mulai sesi live**. Perintah melewati preflight yang sama dengan live, lalu membuka state PROBING dengan entry strategi tetap berhenti dan satu task IOC minimum legal. Window probe maksimum `max_hold_seconds` (180 detik pada konfigurasi ini); dispatch memeriksa lagi mode order, data, akun/tier/margin, dana/cadangan, loss/insiden dan epoch pembatalan sebelum signer. Stop/cancel/close tetap responsif saat task menunggu signer. Flag sekali-pakai disimpan sebelum pengiriman; jangan ulangi probe atau menghapus flag jika hasil tidak pasti. Pantau fill, stop, fee, penutupan dan ledger sampai bursa mengonfirmasi posisi flat serta seluruh order/proteksi selesai. Akhir probe menjaga entry strategi berhenti; restart merekonsiliasi tanpa mengulang probe. PROBE tidak masuk statistik posisi strategi. Sesi strategi maksimum 30 menit/tiga posisi adalah tahap terpisah setelah uji transaksi dan validasi strategi.

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
