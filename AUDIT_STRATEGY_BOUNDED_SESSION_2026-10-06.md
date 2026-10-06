# Satu sesi strategi terbatas — 6 Oktober 2026

## Hasil

Sesi UUID `00fa3c17-168e-4ee7-a757-ea6a474f0b22` selesai karena deadline
1.800 detik. Nol sinyal valid, percobaan entry, broadcast, fill, dan posisi
baru. Saldo/collateral/equity tetap **49,430756 USDC**, perubahan kas
**0,000000 USDC**, selisih audit **0,000000 USDC**. Akhir terverifikasi:
**IDLE, order OFF, entries stopped, sesi closed, izin loaded kosong**;
posisi seluruh akun, order aktif/pending, dan intent belum selesai semuanya nol.
Tidak ada deposit atau otorisasi trading tersisa.

Ini hasil satu pengamatan strategi dengan implementasi yang dibekukan. Tidak
ada eksekusi baru yang membuktikan stop, slippage fill, close, atau fee exact;
bukti transaksi probe sebelumnya tidak digantikan oleh sesi nihil transaksi ini.
Hasil ini tidak membuktikan profitabilitas ataupun kegagalan strategi.

## Versi, otorisasi, dan waktu

Baca AGENTS.md, RUNBOOK.md, SESSION_POLICY_2026-10-04.md, dan
AUDIT_TRANSPORT_HEARTBEAT_RECOVERY_2026-10-06.md sebelum otorisasi. HEAD awal
`2205e0a`; kode produksi yang sudah dimuat **`1205d35`**, PID **341351**.
Hash 21 modul sumber cocok dengan manifest runtime dan tetap sama sampai
penutupan. Engine tidak direstart. Tidak ada perubahan kode, subscription,
strategi, guard, batas request, atau risiko selama tugas ini.

| Kejadian | Waktu WIB 2026-10-06 |
| --- | --- |
| Otorisasi baru dicatat persisten | 11:18:50.471385 |
| Batas persiapan asli | 11:38:50.471385 |
| Capture lokal mulai | 11:19:31.792838 |
| LOAD_SESSION_PERMISSION, command 12 | 11:19:31.812132 |
| DIAGNOSE_PREFLIGHT, command 13 | 11:19:32.095818 |
| START_LIVE, command 14 diminta | 11:19:33.741502 |
| Aktivasi persisten | 11:19:33.785255 |
| Deadline aktif asli | 11:49:33.785255 |
| Entry dihentikan owner | 11:49:33.854282 |
| Sesi closed setelah flat/cleanup verified | 11:49:33.870253 |
| STOP_ENTRIES supervisor, command 15 | 11:49:34.071710 |
| Pemeriksaan akhir akun/izin untuk audit | 12:00:58.266237 |

Persiapan **43,313869 detik**; window aktif **1.800,000000 detik**.
Owner menutup sesi 84,999 ms setelah deadline; permintaan STOP supervisor
berikutnya tidak mengaktifkan apa pun. Hanya satu UUID baru, satu hotload,
dan satu aktivasi. Kuota maksimum tiga tetap persisten, konsumsi nol.
Riwayat dan otorisasi lama tidak dihapus; fingerprint fill/fund tetap sama.

Peluncuran pertama helper lokal berhenti pada assert identitas/status izin:
API status masih menampilkan snapshot lama sesudah pencatatan izin. Ini terjadi
sebelum command, capture, ataupun aktivasi. Setelah identitas aktual diverifikasi,
helper diteruskan memakai UUID, waktu otorisasi, dan deadline asli yang sama.
Tidak ada otorisasi ulang atau restart engine. Log kegagalan dipertahankan.
**41,321453 detik awal persiapan tidak mempunyai capture kontinu**; status
sebelum otorisasi dan command journal tersedia sebagai bukti terpisah.

Hotload tidak mengaktifkan strategi: START merupakan command terpisah.
`coverage.buffer_started_at` sebelum/sesudah LOAD tetap
`1791252744.1770086`, reset reason tetap `engine_start`; jumlah sampel berubah
sesuai sliding window. Tidak ada reset coverage atau penghapusan gap.

## Preflight dan parameter yang dipertahankan

Akun **751745**, Standard/Unified (type 0, trading mode 1), margin BTC mode 0,
initial margin fraction 100,00%. State akun lengkap/fresh, flat global,
active/pending order dan unresolved intent nol. Audit riwayat lengkap, kas
cocok, seluruh empat fill exact-unknown tercakup ekspor resmi yang cocok.
Sumber funding 50 USDT sudah terpakai; budget sumber tersisa 0.

Preflight produksi tanpa pengiriman selesai **11:19:33.503585**, outcome
`complete_no_send`, blocker kosong, **8 GET / 1.385,597400 ms**. Aktivasi
0,281670 detik kemudian. Biaya pembacaan direncanakan/aktual 8; envelope send
2; TTL tetap **5 detik**. `sendable=false`, `broadcast=false`: diagnosis ini
tidak merupakan izin dispatch atau percobaan entry. Tidak ada kandidat yang
memicu preflight entry berikutnya. Tampilan technical preflight masih berlaku
selama 3,457343 detik pertama sesi; itu berbeda dari 709,138 detik kesiapan
teknis untuk evaluasi sinyal.

Strategi tetap `local-deviation-v1-HEURISTIC_UNVALIDATED`: comparator mid Binance
BTCUSDT dibagi mid USDCUSDT; raw `(mid Lighter/comparator - 1)*10.000` bps;
basis median histori terdahulu dengan latest 1 detik dikecualikan; residual
raw-minus-basis, scale MAD×1,4826. Long untuk residual negatif melewati
`max(8, 4×scale)`, short untuk residual positif; aturan flow weakening,
passive depth, gerak comparator, spread dan konfirmasi tetap. Threshold
**8 bps**, buffer biaya **5 bps**, IOC **2 bps**, sizing minimum legal;
cap **25 USDC**, exposure **1× equity**, target risiko **0,50 USDC**,
jeda loss harian **5 USDC**. Minimum preview pada persiapan 0,00012 BTC,
notional sekitar 10,27 USDC, bukan otomatis cap25; setiap dispatch seharusnya
menghitung ulang dengan metadata/harga saat itu. Tidak ada dispatch sesi ini.

Deadline stop submit3s/confirm8s, close mulai177s, hold180s, reduce-only,
single intent/position, nonce owner, reserve exit, serta pemisahan fee audit
dari proteksi tidak diubah. Independensi proteksi/exit merupakan kode produksi
yang dipertahankan dan bukti tes sebelumnya; tidak diuji dengan posisi baru.

## Kesiapan dan overlap

Capture memuat **6.666 snapshot dalam window aktif** (6.676 total); nol error
local status, interval capture maksimum **0,316380 detik**. Durasi memakai
status sebelumnya pada interval antar-capture, di-clip tepat ke aktivasi dan
deadline. Total integrasi 1.800 detik. Angka merupakan estimasi sampling,
bukan jaminan semua transisi subinterval tertangkap. Wall timestamp collector
dan monotonic diambil pada tahap berbeda di sekitar GET lokal; beda interval
maksimum 47,945 ms tidak dipakai sebagai bukti clock step host.

Tidak ada pemanasan ulang proses: coverage valid sebelum aktivasi dipertahankan.
Siap teknis **709,137753 detik = 11m49,138s = 39,396542%**, interval kontinu
terpanjang **9,974413 detik**. Kesiapan ini mengecualikan blocker administratif
yang memang berubah ketika sesi aktif, dan mencakup kapasitas audit+send
produksi; tidak mengabaikan GET preflight yang belum dijalankan.

| Blocker | Detik |
| --- | ---: |
| basis_scale | 1.085,975605 |
| coverage | 586,670701 |
| source_freshness | 286,977181 |
| market_data, sama dengan source dalam window ini | 286,977181 |
| reconciliation_idle | 18,704361 |
| entry_request_reserve, account_fresh, cash/fee evidence | 0 |

Durasi **bertumpang tindih**. Union blocked **1.090,862247 detik**; jangan
menjumlahkan baris tabel. Overlap utama: basis+coverage453,887542s; basis
saja333,724484s; basis+market/source158,855479s; basis+coverage+market/source
125,690380s. Semua kombinasi, termasuk reconciliation, ada di summary.
`basis_scale` merupakan turunan last decision yang tidak punya basis/scale:
776,795183s dengan `DATA_TIDAK_VALID`, 309,180422s dengan
`COVERAGE_DATA_KURANG`. Tidak ditemukan bukti kegagalan matematika median/MAD.
Assessment sumber diagnostik saat berikutnya bisa lolos sementara last decision
belum mempunyai basis; itu tidak meluluskan keputusan historis secara retroaktif.

Source blocker: **FUTURE_UNCERTAIN226,837150s**, **CLOCK_EVIDENCE_EXPIRED60,140031s**.
30 kalibrasi clock:26verified,4unverified; semua HTTP200. RTT kalibrasi ditolak
439,846/726,357/1.450,175/412,641 ms, di atas batas400ms yang tetap.
Dua berturut-turut pada11:37:25 dan11:38:25 menyebabkan proof sebelumnya
melewati TTL120s. Expired mulai11:38:25.879, kembali verified11:39:25.752.
Gap **sampel valid60,291s** kemudian menahan coverage sampai11:48:25.641.
Ini berbeda dari kehilangan frame: book depth tetap masuk. Gap berikutnya
17,897s membuat coverage gagal lagi pada11:48:47.003 hingga akhir window.
Tidak ada imputasi sampel, penghapusan gap, atau proof baru yang dipakai untuk
meluluskan waktu terdahulu. Penyebab eksternal RTT/offset tidak ditentukan.

Receipt Binance quote p95 **181,460 ms**, max **937,699 ms**; source-age upper
p95 **487,412 ms**, max **1.293,952 ms**; clock uncertainty max **216,697 ms**.
Interval source-age lower mencapai **−151,625 ms**, sehingga indikator future
uncertain memang relevan meskipun umur receipt kecil. Ini interval antarjam,
bukan latensi jaringan/fill pasti. Umur kurs maksimum dan distribusi lengkap
tersimpan di summary; tidak ada blocker kurs dalam window.

## Keputusan strategi dan peluang

Journal: **1.758 STRATEGY_OBSERVATION**, nol sisi valid. Capture melihat
**5.633 assessment keputusan unik** berdasarkan monotonic stamp; frekuensinya
berbeda dari journal1Hz sehingga tidak boleh dijumlahkan. **2.203 keputusan**
memiliki residual, technical-ready, quote/input cocok, dan data_guard keputusan
sebenarnya lolos; tidak memakai assessment diagnostik untuk mengizinkan entry.

Residual eligible bertanda: min **−2,800119**, p50 **−0,228519**,
p95 **+0,812517**, max **+1,216320 bps**. Absolut: p50 **0,403179**,
p95 **0,958249**, max **2,800119 bps**. Seluruh data yang masih mempunyai basis
(2.218 keputusan) juga max absolut2,800119bps; data tanpa basis tidak diisi nol.

Model strategi `abs(residual) − spread Lighter − impact minimum-size − 5bps`
mengurangkan spread sekali: **nol estimasi positif**, maksimum **−2,246648 bps**,
p50 **−4,707541 bps**. Spread eligible p95 **0,572787 bps**, impact p95
**0,003896 bps**. Ini model biaya konservatif yang sudah diterapkan, bukan
pengukuran latensi fill, harga eksekusi, atau fee exact. Tidak ada fill untuk
mengukur slippage nyata. Dalam journal,878 observasi mencatat
`DEVIASI_KURANG` sekaligus `BIAYA_DAN_BUFFER_MELEBIHI_PELUANG`;487
`DATA_TIDAK_VALID`;393 `COVERAGE_DATA_KURANG`.

Tidak ada peluang valid yang hilang selama preflight entry: nol kandidat,
nol candidate entry preflight, nol kuota dikonsumsi. **Pada interval layak,
penyebab nihil entry adalah deviasi pasar tidak memenuhi strategi/biaya**.
Di interval blocked, tidak ada basis valid untuk menilai peluang; tidak
menyatakan seluruh pasar selama30menit bebas peluang. Tidak mengusulkan
penurunan threshold dari sesi ini.

## Transport, request, dan dana

Dalam window: **17.648 futures depth**, **0 futures bookTicker**,6.494aggTrade,
26.598Lighter book. Reconnect Binance/Lighter **0→0**, tidak ada kejadian EOF,
timeout heartbeat atau sequence recovery baru. Empat penutupan/rehandshake
socket akun tercatat `application_auth_rotation`, berbeda dari feed failure.
Nol telemetry drop/write error; kemampuan recovery tetap bukti offline/prior
runtime, bukan bukti reconnect baru pada sesi ini.

**378 GET Lighter** dalam journal aktif, rata-rata **12,6/min**:133 refresh
state×2,14 audit lengkap×8. Semua outcomeok. Puncak rolling bucket termasuk
lease **18**, sedikitnya **37 slot hingga cap55**; exit reserve minimal15
dipertahankan, nol quota blocker. Umur komponen account max **12,913942s**,
active_orders max **13,099545s**, tetap di bawah15s. Audit lengkap diulang
sesuai policy periodic120s saat flat; sebelum entry wajib tetap berlaku.
Statistik memakai telemetry yang sudah dikonsumsi engine, tanpa polling akun
tambahan untuk pengukuran. Binance Spot:900observasi REST kurs verified,
1.800weight total di bucket Spot sendiri;30time calibration memakai bucket
Futures, bukan bucket Lighter. Tidak ada retry/noise tambahan untuk statistik.

Empat fee exact historis tetap **NULL**. Keempat reported fee resmi
**0,0000 USDC pada presisi4desimal**, sumber official_account_export; hash
bukti dan waktu pengambilan terpisah ada dalam summary. Tidak ada fill baru
atau fee baru. Kas sesi0,000000USDC dan audit matched tidak dipakai untuk
mengubah fee individual unknown menjadi nol. Fingerprint fill/fund tidak berubah.
Pemeriksaan akhir memakai mekanisme state/audit engine: margin cocok, komponen
fresh, riwayat complete, audit kas matched. Tidak mengirim order untuk verifikasi.

## Bukti, batas, dan keputusan

Statistik aman: `audits/strategy-bounded-session-20261006/summary.json`.
Rekaman penuh, snapshot sebelum/akhir, ledger backup konsisten, authorization,
log helper gagal/sukses, derivasi offline, dan prefix market/transport berada
di folder privat700, file bukti600:
`/home/yuri/Workspace/outputs/lighter-strategy-session-20261006/00fa3c17-168e-4ee7-a757-ea6a474f0b22/`.
**Tidak dimasukkan Git.** RAW_SHA256SUMS memuat hash/path relatif saja.
Prefix gzip dari writer aktif dapat tidak mempunyai trailer; analisis hanya
baris lengkap dan hanya timestamp dalam window sesi. Status capture finalized
normal. Prefix menyimpan record di luar window juga; record itu tidak masuk
angka sesi. Tidak ada rekaman runtime kedua atau perpanjangan.

Validasi dilakukan pada hash sumber, command persisten, state live aktual,
fingerprint dana/fill, integrasi durasi/overlap, jumlah journal/intent/fill,
serta hash artefak. Tidak ada tes regresi atau build baru: implementasi dibekukan,
analisis offline merupakan derivasi statistik, bukan simulasi transaksi.

Keputusan: **sesi selesai operasional dan otorisasi tertutup**. Keterbatasan
utama observasi peluang adalah source clock/coverage; strategi tidak mendapat
sinyal pada data eligible. Langkah berikut paling berguna ialah diagnosis
read-only terarah terhadap RTT kalibrasi dan interval offset yang menolak
future/TTL, menggunakan bukti ini terlebih dahulu. Itu usulan pekerjaan lain,
bukan izin perubahan guard, tuning, ataupun sesi baru.
