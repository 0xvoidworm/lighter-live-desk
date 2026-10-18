# Pemilihan proof clock — implementasi dan satu verifikasi read-only

Baseline repo `13208db`; runtime sebelum pemuatan `1205d35`/PID341351.
Kode yang dimuat `24455ffc5a7f0e24cddb2213c9c333499b966061`/PID363683,
melalui satu restart terkontrol. Otorisasi hanya implementasi/tes/load dan satu
observasi1200s tanpa order. Tidak ada LOAD_SESSION_PERMISSION, START_LIVE,
probe, rearm atau perubahan strategi/konfigurasi.

## Kebijakan dan batas

Latest accepted selalu disimpan sebagai fallback. Maksimal tiga proof accepted
berada dalam RAM; restart unverified. Satu pasangan wall/monotonic dipakai untuk
latest, seluruh kandidat dan hasil assessment. Proof lama hanya dipilih jika
valid dengan anchor, TTL120s, drift100ppm dan validasi asli, serta seluruh interval
setelah drift tercakup dalam latest yang valid. Interval sama memakai latest;
narrower-but-shifted tidak cukup. Pemilihan independen dari hasil quote/sinyal.
Latest invalid tidak dapat dilewati oleh cache. Tidak ada intersection atau
penyegaran anchor/TTL.

Unsynced host, HTTP418/429, global wall-step dan recorder failure menghapus cache.
Stamp quote yang salah menolak quote, tanpa menganggapnya invalidasi clock global.
Entry/dispatch tetap assessment baru. Stop, deadline, cancel dan close independen.
Guard receipt/source1500ms, future lower>=-1ms, uncertainty250ms, host50ms,
wall discrepancy25ms, coverage600s/480/gap5s tetap. Threshold8bps, buffer5bps,
risiko25USDC/1x/0,50USDC/5USDC dan pagu40/55/cadangan15 tidak berubah.

Clock GET rutin60s/timeout2s/defaultkeepalive15s memakai TraceConfig pasif.
Jadwal/wake, host read, start/send/receive, validation flags, returned serverTime
(juga ketika ditolak), pool/reuse/DNS/create/header/body/decode dicatat jika
teramati. Create menggabungkan DNS/TCP/TLS; DNS nested, bukan tambahan yang dapat
dijumlahkan. TLS tersendiri dan resolver-internal cache unknown. Marker invokasi
request/hook header bukan bukti waktu wire/fill. Tidak ada request tambahan.
`selected_assessment` pada attempt adalah assessment terakhir yang telah selesai,
dengan timestamp evaluasinya sendiri; bukan penilaian atas proof baru yang baru
di-install. Keputusan dan diagnostic_observation menyimpan assessment aktual
masing-masing; tidak menyajikan penilaian terdahulu sebagai izin dispatch baru.

Clock evidence dipindahkan dari recorder pasar sinkron ke antrean512/writer thread
telemetry yang sudah ada, sebagai event source_clock di state/transport.
Overflow membatalkan proof/preflight; write failure kembali ke pemilik state
melalui call_soon_threadsafe dan membatalkan proof. Feed/risk loop tidak menulis
clock evidence ke disk. Batas event trace24/request dan recent32 tetap terukur.

## Bukti offline

Validator lama disalin byte-identical ke audits/baselines/source_time_1205d35.py:
SHA256 `5eb035308042dd7e71567b9c41cc6f5daf7bcd1069e784c09d1fbf6ef79a97b7`.
Analyzer historis dan manifest/hash lama tidak diubah. Replay memverifikasi
16file privat,6.666assessment,0mismatch baseline. Implementasi baru pada input
yang sama memilih older1107sample, memulihkan37,432249s penolakan sumber,
0penolakan baru; expiry tetap60,140031s. Coverage alternatif tidak dihitung,
tidak mengisi sampel hilang atau gap dan tidak menjadi izin live.

Suite produksi sebelum load:458passed/3warning deprecation lama,35,74s.
Ruff bersih; mypy22sourcefiles bersih. Tes memakai metode produksi, clock input
terkontrol, transport palsu/localhost aiohttp3.14.3 tanpa signer/broadcast.
Contained/shifted/equal/future/expiry/fallback/all-expired/invalidation/restart,
invariant latest-pass, decision assessment boundary, source-invalid sampling,
stop/deadline/close independence, slow recorder/overflow/write failure diuji.
Analyzer observasi tambahan4tes lulus; tidak mengubah source produksi yang dimuat.
Setelah analisis lengkap:53regresi clock/source/analyzer lulus (1,62s),
Ruff dan mypy22sourcefiles kembali bersih; tanpa restart atau observasi kedua.
Percobaan parsing pertama berhenti pada snapshot bootstrap dengan proof tersedia
tetapi book masih None. Perbaikan hanya analyzer: counterfactual tiga snapshot
tersebut dicatat unknown, bukan dibuatkan quote; seluruh actual blocker tetap
dihitung. Rekaman/runtime tidak diulang dan source produksi tidak berubah.

## Runtime dan keputusan

**Diterima:** kebijakan disetujui berhasil dimuat, invariant tetap, tidak ada
regresi keselamatan teramati. Observasi tunggal **14:13:42,891–14:33:42,891WIB**
(07:13:42,891–07:33:42,891UTC), window tepat1200s; overhead penutupan collector
0,000632s, bukan tambahan window. 4.798snapshot,0collectorerror, kode/config
hash unchanged. Source_time yang dimuat SHA256
`d30f6fb1ff5a9c07f82683234c99918fab48ec4d1ceb862bfa0e396445a9ba02`.
Kriteria sebelum restart: [criteria](audits/clock-proof-implementation-20261006/criteria.json).
Seluruh hasil: [runtime](audits/clock-proof-implementation-20261006/runtime.json),
[loaded/final](audits/clock-proof-implementation-20261006/loaded-final.json).

Warm-up pertama selesai600,834s; kesiapan teknis468,852s (7m48,852s),
78,251% dari599,166s setelah warm-up; interval kontinu terpanjang15,008s.
Durasi memakai sampled hold antarstatus, bukan jaminan setiap nanodetik.
Blocker setelah warm-up saling tumpang tindih:

| Kombinasi blocker | Durasi |
| --- | ---: |
| Tidak ada | 468,851844s |
| basis_scale saja | 49,274085s |
| reconciliation_idle saja | 4,502230s |
| basis_scale + market_data + source_freshness | 75,537348s |
| Keempatnya | 1,000620s |

Total masing-masing (jangan dijumlahkan): basis_scale125,812s,
source/market76,538s, reconciliation_idle5,503s. Basis_scale dapat merupakan
turunan evaluasi sebelumnya yang data_invalid; tidak mengklaim formula basis
rusak. Coverage setelah warm-up tidak terblokir. 11.762depth,0bookTicker,
7.043aggTrade, sequence gap0 pada kedua book,0gap receipt book/FX>5s.
Bootstrap snapshot→bridge→receipt/source valid→coverage ready tercatat.

Pemilihan aktual: older tercakup60,276588s/241snapshot; latest1139,440422s.
0pelanggaran containment/cache/keputusan,0source consistency violation,
0latest-pass menjadi selected-reject pada input yang sama. Pembandingan
**offline atas rekaman runtime yang sama** memberi1,750743s source pass ketika
latest-only counterfactual menolak; ini bukan tambahan coverage/kesiapan teknis
yang diukur atau pembuktian keuntungan. Hasil berbeda dari37,432s pada sesi lama
karena kondisi/quote/proof berbeda. Actual source FRESH1032,635s,
FUTURE_UNCERTAIN166,332s, timestamp missing1,000s bootstrap. Expiry0;
penolakan future tetap aktif, tidak diatasi dengan memperlambat evaluator.

20respons kalibrasi rutin diterima (19request mulai di window, satu bootstrap
dimulai sedikit sebelum capture);20connection_create,0reuse. RTT median189,288ms,
max213,701ms. DNS median42,032/max67,882ms; create gabungan86,649/114,637ms;
waiting headers95,131/127,733ms; body median0,036ms, decode0,031ms. Antrean pool
tidak teramati (NULL); bukan klaim fase TLS0ms. Wake lag maksimum31,062ms.
**RTT>400ms tidak muncul:** fase yang menyebabkan penolakan RTT historis tetap
belum diketahui. Instrumentasi sekarang dapat membedakannya jika muncul kemudian,
tanpa menambah request atau mengubah keepalive/cadence.

Binance public/market dan Lighter public:0reconnect,0sequence gap. Dua socket
lighter_account ditutup untuk **application_auth_rotation**; remote close/EOF
kemudian merupakan cleanup, bukan sebab awal gangguan jaringan. Tidak ada
heartbeat timeout/exception disconnect. Pongs teramati78. Recovery akibat kegagalan
transport/clock invalidation tetap bukti offline; tidak direproduksi runtime ini.
81loop callback lag>25ms, maksimum63,231ms; korelasi bukan sebab upstream/host.

Trace callback clock total0,368ms. Seluruh hook telemetry900ms lebih (919,682ms,
0,07664% window), mencakup telemetry yang sudah ada; bukan benchmark CPU A/B.
Antrean puncak4/512,0event dropped,0write error, max writer7,409ms di thread,
max queue age7,255ms. Payload telemetry tercatat3,808MB uncompressed; ukuran itu
bukan semuanya tambahan clock. Tidak ada disk clock write di feed/risk loop.

REST Lighter used rolling mean12,674/puncak18; exit slots minimum37>=15.
Freshness account maximum12,724s dan active_orders12,866s, masing-masing<15s.
Audit lengkap1198,717s (awal bootstrap belum lengkap); exit capacity>=15 pada
seluruh1199,967s yang teramati/usable. Tidak ada blocker kuota.

Saldo akhir49,430756USDC, kas delta0,000000/selisih audit0,000000; posisi/order/
pending/intent0. Empat fill exact tetapNULL, reported total0,0000USDC/4desimal,
seluruh bukti cocok. Tidak ada fill/command kontrol baru. **IDLE, orderOFF,
sesi closed, loaded permissionNone**; riwayat/counter lama tetap.

Raw privat dan backup ledger berada di
`~/Workspace/outputs/lighter-clock-selection-20261006` (dir0700/file0600),
14hash dalam [RAW_SHA256SUMS](audits/clock-proof-implementation-20261006/RAW_SHA256SUMS).
Open gzip prefix sengaja dibekukan tanpa mengubah byte; incomplete trailer
dicatat, post-window record membuktikan batas lengkap. Filter window tetap1200s.
Hash historis tidak diubah. Artefak audit/analyzer setelah24455ff tidak dimuat
ke engine; seluruh source produksi yang sudah dimuat tetap identik manifest.

**Satu langkah berikut:** persiapan satu sesi strategi terbatas melalui izin baru
dan preflight produksi yang sama, jika pengguna kelak mengotorisasi. Tidak ada
cacat keselamatan konkret dari verifikasi ini yang menuntut tuning/rollback;
FUTURE_UNCERTAIN dan audit sementara tetap dapat menolak kandidat. Ketiadaan
reconnect/expiry20menit tidak membuktikan masalah lama selesai. Tugas ini tetap
tidak mengaktifkan trading, dan tidak menilai profitabilitas.
