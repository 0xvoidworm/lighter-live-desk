# Binance public reconnect — satu pengukuran pembanding

Basis repo: `ab1d5c7`. Engine yang diamati tetap PID 255514, tanpa restart,
izin order OFF, IDLE, otorisasi sesi sebelumnya closed. Tidak ada izin baru,
perubahan strategi, guard, DNS sistem, TLS, VPN, atau jaringan.

## Kriteria ditetapkan sebelum pengukuran

- Tepat satu pengukuran maksimal 900 detik sejak marker `started.json` dibuat;
  DNS, handshake, kegagalan, dan backoff termasuk durasi. Tidak ada pengulangan.
- Klien pembanding Rust terpisah, URL combined public dan dua subscription sama:
  `wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker/btcusdt@depth@100ms`.
  Tidak mengimpor engine, SDK/signer, atau membaca key/config trading.
- Resolver DoH A Cloudflare lalu Google, IPv4, tanpa proxy; mencatat IP tujuan
  dan waktu resolver/TCP/TLS+WS. TLS tetap diverifikasi terhadap nama host.
  Implementasi TLS Rust berbeda dari aiohttp/OpenSSL; pemilihan IP bisa berbeda.
- Mengikuti ping/pong, backoff 2/4/8/16/30 detik, tanpa polling REST book.
  Pesan heartbeat tidak menjadi observasi harga. Klien pembanding mengukur
  transport/sequence pesan, tidak menyatakan book bisa diperdagangkan.
- Status lokal engine dibaca sekali per detik, termasuk trace yang sudah
  dikonsumsi engine, socket `/proc`, antrean, event-loop lag, receipt/enqueue/
  processing. Tidak menambah polling akun, Lighter, Binance market, atau kurs.
- Putus bersamaan mempersempit dugaan ke faktor bersama, bukan membuktikan
  ISP/upstream. Minimal stabil saat app putus mempersempit perbedaan implementasi
  atau rute/IP, bukan otomatis membuktikan bug engine.
- Ukur apakah pesan diterima namun terlambat diproses. Pisahkan penghentian
  lokal karena deadline dari penutupan remote/error transport.
- Jika tidak muncul gangguan: belum direproduksi. Tidak ada klaim perbaikan.
- Akhir: verifikasi akun melalui mekanisme yang sudah ada; order OFF/sesi closed.

## Rekonstruksi historis awal

Persiapan 13:32:13–13:44:09.377 UTC = 716.377 detik. Rekaman status kontinu
baru mulai 13:37:04.945 UTC, sekitar 424.64 detik sampai penutupan; 291.945 detik
awal tidak mempunyai rekaman status kontinu. Trace handshake yang masih tersimpan
memberi sebagian bukti awal, bukan bukti penuh evaluator/heartbeat interval itu.

Sembilan reconnect adalah sembilan kenaikan counter public 54→63 dalam rekaman.
Market dan Lighter masing-masing tetap 1→1. Tidak ada event BOOK_GAP di DB pada
interval persiapan. Semua sembilan mempunyai wrapper ConnectionError,
close_code 1006, cause ClientConnectionResetError; pesan exception asli/frame
close dan ping/pong tidak tersimpan, sehingga pemicu reset belum dapat ditentukan.
Queue pada sampel putus nol, lag event loop 0–3.3 ms.

## Sembilan koneksi historis

Waktu UTC. ID adalah `pid255514:httpN` dari trace handshake HTTP, bukan UUID
native socket. Semua handshake berikutnya 101/berhasil. URL/subscription berasal
dari konstanta kode runtime yang diverifikasi dan jenis pesan BTCUSDT; trace HTTP
lama hanya menyimpan host/path, tidak menyimpan query subscription.

| Counter setelah putus | ID HTTP | Tersambung | Putus | Umur s | Backoff aktual s | Handshake berikutnya s | Depth kembali valid |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| 55 | 111 | 13:37:06.416 | 13:37:26.370 | 19.954 | 30.001 | 0.950 | 13:37:59.647 |
| 56 | 113 | 13:37:57.321 | 13:38:21.211 | 23.890 | 30.001 | 0.530 | 13:38:53.423 |
| 57 | 115 | 13:38:51.742 | 13:39:11.826 | 20.084 | 30.022 | 0.315 | 13:39:42.365 |
| 58 | 117 | 13:39:42.163 | 13:39:55.039 | 12.876 | 30.001 | 0.364 | 13:40:25.616 |
| 59 | 119 | 13:40:25.405 | 13:41:14.988 | 49.584 | 2.001 | 2.380 | 13:41:19.554 |
| 60 | 121 | 13:41:19.369 | 13:41:43.467 | 24.098 | 4.001 | 0.320 | 13:41:48.013 |
| 61 | 123 | 13:41:47.788 | 13:42:17.255 | 29.466 | 8.001 | 0.828 | 13:42:26.311 |
| 62 | 125 | 13:42:26.084 | 13:42:50.085 | 24.002 | 16.001 | 0.563 | 13:43:09.190 |
| 63 | 127 | 13:43:06.650 | 13:43:28.380 | 21.731 | 30.005 | 0.343 | 13:43:58.941 |

Detail per kejadian, pesan terakhir, snapshot, exception yang tersedia, queue,
lag, dan fase handshake: [JSON historis](audits/binance-public-historical-reconnects-20261005.json).
Replay memakai kelas Book proyek dan receipt stamp asli. Tidak ada sequence error
dalam interval rekaman; error bootstrap sebelum interval tidak dihitung sebagai
kejadian sesi. Recorder menulis sebelum apply, sehingga waktu itu bukan waktu
akhir pemrosesan. Pesan buffered mempertahankan receipt aslinya.

Tidak ditemukan watchdog harga basi yang memanggil close/restart. Harga basi
menghalangi entry/sampling. Restart feed karena GapError mempunyai event BOOK_GAP;
tidak ada event itu pada persiapan atau pengukuran pembanding. Cancellation tidak
melewati jalur penambahan counter reconnect. Jalur yang terlihat adalah iterator
WS berakhir/error → wrapper websocket_closed → context cleanup → reconnect.
Ini membedakan urutan kode lokal, bukan membuktikan siapa mengirim FIN/RST di wire.

## Gap 33,645 detik

Nilai ini adalah gap pasangan sampel valid strategi dalam coverage 600 detik,
bukan waktu handshake maupun interval antarperubahan harga. Nilai sebelumnya
33,066 berubah menjadi 33,645 pada snapshot engine `updated_at=13:37:59.689847`;
collector membaca snapshot itu pada 13:37:59.956928. Timestamp status asli dipakai,
tidak dianggap sama dengan waktu collector. Pasangan sampel tidak diekspor
langsung; lokasi sub-milidetik kedua sampel tidak dapat dibuktikan dari status
yang membulatkan last_sample_age ke 0,1 detik.

Kejadian counter 55 cocok dengan kenaikan tersebut:

| Tahap | Durasi s | Bukti |
| --- | ---: | --- |
| Putus → mulai handshake berikutnya | 30.0009 | Trace/counter; konfigurasi backoff 30 |
| Handshake DNS/connect/TLS/header sampai 101 | 0.9504 | Request 113 |
| 101 → snapshot dicatat | 1.0955 | Snapshot REST di raw recorder |
| Snapshot → depth pertama yang bridge/valid | 1.2305 | Replay Book, receipt-to-record 0.332 ms |
| Batas sampel sebelum putus/sesudah valid dan overhead tersisa | sekitar 0.3677 | Selisih dari nilai coverage; bukan fase wire yang terukur |

Backoff sekitar 89% dari gap. Payload baru sudah diterima 13:37:57.378523,
buffered sampai snapshot tersedia, lalu diproses dengan receipt asli; delay
receipt-to-record 1.047 detik pada payload buffered itu bukan queue engine macet.
Depth yang memenuhi sequence baru diterima 13:37:59.647160. Handshake dan
pemulihan book mempunyai biaya nyata, tetapi tidak menjelaskan jeda 30 detik.
Asal hilangnya transport belum dapat disimpulkan dari gap ini.

## Pemeriksaan dokumentasi dan implementasi

[Pemetaan resmi Binance](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Important-WebSocket-Change-Notice)
menempatkan bookTicker/depth di `/public` dan aggTrade di `/market`. Kode/runtime
sudah memakai pemetaan tersebut; perbaikan pemetaan lama tidak dibatalkan.

[Aturan USD-M Futures](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect)
menetapkan server ping sekitar 3 menit, pong dalam 10 menit, 10 pesan masuk/detik,
1024 stream, dan usia koneksi sampai 24 jam. Jangan menggunakan angka Spot
sebagai batas Futures. Aplikasi menggunakan combined URL dua stream tanpa pesan
JSON SUBSCRIBE ulang; klien pembanding juga begitu. Kedua subscription implicit
tidak menghasilkan burst control-message. Respons heartbeat minimal tercatat
lima server ping dan lima matching auto-pong, tanpa duplicate pong/client ping.

aiohttp terpasang 3.14.3: autoping/autoclose default, heartbeat client 20 detik
idle, pong timer sekitar 10 detik; timer mengikuti pembulatan bawaan library.
WS receive normal tidak mempunyai timeout periodik; bootstrap memakai
receive_str(timeout=1), menangkap TimeoutError, lalu melanjutkan. Sumber library
dan regresi lokal menunjukkan timeout receive menandai code 1006 tetapi tidak
menutup socket; socket tetap menerima pesan berikutnya. ClientTimeout total 10 /
connect 5 berlaku pada handshake/REST, bukan bukti timeout read WS 10 detik.
Kesamaan angka umur koneksi dengan heartbeat tidak dijadikan bukti penyebab.

[Pemulihan diff-depth resmi](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly)
diikuti: buffer sebelum snapshot, drop update yang lebih tua, bridge snapshot
dengan U/u, lalu pu harus cocok dengan u sebelumnya. Engine mengharuskan bridged
sebelum market valid. Gap mengharuskan recovery snapshot; tidak menyalin quote
untuk mengisi gap. Tidak ada overflow/sequence recovery pada dua interval ini.

## Hasil satu pengukuran 15 menit

15:03:08.186894–15:18:08.189157 UTC (22:03–22:18 WIB), window data 900 detik.
Finalisasi file selesai 2,3 ms setelah deadline. Tidak ada perpanjangan, restart,
rearm, order, atau pengukuran kedua. Proses engine PID 255514 tetap sama.

- Public aplikasi counter **180→188**, delapan kejadian dalam window; market
  **1→1**, Lighter **1→1**. Queue pada sampel putus nol, lag 0–2,2 ms.
- Klien minimal: **9 koneksi, 8 receive_error**, semuanya
  `Protocol(ResetWithoutClosingHandshake)`. Tidak menerima frame Close; tidak
  mengalami heartbeat timeout lokal. Ini terminasi tanpa handshake WS yang rapi,
  bukan bukti khusus bahwa paket TCP RST diterima.
- Empat kejadian aplikasi mempunyai error minimal terdekat dalam 0,253–1,875 s;
  satu lagi 3,448 s. Lainnya 6,453–21,967 s. Tidak semua putus serentak.
- Minimal menerima **428.480** pesan (420.721 bookTicker, 7.759 depth), tanpa
  sequence gap pu/u di dalam tiap koneksi. Gap antarkoneksi tetap dicatat.
- DoH Cloudflare A berhasil 9 kali, fallback Google nol; 9 TCP/101 berhasil.
  Minimal IPv4, proxy OFF, native-root TLS/SNI diverifikasi. Keluarga socket
  engine dan mode DoH A juga IPv4 pada data yang terekam. IP target minimal dan
  kandidat peer 443 engine tidak beririsan dalam sampel; inode engine tidak
  memastikan hostname/channel. IP antara klien berbeda, sehingga ini bukan uji
  pada rute/frontend yang dipastikan sama. Library TLS/WS juga berbeda.
- Tidak menambah polling akun/market/FX. Tambahan hanya 9 handshake WS public,
  9 request DoH, dan 5 auto-pong. Statistik ini tidak memakai bucket Lighter.
- 899 snapshot lokal, tanpa command runtime selama window. Readiness teknis
  menurut guard yang sudah ada **175,600 s (2m55,6s)**, interval terpanjang
  **120,215 s**. Closed/OFF tidak dihitung sebagai blocker teknis.

| Blocker | Durasi s, saling overlap |
| --- | ---: |
| Coverage | 721.390 |
| Basis/scale | 722.388 |
| Market data | 61.082 |
| Entry request reserve | 66.132 |
| Reconciliation freshness | 7.994 |
| Reconciliation berjalan | 12.001 |

Durasi blocker tidak dijumlahkan. Kombinasi overlap lengkap tersedia di
[JSON pengukuran](audits/binance-public-comparison-20261005.json), bersama semua
event nonmessage minimal, DNS/IP, close ownership, heartbeat, dan timeline app.
Raw lengkap tetap di `Workspace/outputs/lighter-binance-transport-comparison-20261005`;
hash SHA256 tersimpan di JSON. Catatan progress sempat mengandalkan tail file
dan melewatkan error sebelumnya; kesimpulan di sini memakai seluruh rekaman,
bukan cuplikan progress tersebut.

## Pesan diterima, source lag, dan tahap lokal

Indikator E exchange → waktu decode/receive minimal:

| Stream | p50 s | p95 s | p99 s | maksimum s |
| --- | ---: | ---: | ---: | ---: |
| bookTicker | 1.138 | 10.081 | 16.404 | 17.744 |
| depth | 0.497 | 9.832 | 15.613 | 17.700 |

Pesan terakhir minimal sebelum delapan error mempunyai indikator lag
17,641 / 10,233 / 5,049 / 12,770 / 8,044 / 13,975 / 12,795 / 5,987 detik.
App juga menunjukkan public source lag: depth p95 8,377 s, sedangkan aggTrade
p95 0,242 s. Untuk pasangan receipt kedua channel dalam jarak <1 s, selisih lag
public-minus-market p95 **8,155 s**, maksimum **21,328 s**. Selisih dalam exchange
yang sama mengurangi pengaruh offset clock host/exchange. Distribusi app adalah
sampling last-event 1 Hz, bukan histogram seluruh pesan.

Ini menunjukkan data public yang sedang diterima bisa sudah tertinggal di waktu
sumber sebelum koneksi berakhir. Belum diketahui apakah penumpukan berada di
fanout upstream, jalur TCP, atau buffer transport lokal. Bukan bukti latensi fill.
Clock tidak disinkronkan ulang; indikator absolut bukan pengukuran one-way wire.

Pemrosesan app yang tersampel: queue wait maksimum depth 45,761 ms, bookTicker
36,340 ms; receive→enqueue depth 2,086 ms, ticker 0,178 ms. Peak queue seluruh
window 24, event-loop lag 30,1 ms. Tidak ditemukan penahanan engine beberapa
detik yang sebanding dengan source lag. Sampling bisa melewatkan puncak singkat;
tidak ada trace scheduler OS/CPU per paket. Guard saat ini memakai kesegaran
receipt; readiness teknis di atas tidak membuktikan source lag selalu kecil.
Definisi guard tidak diubah dalam tugas ini.

## Perbaikan terbatas, verifikasi, dan batas kesimpulan

Bug observabilitas terbukti: websocket_closed membuang objek cause/pesannya,
dan reconnect hanya menyimpan nama kelas. Sekarang exception original tetap
menjadi `__cause__`; detail reconnect menyimpan message, cause message, dan
exception chain/traceback yang tersedia dari library. Tidak mengubah aturan
reconnect atau pemulihan.

**Ini tidak memperbaiki penyebab transport putus.** Perubahan logging belum
dimuat di engine yang berjalan; engine sengaja tidak direstart dan riwayat
coverage tetap dipertahankan. Tidak mengklaim penerapan logging baru di mainnet.

Verifikasi lokal: 21 tes transport/feed/network, 4 tes analysis (25 Python total),
4 tes Rust; semua lulus. Regresi localhost memakai aiohttp asli: abort transport
menyisakan cause/message lengkap; timeout bootstrap satu detik tidak menutup
socket hidup. Rust menguji matching pong tunggal, status OFF/closed/PID, DNS
nonpublic, backoff cap. Ruff check/format dan rust-workbench check lulus. Tidak
ada SDK signer/key/order transport pada klien minimal atau server test localhost.

Pemicu langsung yang terlihat adalah terminasi transport public tanpa close
WS normal. Dua implementasi independen mereproduksi gangguan/source lag,
sebagian waktunya berdekatan; ini melemahkan penjelasan yang hanya bergantung
pada engine, tetapi belum mengatribusikan gangguan ke ISP, router, host, atau
Binance. Capture paket gagal karena CAP_NET_RAW tidak tersedia; tidak mengubah
permission/network. Tidak ada bukti arah FIN/RST, retransmission, atau penyebab
upstream yang cukup untuk klaim tersebut.

Satu langkah berikutnya: uji A/B read-only depth-only versus kombinasi sekarang
dengan library dan IP target yang sama. bookTicker menyumbang **98,19%** pesan
minimal dan tidak dipakai `_apply_market` untuk memperbarui book strategi;
uji itu mengisolasi pengaruh volume/fanout public terhadap penumpukan/putus.
Ini usulan pengukuran berikutnya, belum dilakukan, bukan perubahan subscription
atau guard dan bukan izin trading.

## Keadaan akhir

[Pemeriksaan akun](audits/binance-reconnect-final-account-20261005.json): akun
751745, equity/available **49,430756 USDC**, posisi semua market nol, seluruh
order aktif/pending nol, intent unresolved nol. State IDLE, order OFF, sesi lama
closed. Tidak ada command, intent, atau fill baru sejak awal tugas. Komponen akun
lengkap/segar <15 s; audit riwayat lengkap dengan jadwal idle 120 s, kas matched
tanpa selisih. Fee exact empat fill historis tetap unknown/NULL dengan bukti
reported fee resmi terpisah. Tidak ada perubahan saldo, fee policy, threshold
8 bps, buffer biaya 5 bps, freshness, coverage/gap 5 s, limit risiko, atau kuota.
