# Scheduler audit lengkap, tanpa order — 5 Oktober 2026

## Lingkup

Dari repo bersih `e5d0142`, termasuk perbaikan burst `b493ac0`. Tidak ada
izin order/probe/sesi trading baru. START_LIVE/rearm tidak dijalankan;
`allow_live_orders=false`, entry stopped dan otorisasi sebelumnya closed.
Threshold8bps, buffer5bps, kebijakan fee, ukuran/risk/stop/exit/timer,
guard freshness15s, dan pagu40normal/47urgent-read/55transaksi tidak dilonggarkan.

Perbaikan scheduler+tes+replay di commit `2db23db75336dd30d77431a0def81d741fa5b3b6`.
Satu rekaman verifikasi1200s dimulai **11:45:49.914 WIB** (04:45:49.914UTC),
PID168247. Deadline tetap **12:05:49.914 WIB**, termasuk warm-up/gangguan.
Kolektor hanya membaca status lokal4Hz; tidak menambah request exchange.
Konfigurasi/hash source engine dibekukan sepanjang window. Parser analisis
offline kemudian diperbaiki untuk status kas NULL saat startup; bukan
perubahan proses engine atau definisi guard.

## Delapan GET dan kelayakan

| GET pada akun flat saat ini | Jumlah/siklus | Kebutuhan/ketergantungan |
|---|---:|---|
| account | 1 | Identitas751745, tier/mode/margin, equity, posisi/aset semua market; Stamp dari respons nyata |
| accountActiveOrders | 1 | Semua order aktif, bukan hanyaBTC; perlu auth, tidak perlu mengulang account |
| deposit/history | 2 | 1deposit + terminal kosong; L1identity diperlukan; semua cursor sampai terminal |
| withdraw/history | 2 | Kosong pertama bercursor, kosong kedua mengulang cursor; tidak boleh dipotong |
| transfer/history | 1 | Terminal kosong; seluruh inflow/outflow/perps route tetap diperiksa |
| trades | 1 | Descending trade_id, checkpoint terakhir/fee-lookup yang sah, deduplikasi fill_id |

Tidak ada status-order GET ketika unresolved intent kosong. Bila ada intent
ambigu, status lookup/recovery adalah pekerjaan prioritas tambahan, bukan
alasan menyebut siklus masih8GET. Jumlah aktual pagination bisa membesar;
masing-masing history/trades dibatasi5halaman dan tidak melabeli truncation
sebagai audit sukses. Halaman kosong bercursor bukan otomatis terminal,
sebagaimana regresi empty-advancing-pages yang menemukan dana belakangan.

**Tidak cukup memakai rata-rata:** cadence≤15s dengan8GET menimbulkan fase
lima batch dalam rolling60s, puncak40. Entry memakai guard
`can_claim(entry,2)` dan `can_claim(probe_read,14)`, sehingga used≤33.
Jadi full REST8GET/freshness<15s dan kapasitas entry **tersedia setiap saat**
tidak bisa dijamin sekaligus. Bahkan tanpa error, fase40 akan menutup entry.
Ini bukan alasan menaikkan pagu atau menganggap data lama segar.

Angka14 dibaca sebagai envelope gabungan entry nonce/send2 + headroom
priority reads12; **bukan**14read tambahan setelah2entry. Dengan used33,
33+2+12=47, lalu band8 untuk transaksi proteksi/close/cancel mencapai55.
Guard33 sebelum tugas ini adalah admission check tanpa voucher persisten;
tidak mempunyai reservasi yang harus di-refund. Urgent read cap47 menyisakan
8slot transaksi, normal40 menyisakan15. Interleaving/permintaan tambahan
bisa menutup admission; angka ini bukan janji exchange selalu bisa selesai
atau seluruh lifecycle memiliki biaya tetap22request.

Request masuk counter **sebelum await jaringan**, tetap dihitung jika
timeout/cancel/gagal setelah call dimulai, dan baru keluar pada umur≥60s.
SDK transaksi tetap memakai biaya lokal konservatif2 per nonce/send;
simulasi tidak mengirim/sign order. Retry tambahan bukan gratis. Konsumsi
HTTP yang teramati dipisah dari kapasitas belum terpakai dan lease audit.
Klien/IP/L1 lain di luar engine tidak dapat dihitung oleh trace ini.
[Rate limit resmi](https://apidocs.lighter.xyz/docs/rate-limits) untuk
Standard tetap60 request tidak berbobot per rollingminute, termasuk sendTx;
pagu lokal55 menyediakan selisih terhadap limit platform, bukan izin memakai60.

## Perubahan kecil dan pengujian

1. Setelah bootstrap, cache **hanya identitas L1 publik**. History tetap
   dibaca lengkap setiap siklus. Urutannya history→orders→account, kemudian
   trades di engine. Account response sekarang dekat publikasi. Jika L1
   berubah, audit gagal dan cache identitas dibuang. Timestamp wall/monotonic
   disimpan saat respons account diterima, tidak dipindah ke waktu publikasi.
2. Scheduler memakai timestamp account asli, bound durasi awal2s yang
   dipertahankan sebagai high-water mark, dan margin0,5s sebelum umur15s.
   Audit lama tidak dipoles menjadi baru. Deadline timeout rutin dibatasi
   sisa umur account−margin, maksimal8s; failure/cancel mempertahankan
   freshness lama, menghentikan entry, dan retry rutin diberi jeda12s.
3. Batch rutin mengalokasikan estimasi jumlah GET secara atomik sebelum
   jaringan. Lease belum terpakai ikut pemeriksaan kapasitas; setiap claim
   mengubah lease menjadi pemakaian, **tanpa dihitung dua kali**. Akhir,
   pembatalan, atau exception melepas hanya sisa belum terpakai; request yang
   sudah dimulai tidak di-refund. Nested/concurrent lease tidak bisa memakai
   slot milik lease lain. Pagination tambahan tetap wajib selesai atau gagal.
4. Pemanggil `safe_reconcile` bersamaan mengikuti satu task yang sama,
   bukan dua full audit berurutan di lock. Cancelling waiter tidak membatalkan
   owner. Audit rutin ditunda ketika pemulihan exposure/order aktif diperlukan;
   worker risiko tetap membaca posisi/status, melindungi dan exit melalui
   jalur sendiri. Network audit tidak mengambil lock signer atau lock risiko.

Regresi meaningful: replay90siklus memakai93trace8GET yang disanitasi,
boundary59,999/60s, request dalam proses dan tambahan retry, lease nested,
cancelled batch, dua pemanggil dan cancelled waiter, identitas berubah,
history tetap diulang, long pagination23read yang tidak muat, timeout tanpa
refresh palsu, serta stop untuk partial/fill feeNULL ketika audit/lock macet.
Signer native SDK entry/stop/cancel/close tanpa broadcast tetap dalam suite.
**319passed**,3warning dependency deprecation; mypy17source, Ruff/format lulus.
Python3.12 dipakai karena scheduler/replay memakai langsung engine/SDK proyek
yang sudah ada; dependency/toolchain tidak diubah.

Replay awal yang terlalu agresif menghasilkan1 pelanggaran15s; tidak dipakai.
Bound awal2s+margin0,5s pada implementasi diuji terhadap seluruh trace, bukan
hanya median. `audits/audit-scheduler-simulation-20261005.json` adalah
**simulasi**, bukan bukti mainnet; latency berikutnya/pagination yang lebih
panjang bisa melampaui model dan wajib fail closed. Angka before/after
runtime ada pada bagian hasil setelah rekaman selesai.

Replay final menggunakan93cycle yang sama: lama744GET/37,20permin,
blocked806,20s; deadline720GET/36,00permin, blocked607,95s, peak40keduanya.
Maks umur account sebelum publikasi14,618s vs14,305s, violation0. Ini model
serial dengan startup/order yang dijelaskan pada script, bukan angka runtime.
Kebutuhan exit mendadak dan retry tambahan diuji pada RateBudget yang sama;
55tidak ditembus dan request sudah dimulai tidak di-refund saat pembatalan.

## Hasil satu verifikasi1200s

Selesai **12:05:49.915 WIB**, elapsed monotonic1.200,000395s,4.798snapshot,
collector error0. Hash source/config engine tidak berubah. Tidak diulang.
Durasi guard diestimasi dari snapshot4Hz dengan left-hold; gap sampel maksimum
0,269s, sehingga sub-sample continuity tidak dapat dipastikan. Countertrace
GET dipisahkan dari reservasi.

| Ukuran runtime | Sebelum:07:49:48–08:09:48 | Sesudah:11:45:49–12:05:49 |
|---|---:|---:|
| GET Lighter mulai dalam20m | 743 (+1carry-in selesai) | 712 |
| GET Lighter/minute pada window | 37,15 | 35,60 |
| Puncak rolling60s teramati /counterlokal | 40 /40 | 40 /40 |
| Age account P95 /maks detik | 13,3 /14,5 | 12,9 /13,7 |
| Quota guard gagal, termasuk overlap | 795,341s | 616,019s |
| Quota satu-satunya blocker | 284,340s | 276,877s |
| Quota+coverage overlap | 475,477s | 293,870s |
| Quota gagal ketika coverage lolos | 319,864s /69,36% | 322,149s /53,63% |
| Coverage lolos sebagai denominator | 461,195s | 600,710s |
| Coverage gagal | 738,606s | 599,251s (warm-up saja) |
| Seluruh guard teknis lolos | 136,324s (2m16s) | 277,561s (4m38s) |
| Run teknis lolos terpanjang | 4,253s | 6,752s |
| Rekonsiliasi lengkap berhasil/gagal | 93 /0 | 89 /0 |
| Cadangan transaksi minimal, termasuk lease | 15 | 15 |
| Lease belum dipakai maksimum teramati | 0 | 7 (alokasi batch8; claim pertama langsung) |
| Binance/Lighter reconnect | 2 /0 | 0 /0 |
| Market queue overflow | 1 | 0 |

Penurunan quota-block total22,55%; saat coverage lolos, proporsi terblokir
turun69,36%→53,63%. Durasi quota-only absolut hanya turun7,463s karena
window coverage kali ini lebih panjang. **Jangan menjumlahkan overlap atau
menyebut semua kenaikan waktu siap sebagai hasil scheduler.** Sesi closed/
OFF bukan kegagalan teknis. Guard lain dan signal/izin tidak dilonggarkan.

Semua89audit sesudah mempunyai8GET lengkap: account89,activeOrders89,
deposit178,withdraw178,transfer89,trades89. Setiap logical call berbobot
lokal1, alasan account_reconciliation, HTTPattempt yang dilacak1. Timeout/
retry SDK/429yang teramati0; maksimumdurasi full audit1.433,316ms,
median991,822ms,P951.189,433ms. Queue pool teramati0 dan waktu tunggu lock
rekonsiliasi maksimum0,021019ms. Tidak ada posisi atau proteksi mainnet baru
untuk menguji interleaving exit: bukti audit/lock macet tidak menghambat
proteksi berasal dari regresi lokal, bukan transaksi pada rekaman ini.
Reservasi tidak dikira request fisik baru. Tidak ada cache history
atau pagination yang dilewati. Cash matched1199,961s;0,039sawal belum
diamati, bukan audit kas yang gagal. Last-source tetap fresh, tidak diubah
oleh heartbeat atau akun diam. Account/funding guard gagal setelah startup0s.

Recorder yang sudah berjalan menyimpan623.473event pada window: disconnect/
overflow0, FX115respons sukses pada loop biasa. Fail attempt/RTT HTTPX FX
tidak diinstrumentasi; jangan menggabungkan bucket Binance dengan Lighter
atau menganggap data age sebagai fill latency. Lag event loop maksimum
teramati9,7ms. Absennya reconnect20m tidak membuktikan masalah eksternal
reset1006sebelumnya hilang.

**Keputusan:** perubahan konkret mengurangi tekanan dan menyediakan window
entry dengan account fresh/audit lengkap/exit reserve, tetapi blocker kuota
belum selesai sepenuhnya. Menjamin entry-ready kontinu dengan8GET/freshness
15s dan guard≤33 tetap tidak layak. Satu tindak lanjut arsitektur yang paling
spesifik adalah pemisahan lane account-state segar dari full-history barrier
sebelum entry/sesudah lifecycle seperti di bawah; butuh perubahan kebijakan
guard yang dijelaskan dan disetujui sebelum implementasi. Tidak ada usulan
menaikkan quota, menurunkan threshold, atau memulai sesi trading.

## Checkpoint dan WebSocket: batas bukti

Trade history sudah incremental dengan sort trade_id descending dan
checkpoint/deduplikasi. Fee exactNULL tidak diubah menjadi0; sumber resmi
reported0,0000DP4 tetap terpisah. Lookup data yang tidak tersedia tidak
dibuka menjadi polling tanpa batas.

History funding tidak memiliki filter lower-bound timestamp/ID yang
terverifikasi pada SDK1.1.5; filter/cursor tersedia, tetapi kontrak ordering
append-only dan immutability tail untuk early-stop pada checkpoint tidak
ditetapkan oleh [SDK resmi TransactionApi](https://github.com/elliottech/lighter-python/blob/main/docs/TransactionApi.md).
Maka checkpoint funding untuk melewati tail tidak diterapkan. Deduplikasi
flow_id pada ledger tetap ada; audit page-limit/cursor-loop/unknown asset/
unsettled status tetap fail closed. Tidak membuktikan tail hanya dari saldo
yang tidak berubah atau page pertama yang sama.

Kanal akun resmi dievaluasi dari [referensi WebSocket](https://apidocs.lighter.xyz/docs/websocket-reference).
Ada subscribed snapshot dan update account_all/account_market/orders serta
user_stats; position funding bukan riwayat deposit/withdrawal/transfer penuh.
Semantik completeness/gap global semua kanal dan freshness saat akun diam
belum cukup dibuktikan untuk menggantikan REST audit ini. Kanal account_market
yang ada tetap memberi petunjuk fill, dengan REST sebagai otoritas.
Heartbeat/idle socket tidak mengubah Stamp account. Tidak menambah subscription
atau mempercayai silent socket sebagai account baru dalam tugas ini.

Arsitektur spesifik bila kesiapan entry kontinu diwajibkan: pisahkan lane
REST posisi/balance/orders segar≤15s dari full-history audit dengan barrier
audit lengkap **tepat sebelum entry dan setelah lifecycle**, didukung WS
hanya setelah bootstrap/continuity/reconnect semantics teruji. Itu mengubah
definisi freshness full-history pada guard, sehingga **belum diterapkan**;
bukan mengganti audit lengkap dengan cache atau heartbeat. Untuk history
23GET, sekurangnya5batch/60s=115GET, melampaui40bahkan60 tanpa lifecycle.
Menambah interval saja tidak dapat memenuhi semua syarat pada kasus itu.

## Pemeriksaan akhir

Pada **12:14:16.883 WIB** (05:14:16.883UTC), status lokal yang bersumber dari
REST engine menunjukkan akun751745 Standard/Unified/Cross, key indeks publik4,
equity/available **49,430756USDC**, posisi semua market0, seluruh order aktif0,
pending intent0. Umur account6,8s dan funding audit6,7s; audit kas matched,
selisih0. Saldo tidak berubah dari awal verifikasi.

Mesin **IDLE**, `allow_live_orders=false`, entry stopped, sesi sebelumnya
closed dengan attempts kosong, autostart disabled. Tidak ada rearm, entry,
probe, broadcast, cancel, close, atau deposit dalam tugas ini. Empat fee exact
tetapNULL, fee reported0,0000 pada4desimal beserta sumber/waktu/hash ekspor
tetap terpisah; metadata fee tidak diklaim selesai. Pemeriksaan membaca status
engine lokal, tanpa menambah polling exchange. Bukti lengkap tanpa rahasia
ada di `audits/audit-scheduler-final-account-20261005.json`.

## Artefak

- `audits/audit-scheduler-simulation-20261005.json`: replay scheduler produksi.
- `tests/fixtures/audit-eight-get-trace-20261005.json`: durasi/path sanitasi.
- `audits/audit-scheduler-before-20261005.json/.csv`: baseline rekaman terakhir
  dihitung ulang, bukan sesi/rekaman baru.
- `audits/audit-scheduler-after-20261005.json/.csv`: satu verifikasi1200s.
- `audits/audit-scheduler-comparison-20261005.json`: perbandingan dan overlap.
- `audits/audit-scheduler-market-evidence-20261005.json`: bukti Recorder existing.
- `audits/audit-scheduler-final-account-20261005.json`: akun, kas, intent dan izin akhir.
- Raw snapshot/config hash/tes private: `~/Workspace/outputs/lighter-audit-scheduler-20261005/`.
  Marker started melarang pengulangan; tidak di-commit.

Baseline main recording memakai ea46fbf sebelum fix overflow b493ac0.
Hasil coverage/seluruhguard tidak boleh diatribusikan hanya pada scheduler:
waktu jaringan/market berbeda dan fix fairness juga sudah aktif pada runtime
baru. Replay mengisolasi scheduling; tabel runtime menunjukkan hasil yang
benar-benar teramati, tanpa memperpanjang rekaman untuk mencari angka bagus.
