# Depth produksi versus pembanding pada peer sama — 7 Oktober 2026

**Kesimpulan:** kontrol peer terpenuhi, tetapi gangguan sumber sekitar5detik
**tidak terulang** dalam satu window900s. Update identik masih mempunyai selisih
receipt ratusan millisecond pada kedua arah. Satu ekstrem produksi berdekatan
dengan lag timer lokal; bukti ini mempersempit pertanyaan, belum menetapkan sebab.
IP sama tidak membuktikan jalur jaringan/backend internal sama. Tidak ada trading
atau perubahan produksi; masalah lama tidak dinyatakan selesai.

## Baseline dan kontrol

Dibaca AGENTS.md, RUNBOOK.md, audit socket terbaru dan comparator sebelumnya.
Repo awal bersih `e0b58f6`; **produksi `776bec4`, PID405247**, hash24 source dan
config cocok dengan bukti pemuatan, lalu diperiksa ulang sebelum dan sesudah
eksperimen. Saldo/collateral49,430756USDC, global flat/order/intent bersih,
kas matched, orderOFF dan sesi closed terverifikasi sebelum upaya koneksi.

Peer diambil dari handshake **transport aktif**, run ID
`6e870e45fd1a4a198f65eb06a9b684a5`, public connection`:3`, FD20/inode1778359.
Pengamatan OS baru sebelum launch cocok unik dengan PID, tuple/family, inode/FD,
dan lifetime. Tujuan aktual **13.112.27.226:443**, AF_INET. Enam pengamatan OS
launch/~5/300/600/890s/final cocok; public tidak reconnect/berganti peer.
Peer public eksperimen lama tetapunknown, tidak dipetakan retrospektif.

Helper commit **`47c0c85`**, Python3.12/aiohttp3.14.3 proyek, comparatorPID411183.
Python diperlukan untuk memakai library/network factory dan validator clock
produksi yang sama; tidak mengimpor engine/account/signer atau membaca key.
Rencana ditetapkan sebelum koneksi di
[DEPTH_SAME_PEER_PLAN_2026-10-07.md](DEPTH_SAME_PEER_PLAN_2026-10-07.md).

Resolver wrapper hanya pada connector pembanding baru, untuk hostname literal
`fstream.binance.com`, lalu dipulihkan saat scope berakhir. Host lain tetap
didelegasikan; tidak ada DNS sistem/produksi, IP produksi, routing, TLS, heartbeat,
autoping, backoff atau guard yang diubah. URL tetap:
`wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms`.
Default network factory IPv4/trust_env false/total10s/connect5s dan heartbeat20
tetap. Sesudah handshake, peer socket+OS, SNI `fstream.binance.com`,
`check_hostname=true`, `verify_mode=CERT_REQUIRED(2)` terverifikasi.
Tidak ada IP alternatif, retry/reconnect pembanding, snapshot REST, kalibrasi
tambahan atau polling akun exchange.

Perbedaan tersisa: koneksi produksi sudah lama hidup, pembanding mempunyai TCP/TLS
dan tuple lokal baru; produksi menjalankan book/strategi/akun/recorder dan callback
HTTP TransportTrace, pembanding hanya raw depth/hook WS/TCP. Dua event loop/stream
TCP independen. Override menghindari resolusi nama untuk handshake pembanding,
tidak membuat jalur/backend atau beban kedua proses identik.

## Satu window dan cleanup

**00:49:49,604914–01:04:49,604914WIB**,900s monotonic sejak sebelum upaya koneksi,
termasuk handshake. Epoch peer cocok **899,710506s** setelah handshake pembanding.
Tidak ada pemisahan epoch tambahan karena public tidak reconnect.
Capture berhenti pada deadline; final cleanup tercatat900,005462s, bukan
perpanjangan data. Koneksi closed dan proses pembanding keluar terverifikasi.

Controller awal mengalami `ModuleNotFoundError: scripts` **sebelum launch/network**;
import path controller diperbaiki dan kegagalan tersebut disimpan. Hanya satu
`Popen` comparator dan satu upaya koneksi publik berlangsung. Tidak ada upaya
koneksi pengganti atau pengulangan pengamatan untuk memperbaiki hasil.

## Pasangan identik dan kesegaran sumber

Raw window: **8.817 produksi /8.820 pembanding**. Dua update produksi sebelum
handshake pembanding dikeluarkan dari kontrol; controlled8815/8820.
**8.814 pasangan identik** berdasarkan U/u/pu,E/T dan SHA256 canonical JSON payload.
Ambiguous key0, mismatch payload0, mismatch `pu`/sequence gap0 pada kedua rawstream.
Unmatched produksi1 sebelum update pertama pembanding; pembanding6 sesudah update
terakhir produksi dalam cutoff. Unmatched internal0. Batas awal/akhir ini tidak
dianggap bukti packet loss; tidak menunggu receipt setelah deadline.

Selisih **produksi minus pembanding**, receipt monotonic pada host sama, millisecond:

| min | p50 | p95 | p99 | max |
|---:|---:|---:|---:|---:|
|-402,002929|0,7114515|52,60706435|110,65092844|328,727110|

Positif berarti produksi lebih lambat; negatif berarti pembanding lebih lambat.
Ini selisih observasi aplikasi, bukan pengukuran pasti latency jaringan/fill.

Replay memakai hanya evidence yang tercatat **pada/sebelum receipt**, dengan
validator produksi, cache/pemilihan interval, TTL120s dan drift asli. Seluruh
riwayat proof tersedia dari prefix transport sebelum window; bukti masa depan
tidak dipakai.15kalibrasi rutin verified,15HTTPattempt/retry0; pembanding menambah0.
Proof expired0 pada receipt yang dinilai.

- Batas atas umur sumber terbesar: **731,602396ms produksi**, **562,980466ms
  pembanding**; lower maksimum592,932760/432,032836ms. Tidak ada umur sumber pasti
  >=1500ms atau episode sumber5s pada controlled updates.
- Produksi: source-fresh1916, FUTURE_UNCERTAIN6899. Pembanding: fresh580,
  FUTURE_UNCERTAIN8240. Pasangan: both-source-valid473, uncertain/guard-rejected8341;
  kategori kedua tidak boleh dilabeli terlambat pasti.
- E−T median1ms, p953ms/p994ms, maksimum7ms. Timestamp sumber dan model clock
  memberi interval umur, tidak memisahkan jaringan, buffering atau scheduling.

## Tahap lokal dan keputusan aktual

| Tahap produksi | p95 ms | p99 ms | max ms |
|---|---:|---:|---:|
| receipt→enqueue |0,238229|0,533773|80,577397|
| enqueue→recorder |0,861171|4,636608|83,837274|
| receipt→recorder |1,040855|4,737842|85,342207|

Enqueue→recorder mencakup antrean/tahap sebelum pencatatan, bukan ukuran murni
seluruh durasi book/strategi. Pembanding dequeue→receipt p990,030860ms,
maksimum1,240499ms. Pulse last-frame produksi berkala tetap bukan timestamp
setiap frame dan tidak menyingkirkan semua buffering sebelum callback.

212event timer-lag produksi >25ms, maksimum **243,236550ms**, backlog pasar
maksimum36 pada event transport. Update `(U=11749944412745,u=11749944424662)`
menunjukkan produksi lebih lambat328,727110ms; callback lag243,236550ms tercatat
3,241ms sesudah receipt produksi, queue26. Receipt→recorder update itu3,389344ms.
Ini korelasi lokal pada satu ekstrem; tahap setelah receipt tidak menjelaskan
seluruh selisih receipt dan tidak menetapkan penyebab awal.

Ekstrem arah sebaliknya402,002929ms mempunyai post-receipt produksi0,272637ms,
tanpa event lag produksi berdekatan. Pada pembanding, library transport→dequeue
update itu0,073698ms, dengan interval umur sumber389,282–517,873ms. Penyebab
sebelum callback tidak terpisah oleh bukti ini. Timer pembanding maksimum4,778ms
memakai sampel0,5s; tidak menyingkirkan stall yang jatuh di antara callback.

**Batas bukti kesiapan:** trace transport tidak menyimpan histori keputusan.
Arsip API **lokal yang hanya membaca status engine tersimpan** baru dimulai pada
detik317,376165 dan berhenti pada deadline900 yang sama. Bagian awal tidak
diisi dengan replay receipt dan tidak dilakukan ekstrapolasi ke seluruh900s.
583snapshot/583keputusan unik tersimpan; `decision_inputs.data_valid` cocok dengan
assessment guard penentunya (mismatch0).449valid dengan basis tersedia,
134ditolak FUTURE_UNCERTAIN. Estimasi hold antar keputusan tercatat (gap<=2,5s):
**448,539944/581,997209s** layak mengevaluasi strategi,133,457265s invalid.
Ini kesiapan evaluasi data/basis yang teramati, **bukan seluruh preflight entry**
atau histori semua tick. Diagnostic-ready445snapshot dihitung terpisah pada
waktu tampilan; tidak menggantikan assessment keputusan tersebut.

Logger pembanding drop0/error0, peak queue5; produksi drop0/error0.
Pembanding23.938.653byte payload,47.107.738byte log tak terkompresi,
max write127,316ms/max queue age55,351ms. Total waktu hook552,220ms.
Writer asynchronous tetap tidak menahan enqueue ketika queue penuh; tidak ada
klaim overhead0 atau kausalitas dari satu maksimum. Maxima status produksi
kumulatif sejak startup dipisahkan dari event lag yang benar-benar dalam window.

## Pengujian, akun akhir dan bukti

**38tes terarah lulus**, dua warning deprecation SDK lama; Ruff bersih,
mypy24source produksi bersih. Uji mencakup scope hostname override, hostname/port
tetap, SNI TLS nyata localhost, penolakan cert tidak trusted/hostname salah,
peer/TLS mismatch berhenti sebelum receive, deadline termasuk handshake,
cancellation/cleanup, resolver restored, serta epoch peer berubah/unknown yang
tidak mewarisi bukti lama. Tidak ada signing/transaksi/exchange dalam tes baru.
Source produksi tidak berubah, sehingga suite/build produksi tidak diulang.

Pemeriksaan akhir: **PID405247, IDLE,49,430756USDC**, collateral sama, posisi
global/order/pending/intent nol, pending activation0, jurnal sehat, kas/riwayat
matched lengkap, selisih/perubahan kas **0,000000USDC**, orderOFF,
entries_stopped, loaded permissionnull, sesi closed. Counter tetapintents6,
fills4, commands21, activation_requests1. Empat fee exact tetapNULL;
reported/evidence/presisi tetap terpisah, tidak diklaim exact nol. Tidak ada order
atau sinyal yang dieksekusi; threshold8bps, buffer5bps, risiko dan guard tetap.

Raw, alamat lokal, reproducible analysis/controller dan prefix terikat hash di
`~/Workspace/outputs/lighter-depth-same-peer-20261007`.
**44file** diverifikasi; prefix gzip recorder produksi aktif dapat tidak mempunyai
trailer, hanya baris lengkap dipakai dengan cutoff monotonic asli.
[Summary aman](audits/depth-same-peer-20261007/summary.json),
[manifest](audits/depth-same-peer-20261007/manifest.json).
SHA256 manifest: `ccbad9c8920eecdc5e1b0e187a81cc515029464f09c22e2063cd8ff302bff373`.

**Satu langkah berikutnya:** profiling pasif dan berbatas pada event loop produksi
ketika timer-lag muncul, terikat waktu/connection/update ID, untuk mengidentifikasi
callback sinkron yang menghabiskan waktu sebelum mengusulkan patch. Lag lokal
terukur dan berkorelasi dengan satu ekstrem; ini lebih didukung bukti daripada
mengubah DNS/reconnect/guard atau mengulang eksperimen yang sama. Aktivitas itu
belum dijalankan dalam tugas ini. Tidak mengatribusikan ISP/host/exchange sebagai
akar masalah, tidak menyatakan strategi menguntungkan, dan tidak membuka trading.
