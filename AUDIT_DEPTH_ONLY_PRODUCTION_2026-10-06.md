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
