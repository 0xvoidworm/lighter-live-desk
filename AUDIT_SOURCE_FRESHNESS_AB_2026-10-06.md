# Audit source freshness dan A/B public Binance — 6 Oktober 2026 WIB

Lingkup: implementasi entry/sampling guard, satu A/B read-only maksimum900detik.
Tidak ada transaksi, START_LIVE, LOAD_SESSION_PERMISSION, rearm, atau perubahan
subscription engine. Baseline repo1e38447. Audit reconnect sebelumnya dibaca.

## Kriteria ditetapkan sebelum pengukuran

- Satu marker create_new; durasi900s sejak mulai termasuk DNS/startup/backoff.
- Dua child process binary/library/config yang sama; hanya subscription berbeda:
  depth@100ms versus bookTicker+depth@100ms. IP IPv4 yang sama dipilih sekali
  dari DNS A fstream.binance.com resmi via DoH; tidak failover selama uji.
- TCP5s, TLS/WS10s, heartbeat20s/pong10s, backoff2/4/8/16/30s sama.
  Native CA, TLS/SNI/hostname verification fstream.binance.com tetap aktif.
- Endpoint engine aktif tetap kombinasi sebelumnya. Tidak mengubah DNS sistem,
  jam, TLS, VPN, router, jaringan, backoff, strategi, ataupun guard lama.
- Bandingkan depth yang sama: jumlah/jenis disconnect, usia T dan E antarjam,
  gap pesan/sequence, volume payload dan log, JSON parse/field processing,
  serialize/write/flush cost. Beban pencatatan dilaporkan, bukan dianggap gratis.
- Tidak ada REST snapshot pada klien A/B: ini bukti transport, bukan bukti
  pemulihan atau correctness local book production. Telemetry akun hanya hasil
  engine yang sudah berjalan; observerlocalhost1Hz tidak menambah polling akun.
- Perbedaan hasil satu pasangan koneksi hanya mempersempit diagnosis, bukan
  bukti sebab mutlak. Kegagalan dipertahankan, tidak diulang agar terlihat baik.

## Bukti timestamp dan clock

Model resmi Binance USD-M connector mendefinisikan T transaction time dan E event
time secara terpisah untuk depth dan bookTicker:
[depth schema](https://raw.githubusercontent.com/binance/binance-connector-python/master/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/websocket_streams/models/diff_book_depth_streams_response.py),
[bookTicker schema](https://raw.githubusercontent.com/binance/binance-connector-python/master/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/websocket_streams/models/individual_symbol_book_ticker_streams_response.py).
[General Info USD-M](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info)
dan respons asli memakai Unix milliseconds, bukan seconds/microseconds.
[REST time](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
weight1/serverTime; exchangeInfo.serverTime tidak digunakan.
Beberapa URL WS lama kini redirect homepage; tidak dipakai sebagai bukti isi docs.

Host proof mengikuti bound
abs(system correction)+root dispersion+root delay/2 dari
[chrony tracking](https://chrony-project.org/doc/4.8/chronyc.html).
Pembacaan tidak mengubah jam. Bound/TTL/alasan teknis ada di RUNBOOK.
REST offset interval menghindari asumsi RTT simetris dan offset0. Domain clock
REST/WS USD-M merupakan asumsi eksplisit, tidak ada jaminan sinkronisasi seluruh
backend dari eksperimen ini. Source age tidak disebut latensi jaringan pasti.

## Regresi reviewer dan implementasi

Fixture observed_at1791213413.4622974: receipt age732.022716ms,
T1791213408998, receipt_ns1791213412743176181, mono99094982111527.
Raw wall-minus-T sekitar4464.2974ms; projected receipt+mono age4477.199ms.
Selisih~12.9ms berasal dari waktu pengukuran/antarclock, tidak menjadi estimasi
latensi pasti. Bukti clock historis tidak tersedia: replay tanpa bukti ditolak
SOURCE_CLOCK_NOT_VERIFIED. Replay dengan interval model[-100,+100]ms ditolak
SOURCE_DATA_LATE. Model replay tidak disebut bukti clock historis/runtime.

Guard tambahan entry/sampling tidak membuang delta. MissingT tidak digantiE/now.
Exit memakai guard receipt lama dan basis valid terakhir bila keputusan entry
ditahan; stop/deadline/close tidak menunggu clock/audit entry. Worker clock terpisah,
tanpa lock order/nonce/account. Fee unknown tetapNULL, kebijakan/risk tidak berubah.

## Hasil pengukuran

Belum dimulai ketika kriteria ini ditulis. Hasil, hash, runtime deployment,
pengujian final dan keadaan akun ditambahkan sesudah satu pengukuran selesai.
