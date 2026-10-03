# Audit probe mainnet — 3 Oktober 2026

Status awal: belum ada entry mainnet. Pengguna mengizinkan tepat satu percobaan
entry probe dengan proteksi/cancel/close, melarang START_LIVE/strategi dan deposit
tambahan. Izin lokal memakai live_entry_scope=probe_only; probe_used persisten.
Dokumen ini akan dilengkapi dengan hasil transaksi dan bukti cleanup.

## Gangguan sebelum probe

Log engine PID 63783 memuat 130 kegagalan rekonsiliasi: 129 TimeoutError
(deadline rekonsiliasi delapan detik) dan satu ClientConnectorDNSError.
Pengelompokan memakai jeda lebih dari 60 detik:

| Episode WIB | Jumlah | Audit akun lengkap pertama sesudah error terakhir |
| --- | --- | --- |
| 03:53:16–04:17:20 (24m04s) | 121 | 04:17:25 |
| 04:20:45–04:21:33 (48s) | 5 | 04:21:39 |
| 06:48:35–06:49:28 (53s) | 4 | 06:49:33 |

Pola episode panjang sekitar lima timeout per menit sesuai interval polling
12 detik, bukan loop retry tanpa jeda. Tidak ada exception rekonsiliasi baru
sesudah episode terakhir sampai pemeriksaan sebelum restart. Counter feed lama
Binance 95/Lighter 42 bersifat kumulatif dan tidak punya timestamp tiap reconnect.
57 BOOK_GAP dalam DB berasal dari proses sebelumnya; yang terakhir 00:43:10,
sebelum PID 63783 mulai. Jangan menghubungkannya ke episode DNS di atas.

Journal NetworkManager/suspend pada rentang itu hanya memberi satu event status
jaringan pukul 06:09:47; bukti yang tersedia tidak memastikan sebab timeout/DNS.
Tidak ada dasar menetapkan ISP, router atau exchange sebagai penyebab pasti.
Preflight memakai koneksi aktual yang pulih dan warm-up wajib; tidak menunggu
jaringan sempurna tanpa batas dan tidak sengaja memutus jaringan saat posisi ada.

## Kontrol yang diperbaiki dan diuji

- Scope probe-only menolak START_LIVE dan dispatch ENTRY strategi, termasuk
  bila state dibentuk LIVE; PROBE tetap satu entry dan tidak diulang pada UNKNOWN.
- Stop reduce-only ditempatkan melalui create_sl_order SDK resmi. Status market,
  sisi, ukuran aktual, trigger, tipe dan reduce-only harus cocok dengan exchange.
- Close normal probe menunggu konfirmasi stop. Stop gagal/ambigu meminta close
  darurat; deadline delapan detik tetap berlaku saat rekonsiliasi gagal.
- Polling probe dua detik agar konfirmasi stop tidak menunggu interval normal
  12 detik. Admission memeriksa kapasitas empat GET eksposur/delapan GET audit.
- Pagu normal 40/transaksi 55 tetap. GET urgent dibatasi 47, menyisakan delapan
  slot transaksi stop/close/cancel. Preflight entry menyisakan kapasitas baca.
- Fill yang datang sesudah snapshot flat tidak boleh menyelesaikan probe atau
  menggeser titik ownership. Snapshot yang tertinggal segera dibaca ulang;
  close yang sudah filled tidak diulang memakai posisi lama.
- Pending probe bertahan sampai stop/order bersih dan audit dana lengkap baru,
  termasuk setelah restart ketika akun flat tetapi cancel belum terkonfirmasi.
- Budget sumber dicatat 50, sisa nol. Inflow Lighter 49,43 tidak memberi
  headroom deposit 0,57.

189 tes offline lulus (tiga warning dependency yang sudah ada), Ruff/format,
mypy 12 source file, config/SQLite, TypeScript/Vite dan whitespace lulus.
Tes mencakup konfirmasi stop sebelum deadline melalui foreground loop, partial
fill/restart, hasil ambigu, close darurat, kuota dan race snapshot/fill.
Tidak ada transaksi mainnet pada tes tersebut.

## Preflight dan rencana

Account-check nyata sesudah engine lama dihentikan: account 751745, tier 0,
trading mode 1, margin 0/100.00 (Cross 1x), key indeks 4 cocok, equity/available
49,43, posisi/order/aset lain nol dan satu inflow USDC 49,43. Metadata BTC:
market 1, minimum base 0,00007, minimum quote 10, size/price decimals 5/1,
taker fee 0. Nilai ini diperiksa ulang lewat preflight runtime sebelum order.

BUY adalah sisi tetap mekanisme probe yang sudah ada, bukan sinyal strategi.
Sizing minimum memakai Decimal, pembulatan step dan sedikit ruang minimum;
slippage entry 2 bps, stop 35 bps dari harga posisi aktual dengan batas fill
stop 30 bps, waktu maksimum 180 detik. Close reduce-only memakai 30/60/100 bps
hanya setelah hasil upaya sebelumnya definitif dan posisi aktual masih ada.
Notional <=25, exposure <=1x equity, risiko terencana <=0,50, daily stop 5,
cadangan dana exit 2. Target risiko tidak menjamin harga eksekusi.

Batas platform diambil dari [rate limits resmi](https://apidocs.lighter.xyz/docs/rate-limits):
Standard 60 request per rolling minute, default transaksi 40/minute. Probe ini
memakai satu entry dan transaksi pengurangan risiko yang terbatas. Proteksi,
reduce-only, dan perbedaan ACK versus eksekusi mengacu pada
[dokumentasi signing resmi](https://apidocs.lighter.xyz/docs/trading).
SDK tetap dipin 78db23f632de8deca9a232a61bbd3bf6e79fc522; tidak ada custom signing.

Sumber budget 50 USDT sudah terpakai termasuk transfer/konversi. Tidak ada
swap, deposit, withdrawal, pengaktifan strategi atau perubahan leverage baru.
Backup sebelum probe: state/backups/pre-probe-20261003.db (ignored).

## Hasil aktual

Menunggu warm-up dan preflight lengkap. Belum boleh menyatakan probe berhasil,
stop terpicu, atau strategi menguntungkan.
