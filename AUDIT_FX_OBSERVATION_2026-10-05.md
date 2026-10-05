# Observasi kurs Binance Spot — 5 Oktober 2026

Lingkup dari9028794/0f380e4: diagnosis/fix dan satu verifikasi tanpa order
maksimal1200s. OrderOFF, sessionclosed, tidak ada rearm/START_LIVE/probe,
deposit, dummy order atau pengiriman transaksi. Split state/history,
threshold8bps, buffer5bps, seluruh risk/deadline, fee exactNULL tetap.

## Sumber dan bukti lama

Sumber aktual **REST**, bukan WebSocket:
`https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=USDCUSDT`.
Best bid/ask dan quantity, data source Memory. Orientasi USDT perUSDC;
metadata resmi yang dibaca sekali pada09:23:40.572UTC membuktikan
baseUSDC/quoteUSDT/statusTRADING. Bobot request diagnostik itu20,
REQUEST_WEIGHT6000/min dan RAW_REQUESTS300000/5min, header IP weight28.
Itu pembacaan diagnosis, terpisah dari verifikasi runtime berikutnya.

Kode lama tidak memfilter harga sama: semua respons valid disimpan, direct
FX task tanpa antrean market.113observasi sukses pada window sebelumnya,
89/112pasang berurutan mempunyai bid/ask sama. REST snapshot ini diambil
ketika diminta, bukan push perubahan harga. BookTicker **WebSocket** resmi
memang event perubahan best price/qty; channel itu tidak dipakai engine.

Gap receipt25,599673s:07:37:04.580408→07:37:30.180081UTC. Pada gap itu
FX menjadiNone. Dalam kode lama setelah bootstrap, hanya exception branch
yang menghapus FX; branch mencakup HTTP, parse/validation dan recording.
Jenis exception/fase tidak disimpan, jadi penyebab eksternal spesifik
belum dapat dibuktikan. Pola tersebut kompatibel dengan request gagal yang
menghabiskan timeout5s ditambah tidur10s setelah setiap request. Tidak ada
dasar menyatakan gap adalah harga tidak berubah, filtering, ISP atau DNS.

HTTPX default keepalive_expiry5s juga lebih pendek dari sleep10s lama;
konfigurasi itu membuat connection pool harus membuka koneksi baru pada
request berikutnya. Cadence2s berada di bawah expiry itu. Tes memakai
HTTP/TCP lokal nyata dengan waktu diskalakan membuktikan reuse vs expiry
dan trace headers (bukan hanya MockTransport). Ini bukti perilaku pool,
bukan pembuktian bahwa DNS/TLS menyebabkan exception25,6s sebelumnya.

## Perbaikan terbatas

- Tetap endpoint bid/ask resmi, tanpa mengganti normalisasi dengan last
  trade/average/USDT=USDC. Bootstrap exchangeInfo membuktikan pair/status/
  rateLimits (20weight, maksimal3percobaan dengan interval60s).
- Fixed start2s, deadline total2s mencakup request/body/parsing/event-loop
  delay. Missed deadline dilewati, tanpa catch-up burst. Snapshot gagal
  tetap menghapus kurs/invalidate proof entry; tidak menghapus history akun
  atau menahan risk worker. Dua kegagalan beruntun tetap dapat menahan
  coverage; guard5s dan freshness15s tidak dilonggarkan.
- Respons baru berharga/qty sama adalah observasi Memory baru yang sah.
  Waktu request, receipt, validation dan perubahan harga disimpan terpisah.
  Freshness memakai **awal request** secara konservatif; processing terlambat
  tidak membuat data lama baru. Exchange event time/update ID tetapNULL,
  karena endpoint tidak menyediakannya. Cached response Age>0 ditolak.
- Instrumentasi semua request/outcome/status/phase/RTT/weight, failures
  tidak lagi hilang. Httpcore connect_tcp menggabungkan DNS/TCP; tidak
  diklaim dapat memisahkan keduanya. TLS/headers/body dicatat jika tersedia.
  Tidak menyimpan headers sensitif, exception text atau body mentah.
- Bucket **Binance Spot** sendiri100weight/rolling60s; ticker2weight,
  sekitar30GET/menit=60weight, sekitar+24GET/menit dibanding cadence lama.
  Bootstrap20 memberi bound82weight pada minute pertama. Official limits,
  observed shared IP weight, 418/429 dan Retry-After juga menghalangi call.
  Started/in-flight/failure tidak di-refund. Lighter40/47/55 dan reserve15
  tidak dibebani request Binance. Klien IP lain hanya terlihat via header,
  bukan diklaim terhitung penuh oleh counter lokal.

Signal tetap `Binance_mid / FX_mid`, lalu residual basis lama, tanpa
mengubah threshold. Pembanding executable dipisahkan dalam diagnostics:
sellBTC→USDT proceeds→buyUSDC pada FXask, sehingga `BTC_bid_USDT / FXask`;
buyBTC→USDT cost→sellUSDC pada FXbid, sehingga `BTC_ask_USDT / FXbid`.
Mid adalah midpoint bid/ask, bukan last trade. Tidak ada smoothing, backfill
gap, atau pemalsuan timestamp untuk menghindari lonjakan residual.

## Pengujian dan runtime

Regresi terarah mencakup same-price/same-size, size-only change, response
hilang, delayed parser/body, snapshot invalid/cached, koneksi HTTP baru
setelah failure, arah bid/ask dan residual identik, expiry tanpa pesan,
fixed deadlines, bucket rolling/provider IP, metadata dan backoff429.
Seluruh suite mencakup signer asli tanpa broadcast dan priority exit lama.
Hasil runtime dari satu window ditambahkan setelah rekaman selesai.

Implementasi dibekukan di283ab14,359tes/mypy19source/Ruff-format39file
lulus sebelum restart. Dua regresi native HTTP ditambahkan selama rekaman
hanya pada file tests; source engine dan parameter rekaman tidak diubah.

## Satu verifikasi20menit, selesai

16:39:52.980645→16:59:52.981303WIB, tepat1200s monotonic, kelebihan
penutupan file0,000687s. PID239076 menjalankan283ab14;19module source dan
konfigurasi tidak berubah.4798snapshot lokal, error0, resolusi maksimum
0,261946s. Bootstrap sekitar28s sebelum marker; sisa warm-up572,278s
tetap termasuk1200s. Tidak ada perpanjangan, window kedua atau order.

| Ukuran | Sebelum (9028794) | Sesudah283ab14 |
|---|---:|---:|
| Gap receipt FX maksimum | 25,600s | 2,344s |
| Blocker FX/kurs | 10,755s | 0s |
| Coverage block, termasuk warm-up | 717,970s | 572,278s |
| Semua guard teknis evaluasi lolos | 465,475s | 620,437s (10m20s) |
| Interval lolos terpanjang | 120,055s | 120,056s |
| Lighter GET/min | 13,20 | 12,90 |
| Lighter peak rolling60s | 24 | 18 |
| Kapasitas menuju transaksi55, termasuk lease | ≥29 | ≥37 |

Median/P95/maksimum umur observasi FX **0,998/1,921/2,325s**. Freshness
konservatif dari awal request **1,100/2,019/2,417s**, tetap jauh di bawah
15s. Signal formula/threshold tidak diubah (hash strategy identik dengan
baseline); kode tetap memakai midpoint bid/ask yang sama. Kesempatan
evaluasi bertambah154,962s pada window ini. Ini bukan eksperimen pasar
berpasangan: startup/pasar berbeda, baseline juga mencakup satu rehearsal
preflight. Tidak mengklaim perubahan itu seluruhnya estimasi kausal fix.

600GET ticker dimulai dalam window =1200weight/20min,30GET/min atau
60weight/min. Rolling request peak31 (jitter boundary), peakweight80
termasuk bootstrap20 dan carry-in sebelum marker, di bawah local100.
Header agregat IP maksimum yang terekam pada request window60, dibanding
limit6000/min. Metadata startup20 dan satu metadata diagnosis20 terpisah
di luar window. Stats tidak menambah polling: semua600GET adalah observasi
yang dikonsumsi engine. Cadence lama sekitar6GET/min; tambahan sekitar
24GET/48weight permenit. Bucket Lighter tidak berubah/dipakai untuk FX.

Status capture membuktikan599response verified,1in-flight pada sampel
terakhir. **Recorder independen membuktikan request616 juga terverifikasi
dalam window asli**: start1791194392,6816485; receipt
1791194392779872332ns (16:59:52.779872WIB), recorded
1791194392779947477ns, sebelum deadline16:59:52.980645. Identitas dicocokkan
via timestamp start unik dan PID239076. Jadi600observasi sukses dapat
dibuktikan, tanpa mengasumsikan sukses dari request start. Exact HTTP
status/phase akhir request616 tidak ada dalam status sample; bukti tail
dilabeli verified observation dan tetap dipisahkan dari trace599response.

Tidak ada failure/timeout/429 teramati. RTT599response yang lengkap
median95,174/P95 127,048/max454,004ms; ini **bukan latensi fill**. Satu
TCP connection sejak bootstrap,0reconnect TCP dalam window. Recorder
menulis600FX event; status menangkap599observasi,563pasang berurutan
berharga sama,562di antaranya ukuran berubah. Tidak ada filter berdasarkan
harga/mid/update ID. Bid/ask source time tetapNULL sesuai schema REST.

Lighter258GET =10audit8GET+89state2GET; seluruhnya sukses. Umur account
maks12,716s, orders12,824s; snapshot account independen dan kas tetap cocok.
Reconciliation-idle11,505s, dengan7,253s overlap di luar warm-up. Blocker
quota0, FX0, stale account0, market-data0 pada sampel. Total kombinasi
blocker1200s:568,026s warm-up saja,4,252s warm-up+audit,7,253s audit saja,
620,437s siap,0,033s belum teramati pertama. Jangan menjumlahkan blocker
individual. Reconnect Binance/Lighter, overflow dan error Recorder0;
coverage pasca-warm-up tidak kehilangan kontinuitas, tidak ada backfill.

Suite akhir **361tes lulus**,3warning dependency,17,22s; mypy19source dan
Ruff/format lulus. Native HTTP menggunakan localhost, native signer suite
tetap tanpa broadcast. Analyzer offline dijalankan terhadap window nyata
dan kelengkapan request dibandingkan dengan Recorder serta event ledger.
Tidak mengubah dependency/toolchain Python3.12 proyek/SDK yang sudah ada.

## Keadaan akhir dan batas bukti

Pemeriksaan17:06:59.237WIB: account751745/key4, Standard/Unified/Cross,
equity/available/collateral **49,430756USDC**, posisi/global active/pending
order dan unresolved intent0. Umur account4,189s, orders4,304s; history
lengkap, kas matched/selisih0, cash-change0, intent/fill baru0. Empat fee
exact tetapNULL, reported0,0000USDC/4desimal dari ekspor resmi tetap cocok.
MesinIDLE, entries stopped, orderOFF, send authorizationfalse, kedua flag
pengecualian fee false, sessionclosed, autostartdisabled. Tidak ada
transaksi baru atau pengujian stop/order mainnet.

Gap25,6s sebelumnya bukan bukti channel hanya berubah ketika harga berubah.
Yang terbukti: exception branch menghapus kurs, cadence setelah request
menambah downtime dan melewati keepalive expiry. Fase/jenis exception lama
tetapunknown. Fix mengurangi downtime terprogram, membuka kesempatan
evaluasi lebih banyak, dan memberi bukti yang cukup untuk membedakan
received/rejected/timeout pada kegagalan berikutnya. Tidak menjamin jaringan
selalu sehat; gap riil berikutnya tetap memblokir guard. Sampel20menit ini
tidak membuktikan profitabilitas atau eksekusi order.

Bukti aman: `audits/fx-observation-runtime-20261005.json` dan
`audits/fx-observation-final-account-20261005.json`. Manifest menyimpan
SHA256 rekaman `34f89fb74cb368d40720fb144cd2fae8149550f5dc33bbff0d4b7a33af316199`.
Raw private di `/home/yuri/Workspace/outputs/lighter-fx-observation-20261005/recording`,
tidak di-commit. Reproduksi offline, tanpa API/order:

```sh
.venv/bin/python scripts/analyze_fx_observation.py \
  /home/yuri/Workspace/outputs/lighter-fx-observation-20261005/recording \
  audits/fx-observation-runtime-20261005.json \
  --market-directory state/market \
  --baseline audits/split-account-history-runtime-20261005.json
```

Sumber resmi:
- [Spot market endpoint, Memory dan bobot](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market#symbol-order-book-ticker)
- [Pair metadata dan rateLimits](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#exchange-information)
- [IP limits dan Retry-After](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#limits)
- [BookTicker WS berdasarkan perubahan](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md#individual-symbol-book-ticker-streams)
- [HTTPX keepalive expiry default5s](https://www.python-httpx.org/advanced/resource-limits/)
