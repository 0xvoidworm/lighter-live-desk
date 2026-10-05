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

Sumber resmi:
- [Spot market endpoint, Memory dan bobot](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market#symbol-order-book-ticker)
- [Pair metadata dan rateLimits](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#exchange-information)
- [IP limits dan Retry-After](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#limits)
- [BookTicker WS berdasarkan perubahan](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md#individual-symbol-book-ticker-streams)
