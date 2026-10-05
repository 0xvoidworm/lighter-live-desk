# Kontrol sesi dan isolasi observasi kurs — 5 Oktober 2026

Lingkup: diagnosis, implementasi, dan paling banyak satu observasi1200s tanpa
order. Tidak ada otorisasi baru, START_LIVE, probe, deposit, order dummy,
signing transaksi atau broadcast pada akun nyata. Basis repo9e7084e/4e40915.
AGENTS.md, RUNBOOK, audit sesi, kode actual dan rekaman privat dibaca dahulu.

## Keadaan aktual sebelum mutasi runtime

11:32:52UTC: PID244409 IDLE/orderOFF/entry stopped, sesi5c2672c7...closed.
751745 Standard0/Unified1/Cross0/initial margin100;49.430756USDC,
globalposition/active/pending/unresolvedintent0, history lengkap/cash matched.
Empat fee exactNULL dan official reported0.0000DP4 tetap. Coverage saat itu
terblokir gap baru5.704s dan Binance public reconnect3; ini dipisahkan dari
insiden5.551s lama. Akun harus dicek lagi sebelum deployment/restart.

## Diagnosis gap lama: bukti dan keterbatasan

Timeline lengkap tanpa rahasia: audits/control-feed-gap-timeline-20261005.json.
Snapshot kurs187 mulai11:06:45.093977UTC, menunggu header HTTP2000.007ms,
timeout11:06:47.095457UTC/total2001.475ms. Request memakai koneksi yang ada;
failure_phase=receive_response_headers, bukan bukti DNS/TCP/TLS error.
FX verified observation gap6.378s, berbeda dari gap valid-sample5.551s.

Binance depth terakhir11:06:43.898901, berikutnya11:06:48.580271 (gap4.681s),
Lighter book terakhir11:06:43.753801, berikutnya11:06:50.317190 (gap6.563s).
Tidak ada pesan book yang tercatat diterima aplikasi selama FX header wait.
Pesan berikutnya diproses2.588ms/0.234ms; maksimum seluruh pesan7.472ms pada
window20s, tidak ada antrean pemrosesan>100ms. Evaluator menghasilkan27
evaluasi berbeda dalam window8s, status heartbeat age≤0.289s, tanpa snapshot
book evaluator yang berbeda dari book terkini. Tidak ada bukti event loop,
lock FX, atau queue market menahan feed beberapa detik.

Source event time book berikutnya hanya101ms setelah source event sebelumnya
meskipun diterima beberapa detik kemudian. Ini konsisten dengan penundaan
sebelum observasi aplikasi, bukan bukti sumber exchange berhenti. Stempel lama
dibuat setelah JSON parse/emit, dan tidak merekam kernel packet/internal
socket-reader time. Karena itu jaringan versus buffering sebelum app read
belum dapat dipisahkan. Jangan menyalahkan ISP/router/exchange/laptop.

Terpasang aiohttp3.14.3, HTTPX0.28.1, websockets16.1.1. Binance aiohttp
total10/connect5/ceil_threshold5; receive WebSocketNone/close10. Helper ceil
menambah kurang dari1s pada timeout tertentu (detail equality berbeda antar
helper). FX memakai HTTPX dan asyncio total2s; tidak memakai aiohttp timeout.
Tidak ada bootstrap connect/reconnect dalam insiden ini. Kemiripan angka
5.551 dengan pembulatan5s tidak membuktikan hubungan sebab-akibat. Tidak
ada timeout/threshold yang dilonggarkan atau dependency yang diperbarui.

## Perubahan terkecil

Jalur FX, feed, account refresh dan risk worker sudah memakai task terpisah;
tidak ditemukan ketergantungan lokal yang perlu direfactor. Tidak mengubah
concurrency FX/normalisasi/kurva basis atau guard. Tambahkan observabilitas:
receipt Stamp tepat sesudah app WebSocket read sebelum JSON parse;
exchange timestamp tetap asli; enqueue monotonic stamp, processing start/end,
queue wait/parse-to-enqueue/source receipt gap dan applied/old-sequence counters.
Recorder menyimpan enqueue stamp, status market_processing membedakan feed
yang diterima dari perubahan book yang diterapkan. Duplicate/old-sequence,
ticker/heartbeat tidak memperbarui quote; umur/gap historis tidak diisi ulang.

Command LOAD_SESSION_PERMISSION: engine adalah pemilik runtime config/state.
Command memuat hanya izin dari config lokal setelah UUID authorized belum
dipakai, identitas/config/risk hash, flat/account≤15s/history/kas/fee cocok.
Ia mempertahankan objek/buffer coverage, quote/account timestamp, deadline,
kuota dan history; tidak memanggil START_LIVE/order/signing atau network.
Risk/identity changes di luar permission flags ditolak. START_LIVE tetap
command terpisah setelah seluruh preflight; identity/hash diperiksa atomik
lagi pada aktivasi. Restart fisik tidak memulihkan loaded permission otomatis.

Authorization pra-aktivasi mempunyai expiry persisten20menit sejak authorize,
sesuai batas persiapan yang sudah digunakan. LOAD/reload tidak memperpanjang
expiry. Record legacy tanpa expiry ditolak, tidak di-upgrade menjadi izin baru.
Sesi aktif tetap1800s sejak START_LIVE, quota≤3 sebelum signing; expiry
pra-aktivasi tidak menggantikan deadline aktif. Active reload ditolak tanpa
mengubah deadline/attempts. Expiry/revocation retire UUID dan memblokir reuse.
STOP_ENTRIES/reloadOFF mencabut izin/fee flags dalam memory dan disk; active
record masuk ending dan proteksi/cancel/close/monitoring tetap berjalan sampai
cleanup. Supervisor tidak lagi menulis config/session sendiri untuk revocation.

Threshold8bps/scale multiplier4, buffer5bps, gap5s/window600s/480samples,
quote1500ms/FX15s, Lighter40/47/55+exit15, notional25/exposure1×/risk0.50/
loss harian5, fee exactNULL/report policy, split state/history tetap.

## Kriteria satu observasi runtime, ditetapkan sebelum mulai

- Tepat1200s sejak recorder dimulai, mencakup warm-up dan gangguan; tidak
  mengulang/mereset window demi hasil. Rekaman hanya membaca local status dan
  Recorder yang sudah dikonsumsi engine, tidak menambah polling exchange.
- Akun real flat/orderOFF/sessionclosed sepanjang window; tidak membuat
  otorisasi, START_LIVE atau intent. Source/config dibekukan.
- Masa warm-up dilaporkan terpisah dari technical-ready sesudah warm-up;
  closed/orderOFF bukan blocker teknis. Gap setiap book/FX, queue/parse/apply,
  coverage/market/account/quota blockers dan overlaps dilaporkan apa adanya.
- Satu LOAD_SESSION_PERMISSION dengan diskOFF pada akun real hanya menguji
  revocation/no-reset, tidak memuat izin positif; buffer epoch/quote/account
  timestamps/session deadline/quota tidak di-reset. Positive load/activation
  hanya diuji dengan akun77 dan fake order transport pada tmpdir.
- Header FX tertunda diuji dengan HTTP server lokal; feed nyata HTTP/WebSocket
  library lokal harus tetap menerapkan book, account refresh/risk loop tetap
  berjalan, FX stale tetap invalid, zero transaksi nyata. Runtime tanpa
  kegagalan FX tidak diklaim membuktikan penanganan outage mainnet.

## Hasil pengujian dan observasi

Sebelum deploy: suite penuh **384 passed, 3 warnings**; Ruff check/format
lulus, mypy lulus untuk19 source files. Regresi mencakup HTTP header tertunda
2 detik dengan kedua WebSocket library lokal, account refresh/risk worker,
kurs basi, serta expiry/revocation/closed/restart dan aktivasi transport palsu.
Tidak ada broadcast di pengujian. Trace SDK signing tanpa broadcast yang
sudah ada juga tetap lulus. Analyzer tambahan offline lulus Ruff check/format;
tidak mengimpor transport exchange atau signer.

Satu deploy kode0615782 dilakukan setelah pemeriksaan segar memastikan
PID244409 IDLE/OFF/closed, seluruh posisi/order/intent bersih. Percobaan awal
script deployment berhenti pada import module yang salah sebelum pemanggilan
restart; diperbaiki, diperiksa ulang, lalu engine/API direstart satu kali.
PID baru255514; tidak ada authorize/START_LIVE/PROBE/signing/broadcast.
Restart ini memasang kode baru; perubahan izin berikutnya tidak membutuhkan
restart. Service tetap disabled untuk autostart.

### Observasi runtime tunggal, hasil apa adanya

19:12:16.216954–19:32:16.217423WIB (12:12:16–12:32:16UTC), deadline1200s,
elapsed monotonic1200.000497s, 4798snapshot lokal/error0. Hash source19file
dan konfigurasi tidak berubah. Data berasal dari recorder dan trace yang
sudah dikonsumsi engine; statistik tidak menambah polling exchange.
Raw private: `/home/yuri/Workspace/outputs/lighter-control-feed-observation-20261005`.
SHA256snapshot: `932b15987132fac35ddc886257724a514a9048fbe9d9dd06679c5714163aeb48`.

| Ukuran | Hasil |
|---|---:|
| Warm-up dalam rekaman |593.755s (9m53.755s)|
| Interval setelah warm-up |606.213s|
| Seluruh guard teknis lolos |553.941s (9m13.941s;91.38% setelah warm-up)|
| Interval lolos kontinu terpanjang |52.521s|
| Capture sebelum snapshot pertama, tidak diklasifikasikan |0.032s|
| Gap penerimaan depth Binance terbesar |4.149s|
| Gap penerimaan book Lighter terbesar |3.763s|
| Gap penerimaan observasi kurs terbesar |2.378s|
| Umur kurs konservatif terbesar |2.347s (<15s)|
| Umur state account/orders terbesar |12.801s/12.909s (<15s)|
| Receipt→recorder Binance/Lighter terbesar |114.686ms/35.178ms|
| Queue→recorder Binance/Lighter terbesar |33.109ms/35.111ms|
| Loop lag terlapor terbesar / backlog terbesar |12.9ms/51event|

Warm-up tetap600s sejak engine_start; sekitar6.2s terjadi sebelum capture,
tidak dihapus/diperpanjang. Quote ages maksimum3999ms/3538ms menyebabkan
blok sesuai batas1500ms. Tidak ada gap penerimaan book/kurs>5s; valid-sample
gap maksimum sepanjang proses4.439s, long_gap_count0. Coverage tidak di-reset
oleh izin atau reconnect dan tidak diberi data salinan untuk mengisi gap.

Setelah warm-up, blocker durasi tumpang tindih: market_data27.761s dan
basis_scale27.761s (bersama; tidak ada input valid untuk perhitungan basis),
entry_request_reserve27.262s, reconciliation_idle6.252s,
reconciliation_fresh2.501s, minimum_sizing_depth3.502s. Coverage blocker0s
setelah warm-up. Jumlah durasi blocker **tidak boleh dijumlahkan**: hanya
52.272s dari interval post-warm-up tidak lolos seluruh guard. Tabel overlap
lengkap ada pada JSON audit. Izin OFF/sesi closed dikeluarkan dari guard teknis.

600request kurs semuanya verified: median95.575ms, p95 129.740ms,
max422.158ms. Tidak ada timeout FX runtime, sehingga kasus header timeout
dibuktikan oleh tes HTTP/WebSocket lokal dan bukan direproduksi pada mainnet.
Transport book mengalami3reconnect Binance public dan1Lighter; Binance trade
tidak reconnect. Binance mencatat1006/ClientConnectionResetError, Lighter
ConnectionClosedError; ini bukti putus socket, bukan identifikasi ISP/exchange.
Timestamp stage mengukur setelah application read; buffering sebelum read
dan resources laptop pada kejadian lama tetap tidak diketahui. Recorder
dipanggil sebelum book diterapkan; waktu apply tersedia terpisah pada status.

44flag decision_input_match=false semuanya pada book absent/market_data
invalid. Flag mensyaratkan dua book non-NULL; membandingkan timestamp snapshot
aktual menghasilkan **0perbedaan**, termasuk **0ketidakcocokan saat eligible**.
Tidak ditemukan evaluator memakai snapshot lama pada interval ini.

274SDKGET (13.7/min), peak rolling60s32, minimum kapasitas transaksi23slot
(>=15cadangan exit). 12audit lengkap sukses ×8GET;89state refresh sukses
×2GET. Audit median987.364ms/max1289.489ms, lock-wait max0.013ms. Entry
quota blocker27.262s terjadi tanpa preflight kandidat; guard tetap menghitung
biaya audit8GET dan send2/envelope urgent22, tidak menyatakan siap mengirim
tanpa audit terikat. Tidak ada percobaan pengiriman atau dummy order. FX tetap
menggunakan bucket Binance terpisah; perubahan ini menambah0request exchange.

LOAD_SESSION_PERMISSION command9 dengan diskOFF diproses pada runtime nyata:
PID sama, record session identik, buffer epoch tetap,89sample sebelum/sesudah,
hash config sama, order OFF. Pengumpulan data selanjutnya berlanjut. Positive
load/activation, expired/revoked/closed/restart tetap bukti terisolasi saja.

Bukti agregat yang dapat diaudit:
- `audits/control-feed-observation-20261005.json`: interval/gap/stage/FX/permission.
- `audits/control-feed-request-audit-20261005.json`: endpoint/biaya/audit/kuota/overlap.
- `audits/control-feed-final-account-20261005.json`: pemeriksaan akhir aktual.

### Keadaan akhir dan tindak lanjut

Pada19:33:22.835WIB, account751745 Standard0/Unified1/Cross0 diverifikasi
dari state REST segar: equity/available49.430756USDC; posisi semua market0,
order aktif/pending0, unresolved intent0. Ledger tetap4fill; seluruh intent
historis terminal (FILLED4/CANCELED1/REJECTED1). IDLE, entries_stopped=true,
allow_live_orders=false, sesi lama closed/tombstoned, attempts0. Tidak ada
otorisasi trading tersisa dan tidak ada aktivasi autostart.

Perubahan kas0.000000USDC; net_difference0.000000/matched, audit lengkap.
Fee exact4fill tetapNULL; total reported0.0000USDC pada4desimal dengan bukti
ekspor yang cocok. Ini tidak membuktikan fee exact0. Tidak ada fill baru.

Hambatan restart hanya untuk izin sudah diperbaiki dan diuji; ketergantungan
FX yang menahan book/account/exit tidak ditemukan pada kode/timeline.
Langkah berikut paling berguna: gunakan pemuatan izin eksplisit tanpa restart
pada persiapan sesi berikutnya **setelah ada otorisasi baru**, mempertahankan
coverage saat ini dan tetap menunggu seluruh preflight. Itu menguji hambatan
operasional yang terbukti tanpa tuning threshold. Konektivitas masih dapat
memblokir sementara; hasil20menit ini tidak menjamin kesiapan kontinu/fill
atau profitabilitas strategi.
Dokumentasi primer: [aiohttp timeout](https://docs.aiohttp.org/en/stable/client_quickstart.html#timeouts),
[ClientTimeout](https://docs.aiohttp.org/en/stable/client_reference.html#aiohttp.ClientTimeout),
[HTTPX](https://www.python-httpx.org/advanced/timeouts/). Installed source3.14.3
diperiksa langsung; stable documentation sekarang3.14.4, bukan bukti runtime
3.14.4 terpasang. Engine/SDK Python dipertahankan karena integrasi existing.
