# Perbaikan proteksi setelah probe kedua — 4 Oktober 2026

Tugas ini tidak mengizinkan entry mainnet baru. Tidak ada PROBE_ONCE,
START_LIVE, transaksi, perubahan margin, atau deposit baru. Akun 751745/key 4
dibaca ulang dengan SDK resmi: Standard/Unified, BTC Cross 1x (0/100.00),
saldo/equity 49,430576 USDC, posisi BTC dan seluruh order/aset lain nol,
satu inflow 49,430000. Izin order tetap false dan scope tetap probe_only.

## Penyebab yang terbukti

Source sebelum entry (`fa95fb9`) dan audit/timestamp probe kedua diperiksa.
Respons trade berisi akun/client, harga dan ukuran valid, tetapi fee kosong.
`_record_trade` melempar ValueError sebelum snapshot exposure dipublikasikan,
ownership dicatat, timer posisi diaktifkan, dan `protect_position` dipanggil.
Dashboard tetap memakai snapshot flat. Tidak ada stop yang dikirim saat itu.
Patch `285727b` kemudian membuka close darurat pada error fee, tetapi masih
mengaitkan lifecycle proteksi dengan kelengkapan pembukuan; itu belum cukup.

| Bukti lokal, UTC 3 Oktober | Kejadian |
| --- | --- |
| 16:13:17.135568 | ENTRY_DISPATCH client 2 BUY 0,00012 BTC |
| 16:13:20.433–16:15:10.149 | ValueError rekonsiliasi berulang |
| 16:15:12.776440 | Engine dihentikan untuk mengambil alih signer/nonce |
| 16:16:20.802030 | EXIT_DISPATCH client 3, ukuran aktual, reduce-only |

Jarak dispatch lokal adalah 183,666462 detik. Timer normal 180 detik tidak
terpasang karena jalur sebelumnya gagal sebelum exposure terlihat. Waktu
diagnosis, penghentian proses, pembacaan aktual dan close manual termasuk
dalam interval lokal tersebut. Ini bukti kegagalan alur lokal; tidak ada
bukti yang menetapkan ISP, router, atau exchange sebagai penyebab pasti.

Raw exchange entry 1791020396773 dan close 1791020597030 menghasilkan durasi
fill-to-close **200,257 detik**. Jam exchange dan jam lokal berbeda sekitar
6h33m; jangan menyatukan timeline itu atau mengatribusikan selisih durasi
seluruhnya sebagai latensi exchange. Timestamp asli tetap dipertahankan.

## Perilaku sesudah perbaikan

- Fill milik intent lokal tetap disimpan saat fee kosong, dengan fee SQL NULL.
  Pending fee memblokir entry, tetapi tidak menahan stop, cancel, close atau
  pembacaan posisi. Fee dapat diisi belakangan hanya dari field fee API yang
  sah; dedup tetap memverifikasi ID, ukuran dan harga.
- Worker risiko membaca posisi segar dan status order langsung tanpa menunggu
  lock/history fee/funding rekonsiliasi. Fill lokal atau cumulative fill status
  exchange membuktikan ownership; ukuran proteksi/exit memakai posisi exchange
  aktual, termasuk partial fill. Ketidakpastian membekukan entry dan meminta
  pemulihan, bukan entry/stop/close duplikat secara buta.
- Snapshot posisi dari jalur prioritas tidak membuktikan seluruh active order
  atau audit dana segar; data cache tidak dipakai sebagai bukti cleanup penuh.
  Flat yang tidak cocok dengan cumulative fills tidak membatalkan stop atau
  menganggap close ambigu selesai. Retry partial close memerlukan status
  terminal definitif dan pembacaan posisi sesudah status terminal diamati.
- Stop memakai helper SDK resmi dan reduce-only. ACK belum dianggap proteksi:
  status exchange harus pending/open, tipe stop, arah, market, trigger dan
  remaining size harus sesuai posisi aktual. Stop tidak dikirim ulang saat
  hasilnya ambigu; emergency close memakai jalur reduce-only yang sebelumnya
  terbukti mainnet dan tetap menunggu bukti posisi nol.
- Timer hold diaktifkan dari bukti fill/exposure, konservatif dari waktu intent
  lokal. Selama proses hidup, elapsed memakai monotonic clock. Restart tidak
  memberi 180 detik baru: deadline tersimpan dianggap jatuh tempo dan exposure
  dibaca ulang sebelum meminta close. Ukuran tidak dipulihkan dari tebakan.
- Batas pemasangan stop 3 detik, konfirmasi maksimal 8 detik sejak exposure
  diketahui, terpisah dari hold 180 detik. Close diminta mulai detik 177 agar
  tersedia anggaran submission 3 detik. Antrean lock signer/nonce/send ikut
  dalam timeout total 3 detik. Task entry/stop yang menggantung dilepas pada
  pemulihan prioritas, sementara intent ambigu tetap UNKNOWN.
- Pembacaan posisi/status mempunyai timeout 2 detik per panggilan dan berjalan
  terpisah dari action worker. Pagu normal/urgent/transaksi tetap 40/47/55 per
  60 detik; polling posisi terlindungi memakai pagu normal, pemulihan mendesak
  boleh memakai 47 sehingga delapan slot transaksi tersisa. Audit normal
  tidak mengambil cadangan tersebut.

Deadline mengatur usaha pengiriman lokal, bukan menjamin fill exchange pada
detik 180. Posisi/status yang belum diketahui atau quote Lighter yang invalid
tetap menahan order yang memakai ukuran/harga tebakan; pemantauan berlanjut.
Tidak ada putus jaringan mainnet disengaja atau posisi baru untuk tes.

## Bukti keuangan

49,430000 → 49,430576 USDC, perubahan saldo **+0,000576**.
Gross PnL kedua fill = 0,00012 × (84.543,8 − 84.539,0) = +0,000576.
Audit net kas cocok dan inflow tetap 49,430000; selisih gross PnL terhadap
perubahan kas 0,000000 tidak membuktikan fee individual nol.
Trade 33141524453 dan 33141644233 tetap memiliki fee API kosong.

Migration 3 mengoreksi dua fee ledger yang dahulu diinferensikan nol menjadi
NULL, serta menambahkan FEE_PROVENANCE_CORRECTION. Event/bukti historis tidak
dihapus, ledger kas dan timestamp tetap dipertahankan. Status recorded_fees
null dan pending_fee_count 2. UI menampilkan “Belum diketahui (2 fill)”.
Audit fee belum lengkap dan tetap memblokir probe berikutnya. Sumber 50 USDT
sudah habis teralokasi, sisa jatah deposit nol; tidak ada tambahan dana.

## Pengujian dan batas bukti

211 tes lulus; Ruff, format, mypy (12 modul) dan build UI lulus. Tiga warning
berasal dari deprecation dependency SDK/websocket/Starlette. Regresi mencakup
fee kosong setelah fill, lock rekonsiliasi/history menggantung, partial fill,
stop ambigu/deadline, cancel ambigu, snapshot flat yang mendahului fill,
backfill fee, stale position, restart stop UNKNOWN, clock mundur, dan cadangan
request habis pada pagu normal. Tidak ada entry kedua dalam tes pemulihan.

Tiga tes signer memakai native SDK resmi distribusi 1.1.5, pin
78db23f632de8deca9a232a61bbd3bf6e79fc522, key sementara. Crypto signing asli
memeriksa entry IOC, stop, cancel dan close/partial size; nonce dan transport
diganti, seluruh I/O jaringan diblokir. IOC expiry -1 gagal sebelum transport;
expiry 0 dan stop helper resmi lolos. Ini bukti lokal, bukan penerimaan mainnet.

Log pengujian: `/home/yuri/Workspace/outputs/lighter-protection-fix-20261004/`.
Bukti terstruktur serta pemeriksaan runtime akhir:
`audits/protection-fix-20261004.json`.
Output lengkap pengujian juga dicommit pada
`audits/protection-fix-20261004-tests.txt`; hash source/log disimpan dalam JSON.

Engine/API aktif setelah restart. Pemeriksaan 01:35:03 WIB mencatat
WARMING_UP/COVERAGE_DATA_KURANG setelah tiga reconnect Binance, Lighter nol;
dua quote dan kedua stream Binance saat itu valid. Warm-up 600 detik tidak
dibypass; guard coverage menahan kesiapan, bukan dipaksa IDLE/READY.
Snapshot akun/audit dana masih <15 detik, izin order false, entry berhenti,
probe_pending 0 dan probe_used 1. Query SDK langsung 01:32:29 WIB membuktikan
seluruh order aktif nol. Ledger tetap tiga intent historis dan dua fill;
tidak ada intent atau dispatch baru sepanjang tugas. SQLite quick_check OK.

Perbaikan lokal telah lolos regresi. Probe proteksi berikutnya masih diblokir
dua fee unknown dan membutuhkan izin baru; guard tidak dilonggarkan. Stop
diterima exchange dan lifecycle mainnet dengan proteksi belum terbukti.
Coverage feed juga masih menghalangi preflight pada snapshot akhir.
Tidak ada klaim strategi menguntungkan.
