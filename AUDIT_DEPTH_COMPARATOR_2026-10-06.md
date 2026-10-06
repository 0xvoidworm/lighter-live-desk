# Satu pembanding depth Binance, 6 Oktober 2026

## Keputusan

Keterlambatan depth **tidak terbatas pada engine produksi**. Dalam satu window
900 detik terdapat tujuh update dengan payload identik yang terlambat pasti pada
klien minimal, sementara source guard produksi untuk update itu lolos. Produksi
juga mengalami peningkatan umur pada update tersebut, tetapi upper bound-nya
masih 1.023–1.390 ms. Ini bukan bukti akar masalah aplikasi, ISP, atau upstream.
Episode lama sekitar lima detik tidak direproduksi dalam window baru.

**Langkah berikutnya:** tambah pencatatan pasif peer IP dan identitas socket yang
terikat connection ID/channel pada handshake produksi, dalam tugas rollout
terpisah. Pembanding menuju IP yang berbeda dari seluruh tujuan socket produksi
yang teramati; tanpa pemetaan public ke socket tertentu, perbedaan jalur belum
bisa dipisahkan dari perbedaan implementasi. Jangan mengubah kebijakan reconnect
berdasarkan eksperimen ini.

## Baseline dan batas

- Repo awal `7eaf6e7`; engine tetap PID **391484**, produksi **8d785cb**.
  PID/run ID, bukti restart sebelumnya, hash seluruh source produksi, dan hash
  config cocok. Tidak ada restart, perubahan source produksi, konfigurasi,
  strategi, guard, jaringan, izin, atau sesi.
- Dibaca: AGENTS.md, RUNBOOK.md, AUDIT_CALIBRATION_HTTP_REUSE_2026-10-06.md.
  Semua **39 hash** manifest bukti lama diverifikasi ulang.
- Rencana ditetapkan sebelum pengukuran di
  [DEPTH_COMPARATOR_PLAN_2026-10-06.md](DEPTH_COMPARATOR_PLAN_2026-10-06.md).
- Helper recorder dimuat dari commit **e15a1f4**, Python3.12 proyek,
  **aiohttp3.14.3**, factory/resolver DoH dan hook socket pasif produksi yang sama.
  Python diperlukan untuk memakai library/transport yang sama; tidak memuat
  engine, account, signer atau key.
- URL persis
  `wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms`.
  IPv4, trust_env false, TLS/SNI/validasi hostname, autoping, heartbeat20 dan
  default session/connector dipertahankan. Tidak ada REST snapshot, kalibrasi
  tambahan, polling exchange/account, ping tambahan, atau reconnect paksa.

## Episode lama: relatif 550–578 detik

Marker lama 21:17:20,998706 WIB; window diperiksa 21:26:30,998706–21:26:58,998706.

| Bukti | Hasil |
|---|---:|
| depthUpdate | 278 |
| mismatch pu terhadap u sebelumnya di dalam window | 0 |
| receipt minus E, belum dikoreksi clock, maksimum | 5.146,802 ms |
| E minus T | 1–3 ms |
| receipt → enqueue, maksimum | 1,473354 ms |
| enqueue → recorder sebelum book apply, maksimum | 49,372633 ms |

Replay saat receipt memakai hanya event proof yang sudah tersedia saat itu,
validator produksi, TTL120s, drift100ppm, anchor/wall-step dan seluruh bounds
asli. Proof maksimum-delay: `1791296780921:182463236353786`, age27,453382s,
drift2,745338ms, interval offset setelah drift **[-93,777074;37,970189]ms**.
Umur **T** update terburuk **[5.054,025;5.185,772]ms**; E−T pada update ini1ms,
sehingga interval umur E adalah kedua batas itu dikurangi1ms. T tetap sumber
guard; tidak diganti dengan E yang lebih menguntungkan.

Hasil receipt replay: 81 source-valid, 153 SOURCE_DATA_LATE, 44 FUTURE_UNCERTAIN.
Sebanyak147 memenuhi lower-bound >=1500ms. Burst terpanjang116 update, span
receipt11,309279s; tiga burst lain14/6/11 update dengan span0,911580/0,443050/
1,032086s. Span sampel bukan estimasi durasi blocker kontinu.

Pada window sama, aggTrade1262 pesan: upper bound umur T maksimum684,795ms.
Lighter555 book: indikator raw receipt–exchange maksimum484,462ms; ini tidak
dikoreksi dengan clock Binance karena domain clock berbeda.

Enam pulse menunjukkan last-frame library transport→application Binance public
0,147–2,331ms; library dequeue→application0,029–0,532ms. Pada112 snapshot,
lag evaluator maksimum8,3ms dan backlog maksimum1. Empat event timer transport
mencatat25,107/36,321/48,111/46,593ms. Data ini tidak menjelaskan lima detik,
tetapi **last-frame berkala bukan waktu setiap frame**; buffering sebelum
receipt atau di antara snapshot belum tersingkirkan. Timestamp recorder juga
bukan waktu book apply selesai.

## Eksperimen baru: satu window, satu koneksi

**22:16:55,181180–22:31:55,181180 WIB** (15:16:55–15:31:55 UTC),900s termasuk
handshake. Konsumsi data berhenti pada deadline; penutupan selesai900,001506s.
Koneksi pembanding ditutup, proses selesai; pengukuran tidak diulang/diperpanjang.
Produksi tetap berjalan dengan coverage/gap asli.

| Pasangan dan kontinuitas | Hasil |
|---|---:|
| depth produksi / pembanding | 8.823 / 8.820 |
| pasangan U/u/pu,E/T dan SHA256 payload kanonis identik | **8.820** |
| mismatch isi / pasangan duplicate ambigu | 0 / 0 |
| tidak berpasangan produksi / pembanding | 3 / 0 |
| mismatch pu kedua stream, di dalam koneksi | 0 / 0 |
| reconnect/sequence gap produksi public dan pembanding | 0 / 0 |

Tiga update produksi tidak berpasangan berada di awal sebelum pembanding
memperoleh stream. Tidak ada snapshot/bridge tambahan pada pembanding; ini
perbandingan raw update, bukan pembuktian bootstrap book pembanding.

| Receipt produksi minus pembanding (monotonic host sama) | ms |
|---|---:|
| minimum (pembanding lebih lambat) | -783,184915 |
| p50 | 0,023517 |
| p95 | 86,560335 |
| maksimum (produksi lebih lambat) | **1.326,202507** |
| absolut p50 / p95 / maksimum | 1,248104 / 158,219221 / 1.326,202507 |

Distribusi ini tidak memerlukan asumsi kesamaan jam exchange/host. Offset clock
hanya digunakan pada penilaian umur sumber, secara kausal pada masing-masing
receipt, bukan memakai proof masa depan atau sebagai izin entry historis.

### Penilaian umur sumber

- **7 comparator-only certain-late**: lower bound>=1500ms dan produksi passed.
  Dua burst2 dan5 update, receipt relatif468,986933–469,166744 dan
  469,430994–469,787698s; span **0,179811 dan0,356705s**.
  Pembanding lebih lambat383,050–612,473ms untuk tujuh pasangan ini.
- Pembanding: 11 SOURCE_DATA_LATE, tujuh terlambat pasti; maksimum interval
  umur T **[1.813,163;1.938,951]ms**. Raw receipt–E maksimum1.895,552ms,
  receipt–T maksimum1.896,552ms.
- Produksi: tiga SOURCE_DATA_LATE dengan upper bound melintasi1500ms; tidak
  ada lower bound yang mencapai1500ms. Maksimum interval T
  **[1.455,962;1.594,012]ms**. Ini tetap penolakan guard, bukan data yang diluluskan.
- Tidak ada pasangan kedua lower bound>=1500ms atau production-only
  certain-late dengan counterpart passed. Ada777 pasangan kedua source-valid;
  **8.036 pasangan** memiliki setidaknya satu penolakan/ketidakpastian guard.
  FUTURE_UNCERTAIN pada6875 update produksi dan7640 pembanding dipertahankan.
  Source guard ditolak bukan sinonim data pasti terlambat; selisih receipt tetap
  terukur untuk seluruh pasangan. Tidak ada threshold/clock policy yang diubah.

### Bukti tahap lokal dan jalur

- Pada tujuh update comparator-only: dequeue→receipt pembanding maksimum
  **0,012257ms**, last-chunk→receipt maksimum0,357295ms; produksi
  receipt→enqueue maksimum1,013769ms, enqueue→recorder2,936083ms.
- Seluruh window produksi: receipt→enqueue p95/max0,287974/34,276096ms;
  enqueue→recorder p95/max1,011860/62,354418ms. Backlog pada pulse maksimum46.
  Ada228 event timer lag>25ms, maksimum112,627618ms. Ini bukti delay lokal
  terukur, bukan bukti penyebab tunggal beda receipt1,326s atau episode lama5s.
- Pembanding: loop timer maksimum3,550562ms; dequeue→receipt p95/max
  0,026098/0,808257ms. Last-chunk adalah chunk terbaru yang teramati, bukan
  timestamp wire per payload; seluruh kemungkinan buffering belum tersingkirkan.
- Peer pembanding **18.176.246.42:443**. Peer public produksi **unknown** karena
  trace lama tidak mengikat channel ke socket.157 sampel `ss -tinp`, mulai
  relatif116,055s sampai899,591s, memperlihatkan tujuan produksi:
  13.35.36.9,3.168.203.20,3.168.203.52,3.168.203.77,52.192.203.173,
  52.69.173.204,57.182.106.185 (443). Tidak ada IP pembanding pada sampel itu.
  Endpoint/library/resolver sama tidak membuktikan jalur atau backend sama.
- TCP_INFO pembanding pasif: SRTT p50/p95/max92,344/96,088/96,088ms;
  total retransmission outbound0. Ini bukan latensi pesan, dan tidak
  menyingkirkan kehilangan/buffering pada arah masuk atau remote.

### Beban pencatatan dan verifikasi

Writer bounded512 pada thread terpisah, peak queue5, drop/error0, maksimum
queue age46,574ms; maksimum write73,359ms pada thread, bukan feed loop.
Payload pesan32.163.653byte; JSON evidence sebelum kompresi60.113.515byte.
Hook total560,412ms/900s, pemrosesan pembanding total1.377,950ms
(rata-rata0,156230ms/update). Ini waktu terukur, bukan estimasi CPU keseluruhan.
Logger produksi juga drop/write-error0 pada kedua pulse batas. Recorder lambat
tidak mengubah timestamp sumber atau menyegarkan data lama.

**10 tes offline lulus** (0,17s), Ruff helper/tests lulus: causal proof tanpa
masa depan, expired/global invalidation/missing/future, empat kategori identical
update, mismatch payload, duplicate ambiguity, fake deadline/close tanpa REST,
OSError dan timeout awal tanpa reconnect. Validator sumber yang dipakai adalah
implementasi produksi sebenarnya. Tidak ada exchange/signer nyata pada tes.
Suite/build produksi tidak diulang karena source produksinya tidak berubah.

## Keadaan akhir dan bukti

Status engine rutin/SQLite read-only: saldo/collateral **49,430756 USDC**, posisi
global/BTC0, seluruh order aktif/pending0, unresolved intent0, pending activation0.
Kas matched, selisih0,000000 USDC; umur komponen akun/order2,225/2,389s pada
pemeriksaan akhir. IDLE, entries stopped, order OFF, sesi closed, loaded permission
NULL. Counter tetap6 intent historis,4 fill,21 command,1 request aktivasi historis;
tidak ada command, order, broadcast atau fill baru. Request ACTIVATED historis
dipertahankan; tidak ada request menunggu dan sesi terkait sudah closed.
Empat fee exact tetap NULL; reported fee dan presisi historis tidak diubah.
Pemeriksaan terminal22:48:06,572973 WIB kembali cocok; umur akun/order
0,702/0,819s. Hash source produksi dan config tetap sama.

Raw privat, helper proses agregasi dan hasil replay ada di
`/home/yuri/Workspace/outputs/lighter-depth-comparator-20261006`.
Hash di [manifest](audits/depth-comparator-20261006/manifest.json), hasil numerik
di [summary](audits/depth-comparator-20261006/summary.json). Copy raw produksi
adalah prefix gzip terbatas pada ukuran saat capture; trailer mungkin belum ada.
Hanya baris JSON lengkap dipakai, lalu dipotong menurut marker monotonic asli.
Payload privat, status penuh, IP LAN dan key/token tidak masuk Git.
Manifest baru berisi39 file; SHA256 manifest
`e00499cd8531326a8e84bfd1242ecbab934699d165ed2acb1b273d9f51ed5fad`.

Kesimpulan berlaku untuk satu window15menit: ada keterlambatan pada klien
minimal juga, tetapi penyebab upstream/jaringan/host belum terbukti. Perbaikan
reconnect atau klaim profitabilitas tidak didasarkan pada hasil ini.
