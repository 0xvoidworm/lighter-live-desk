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

Kriteria di atas committed sebelum pengukuran pada `dccf067`.
Satu pengukuran: **6 Oktober00:05:02.025979–00:20:02.025979 WIB**, data900s.
Completion bookkeeping selesai00:20:02.027690 (900.001711s), bukan perpanjangan data.
Kedua child exit0, observer tanpa error; tidak ada pengulangan pengukuran.

DNS Cloudflare A resmi memilih **16.76.58.128**, kedua peer TCP16.76.58.128:443.
Native-root TLS/hostname/SNI tetap; library/config/binary identik, beda subscription.
Handshake383.159ms depth versus384.290ms combined. Kedua koneksi pertama bertahan
sampai deadline. Tidak ada receive_error, remote_close, reconnect atau pu gap.
Ping server lima kali per arm dijawab satu auto-pong sesuai payload; close akhir lokal.

| Ukuran selama900s | Depth-only | BookTicker+depth |
|---|---:|---:|
| Pesan depth | 8.817 | 8.817 |
| Pesan bookTicker | 0 | 359.376 |
| Total pesan | 8.817 | 368.193 |
| Payload JSON byte | 20.883.702 | 93.685.777 |
| Log byte | 22.766.847 | 171.847.330 |
| Reconnect / sequence gap | 0 / 0 | 0 / 0 |
| T receipt-minus-source p50 / p95 / p99 ms | 60,292 / 174,616 / 353,022 | 68,628 / 240,621 / 922,097 |
| T receipt-minus-source max ms | 907,606 | 2.744,998 |
| Clock-bound upper age T p95 / p99 / max ms | 353,185 / 524,856 / 1.055,583 | 422,141 / 1.117,101 / 2.877,553 |
| JSON parse/field depth p50 / p95 / max µs | 43,972 / 97,605 / 739,599 | 42,837 / 96,829 / 1.140,736 |
| Total serialize/write/flush s | 0,712412 | 5,620557 |
| Maksimum serialize/write/flush satu record ms | 11,658 | 8,268 |
| Maksimum gap antarframe pesan dalam koneksi s | 0,684 | 0,435 |

Payload bukan byte TLS/wire; parse timing tidak mencakup semua konstruktor body.
Log cost mencakup serialization, locking, write dan flush, tanpa fsync.
Cost wall time log~0,079% versus0,625% durasi. Combined:41,76× pesan,
4,49× payload,7,55× log. Sampel `ps` pada~510,7s: CPU lifetime depth0,2%,
combined1,9%, engine28,8%; RSS8536/8844/115276KiB. Ini satu sampel CPU, bukan
profil CPU per tahapan. Semua child memakai proses/logger terpisah.

Seluruh **8.817 pasangan u+T** cocok, mismatch ekonomi U/pu/bid/ask0.
Combined-minus-depth receipt: median8,146ms, p95 107,094ms, p99 824,511ms,
minimum-572,900ms, maksimum2.681,859ms. Offset sumber yang sama saling menghilangkan
pada selisih pasangan di host yang sama. Wall-versus-monotonic discrepancy maksimum
0,014ms depth/0,176ms combined, jauh lebih kecil dari selisih terbesar.

Contoh terbesar: u11741755871441, T1791220702233, E1791220702234;
depth diterima1791220702.296139, combined1791220704.9779978. Parse frame tersebut
92,765µs versus78,552µs. Delay telah ada saat WS frame dikembalikan sebelum JSON
parse frame tersebut. Ini tidak memisahkan antrean frame/socket lokal, penjadwalan,
TCP/jaringan, atau fanout upstream. Combined tetap terus menerima frame lain;
gap frame maksimum0,435s tidak menjelaskan ini sebagai socket diam2,68s.
Tidak ada bukti memadai untuk menunjuk ISP, Binance, atau bug engine sebagai sebab.

Pada model clock yang tercatat,53 depth combined mempunyai upper age>=1500ms,
depth-only0. Klasifikasi ini saat receipt, bukan evaluasi entry. Banyak frame yang
baru diterima mempunyai lower bound<0 karena interval offset: label
`future_uncertain` berarti rentang clock mencakup masa depan, **bukan bukti timestamp
exchange salah/future**. Guard konservatif tetap menolak saat evaluasi bila rentang
belum memadai.12 frame depth per arm mempunyai clock model unavailable dari telemetry
1Hz; itu tidak diubah menjadi umur nol. Batas domain clock dan sampling tetap berlaku.

## Runtime guard, warming dan beban request

Sebelum restart: PID255514, IDLE, equity49.430756, akun/order/intent bersih,
komponen akun2,106/2,247s, orderOFF/sessionclosed. Satu restart untuk memuat guard
dan trace logging sebelumnya; PIDbaru290293, kode`dccf067`. Config/otorisasi tidak
diubah, subscription public kombinasi dan market aggTrade dipertahankan.
Startup nyata memulai coverage baru pada1791219849.648427; rekaman/gap sebelumnya
tidak dihapus/backfill, dan sampel lama tidak diklaim sudah lolos source guard.

Selama A/B: technical ready **318,918s (5m18,9s)**; interval kontinu terpanjang
37,059s. Coverage masih warming548,886s pertama pengukuran karena proses dimulai
~52s sebelumnya. Sesudah coverage pertama lolos, kesiapan sekitar318,9 dari351,1s.
Coverage/basis/source blocker saling overlap; sesi closed/orderOFF tidak dihitung
sebagai kegagalan teknis. Durasi blocker dan kombinasi lengkap tersimpan dalam JSON:

- coverage548,886s; basis_scale575,964s;
- market_data/source_freshness masing-masing37,115s (blocker yang overlap);
- reconciliation_idle9,047s;
- SOURCE_DATA_LATE dengan receipt masih<1500ms:5,023s;
- top-level source indicator FUTURE_UNCERTAIN34,103s, fresh860,771s.

Angka alasan top-level dan boolean diagnostic berasal dari evaluasi terpisah,
sehingga ada perbedaan~2s pada sampling di batas guard. Setelah rekaman, telemetry
dikoreksi agar check dan alasan di diagnostic memakai satu assessment, dengan regresi.
**Koreksi telemetry ini belum dimuat ke PID290293** untuk menjaga coverage valid;
source guard entry/sampling sendiri sudah dimuat. Tidak ada rekaman ulang untuk
menutupi keterbatasan ini. Seluruh umur/proof menyertakan monotonic evaluasinya.

Peak sampled queue13, event-loop lag13,3ms. Max receipt-to-processing depth170,108ms,
receive-to-enqueue159,932ms, queue wait22,751ms, processing16,592ms. Nilai ini bukan
latensi TCP/socket satu arah dan tidak membuktikan sumber semua delay. Tidak ada
reconnect public, market, Lighter sepanjang900s; overflow tidak tercatat.

Clock request15 selama jendela (id2–16), plus satu kalibrasi sebelum A/B (id1).
**15 percobaanGET/15 weight lokal konservatif futures**, tidak mengambil bucket Lighter; A/B tidak memakai
REST snapshot atau tambahan polling akun. Header futures used-weight1 pada respons
berhasil; bobot aktual yang diterima server pada request timeout tidak diketahui,
tidak diasumsikan nol. Satu timeout clock id8 (durasi/phase aktual tidak tersedia di trace callback,
ClientTimeout.total2s), satu respons568,045ms id12 ditolak RTT>400ms. Tidak dihapus
atau dianggap sukses. Bukti lama tidak disegarkan oleh kegagalan; TTL120s tetap.
Tidak ada blind retry/burst, kalibrasi berikutnya mengikuti deadline60s.

Pagu Lighter40/55 tetap; peak used+reservation18, capacity ke55 minimal37,
cadangan exit15 tetap tersedia. Umur akun sampled max12,610s, order aktif12,719s
<15. Audit history complete899/899 snapshot; tidak menambah audit rutin untuk
statistik. Ini bukti telemetry runtime, bukan simulasi pengiriman entry.

## Hasil yang dapat ditindaklanjuti

Siklus reconnect sebelumnya **belum direproduksi**, bukan dinyatakan diperbaiki.
Dalam satu pasangan ini depth-only mengurangi volume/log dan ekor delay receipt
depth; perbedaan parse depth jauh terlalu kecil untuk menjelaskan2,68s pada frame
contoh. Hal tersebut mempersempit ke jalur delivery/antrean sebelum parse frame,
tetapi tidak membuktikan satu lapisan penyebab atau perbedaan reliability reconnect.
IP yang sama juga dapat menuju worker/backend berbeda.

Langkah berikut paling didukung: perubahan produksi terkecil untuk menghapus
subscription bookTicker yang tidak digunakan `_apply_market` untuk membentuk quote
(quote berasal dari snapshot+depth), dengan verifikasi read-only bootstrap snapshot,
U/u/pu recovery, stale-source guard, coverage/basis dan dashboard tanpa ticker.
Pertahankan aggTrade pada market, backoff, seluruh guard/risk. **Usulan belum
diterapkan pada runtime maupun URL produksi repo** dalam tugas ini.

## Tes, versi dan keadaan akhir

404 tes Python lulus (termasuk signer asli tanpa broadcast, perlindungan fee pending,
source-age/missing/future/uncertainty/reviewer replay, delta sequence, exit independence,
offline A/B dan snapshot diagnostic). Lima tes Rust serta rust-workbench check
(fmt/clippy/locked tests/docs/release/audit177dependencies) lulus. Ruff/mypy lulus.
Tidak ada pengujian yang mengirim transaksi baru; transport order di regresi palsu.
Perluasan analisis memakai pipeline Python JSON yang sudah ada; klien mandiri Rust.

Loaded: guard source, T/E terpisah, clock worker, entry/sampling checks, proteksi/exit
independen, trace exception sebelumnya, PID290293/`dccf067`.
Belum loaded: koreksi satu-assessment diagnostic setelah rekaman. Analyzer, fixture
hash dan audit offline bukan modul pengirim order. Tidak ada hot permission load.

Pemeriksaan akhir **00:35:21.568981WIB** melalui state engine yang sudah dikonsumsi:
equity/available/collateral49.430756USDC, posisi/order/pending/intent/posisi lain0,
Standard/Unified, margin100%=1×, account/order ages2,891/3,003s, historycomplete,
audit kasmatched selisih0.000000USDC. Cash change0; empat fee exact tetapNULL,
bukti reported historis tetap terpisah. IDLE, entries stopped, orderOFF,
otorisasi9ce8b029-1f0b-4b1e-afb7-f6c1732e7b3a tetapclosed, tidak ada izin baru.
Coverage valid tetap dipertahankan (581 recent, gap max4,561s, long gap0).
Pemeriksaan ulang saat penutupan00:42:27.020912WIB: saldo/state/posisi/order/intent,
kasmatched dan izin tetap sama, account/order ages1,903/2,010s. Source guard saat
snapshot itu FUTURE_UNCERTAIN, sehingga entry tetap ditahan; tidak diklaim seluruh
guard lolos pada akhir tugas. Ini pemeriksaan akun, bukan perpanjangan A/B.

## Artefak dan integritas

Raw lengkap: `/home/yuri/Workspace/outputs/lighter-depth-ab-20261006/` (untracked).
Ringkasan dapat diaudit: `audits/binance-source-depth-ab-20261006.json`;
keadaan akhir: `audits/source-depth-ab-final-account-20261006.json`.
SHA256 binary pengukuran:c840624f65f3f811f12e951de56ce077785111d06081b216737d6116d343abb2.

- started.json:0ae79b07835feb6a8c7370315cdc3a3e4542c3c2b5a57335a486b230a867e618
- finished.json:dd67b419c0fa386e8cf3212838ca7b7f859b32f785bcb9bdaf705f8f32de6ece
- app.jsonl:195bc75f28a1bb7dbfa8ca0903d38367784a87373a1d222add42a3b046cee6d4
- depth.jsonl:3f05f9f687412b209ed82f41433f6c54e4b98e89a036b6fc9a165b688562ceb3
- combined.jsonl:b651256091878654b4983d29effce03679a808eae612dcaf9ba7ff93edfdb40c

Raw reviewer sebelumnya:
be5a3a0af2facf6191edabf363887b020724fbddf417284618a8d25259ba5d02,
identitas koordinat disimpan pada fixture tanpa mengubah raw. Semua angka latensi
fill tidak diketahui: tidak ada order/broadcast/fill dalam tugas ini.
