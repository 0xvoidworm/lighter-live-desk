# Audit transport, heartbeat dan recovery — 6 Oktober 2026 WIB

## Lingkup dan baseline

Tugas hanya mengizinkan implementasi/tes, satu restart terkontrol, dan satu
observasi20menit tanpa order. Tidak ada probe, LOAD_SESSION_PERMISSION,
START_LIVE, rearm, transaksi, koneksi pembanding, ping tambahan, perubahan jaringan
atau polling akun tambahan. Otorisasi strategi historis tetap closed.

Repo awal bersih pada1ecb4ef;31813c8 merupakan perubahan pencatatan keputusan
yang belum dimuat. Runtime awal818b508/PID301678, aktif sejak01:19:21WIB.
Baca AGENTS.md, RUNBOOK.md dan AUDIT_CLOCK_DECISION_RECONNECT_2026-10-06.md.
Audit terdahulu sudah memiliki snapshot/depth, source/receipt timestamp,
exception chain, backoff dan REST phase. Pemicu transport awal, callback timer,
PONG sebelum autoping, dan coverage pascareconnect belum tercatat lengkap.

Pemeriksaan sebelum restart memakai state/audit engine yang sudah ada, bukan
GETexchange baru: akun751745, Standard/Unified, margin0/IM100%,
equity/available/collateral49,430756USDC; posisi/order/pending/intent0;
komponen akun lengkap dan berumur<15s; history complete, kas matched/difference0;
IDLE, entries_stopped, orderOFF, sesi closed. Empat fee exact tetapNULL.

## Perubahan yang dimuat

Commit produksi1205d35 memuat perbaikan keputusan31813c8 beserta instrumentasi.
Satu restart dilakukan09:12:22WIB; PID baru341351, NRestarts0.
Marker/code hash, PID dan statusOFF/closed disimpan terpisah. Tidak ada restart
kedua atau perubahan modul produksi/config selama rekaman.

`websocket_trace` mengikat run/connection/event ID, handshake wall/monotonic,
frame terakhir library/aplikasi, jadwal/callback heartbeat, pengiriman ping asli,
deadline pong, close pertama dan cleanup tambahan. Hook aiohttp3.14.3
mengamati dequeue sebelum autoping; hook websockets16.1.1 mengamati frame protokol
sebelum pencocokan pong. Iterator aplikasi memang dapat menyembunyikan PONG.
Dequeue bukan timestamp kernel; CHUNK bukan pesan harga atau bukti freshness.

Keepalive websockets terikat hash implementasi terpasang dan regresi AST
control-flow; jika versi berbeda, kembali ke keepalive asli dengan hook jadwal
ditandai unavailable. Native asyncio.timeout tetap membatalkan task/mengubah
CancelledError menjadi TimeoutError sesuai library. Tidak mengubah sleep,
heartbeat/autoping, timeout, subscription, backoff, DNS atau TLS/SNI.

Klasifikasi menyimpan pemicu paling awal: EOF, frame remote close, library
heartbeat timeout, task cancellation atau close aplikasi. Exception tambahan
tetap berurutan; EOF lalu error menulis close tidak dilaporkan sebagai kegagalan
writer yang memulai putus. Socket akun mencatat rotasi auth480s sebagai penutupan
aplikasi terjadwal, tanpa payload/token. Tidak ada klaim sebab ISP/host/upstream.

Logger baru memakai put_nowait/queue512, daemon thread gzip sendiri, counter frame
coalesced dan pulse5s. Overflow/write error dihitung; disk lambat tidak menahan
feed/order lock. Timer pasif0,5s mengukur keterlambatan callback, tidak mengubah
evaluator. Context mencatat REST yang benar-benar sedang aktif, queue pasar/akun,
dan durasi write recorder pasar yang diukur. Snapshot→bridge U/u/pu→receipt/source
valid→coverage observed memakai connection ID. Coverage observed berasal dari
status yang memang dibaca; tidak mengarang waktu transisi di antara snapshot.

Subscription tetap depth-only/public dan aggTrade/market; kurs Spot terpisah.
Freshness, future/clock/TTL, coverage600s/480/gap5s, threshold8bps/buffer5bps,
fee, risiko dan pagu40/55/cadangan exit15 tidak berubah. Entry/dispatch menilai
data ulang; stop/deadline/close tidak bergantung pada kelulusan guard entry.

## Bukti offline sebelum rollout

Full suite430passed (36,56s), Rufflulus, mypy21sourcefileslulus.
Targeted suite47passed. Dua tes analyzer offline tambahan lulus sesudah rollout;
tidak memanggil exchange atau mengubah source produksi. Tiga warning deprecation
full suite berasal dari library/toolchain yang sudah ada.

Regresi memakai ClientWebSocketResponse sebenarnya dengan reader/writer palsu:
EOF sebelum error cleanup, PONG tersembunyi autoping, callback awal/terlambat,
timeout pong, cancellation dan close normal. Hook websockets diuji dengan Frame
asli, matched PONG, remote CLOSE, timeout1011 dan cancellation. Recorder lambat
serta overflow direproduksi dengan writer thread ditahan; loop tetap berjalan.
Snapshot/bridge/source/coverage memakai Engine/Book/strategy sebenarnya.

Regresi31813c8 memakai data_valid produksi pada boundary clock; assessment yang
dicatat sama dengan yang menentukan valid, termasuk source=null bila belum
diperiksa. Missing/future/uncertain/late source tetap diblokir; delta tetap
diterapkan untuk sequence; stop/timer/close independen dan dispatch memeriksa ulang.
Transport/signing uji palsu tidak broadcast. Detail batas hook/kriteria ditetapkan
sebelum rollout di TRANSPORT_TELEMETRY_DESIGN_2026-10-06.md.

## Hasil observasi dan keputusan

Rekaman tunggal09:12:45,680063–09:32:45,680589WIB; window analisis tepat1200s,
collector selesai1200,000557s. Ada4798snapshot, collector error0, source/config
hash unchanged. Awal0,043688s belum teramati, tidak dihitung siap. Integrasi
memakai snapshot4Hz, bukan mengarang waktu keputusan kontinu.

| Pengukuran | Seluruh20menit | Sesudah warm-up |
|---|---:|---:|
| Warm-up belum lengkap |639,798s|—|
| Waktu teramati sesudah warm-up |—|560,158s|
| Seluruh guard teknis lolos |285,383s (4m45s)|50,947%|
| Interval siap kontinu terpanjang |7,503s|7,503s|
| Coverage terblokir |826,784s|186,986s|
| Source/market_data terblokir |185,241s|29,665s|
| Basis/scale terblokir |911,823s|272,025s|
| Rekonsiliasi sedang berjalan |11,005s|5,752s|

Durasi blocker bertumpang tindih; market_data/source merupakan penolakan yang
sama, bukan dua gangguan independen. Sesudah warm-up,173,578s bersamaan
basis+coverage;12,158s juga source, dan67,282s basis saja. Matriks overlap lengkap
ada dalam summary. Basis/scale juga turunan warm-up/coverage atau keputusan
invalid:116,956s memakai evaluasi terdahulu invalid,155,069s valid/unknown;
angka terakhir bukan bukti kesalahan estimator. Tidak menyimpulkan penyebab
komputasi basis dari nama blocker saja.

Tidak ada reconnect public/market/Lighter, EOF, remote close, pong timeout atau
sequence gap selama window. Ada11764depth,13505aggTrade,20657Lighter order-book;
futures bookTicker0. Gap receipt book maksimum Binance0,815s/Lighter0,784s,
FX2,617s. Subscription URL dan ID ada pada summary; tidak menggunakan IP tetap.

Bootstrap tercatat sebelum marker: snapshot Lighter pada−19,687s, snapshot
Binance−19,391s, bridge U/u/pu−19,390s. Handshake awal Lighter514,635ms,
akun499,182ms, public814,306ms, market815,831ms. Receipt/source pertama lolos
pada+39,641s, warm-up pada+639,842s, coverage observed siap pada+819,444s.
Pada+1192,776s coverage kembali gagal karena gap sampel valid5,592s; frame book
tetap diterima. Riwayat/gap tidak dihapus. Kesiapan sesaat bukan izin entry.

20kalibrasi clock dalam window:16verified,4ditolak, semuanyaHTTP200. RTT yang
ditolak738,561/597,347/421,579/403,178ms melewati batas400ms. Dua penolakan
berturut-turut menyebabkan proof lama expired60,032s; ini menjelaskan gap
sampel≈60s dan penundaan coverage, bukan kehilangan transport. Source reason
berdurasi CLOCK_NOT_VERIFIED39,767s, FUTURE_UNCERTAIN84,941s, DATA_LATE0,500s,
EXPIRED60,032s; sumber lolos1014,716s. Source upper bound maksimum1752,094ms,
uncertainty maksimum179,855ms. Usia receipt book maksimum767/702ms tetap segar.
Bukti ini tidak menentukan fase penyebab RTT lambat atau latency fill.

Dua rotasi socket akun pada09:20:35,842797 dan09:28:46,467467WIB berawal
`application_auth_rotation`;10event followup cleanup dipisahkan dari dua pemicu
awal. Handshake pengganti314,399/697,007ms. Ini perilaku auth480s yang sudah ada,
bukan reconnect pasar.79PONG cocok dengan pending ping diamati di library;
ping server Binance/autopong juga tercatat. Iterator bukan sumber bukti PONG.

199callback heartbeat: lag median0,933ms/p9545,243ms/maks58,375ms.
273callback timer observasi melebihi25ms, p9577,162ms/maks98,218ms. Pada7kejadian
ada REST aktif (account4,activeOrders2,deposit/history1); compact active trace
hanya menyediakan path, sehingga host/phase ditandaiNULL, bukan ditebak.
Queue pasar pada callback lag median3/p9514/maks55; queue akun0. Write recorder
pasar maksimum kumulatif29,394ms. Korelasi ini tidak membuktikan penyebab lag;
timestamp kernel, tekanan CPU/disk host dan fase REST pada callback belum tersedia.

Logger baru:1240event dalam window,3,38MB JSON menurut delta counter snapshot;
hook terukur1,165s (0,097%window), bukan benchmark CPU komparatif dan tidak
mencakup semua overhead serialization status. Puncak queue7/512, umur queue
12,433ms, write thread maksimum13,911ms, dropped0/write_errors0. Counter status
dibatasi interval snapshot pertama/terakhir; count file memakai window tepat,
jadi tidak dipaksakan identik pada boundary. Tidak ada bukti disk logger baru
menahan feed. Receipt→enqueue depth p950,322ms, queue→recorder p952,644ms.

Semua pembacaan state89 dan audit10yang tercatat selesaiok. Umur komponen akun
maksimum12,959/13,120s (<15s); audit complete/cadangan exit≥15 pada seluruh
1199,956s teramati. Request Lighter rata-rata12,9/min, rolling used/reserved
puncak18, kapasitas transaksi minimum37. Blocker kuota/state0. Clock20slot
Binance Futures dan FX600observasi tetap bucket penyedia sendiri, Lighter0.
Tidak menambah polling. Decision actual guard/source assessment konsisten pada
4798snapshot: pelanggaran0; entry tetap harus menilai ulang.

Pemulihan kegagalan transport/EOF/timeout/cancellation baru dibuktikan offline;
tidak ada disconnect runtime untuk membuktikan rantai recovery tersebut.
Bootstrap, rotasi auth normal, dan transisi coverage merupakan bukti runtime.
Penyebab reconnect lama tetap belum diketahui; error cleanup tidak dipromosikan
menjadi pemicu awal. Tidak ada klaim strategi menguntungkan.

## Bukti, keadaan akhir dan langkah berikutnya

Raw immutable beserta seluruh log tes (termasuk kegagalan fixture/lint awal):
`/home/yuri/Workspace/outputs/lighter-transport-observation-20261006/`.
Market/transport gzip merupakan prefix file engine yang masih terbuka; EOF tanpa
trailer disimpan sebagai batas salinan. Tidak mengisi baris akhir yang hilang.
Record sesudah deadline membuktikan coverage salinan sampai cutoff tetapi tidak
dimasukkan ke statistik window. Tidak memulai observasi baru untuk menunggu gap.

Summary/manifest/verifikasi akun dan hash ada pada
`audits/transport-heartbeat-recovery-20261006/`. RAW_SHA256SUMS dijalankan dari
direktori outputs; ARTIFACT_SHA256SUMS dijalankan dari repo. Analyzer menggunakan
pipeline JSON/gzip Python proyek yang sudah ada, tanpa impor engine/signer atau
akses exchange. Analisis ulang memakai bukti frozen yang sama, bukan rekaman baru.

Pemeriksaan akhir09:43:39,058599WIB dilakukan setelah analisis, bukan rekaman
tambahan. Coverage saat pemeriksaan itu sudah ready (gap maksimum2,966s); waktu
pulih persis antara akhir window dan pemeriksaan ini tidak diinventarisasi dan
tidak ditambahkan ke285,383s statistik. Saldo akhir49,430756USDC; perubahan kas0,
selisih audit0, posisi/order/intent0, IDLE/OFF/closed. SQLite read-only membuktikan
empat fill/empat exact feeNULL dan
intent baru0. Reported fee historis tetap terpisah pada presisi4desimal; tidak
menyulap NULL menjadi0 atau memperluas kebijakan fee.

**Keputusan:** instrumentasi diterima; tidak ditemukan kegagalan transport lokal
konkret yang mengharuskan perubahan heartbeat/backoff/guard. Mesin dapat masuk
tahap **persiapan** satu sesi strategi terbatas setelah izin baru, dengan batas
persiapan dan preflight yang sudah berlaku. Pada akhir window coverage kembali
terblokir, sehingga belum boleh aktivasi saat itu. Tunggu guard aktual/coverage,
audit kas/fee dan bukti preflight5s lolos; jangan memakai285s kesiapan historis
sebagai izin. Tidak mensyaratkan readiness100% atau bebas reconnect selamanya.

Langkah berikutnya yang dipilih: persiapan sesi terbatas berizin baru, tanpa
tuning. Instrumentasi yang kini dimuat akan membedakan pemicu jika koneksi putus;
guard sudah menunjukkan blok clock/gap secara jujur dan kapasitas exit tetap ada.
Jika batas persiapan tidak cukup, tutup izin sesuai runbook dan laporkan blocker.
Tidak ada aktivasi/rearm/sesi baru dalam tugas ini. Kode produksi yang berjalan
1205d35/PID341351; commit penutup hanya analyzer/tes/dokumentasi/evidence,
tanpa perubahan modul produksi yang belum dimuat.
