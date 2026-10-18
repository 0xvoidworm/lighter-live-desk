# Audit clock, bukti keputusan, dan reconnect sesudah observasi

6 Oktober 2026 WIB. Pekerjaan offline/read-only; tidak ada pengiriman, probe,
permission load/rearm, START_LIVE, restart, atau rekaman runtime baru. Proses
301678 dipertahankan. Worktree awal bersih pada 342eeaa, kode yang berjalan
818b508. Instruksi AGENTS.md, RUNBOOK.md dan audit depth-only dibaca.
Perbaikan yang **belum dimuat**: 31813c8. Tidak ada perubahan konfigurasi/guard.

## Batas bukti dan reproduksi

Window asli: 01:19:34.358374–01:39:34.358736 WIB, 1.200 detik, termasuk
warm-up. Gunakan 4.798 snapshot asli, bukan rekaman ulang. Angka kesiapan
reviewer tetap: 513,404538 detik; 84,206764% sesudah warm-up;
interval siap terpanjang 14,255635 detik. Window itu tidak mempunyai reconnect
atau sequence gap; diagnosis sesudahnya tidak mengubah hasil window tersebut.

Bagian pascawindow dibatasi tetap sampai **08:06:04 WIB** (epoch1791248764),
memakai salinan log pasar run574ae81f79294ba2b12b7562d5dfa135 dan journal yang
sudah ditulis engine. Tidak menambahkan polling/klien/socket. Counter 3 public,
2 market, 3 Lighter cocok dengan delapan disconnect di batas tersebut.
Counter saat pemeriksaan akhir lebih tinggi; itu dicatat terpisah, bukan
dimasukkan secara diam-diam ke analisis historis ini.

Raw tetap di `~/Workspace/outputs/lighter-clock-reconnect-analysis-20261006/`:
salinan market, journal engine terfilter, ekspor diagnostik akun yang sudah
persisten, analisis penuh, dan log tes termasuk kegagalan awal/baseline.
Raw20menit tetap di `lighter-depth-production-20261006`. Gzip jam terakhir
merupakan prefix anggota yang masih terbuka: EOF tanpa trailer diperlakukan
sebagai batas salinan, tanpa menyintesis baris. Salinan memuat record sesudah
cutoff; hanya waktu <=cutoff dianalisis. Hash ada di
`audits/clock-decision-reconnect-20261006/RAW_SHA256SUMS`.
Summary dan verifikasi akun disimpan pada direktori audit yang sama.
Pemeriksaan `sha256sum -c` dijalankan dari direktori outputs tersebut; semua
hash cocok. ARTIFACT_SHA256SUMS untuk artefak Git dijalankan dari root repo.

Reproduksi offline (Python mengikuti pipeline proyek dan mengimpor guard
produksi; tidak membuat implementasi clock kedua dalam bahasa lain):

```sh
.venv/bin/python scripts/analyze_control_feed_observation.py \
  --source ~/Workspace/outputs/lighter-depth-production-20261006 \
  --market ~/Workspace/outputs/lighter-clock-reconnect-analysis-20261006/market \
  --clock-reconnect-cutoff 1791248764 \
  --journal ~/Workspace/outputs/lighter-clock-reconnect-analysis-20261006/engine-journal.jsonl \
  --output /tmp/lighter-clock-replay.json
```

`persistent_diagnostics()` mengekspor baris SQLite dengan koneksi mode=ro,
cutoff yang sama dan allowlist field; tidak membuka transport trading. DB/key
tidak disalin ke audit. Ekspor5MB tetap di outputs, summary saja di Git.

## Mengapa source guard dan interval siap terputus

`T` adalah transaction time Unix millisecond Binance; `E` event time terpisah.
Timestamp sumber tidak diganti receipt. Model guard yang sudah ada:

- indikator antarjam = receipt_wall_ms−T + (eval_mono−receipt_mono)/1e6;
- drift = umur proof ×100ppm; low/high offset diperlebar dengan drift;
- umur sumber = indikator + offset interval; low >=−1ms, high <1500ms;
- half-width <=250ms, TTL120s, wall/monotonic discrepancy <=25ms.

Proof mempunyai anchor wall/monotonic, offset dasar dan serverTime; replay hanya
memilih record proof yang telah tersedia sebelum assessment, tidak memakai
kalibrasi yang datang kemudian. Wall evaluasi lama direkonstruksi dalam bracket
timestamp keputusan; waktu persis panggilan guard pertama tidak direkam.
**4.798/4.798 alasan assessment kedua cocok** dengan guard produksi dalam replay:
4.124 fresh dan674 FUTURE_UNCERTAIN. Ini bukan bukti bahwa panggilan pertama
selalu memberi hasil identik.

Seluruh penolakan sumber diagnostik dalam window adalah FUTURE_UNCERTAIN,
bukan SOURCE_DATA_LATE, missing timestamp, definite future, expired proof,
wall step atau half-width >250ms. Setelah warm-up, low pada snapshot yang
ditolak berada antara −49,252644 dan−1,024455ms: rentang masih mencakup umur
yang lebih future daripada toleransi, sedangkan upper positif. Guard tidak
membuktikan bahwa pesan memang berasal dari masa depan atau jaringan terlambat.
Upper maksimum seluruh window1087,759ms, receipt maksimum525,066ms,
half-width maksimum152,196ms. Semua masih di bawah batas masing-masing.

Satu dari20 pembacaan clock mempunyai HTTP200 tetapi RTT1556,944ms >400ms.
Proof itu tidak diterima; proof sebelumnya dipertahankan dengan umur aslinya,
tidak disegarkan. Umur proof maksimum snapshot119,971906s; allowance drift
maksimum pada replay keputusan11,996058ms. Jadi proof gagal itu memang
mengurangi margin, tetapi tidak ada expiry yang tercatat dalam window.

Harga baru dengan T lebih dekat ke evaluasi dapat kembali menyentuh batas future
uncertainty. Ini sah menurut model konservatif, tanpa menunda evaluator.
Interval siap pendek juga mencerminkan waktu yang berbeda antara evaluasi
strategi dan diagnostic observation:

| Konteks basis_scale sesudah warm-up | Detik |
| --- | ---: |
| Keputusan invalid, source kedua uncertain; diagnostik juga uncertain | 42,519108 |
| Keputusan invalid, source kedua uncertain; diagnostik berikutnya sudah fresh | 47,268816 |
| Keputusan invalid, source kedua dan diagnostik fresh; satu kasus ambigu pascawarm-up | 0,250082 |
| Total blocker basis_scale | 90,038006 |

Semua blocker basis_scale pascawarm-up mempunyai decision_inputs.data_valid=false.
`strategy.evaluate()` mengembalikan basis/scale=None pada input invalid. Ini
blocker turunan, bukan bukti median/MAD gagal dihitung pada input valid.
Basis None saat warm-up belum selesai juga normal, dibedakan dari kegagalan ini.
Ada378 snapshot di seluruh window dengan keputusan invalid tetapi assessment
diagnostik sumber sudah lolos; jarak evaluasinya median10,399ms,
p9515,709ms, maksimum22,102ms. Data yang baru lolos tidak meluluskan evaluasi lama.

Pada transisi siap→terblokir yang terlihat di snapshot250ms setelah warm-up:
59 kali gabungan source/market/basis,106 kali basis saja,4 kali rekonsiliasi.
Ini transisi tersampel, bukan jumlah semua keputusan engine. Durasi overlap
tetap mengikuti audit asli; source42,519108s, basis90,038006s dan
reconciliation_idle7,002922s tidak boleh dijumlahkan sebagai downtime unik.

## Dua catatan keputusan dan perbaikan

Snapshot elapsed373,455187s dan1118,286367s mencatat valid=false tetapi
assessment sumber kedua passed=true. Proof masing-masing:
`1791224723736:110406031864672` dan `1791225443725:111126064469898`.
Low assessment kedua −0,992101ms dan−0,995291ms: hanya7,899us dan4,709us
melewati batas−1ms. Assessment kedua tercatat1,347us dan0,451us sesudah
timestamp keputusan; assessment diagnostik berikutnya11,880ms dan6,078ms
lebih akhir. Kedua book masih muda pada timestamp keputusan (maksimum42,431ms),
FX age1,370s/0,128s, antrean0. Ini mempersempit kemungkinan, tetapi waktu dan
hasil guard pertama tetap hilang. Crossing boundary masuk akal, belum terbukti;
valid=false juga bisa berasal dari receipt/transport guard. Tidak ada keputusan
historis yang diubah menjadi lolos.

Fix kecil: `data_valid()` selalu menghitung guard baru dan menghasilkan record
output-only dengan waktu mulai/akhir wall dan monotonic, hasil receipt/transport,
source_checked, assessment sumber sebenarnya dan passed. `decide()` menyimpan
record yang dipakai itu, tanpa source assessment kedua sebagai alasan keputusan.
Jika guard receipt/transport gagal, source=None dan source_checked=false.
Assessment mencatat eval wall/mono, proof ID, anchor, offset dasar, drift dan TTL.
Output diganti pada setiap pemeriksaan, tidak dimutasi lewat pemeriksaan berikutnya.

Preflight/dispatch/before-sign tetap memanggil data_valid() baru; tidak membaca
record lama untuk izin. Diagnostic observation tetap menilai snapshotnya sendiri.
Stop, deadline dan close tetap terpisah; tidak ada perubahan timer, sumber quote,
delta U/u/pu, threshold8bps, buffer5bps, future/clock tolerance, TTL, coverage,
backoff, risiko ataupun fee policy.

## Reconnect sesudah window

Waktu WIB. Tabel gap adalah jarak **receipt data channel yang dicatat**, bukan
durasi packet loss terukur. Untuk Lighter dipakai order_book, bukan semua frame
ticker/trade/ping. Untuk market dipakai aggTrade, yang dapat mempunyai keheningan
alami. Tidak ada wire timestamp ping/pong atau ID koneksi persisten pascawindow.

| Disconnect | Kanal | Pemicu yang tercatat | Backoff | Gap receipt terkait |
| --- | --- | --- | ---: | ---: |
| 07:27:58,009 | Lighter | keepalive ping timeout, lalu closing timeout | 2s | 110,484s |
| 07:28:37,763 | public | No PONG setelah10s | 2s | 70,406s |
| 07:29:05,957 | Lighter | keepalive ping timeout, lalu closing timeout | 4s | sama110,484s |
| 07:29:11,764 | public | No PONG setelah10s | 4s | sama70,406s |
| 07:29:16,763 | market | No PONG setelah10s | 2s | 33,878s |
| 07:48:14,179 | Lighter | keepalive ping timeout, lalu closing timeout | 2s | 28,699s |
| 07:48:20,764 | market | No PONG setelah10s | 2s | 34,797s |
| 07:48:23,192 | public | reader EOF, lalu gagal menulis close frame | 2s | 2,479s |

Gap yang sama melintasi dua upaya recovery pertama, bukan dua gap yang dijumlahkan.
Public terakhir bukan bukti PONG timeout: traceback pertama `aiohttp.streams.EofStream`,
kemudian `ClientConnectionResetError: Cannot write to closing transport` di close().
Error penulisan adalah tambahan saat cleanup; pemicu transport EOF lebih dahulu.
Di market, aiohttp heartbeat sendiri menutup response ketika PONG tidak tiba;
kode1006 tidak menjelaskan sebab PONG hilang. Lighter mengirim1011 setelah
keepalive timeout; timeout close berikutnya bukan penyebab awal. Library terpasang
aiohttp3.14.3/websockets16.1.1 cocok dengan stacktrace/source lokal tersebut.
Tidak ada bukti explicit engine cancellation yang memulai delapan reconnect ini.

Korelasi REST yang persisten memberi bukti lebih kuat daripada counter:
15 rekonsiliasi error,7 pada07:27–07:29 dan8 pada07:47–07:48; timeout pertama
07:27:28,730 dan07:47:19,303. Deadline8s selesai dalam8001,153–8022,436ms;
lock wait maksimum0,014057ms; request used60s9–33. Bukan antrian lock selama8s
atau kehabisan cap40 yang menjelaskan error tersebut. Sepanjang pascawindow
ada26 state-refresh timeout juga. Request gagal berakhir di waiting_headers25,
connect_or_tls15 dan dns1. `CancelledError` per-request adalah pembatalan ketika
deadline operasi habis, bukan alasan menyimpulkan aplikasi sengaja memutus socket.
Connect/TLS belum bisa dipisahkan pada trace ini.

Receipt→recorder maksimum124,359ms, p951,705ms pada735.426 observasi
depth/book/aggTrade/FX pascawindow. Tidak terlihat antrean pemrosesan beberapa
detik setelah receipt yang menjelaskan gap panjang. Data masih tiba di kanal
lain selama sebagian gap; contoh public hanya berumur0,522s ketika Lighter
timeout07:48:14, dan0,075s ketika market timeout07:48:20.
Ini tidak mengukur delay sebelum parsing/receipt. Event-loop lag historis kontinu
pascawindow tidak tersedia; deadline8s yang tepat waktu mempersempit hipotesis
stall panjang, tanpa meniadakan stall pendek atau starvation timer tertentu.

Pada percobaan clock07:28:23 yang tidak terverifikasi, umur proof lama sudah
120,919s pada respons; TTL120s tetap membatalkan penggunaannya. Ini berbeda
dari window asli yang belum menunjukkan expiry. Tanpa status kontinu di saat
itu, durasi penolakan entry akibat proof expired tidak dapat dihitung.

Pasangan waktu recorder tidak menunjukkan wall-minus-monotonic step >25ms
dalam interval ini. Query unit suspend/sleep, NetworkManager dan chronyd tidak
menghasilkan event; logind yang terlihat hanya aktivitas session kemudian.
Ketiadaan log bukan pembuktian tidak terjadi gangguan jaringan atau suspend.
Gangguan beberapa sumber dan REST secara bersamaan cocok dengan beberapa
hipotesis jalur/host/upstream; **belum ada bukti untuk memilih ISP, exchange,
suspend atau bug event loop sebagai akar masalah**.

## Pemulihan yang benar-benar terbukti

Setelah public07:28:37 ada snapshot10,635s kemudian, tetapi tidak ada bridge
valid sebelum public gagal lagi07:29:11. Snapshot pertama saja bukan recovery
lengkap. Setelah retry kedua4s: receipt depth4,548s, snapshot tercatat4,661s,
bridge valid4,672s setelah disconnect. Gap70,406s terdiri65,859s sebelum
disconnect terakhir dan4,548s sampai receipt baru; handshake, snapshot REST
dan waktu tanpa data belum bisa dipisahkan dari trace yang tidak persisten.

Sesudah public07:48:23: depth receipt2,472s, snapshot2,473s, bridge valid2,482s;
sebagian besar recovery itu konsisten dengan backoff2s, tanpa klaim RTT handshake
yang tidak direkam. Replay deterministik semua snapshot/delta dalam salinan
memberi **0 sequence error**; tabel events runtime juga **0 BOOK_GAP** sampai
cutoff. Ini bukti raw runtime yang baru dibaca plus hasil replay, melengkapi
tes recovery offline audit sebelumnya. Tidak membuktikan bahwa setiap guard
akun/FX/source/coverage sudah lolos tepat saat bridge kembali.

Coverage tidak dihapus oleh reconnect; code menjaga gap dan timestamp asli.
Tidak ada status kontinu sesudah01:39:34 untuk menentukan persis waktu mulai
coverage lolos setelah setiap gangguan. Snapshot08:06 menunjukkan coverage
ready lagi, tanpa menjadikannya bukti bahwa periode gap sebelumnya valid.

## Pengujian, runtime dan keputusan berikutnya

160 tes terarah lulus (source, diagnostic, analysis, failure paths, probe-only).
Seluruh suite **418 lulus**,3 warning dependency deprecation yang sudah ada;
Ruff dan mypy20module lulus. Boundary baru memakai Engine.data_valid dan
market_data_valid produksi, clock.assess produksi, serta fixture transport
palsu: uncertain→fresh dan fresh→uncertain, record tidak berubah saat check baru,
market guard gagal/source tidak diperiksa. Dua contoh rekaman direplay; model
10us sebelumnya uncertain, tanpa mengklaim itu waktu keputusan aslinya.
Tes lainnya menjaga delta/sampling dan independensi stop/timer/close.

Regresi boundary yang sama terhadap archive src818b508 gagal pada kedua arah
(dua assessment, bukan satu). Kegagalan fixture awal terjadi karena IDLE
melakukan pemeriksaan operasional tersendiri; fixture diisolasi HALTED agar
hanya pencatatan keputusan yang diuji. Tidak melonggarkan guard produksi.
Tes analyzer menolak proof yang baru tercatat setelah waktu evaluasi, dan ekspor
diagnostik mengecualikan field secret palsu. Semua memakai transport palsu/offline.

Verifikasi akhir **08:31:27,814 WIB**, menggunakan status lokal dari pembacaan
exchange rutin engine: akun751745 Standard/Unified (type0/trading1/margin0),
saldo/collateral49,430756USDC; posisi0, order aktif/pending0, unresolved intent0.
Umur account8,976s/order9,158s; history complete, umur41,535s; net cash matched,
selisih0,000000USDC. Empat fee exact tetapNULL, bukti reported resmi terpisah
tetap berlaku; tidak ditafsirkan sebagai nol. IDLE, entry stopped, orderOFF,
sesi closed, tidak ada otorisasi baru. Budget16 used/0 reserved,39 slot hingga
cap55; tidak ada request exchange tambahan untuk verifikasi ini.

PID301678, NRestarts0, mulai01:19:21 WIB, tetap memakai818b508. Source guard dan
diagnostic one-assessment sebelumnya dimuat; decision guard metadata31813c8
belum dimuat (`data_guard` tidak ada pada runtime). Counter akhir public6,
market3, Lighter4 termasuk kejadian **sesudah cutoff**. Tidak ada klaim semua
13 kejadian itu sudah didiagnosis atau reconnect telah selesai.

**Satu langkah berikutnya yang paling didukung bukti:** instrumentasi pasif
transport bersama sebelum sesi live: persist per-connection ID, ping-send/
pong-receive/deadline, initial EOF/close-owner, handshake start/end, dan loop-lag
monotonic pada kejadian gangguan. Ini menutup lubang yang sekarang mencegah
pembedaan kehilangan transport versus timer lokal; sekaligus muat logging
keputusan yang sudah diperbaiki melalui rollout yang diotorisasi terpisah.
Tidak mengubah guard, menguji jaringan, atau mengaktifkan sesi dalam tugas ini.
Clock guard tetap fail closed; belum ada alasan mengubah toleransi atau menunda
evaluator. Kesiapan sesaat/sampel ini tidak membuktikan profitabilitas.
