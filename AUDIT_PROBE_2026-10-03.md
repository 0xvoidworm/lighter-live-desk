# Audit probe mainnet — 3 Oktober 2026

Hasil: percobaan ditolak signing lokal sebelum broadcast; siklus mainnet belum
terbukti. Tidak ada entry kedua. Pengguna mengizinkan tepat satu percobaan
entry probe dengan proteksi/cancel/close, melarang START_LIVE/strategi dan deposit
tambahan. Izin lokal memakai live_entry_scope=probe_only; probe_used persisten.
Cleanup serta audit dana lengkap berhasil, dan izin order dimatikan lagi.

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

Preflight runtime setelah warm-up lolos: READY, probe_blockers kosong, akun
flat, dua feed valid, snapshot/audit dana segar, journal sehat, scope probe-only
dan 22 slot transaksi tersedia. Preview minimum 0,00012 BTC, notional batas
10,146216 USD. Nilai tepat pada dispatch dicatat berikut:

| Field | Hasil |
| --- | --- |
| Waktu percobaan WIB | 21:29:14,143 |
| Client ID lokal | 1 |
| Sisi / ukuran | BUY / 0,00012 BTC |
| Ask acuan dispatch | 84.541,6 |
| Batas harga IOC | 84.558,6 |
| Notional maksimum pada limit | 10,147032 USD |
| Risiko terencana | 0,0375440184 USD |
| Hasil signing | OrderExpiry is invalid |
| SendTx / order ID bursa / trade ID | Tidak ada |
| Fill / fee transaksi / PnL | 0 / 0 USDC / 0 USDC |
| Equity / available sebelum dan sesudah | 49,43 / 49,430000 USDC |
| Stop / close mainnet | Tidak dikirim karena tidak ada fill |

Adapter sebelumnya tidak mengisi order_expiry untuk IOC. Default SDK -1
ditolak signer. Diagnosis lokal dengan parameter yang sama mereproduksi error;
expiry 0 menghasilkan signing valid tanpa mengirimnya. Source SDK pinned
create_order kembali sebelum send_tx pada error signing. Adapter sekarang
mengirim expiry 0 dan mengenali error signing lokal yang definitif sebagai
REJECTED, tanpa mengulang entry. 190 tes/lint/format/mypy lulus setelah fix;
tes parameter expiry juga memeriksa IOC exit reduce-only.

Intent semula UNKNOWN karena adapter menutupi alasan SDK. Audit yang memiliki
bukti penolakan sebelum pengiriman mengubah hanya intent 1 menjadi REJECTED
dan mencatat LOCAL_REJECTION_AUDIT. probe_used tetap 1, tidak direset. Engine
kemudian mengonfirmasi posisi/order/proteksi/exit nol serta audit dana lengkap
baru pada 21:38:45 WIB; probe_pending menjadi 0. Izin order dimatikan dan
engine direstart 21:38:49 WIB untuk verifikasi IDLE setelah warm-up.

Query tambahan accountTxs ditolak ForbiddenException; hasil itu bukan bukti
tidak ada transaksi. Bukti tidak dibroadcast berasal dari jalur error signer
lokal sebelum send_tx, disertai tx_hash null, fill nol, dan rekonsiliasi akun.
Tidak ada packet capture yang dijadikan bukti. API next_nonce masih melaporkan
1 pada diagnosis read-only, tanpa klaim timestamp nonce sebelum percobaan.

Belum dibuktikan: fill mainnet, penerimaan stop exchange, stop terpicu, close
reduce-only aktual, fee/slippage eksekusi. Tidak ada kesimpulan profit strategi.
Sesuai batas satu percobaan dan larangan mengulang entry yang ditolak, tidak
ada order baru setelah fix. Percobaan berikutnya membutuhkan izin baru.

Bukti tanpa key/token di Workspace/outputs/lighter-mainnet-probe-20261003:
preview.json, rejected-attempt.json, initial-preflight.json,
final-validation.json dan final-runtime.json. Salinan audit versi Git:
audits/probe-20261003.json.

Verifikasi akhir 21:51:23 WIB: IDLE, engine/API PID 80858/50780 aktif,
NRestarts=0, autostart disabled, allow_live_orders=false, entry berhenti,
probe_used=1 dan probe_pending=0. Posisi/order/proteksi/exit nol, equity
49,43 dan available 49,430000 USDC, audit dana lengkap berumur 7,3 detik.
Quote Binance/Lighter valid (63/127 ms), reconnect nol, error rekonsiliasi/
setup/jurnal nol sejak restart akhir. DOM IDLE, sisa budget sumber $0,
tombol live/probe nonaktif. Pengamatan IDLE sebelumnya juga lulus 21:49:37.
