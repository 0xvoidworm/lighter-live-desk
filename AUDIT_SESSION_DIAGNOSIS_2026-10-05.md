# Diagnosis sesi strategi tanpa transaksi — 5 Oktober 2026

Dari `8814332`; tidak ada otorisasi sesi baru, signing order, broadcast, entry,
cancel, close, atau deposit dalam pekerjaan ini. Sesi sebelumnya tetap closed.
Perubahan hanya memperbaiki timestamp pemulihan dan kualitas bukti diagnosis.
Strategi, threshold, risk, fee policy, cadence rekonsiliasi, dan guard tetap.

Bukti turunan: `audits/session-diagnosis-20261005.json` dan CSV interval.
Analyzer offline: `scripts/analyze_session_diagnostics.py` (Decimal untuk harga
dan bps). Manifest `audits/session-diagnosis-runtime-20261005.json` menyimpan
PID dan hash source yang benar-benar diamati pada drill final. JSON menyimpan hash
sumber observasi; log/status/rekaman mentah tetap lokal dan tidak di-commit.
Ini diagnosis observasional, bukan bukti profitabilitas atau kegagalan strategi.

## Gangguan dan pemulihan

Sumber historis: snapshot status sekitar 0,5 detik, audit sesi, event database,
serta journal **PID 116516**, cocok dengan proses preflight asli. Query journal
berdasarkan unit/waktu saja dapat mencampur proses lain; PID asli dipakai untuk
timeline berikut. Waktu tabel UTC; WIB = UTC +7 jam.

| Waktu 4 Oktober UTC | Kejadian dan bukti |
|---|---|
| 14:30:23.528 | Sesi aktif; coverage sudah lengkap. |
| sekitar 14:35:06–17 | Quote depth Binance kehilangan kesegaran; bookTicker/channel masih menerima frame. Interval sampel valid kemudian mempunyai gap 10,532 detik. Tidak ada bukti DNS/TLS/putus jaringan spesifik untuk interval ini. |
| 14:35:17.247 | Coverage gagal: 578 sampel terbaru, tetapi satu gap 10,532 >5 detik. Warm-up durasi sudah lengkap. |
| 14:45:06.490 | Coverage pulih setelah gap keluar dari jendela 600 detik: tertahan 589,243 detik. |
| 14:48:18.615–20.649 | Binance public reconnect pertama; market/trades dan Lighter tetap tersambung. |
| 14:52:16.473–19.008 | Binance public reconnect kedua; channel lain tetap tersambung. |
| 14:53:12.791 | Journal `reconciliation blocked: TimeoutError`; entry sesi dihentikan permanen. |
| 14:53:18.791 | Sesi closed, tanpa percobaan entry. |
| 14:53:19 | Akun kembali segar: umur snapshot 2,1 detik, audit dana 0,3 detik. Pemulihan tidak membuka ulang sesi. |
| 14:53:38.648; 14:55:25.144 | Dua timeout rekonsiliasi tambahan **setelah** sesi ditutup. |

Coverage tidak reset saat reconnect: `buffer_reset_reason=engine_start` tetap,
sampel bertambah, dan kode mempertahankan riwayat sampai usia 1.800 detik.
Gap yang kedua ujungnya masih berada di rolling window 600 detik memblokir
coverage. Pemulihan tidak membuang sejarah yang masih valid. Menunggu gap
keluar memang perilaku guard; batas 600 detik/480 sampel/gap 5 tidak diubah.

Timeout delapan detik membatasi seluruh rantai `reconcile()`, setelah lock
rekonsiliasi diperoleh. Endpoint yang mungkin dalam rantai itu: account,
accountActiveOrders global, deposit/history (dua asset), withdraw/history
(dua asset), transfer/history, trades. Log lama tidak mencatat endpoint aktif
atau fase HTTP. Jadi **endpoint dan DNS/connect/TLS/headers/body yang gagal
tidak dapat dibuktikan ulang**. Tidak ada dasar menyalahkan ISP/router/exchange.

Saat timeout, pemakaian lokal berubah 37→38, bukan melampaui normal cap 40;
error cap lokal adalah RuntimeError, bukan TimeoutError. Lag event loop yang
tersampel pada menit kegagalan maksimal 5,6 ms; sepanjang sesi maksimal sekitar
45,5 ms. Ini tidak menunjukkan stall delapan detik, tetapi sampling tidak
mengukur semua stall. Lock/pool/retry lama tidak direkam. SDK terpasang memakai
`Configuration.retries=None`, `retry_client=None`; tidak ada bukti retry HTTP
tersembunyi. Backoff reconnect public yang teramati sekitar 2–2,5 detik, bukan
589 detik. Korelasi waktu tidak membuktikan akar masalah transport yang sama.

Delapan pembacaan per audit dengan cadence 12 detik menghasilkan tekanan
budget sampai 40/60 detik. Guard entry mensyaratkan dua slot normal dan 14 slot
urgent-read: used≤33. Akibatnya entry sering terblokir walaupun quote dan akun
segar. Ini konsekuensi cadangan yang disengaja; bukan alasan mengurangi
cadangan exit atau memperlonggar umur audit. Cadence/kuota tidak diubah.

## Perbaikan dan observasi terbatas

- `Book.quote()` mempertahankan timestamp event yang benar-benar memperbarui
  book; sebelumnya setiap pembacaan membuat Stamp baru. Duplicate sequence
  tidak memperbarui umur data.
- Buffer depth selama bootstrap Binance menyimpan Stamp saat pesan diterima,
  lalu meneruskannya tanpa timestamp baru saat flush. Pesan tertahan 10 detik
  tetap tua dan gagal guard 1.500 ms. Timestamp exchange snapshot disimpan bila
  ada. Bug ini terbukti dari kode/regresi; belum bisa dinyatakan sebagai akar
  putus koneksi historis.
- Trace GET resmi SDK merekam host/path, fase DNS, antrean pool, connect/TLS,
  headers, body, durasi, status HTTP, dan kelas error/cancellation. Tidak ada
  query, header auth, body, token, atau pesan exception mentah. aiohttp
  menggabungkan TCP connect dan TLS; keduanya belum terpisah dalam trace ini.
- Rekonsiliasi merekam waktu antre lock, rantai request, batas delapan detik,
  outcome dan budget. Status memuat input evaluasi sebenarnya, FX/umur FX,
  backlog, timestamp sumber quote, dan jumlah posisi/order exchange.
- Reconnect menyimpan close code, tipe frame dan kelas penyebab socket.
- Recorder memakai file eksklusif per run/segmen dengan PID, run ID, receipt,
  exchange dan monotonic timestamp, serta mencatat FX. File lama tidak diubah
  atau ditambahkan stream baru; flush setiap detik memudahkan pengamatan.

Observasi tanpa order 180 detik (00:42:45–00:45:44 WIB): nol reconnect,
lag maksimal 3,4 ms, pembacaan SDK teramati berhasil. Tahap awal instrumentasi
baru mencakup account/orders/trades; hasil ini tidak diklaim mencakup dana.
Setelah koreksi nama route SDK dana, verifikasi terbatas **35,477 detik**
(00:59:39–01:00:14 WIB) mencatat tiga rekonsiliasi lengkap /24 GET berhasil pada
keenam route unik. HTTP median 114,158 ms, maksimum 132,641 ms; rekonsiliasi
930,504–960,545 ms; antre lock maksimum 0,011 ms, backlog maksimum satu,
lag maksimum 3 ms, cadangan transaksi minimum 15 slot. Nol reconnect.
Tidak ada request tambahan untuk tracing; pembacaan memakai budget engine.
Hasil singkat ini membuktikan instrumentasi berjalan, bukan jaringan kebal
timeout. Restart memulihkan monitoring, tanpa mengaktifkan entry.

## Perhitungan dan jendela evaluasi

Misalkan B=mid BTCUSDT Binance, L=mid BTCUSDC Lighter, F=mid USDCUSDT Binance
(USDT per USDC). Pembanding dalam USDC adalah B/F. Kode menghitung:

`raw_bps = (L/(B/F) - 1)*10000`

`basis = median(raw historis); residual = raw_bps - basis`

`scale = median(abs(raw historis-basis))*1.4826; threshold = max(8,4*scale)`

Satu bps =0,01%. Residual negatif di bawah -threshold menjadi kandidat BUY
(Lighter relatif murah); positif di atas threshold menjadi SELL. Guard FX
mewajibkan umur <15 detik dan penyimpangan kurs dari 1 tidak lebih dari 10 bps.
Harga dua book dibaca dalam evaluasi yang sama; keduanya wajib segar <1.500 ms.
Ini pairing berdasarkan waktu penerimaan lokal, **belum** join exchange-time
yang serempak. Ketidakselarasan timestamp masih bagian ketidakpastian sinyal.

Basis hanya memakai sampel `timestamp < now-1`, tidak sampel sekarang, satu
detik terakhir atau masa depan. Retensi 1.800 detik; coverage 600 detik.
Regresi membuktikan normalisasi, dua arah, dan penolakan sampel masa depan.
Median menghilangkan premium venue yang menetap. Deviasi yang menetap dan
mendominasi >setengah history akhirnya terserap basis: konsekuensi desain
deviasi sementara, bukan bukti bug lookahead. Threshold/basis tidak diubah.

Sesi entry aktif 1.369,264 detik. Rekonstruksi 2.705 snapshot mencakup
1.369,261 detik; interval maksimum 0,558 detik, dengan status sebelumnya
ditahan sampai snapshot berikutnya/entry-stop. Durasi berikut **estimasi
snapshot**, bukan pembuktian kesiapan kontinu seluruh guard internal:

| Kategori | Durasi |
|---|---:|
| Basis/residual tersedia | 743,337 detik |
| Semua guard yang tersimpan lolos, sebelum filter sinyal | 230,817 detik (3m51s) |
| Basis tersedia tetapi guard tersimpan terblokir | 512,520 detik |
| Seluruh syarat entry termasuk sinyal valid | 0 detik |

Pembatas tumpang tindih: reserve request 942,486 detik; coverage 589,243;
quote/book tidak valid 37,203; channel putus 4,568; akun/dana tua 5,694.
JSON/CSV memuat interval dan alasan. Umur FX, antrean, signing lock dan usia
sukses rekonsiliasi tidak direkam per evaluasi lama; **durasi persis seluruh
guard secara kontinu tidak bisa dipastikan**. Bukti 1 Hz database mencatat
1.351 evaluasi: 728 DEVIASI_KURANG dan 728 buffer biaya (tumpang tindih),
568 coverage, 55 data tidak valid, nol sinyal valid/entry/fill.

| Residual (bps) | Guard tersimpan lolos | Terblokir, basis tersedia |
|---|---:|---:|
| Sampel | 456 | 1.012 |
| Minimum bertanda | -1,955545 | -1,779251 |
| Median bertanda | +0,112269 | +0,107445 |
| Maksimum bertanda/absolut | +2,957553 | +2,870308 |
| Median absolut | 0,335354 | 0,364038 |
| P95 absolut | 1,388127 | 1,279428 |
| P99 absolut | 2,704178 | 2,638834 |
| Total abs>1 bps; episode terpanjang | 23,798s; 4,040s | 36,926s; 7,067s |
| Total abs>2 bps; episode terpanjang | 5,562s; 3,036s | 11,146s; 3,530s |
| Total abs>3/5/8 bps | 0s | 0s |

Distribusi bertanda/absolut lengkap, termasuk durasi positif/negatif, ada di
JSON. Maksimum 2,957553 dari snapshot 0,5 detik berbeda wajar dari 2,945796
di audit evaluasi database 1 Hz. Kedua seri tidak dianggap identik.

Kandidat formal tidak ada. Observasi terdekat saat guard tersimpan lolos:

| Arah, UTC | Raw | Basis | Residual | Threshold | Spread+buffer | Filter penolak |
|---|---:|---:|---:|---:|---:|---|
| Long, 14:33:39.396 | 0,597828 | 2,553372 | -1,955545 | 8 | 5,634178 | deviasi; biaya/buffer |
| Short, 14:48:05.971 | 5,294471 | 2,336918 | +2,957553 | 8 | 5,011751 | deviasi; biaya/buffer |

Filter flow/depth arah tidak dijalankan tanpa kandidat threshold; tidak
diklaim menolak kandidat ini. Jika kandidat ada: Binance move 10 detik tidak
boleh adverse >5 bps; ≥3 trade di tiap separuh 30 detik; adverse flow akhir
≤70% awal; ≥20 depth sample/30 detik dan depth akhir ≥1,2×minimum; residual
harus mengalahkan spread+5 bps. Risk dispatch tetap memeriksa sizing, spread,
IOC slippage, posisi, request reserve, fee/cash binding dan snapshot segar.

Ambang 8 bps dan buffer biaya 5 berasal dari commit `4b5af97`, default
heuristik `HEURISTIC_UNVALIDATED`, bukan hasil pengukuran biaya/kalibrasi yang
ditemukan di repo. Data sesi ini tidak cukup mengusulkan angka pengganti.

## Ekonomi, batas rekaman, dan keadaan akhir

Ukuran minimum sesi lama 0,00012 BTC (~10,2 USDC), berdasarkan min quote 10,
min base 0,00007 dan increment 0,00001; bukan maksimum 25. Saat guard lolos,
spread Lighter median 0,023486, P95 0,704323, maksimum 0,975257 bps. Rekaman
status menyimpan VWAP untuk $25: impact minimum-size berada antara nol dan
impact $25 terhadap BBO pada book/sisi yang sama. Batas atas maksimum teramati
BUY 0,236135; SELL 0,519094 bps. Ini batas impact statis, bukan fill/slippage
yang diukur; spread dua leg, perpindahan quote, antrean dan latensi belum
dibayar. Cap IOC 2 bps bukan janji slippage aktual atau keuntungan residual.

Rekaman legacy `market-20261004-14.jsonl.gz` gagal decompress setelah 1.081.961
baris: `invalid stored block lengths`; SHA256
`9eebbdde06be6a1629db04dbefe862ccc015bf520e5af53afaa1741c95f333fa`.
Prefix tidak memuat snapshot Binance yang diperlukan, replay menghasilkan
gap dan quote Lighter pada awal sesi tidak cocok dengan status audited.
File lama tidak mempunyai PID/run ID; sumber/corrupt-write spesifik tidak
terbukti. Exchange timestamp legacy berbeda ~16.307 detik dari receipt,
sehingga tidak sah dipakai sebagai pengukuran latensi sesi. Tidak ada replay
depth/as-of ekonomi lengkap yang dapat dipertanggungjawabkan dari file itu.

Prefix 1.000 event file baru valid, PID135588/run
`7a720278e0d34fdb88cb25e18edea3c2`, dengan semua timestamp sumber. Pada drill
final, receipt-minus-exchange 49,051–74,142 ms (median 50,644), skew timestamp
dua quote median 47 ms/maksimum 301 ms. Ini selisih clock teramati, bukan
pengukuran latensi jaringan satu arah atau p99 pengiriman order.

Pemeriksaan shared-engine 5 Oktober **01:00:14 WIB**: akun751745 Standard/
Unified, cross100%, saldo49,430756 USDC, posisi BTC dan posisi lain nol,
seluruh active order exchange nol, intent ambigu nol, audit kas matched,
delta kas dan selisih0,000000. Empat fee exact tetap NULL, fee reported
0,0000/fill empat desimal beserta bukti resmi tetap utuh; NULL tidak diubah
menjadi nol. Source budget50 sudah terpakai. Engine IDLE, entries stopped,
allow_live_orders=false, kedua reported-entry policy flag false, sesi closed.
Proteksi/exit tetap terpisah dari bookkeeping; tidak ada lifecycle mainnet baru.

Pemeriksaan ulang penutup **01:11:29 WIB** juga membuktikan keadaan yang sama
dengan snapshot akun/audit dana <15 detik dan status engine <1 detik. Rincian
terpilih: `audits/session-diagnosis-final-20261005.json`. Engine/API service
tetap disabled autostart; monitoring yang sedang berjalan memakai kode baru.

295 tes lulus; mypy15 source, Ruff/format lulus. Regresi baru: transport SDK
nyata localhost untuk timeout headers/body, antre pool, DNS, route dana,
redaction, stamp book/duplicate/bootstrap, file recorder eksklusif, WS detail,
arah sinyal/FX dan basis tanpa future data. Bukti command/result tersimpan
di `audits/session-diagnosis-tests-20261005.txt`.

Langkah berikut paling berguna: satu perekaman **tanpa order selama 20 menit**,
10 menit warm-up dan 10 menit evaluasi, dengan run ID/timestamp/depth/FX serta
trace baru; pertanyaan: berapa jendela layak, deviasi bersih biaya statis dan
latensi, dan apakah cadangan pembacaan terus mendominasi blocker. Berhenti
pada batas itu, jangan mengejar sinyal atau memilih threshold supaya ada trade.
Ini usulan observasi berikutnya, bukan otorisasi sesi/order baru.

Reproduksi statistik offline pada workspace ini:

```sh
.venv/bin/python scripts/analyze_session_diagnostics.py \
  --historical ~/Workspace/outputs/lighter-strategy-session-20261004/session-observation.jsonl \
  --prior-audit audits/strategy-session-20261004.json \
  --journal ~/Workspace/outputs/lighter-diagnosis-20261004/original-engine-journal.jsonl \
  --readonly ~/Workspace/outputs/lighter-diagnosis-20261004/readonly-observation.jsonl \
    ~/Workspace/outputs/lighter-diagnosis-20261004/readonly-final-cycle.jsonl \
  --output-prefix audits/session-diagnosis-20261005
```
