# Audit depth-only produksi dan konsistensi telemetry — 6 Oktober 2026 WIB

Baseline bersih `d162985`, termasuk guard sumber `dccf067`. Tugas ini hanya
menghapus subscription public bookTicker yang tidak dipakai quote, memperbaiki
assessment diagnostik ganda, lalu satu restart terkontrol dan satu observasi
1200 detik. Tidak ada izin order, probe, rearm, LOAD_SESSION_PERMISSION atau
START_LIVE. Sesi sebelumnya tetap closed.

## Kriteria ditetapkan sebelum restart dan rekaman

- Verifikasi akun nyata flat, seluruh order/pending/intent bersih, audit dana
  matched, izin OFF dan sesi closed sebelum restart. Jika tidak terbukti,
  rollout dihentikan tanpa transaksi.
- Tes offline harus membuktikan snapshot + depth buffer, bridge U/u dan pu,
  duplicate/out-of-order, gap dan recovery; transport palsu tanpa bookTicker.
  Source late/missing/future/uncertain tetap diterapkan untuk sequence, tidak
  menambah coverage/basis atau mengizinkan entry. Proteksi/exit tetap terpisah.
- Regresi telemetry memakai Engine.data_valid asli, bukan fixture stub.
  Satu assessment sumber harus menentukan checks dan reason dalam satu
  diagnostic_observation; pemeriksaan entry berikutnya harus menilai ulang.
- Restart tepat sekali setelah tes lolos; catat loaded commit, PID dan waktu.
  Rekaman 1200 detik sejak awal pengamatan proses baru, termasuk warm-up;
  tidak diulang, tidak diperpanjang, tidak mereset gap. Kolektor hanya membaca
  status/trace lokal yang telah dikonsumsi engine, tanpa polling exchange baru.
- Ukur bootstrap, subscription aktual, reconnect/sequence gap, backlog,
  receipt/source age dan uncertainty clock, blocker berikut overlap, warm-up
  dan readiness setelahnya, longest ready interval, freshness akun, kapasitas
  request dan audit. Belum adanya reconnect runtime bukan bukti penyebab lama
  telah diperbaiki; recovery yang dipaksa hanya bukti pengujian terisolasi.
- Tidak mengubah DNS/TLS/SNI/backoff, memasang IP produksi, atau mengubah guard,
  threshold8bps, buffer5bps, clock uncertainty, coverage, fee dan batas risiko.
  Empat fee exact historis tetapNULL, reported fee tetap terpisah.
- Keputusan menerima penyederhanaan didasarkan pada subscription depth-only
  aktual, bootstrap/sequence yang benar, telemetry konsisten, serta tidak ada
  exposure/intent/order/otorisasi baru. Gangguan runtime tetap dicatat lengkap
  dan menjadi batas kesiapan operasional, bukan alasan melonggarkan guard.

## Pemeriksaan ketergantungan dan implementasi

Quote Binance berasal dari Book.binance_snapshot/binance_delta melalui
Engine._apply_market. bookTicker tidak mengubah book, sampling, flow, stop
atau exit. Monitoring/dashboard memakai quote dan status channel public,
tidak membutuhkan ticker. Parser tetap mengenali event bookTicker untuk replay
rekaman historis; tidak ada subscription produksinya. REST Spot bookTicker
USDCUSDT untuk normalisasi kurs bukan subscription futures yang dihapus.
aggTrade tetap pada /market. Skrip A/B mempertahankan URL combined sebagai
arm pembanding historis, bukan konfigurasi produksi.

Diagnostic observe memakai market_data_valid (guard receipt/transport asli)
dan satu source_freshness yang sama untuk checks dan alasan. data_valid pada
jalur sampling/entry tetap asli dan melakukan assessment sumber baru setiap
panggilan; tidak ada cache diagnostik yang dapat mengizinkan entry.
Fixture pengujian kini memasang keadaan Feeds/bridge palsu yang lengkap agar
guard transport sebenarnya dapat diperiksa, tanpa menjalankan socket.
Kolektor lama dalam toolchain Python proyek dipakai kembali dan menolak PID
yang berubah serta order/pending exchange; tidak dibuat klien exchange baru.

## Hasil pengujian dan observasi

Sebelum rollout: 103 tes terarah lulus; seluruh suite 410 tes lulus dengan
tiga warning deprecation dependency yang sudah ada. Ruff dan mypy20module
lulus. Regresi boundary yang sama dijalankan terhadap sumber baseline d162985
(PYTHONPATH eksplisit ke checkout arsip tanpa secret) dan gagal tepat pada
technical_ready, sehingga memang mereproduksi pemanggilan ganda sebelum fix.
Tes depth-only memakai Feeds._binance_public, snapshot dan socket palsu;
recovery memakai backoff2s asli. Fixture baru tidak membuka transport nyata.

Percobaan pengujian awal ikut disimpan: dua fixture entry belum memasang
keadaan bridge/feed, import fixture terlambat ditambahkan, dan pemasangan
feed global mengubah skenario tes slow-poll. Fixture diperbaiki secara lokal
sesuai skenario; tidak ada perubahan guard produksi untuk meluluskan tes.
Log mentah ada di Workspace/outputs/lighter-depth-production-tests-20261006.

Observasi belum dimulai pada commit persiapan ini. Hasil, raw evidence/hash,
versi yang dimuat, waktu dan keadaan akhir ditambahkan setelah satu rekaman.

## Rollout yang benar-benar dilakukan

Pemeriksaan sebelum restart memakai status akun yang telah dikonsumsi engine:
akun751745, equity/collateral49.430756USDC, Standard/Unified, margin100%=1×,
posisi/order/pending/posisi lain/intent nol, komponen akun lengkap dan <15s,
historycomplete dan kasmatched selisih0. Engine290293 IDLE, entries stopped,
orderOFF dan UUID9ce8b029-1f0b-4b1e-afb7-f6c1732e7b3a tetapclosed; tidak ada
probe/protection/exit yang berlangsung. config allow_live_orders dan kedua
flag pengecualian feeOFF. Bukti before-restart.json menyimpan timestamp asli.

Satu `systemctl --user restart lighter-live-desk-engine.service` pada
**01:19:21WIB** memuat commit **818b508137f8647502353658f1875670a21caf8c**,
PID baru **301678**. Tidak ada restart API, LOAD/rearm/START_LIVE atau modifikasi
izin. Marker rekaman dibuat **01:19:34.358374WIB** (18:19:34.358374UTC tanggal5),
deadline tetap **01:39:34.358374WIB**. Ada sekitar13s startup sebelum marker;
1200s pengamatan tetap mencakup seluruh sisa warm-up dan gangguan. Tidak ada
reset atau pengulangan rekaman. Perubahan analyzer offline sesudah rollout
bukan perubahan modul engine; hash seluruh src/lldesk dan config diverifikasi
kembali oleh kolektor saat selesai. Threshold/risk/clock/fee/transport tetap.

Pipeline analisis Python yang sudah ada diperluas dengan mode tanpa permission
reload, metrik source dan kuota serta deduplikasi trace aktif/selesai; enam tes
analyzer offline lulus dalam0.05s. Tidak ada klien Rust A/B atau subscription
pembanding baru dalam rekaman ini. Kolektor hanya GET API status lokal4Hz;
engine tetap menjadi satu-satunya pemilik koneksi dan pembacaan exchange.

## Hasil satu rekaman runtime

Marker selesai **01:39:34.358736WIB**, durasi monotonic1200.000380s untuk
window analisis tepat1200s. Terdapat4798snapshot lokal, error kolektor0,
hash modul/config tidak berubah. Awal0.033092s belum teramati; tidak dianggap
siap. Integrasi waktu menggunakan snapshot4Hz dan interval hingga snapshot
berikutnya, jadi ini estimasi dari telemetry, bukan pengamatan setiap nanodetik.

Bootstrap snapshot diterapkan18:19:24.129555UTC, sebelum marker, sequence
11742095104539,1000bid+1000ask. REST T1791224364034 dan E1791224364038
tetap asli. HTTP handshake /public dan /market101, snapshot /fapi/v1/depth200.
Loaded source hash dan commit mengikat URL produksi
`wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms`,
aggTrade tetap `/market/stream?streams=btcusdt@aggTrade`. Trace HTTP sengaja
hanya menyimpan path tanpa query; bukti subscription ditriangulasi dengan
loaded source hash, restart terverifikasi dan ingress nyata: **11764depth,
13398aggTrade,0futures bookTicker** dalam window. Tidak diklaim query telah
direkam di wire. Normalisasi kurs tetap menggunakan REST SpotUSDCUSDT.

Public, market dan Lighter masing-masing0reconnect; sequence gap kedua book0,
overflow0. Gap penerimaan maksimum: depth0.700939s, Lighterbook0.606170s,
aggTrade3.175218s, FX2.319507s. Tidak ada gap book/FX>5s. Backlog maksimum8,
lag loop yang dilaporkan maksimum19ms. Receive→processing depth maksimum
112.427ms termasuk bootstrap sebelum window; receive→recorder dalam window
maksimum46.213ms, enqueue→recorder46.108ms; operasi apply depth maksimum
16.912ms. Untuk Lighter: receive→processing57.780ms dan apply13.702ms.
Angka ini tidak mengukur buffering sebelum aplikasi menerima pesan.

| Ukuran kesiapan | Hasil |
| --- | ---: |
| Warm-up yang teramati dalam rekaman | 590.272s / 9m50.272s |
| Sampel pertama warm-up complete | t=590.305s |
| Sisa window setelah warm-up | 609.695s |
| Seluruh guard teknis lolos | 513.405s / **8m33.405s** |
| Proporsi siap setelah warm-up | **84.207%** |
| Interval siap kontinu terpanjang | **14.256s** |
| Blocker coverage setelah warm-up | 0s |
| Blocker kuota entry/account freshness/history completeness | 0s |

Kesiapan ini untuk evaluasi sinyal + admission kapasitas preflight, bukan
bukti audit satu percobaan telah selesai atau izin mengirim order. Sesi closed
dan orderOFF sengaja tidak dihitung sebagai kegagalan teknis.

Sesudah warm-up, kombinasi blocker yang saling eksklusif berikut menjumlahkan
609.695s; durasi blocker per nama tidak boleh ditambahkan lagi:

| Kombinasi blocker | Detik |
| --- | ---: |
| Tidak ada | 513.405 |
| basis_scale + market_data + source_freshness | 41.769 |
| basis_scale saja | 47.519 |
| reconciliation_idle saja | 6.253 |
| basis_scale + market_data + source_freshness + reconciliation_idle | 0.750 |

Seluruh basis_scale blocker setelah warm-up90.038s mempunyai
`decision_inputs.data_valid=false` pada evaluasi sebelumnya. Tidak ada bukti
scale nol pada evaluasi input valid yang menjadi penyebabnya. Source guard
diag saat ini dapat sudah lolos sementara keputusan sebelumnya masih ditolak;
itu tidak disamakan dengan kegagalan menghitung basis atau hilangnya feed.
Tidak ada perbedaan timestamp quote evaluator dan diagnostik pada4798sample.

## Sumber, clock dan request

**0pelanggaran** konsistensi source assessment versus checks pada4798snapshot.
Guard source gagal74.533s total,42.519s setelah warm-up; seluruhnya
`SOURCE_TIMESTAMP_FUTURE_UNCERTAIN`, bukan SOURCE_DATA_LATE atau expiry clock.
Ini interval offset yang belum dapat membuktikan timestamp tidak future,
bukan bukti exchange salah jam atau gangguan jaringan. Half-width clock
median110.068ms,p95128.427ms,maksimum152.196ms; limit250ms tidak berubah.
Receipt age Binance median67.197ms,p95126.661ms,maksimum525.066ms.
Indikator antarjam maksimum932.929ms; source upper bound median279.597ms,
p95380.698ms,maksimum1087.759ms (<1500ms). Tidak dianggap latensi fill.

Ada20rekaman clock attempt:19verified dan1clock_unverified (id9,HTTP200,
RTT1556.944ms melebihi batas instalasi400ms). Respons lambat tidak menyegarkan
proof lama; usia evidence maksimum119.972s dan tidak ada expiry yang teramati.
Kegagalan ini tetap tersimpan, tanpa retry tambahan atau penggantian timestamp.
Clock20slot konservatif Binance Futures, Lighter0; FX600observasi tercatat,
599request yang mulai di dalam window, semuanya verified, memakai bucket Spot
sendiri. Beban/frekuensi kedua worker tidak diubah untuk pengukuran.

Pembacaan Lighter:252GET selama1200s = **12.6/min**, semuanyaHTTPattempt1/ok,
tidak ada retry/timeout. Endpoint account99,accountActiveOrders99,trades9,
deposit/history18,withdraw/history18,transfer/history9. Audit lengkap9 dan
state refresh90, semuanyaok. Seluruh snapshot usable mempunyai history
complete; audit berjalan tetap memblokir lewat reconciliation_idle11.755s
total, termasuk7.003s sesudah warm-up. Tidak melabeli audit sedang berjalan
sebagai bukti preflight baru yang sudah selesai.

Rolling usage yang teramati median10,p9518,maksimum18; used+reserved juga
puncak18, reservasi belum dikonsumsi maksimum7. Kapasitas transaksi tersisa
minimum**37**, selalu≥cadangan15; pagu40/55 tetap. Umur komponen akun maksimum
12.791s,active_orders12.901s (<15s); tidak ada blocker kuota atau akun basi.
Pembacaan statistik hanya lokal, tidak menambah GETexchange. Tidak ada signing,
broadcast, entry, fill baru atau perubahan dana dalam tugas ini.

## Keadaan akhir, bukti dan keputusan

Pemeriksaan akun sesudah rekaman **01:41:11.518118WIB**: PID301678 IDLE,
equity/available/collateral**49.430756USDC**, posisi0, seluruh order aktif/pending,
posisi lain dan intent0; account/order age5.440/5.554s, historycomplete (usia
93.870s), net cashmatched selisih**0.000000USDC**, perubahan kas**0.000000USDC**.
OrderOFF, entries stopped, sesi UUID sebelumnya tetapclosed. Empat fee exact
tetapNULL; masing-masing reported0.0000USDC pada empat desimal, source ekspor
resmi, identitas/ekonomi trade serta waktu pengambilan tetap terpisah. Tidak
diturunkan menjadi exact0. Tidak ada fee transaksi baru yang perlu diisi.

Keputusan: **penyederhanaan feed diterima**. Quote tetap berasal dari depth,
bootstrap/sequence benar, telemetry satu-assessment telah dimuat, akun bersih,
tidak ada trade atau otorisasi baru. Coverage tidak dihapus dan tidak dilonggarkan.
Recovery close/sequence gap yang dipaksa hanya dibuktikan lewat tes offline;
reconnect lama belum direproduksi atau ditemukan penyebabnya. Tidak ada klaim
stop baru diterima mainnet, strategi menguntungkan, atau readiness selalu stabil.

Satu langkah berikutnya yang paling didukung bukti: audit timing/interval clock
pada FUTURE_UNCERTAIN dan evaluasi sebelumnya yang gagal, memakai proof dan
timestamp yang direkam, untuk membedakan batas estimasi antarjam dari timing
evaluasi. Pertahankan guard; tidak mengubah threshold atau membuka sesi trading.

Raw tidak di-commit: `~/Workspace/outputs/lighter-depth-production-20261006/`
berisi started/finished, snapshots.jsonl.gz, before/final account, restart.txt,
summary dan satu copy immutable recording market PID301678. Copy gzip tersebut
adalah prefix stream engine yang masih terbuka, sehingga tidak mempunyai trailer
final; analyzer membuktikan terdapat record sesudah deadline sebelum EOF dan
hanya menganalisis window tetap1200s. Engine tidak dihentikan lagi untuk menutup
file. Hash berlaku pada copy yang dibekukan, bukan file live yang terus berubah.

Summary dan final-account tanpa secret disimpan di
`audits/depth-only-production-20261006/`; RAW_SHA256SUMS mencakup tujuh bukti.
SHA-256 utama:

| Artefak | SHA-256 |
| --- | --- |
| started.json | 948ab55b002fe0729e6373e5f5873daf955c87b17e360c2f912b5a0c9bd5cd74 |
| finished.json | 4fad2c6d5864f33dccbde78e4b7fc23b5ed285bf6448284aa513d5f542d151aa |
| snapshots.jsonl.gz | 19bb5650b707297f68d74762754e35d49c0b66e63c6f6d77b8bf48393accb5e2 |
| market prefix PID301678 | 9f6b0b82e4ba39ef12b53facf576055bf1bbb4ebf7db9af9450521bc073b2db9 |
| before-restart.json | a8bc273989ac7a0b1ea861450756d7bce8904364ec870881340256b736fcbf1b |
| final-account.json | 46f92321dae73719e9dc1b8318e965217b8abf83639314ab7a64b8b3f74123de |
| restart.txt | 7b0340341dcfcf8bea17fa616bb203d439f9f17aedd8f134928caad9cdd5b8f9 |

Loaded: src/lldesk dan sourceguard terdahulu + depth-only/telemetry fix818b508.
Analyzer, unit test analyzer dan audit sesudahnya adalah artefak offline; tidak
memerlukan restart atau pemuatan izin. Raw code/config hash unchanged menutup
bukti versi yang benar-benar berjalan. Tes utama410passed, tes analyzer6passed,
ruff/mypy bersih. Key/token/config/state/rekaman pasar tidak masuk commit.
