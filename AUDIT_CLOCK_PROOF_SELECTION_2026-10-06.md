# Review clock sesi 6 Oktober — offline

## Keputusan

**Rekomendasi tunggal: pemilihan proof dengan penyertaan interval**, cache kecil
proof accepted yang belum kedaluwarsa, dan latest accepted sebagai fallback.
Pilih proof lama hanya bila seluruh intervalnya setelah drift berada di dalam
interval latest yang masih valid. Bukan sekadar memilih RTT/width terkecil.
Ini **usulan perubahan kebijakan pemilihan proof**, belum diimplementasikan
atau dimuat. Memerlukan persetujuan kebijakan pada tugas implementasi berikutnya.
Tidak mengubah batas guard atau strategi dan tidak memperpanjang TTL.

Dasar:22 pergantian mempunyai proof lama yang masih sah;9 proof baru lebih
lebar,5 juga mencakup seluruh interval lama (request130/134/144/150/153).
Replay kebijakan penyertaan pada input yang benar-benar direkam mengurangi
penolakan sumber37,432s tanpa penolakan baru. **Tidak menyelesaikan expiry**,
tidak membuktikan coverage/entry/live readiness, dan belum merupakan hasil runtime.

## Bukti dan kondisi awal

Baca AGENTS.md, RUNBOOK.md, audit bounded session, summary dan kedua manifest.
HEAD awal `0c998dc`; runtime **1205d35/PID341351**, sumber produksi tidak diubah.
Seluruh **16 hash bukti privat cocok**. Input tetap berada di
`~/Workspace/outputs/lighter-strategy-session-20261006/00fa3c17-168e-4ee7-a757-ea6a474f0b22/`.
Analisis tidak menyalin raw/private account recordings ke Git.

Status engine aktual saat mulai: IDLE, orderOFF, sesi UUID tersebutclosed,
izin loaded kosong, posisi seluruh akun/order/pending nol, equity49,430756USDC,
kas matched. Hanya GET status lokal yang sudah diterbitkan engine; tidak
menambah request exchange, recorder runtime, izin, aktivasi, restart, atau config.

Timeline30kalibrasi lengkap dengan wall start/receive, RTT penuh, serverTime,
Chrony, offset lama/baru, drift, identitas proof, TTL dan alasan ada di
`audits/clock-evidence-review-20261006/analysis.json`. Ini metadata clock yang
dipilih/diturunkan untuk audit, tanpa payload akun/order/authentication.

## Fakta implementasi dan observabilitas

`source_time.py:BinanceSourceClock.run` memakai GET
`https://fapi.binance.com/fapi/v1/time` setiap60s, client khusus yang hidup
sepanjang worker, request timeout2s. Chrony dibaca sebelum request started;
RTT yang dicatat mencakup request sampai JSON selesai, bukan durasi Chrony.
RTT min163,398ms, median244,466ms, max1.450,175ms.

Library terpasang **aiohttp3.14.3**. `network.py:binance_session` tidak memberi
keepalive_timeout; BaseConnector memakai default15s dan memeriksa umur idle
saat reuse. Gap request60s melewati batas reuse lokal tersebut. Client clock
tidak berbagi pool dengan client feed/kurs. DNS cache connector dimatikan;
resolver memiliki cache sendiri. Ini mekanisme lokal yang terverifikasi,
**bukan bukti fase mana yang membuat empat RTT melewati400ms**.

Trace tidak menyimpan queue/pool/reuse, DNS, TCP, TLS, headers/TTFB, body/decode,
atau exact start/receive monotonic untuk request rejected. TCP/TLS resumption
dan penutupan idle oleh remote juga unknown. Jangan menamai seluruh RTT sebagai
handshake/network latency atau mengurangkan serverTime sebagai timing fase.
ServerTime adalah Unix **milidetik**, bukan offset clock yang sudah diketahui.

26 respons accepted: serverTime dan received monotonic tersimpan; started
monotonic dapat direkonstruksi dari received minus RTT. Native `install()`
direplay dan offset identik.4 rejected: object `clock` merupakan **proof lama**;
serverTime baru tidak disimpan, tetap **NULL/unknown** di timeline. Menyalin
serverTime lama ke request tersebut akan membuat bukti palsu.

Semua30 HTTP200;26accepted/4rejected. Chrony semuanya synchronized/Normal,
error maksimal14,576ms (<50), selisih wall elapsed vs RTT maksimal0,001579ms
(<5ms validasi install). Setiap rejected pasti melanggar RTT400ms; karena
serverTime baru hilang, validasi timestamp tambahan tidak dapat disingkirkan.

| Request | Mulai WIB | Terima WIB | RTT ms | Hasil |
| --- | --- | --- | ---: | --- |
| 140 | 11:31:25.203407 | 11:31:25.643254 | 439,846 | rejected |
| 146 | 11:37:25.163855 | 11:37:25.890210 | 726,357 | rejected |
| 147 | 11:38:25.193249 | 11:38:26.643425 | 1.450,175 | rejected |
| 156 | 11:47:25.162750 | 11:47:25.575392 | 412,641 | rejected |

## Dua masalah berbeda

**Expiry:**146/147 tidak memasang proof baru. Proof145 melewati TTL asli120s;
diagnostik expired11:38:25.879–11:39:25.752 sebelum148verified.
Total expired yang tertangkap60,140031s termasuk kejadian singkat berikutnya.
Gap sampel valid60,291s menahan coverage sekitar539,89s setelah sumber kembali;
total coverage blocker runtime586,670701s termasuk gap lain. Ada jeda TTL
sub-poll saat129/141 mengganti proof berumur>120s; sampling0,316s maksimum
tidak menjamin semua jeda singkat tertangkap. Tidak mengubah riwayat gap.

**Future uncertain:** interval umur sumber = indikator wall/source yang teramati
ditambah interval offset proof, diperlebar drift100ppm (0,1ms perdetik umur proof).
Guard menolak bila lower<-1ms walaupun upper belum membuktikan future pasti.
Clock uncertainty tetap<=250ms, source upper tetap<1.500ms, host<=50ms,
wall-step<=25ms. Receipt baru tidak membuktikan lower bound sudah cukup yakin.
FUTURE_UNCERTAIN runtime **226,837150s** berbeda dari expiry60,140031s.
Menghilangkan cold connection tidak otomatis menyelesaikan interval future.

`install()` menerima lalu mengganti evidence tanpa perbandingan kualitas.
Sembilan widening tetap lolos semua guard install. Lebar yang lebih kecil
belum berarti lower bound lebih baik: pusat interval juga bergeser. Tidak
menganggap pusat interval sebagai pengukuran offset clock yang pasti.

## Replay kausal dan batas coverage

Pakai `BinanceSourceClock.install/assess` produksi yang hash-nya cocok runtime.
Replay **6.666 assessment sumber**: **0 mismatch** reason/pass maupun bounds.
Alternatif memakai hanya proof accepted dengan waktu bukti tersedia<=evaluasi;
tidak menggunakan kalibrasi masa depan, interval intersection, timestamp baru,
atau pemilihan berdasarkan apakah harga/sinyal lolos. Masing-masing proof
mempertahankan anchor, Chrony, drift, TTL dan seluruh validasi aslinya.

| Penolakan sumber | Produksi | Width minimum, offline | Interval tercakup, offline |
| --- | ---: | ---: | ---: |
| FUTURE_UNCERTAIN | 226,837150s | 223,943047s | 189,404900s |
| EVIDENCE_EXPIRED | 60,140031s | 60,140031s | 60,140031s |

Width minimum memulihkan37,432249s tetapi membuat34,538146s penolakan baru;
net hanya2,894103s. **Jangan memakai width/RTT minimum saja.** Pemilihan menurut
penyertaan memulihkan37,432249s, penolakan baru0,1107captured evaluations memilih
proof berbeda. Tidak menerjemahkan hasil ini menjadi tambahan kesiapan teknis.

Durasi adalah integral assessment sebelumnya antara capture lokal, di-clip ke
window1.800s. Timestamps evaluasi/receipt/source asli dipakai pada tiap replay,
tidak menilai keputusan lama dengan assessment tampilan berikutnya. Replay
tidak memasukkan ulang sampel strategy/basis. Trace tidak merekam assessment
clock/proof dan waktu processing **setiap pemanggilan strategy.sample**; raw
receipt/queued/recorded bukan timestamp processing tersebut. Karena itu
**coverage alternatif tidak dihitung**. Interval expiry tetap tanpa proof
valid; cache lama tidak bisa mengisi gap60s tersebut atau meluluskan coverage.

## Satu tindakan berikut dan pengujian yang diperlukan

Implementasi yang diusulkan berada hanya di **source_time.py**, pada
`BinanceSourceClock.install/assess` dan pemulihan/invalidation proof di `run`:

- Selalu simpan latest accepted, tambah cache terbatas (maks3/TTL120s pada
  cadence60s). Seluruhnya volatile; restart mulai unverified, tanpa reuse bukti lama.
- Pada assessment baru, validasi latest dan kandidat dengan guard asli.
  Ambil satu pasangan wall/monotonic evaluasi dan pakai bersama untuk semua
  kandidat serta hasil final; jangan menghitung alasan dengan timestamp berbeda.
  Kandidat harus sudah tersedia, host sah, umur dalam TTL asli, wall/stamp
  konsisten, uncertainty sah, dan seluruh interval drift berada dalam latest.
  Pilih yang paling sempit dari kandidat tercakup, tanpa melihat source pass/harga.
- Saat old expired, fallback latest accepted yang disimpan. **Jangan menolak
  penyimpanan proof baru lalu terus memakai old**, dan jangan menyegarkan anchor.
- Semua invalidation yang sebelumnya mengosongkan evidence (unsynced host,
  418/429, recorder failure), dan global wall-step harus tetap memblokir/clear
  cache. Jangan memakai pool untuk melewati status invalidasi. Rekam latest
  dan selected proof ID, umur, drift dan alasan; dispatch tetap assessment baru.

Ini perubahan kebijakan penerimaan bukti entry/sampling, bukan sekadar perbaikan
telemetry dan bukan perubahan threshold/freshness. **Belum diterapkan.**
Tes sebelum rollout: narrower-but-shifted tidak dipilih; contained old dipilih;
future proof dilarang; old expiry fallback latest tanpa reset TTL; kedua proof
expired tetap blocked; missing/future timestamp, batas host/uncertainty/wall-step,
global invalidation/record overflow, restart, dan stop/deadline/exit independen.
Transport harus palsu tanpa broadcast. Ukuran keberhasilan: replay baseline
identik; native guard tidak dilonggarkan; source pass sebelumnya tidak menjadi
reject karena selection; hasil37,432s direproduksi; expiry tetap diblokir;
setiap assessment dapat diaudit dengan identitas proof sebenarnya. Runtime,
coverage dan keselamatan entry harus diverifikasi kemudian dengan izin terpisah.

Hipotesis keepalive belum dipilih sebagai perubahan produksi. Untuk menentukan
apakah DNS/connect/TLS benar-benar mendominasi RTT, bukti minimum yang hilang:
connection_reused, pool wait, resolver/cache, connection_create timing,
headers/body/decode timing, monotonic start/receive, returned serverTime pada
rejected, reject flags asli, serta scheduled/wake/Chrony timing. Eksperimen
minimum yang relevan ialah satu observasi read-only terbatas15menit atas
request60s yang **sudah berjalan**, tanpa polling tambahan, dengan TraceConfig
pasif. Create timing mencakup TCP+TLS; jangan melabelinya TLS tersendiri tanpa
hook tambahan. Pertahankan config; jika RTT tinggi tidak muncul, tandai belum
direproduksi. Eksperimen ini **tidak dilakukan/diotorisasi pada tugas ini**.

## Validasi dan akhir

30tes offline lulus:7analisis kausal +23validator produksi, termasuk tidak ada
transport yang dijalankan oleh analisis. Ruff lulus. Python3.12 proyek dipakai
untuk integrasi langsung dengan validator yang ada, tanpa migrasi toolchain.
Hanya script analisis, tes analisis, JSON turunan, dokumen dan hash yang di-commit.
Sumber produksi, konfigurasi, runtime recorder/PID tidak berubah; tidak ada
restart, order, broadcast, pemuatan izin, atau sesi baru. Strategi8bps/buffer5,
risiko, request reserve dan empat fee exactNULL dengan reported terpisah tetap.
Keadaan akhir diperiksa kembali melalui status engine dan dicatat dalam
`audits/clock-evidence-review-20261006/final-status.json` (whitelist angka/status).
