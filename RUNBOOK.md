# Runbook Lighter Live Desk

Koneksi public Binance produksi berlangganan hanya `btcusdt@depth@100ms` pada
`/public/stream`, dengan bootstrap snapshot REST dan bridge sequence U/u/pu.
Quote, monitoring dan dashboard tidak memerlukan futures bookTicker. aggTrade
tetap pada `/market/stream`; REST Spot bookTicker USDCUSDT untuk kurs terpisah.
Diagnostic observation memakai satu assessment sumber per snapshot; jalur entry
selalu menilai ulang sumber melalui data_valid dan tidak memakai hasil cache
diagnostik sebagai izin. Lihat AUDIT_DEPTH_ONLY_PRODUCTION_2026-10-06.md.

`decision_inputs.data_guard` mencatat pemeriksaan receipt/transport dan assessment
sumber yang benar-benar menentukan `data_valid` untuk evaluasi itu, beserta bracket
wall/monotonic. Jika receipt/transport gagal, sumber ditandai belum diperiksa (`null`),
bukan dinilai ulang sebagai alasan keputusan lama. Assessment menyimpan identitas
proof, anchor, offset dasar, drift dan TTL. Entry/preflight/dispatch tetap melakukan
pemeriksaan baru; rekaman keputusan tidak memberi izin. Penilaian diagnostik yang
lebih baru dapat berbeda secara sah. Lihat AUDIT_CLOCK_DECISION_RECONNECT_2026-10-06.md.

## Bukti transport pasif

`websocket_telemetry` menyimpan connection/event ID, handshake, frame library versus
pesan aplikasi, jadwal/callback heartbeat, serta pemicu close pertama dan error
cleanup terpisah. Pada aiohttp, PONG diamati sebelum autoping menyembunyikannya;
pada websockets, frame diamati sebelum pencocokan pong. Tidak adanya PONG di
iterator aplikasi bukan bukti pong hilang. Callback terlambat dicatat terhadap
deadline monotonic, tanpa mengubah heartbeat atau timer evaluator.

Event jarang ditulis ke `state/transport/` oleh thread dengan queue512 dan
`put_nowait`; data frame hanya dihitung. Periksa `events_dropped`, `write_errors`,
umur antrean dan lag sebelum memakai bukti sebagai timeline lengkap. Logger tidak
menambah polling akun, ping, atau koneksi. Snapshot, bridge U/u/pu, source-valid
dan coverage observed terikat connection ID; gap historis tetap berlaku.
Hook keepalive websockets terikat hash implementasi16.1.1; jika berubah, hook
jadwal dinonaktifkan dan keepalive asli dipakai. Lihat
TRANSPORT_TELEMETRY_DESIGN_2026-10-06.md untuk batas pengamatan dan kriteria rollout.

## Kesegaran sumber Binance untuk entry dan sampel

Receipt monotonic tetap memakai guard lama 1500ms. Entry/sampel juga wajib
mempunyai `T` Unix millisecond valid (transaction time); `E` event time dicatat
terpisah, tidak dipakai sebagai pengganti `T` yang hilang. Semua delta tetap
diterapkan menurut U/u/pu meskipun source guard gagal; tidak menambah sampel
coverage/basis. Heartbeat/ticker tidak menyegarkan depth. Stop, timer dan close
tidak mensyaratkan source guard entry; exit reversion dapat memakai basis terakhir
yang lolos, tanpa menambah sampel baru.

`source_freshness` memisahkan receipt age, source indicator antarjam, offset interval,
dan source age lower/upper bound. Upper bound wajib <1500ms, lower bound >=-1ms.
Unknown/missing/future/uncertain timestamp memblokir entry dengan alasan eksplisit.
Ini indikator umur data menurut model clock, bukan pengukuran latensi fill/network.

Worker clock independen membaca chronyc tracking (read-only) dan Binance USD-M
GET /fapi/v1/time tiap60s, weight1/request, timeout2s, tanpa catch-up/retry burst.
Clock interval: serverTime-minus-local-receipt sampai serverTime-minus-local-send,
ditambah host NTP error bound dan pembulatan1ms. RTT harus <=400ms; half-width
akhir <=250ms. Host bound <=50ms, ref chrony <=3600s/Normal, bukti clock TTL120s,
allowance drift100ppm (12ms/TTL), wall/monotonic discrepancy <=25ms.
Bounds ini kontrol konservatif, bukan SLA exchange; memakai domain timestamp
UTC USD-M REST/WS yang sama. Chrony saja tidak membuktikan offset Binance.
Kegagalan kalibrasi tidak menyegarkan bukti lama; bukti expires, unsync/restart
membatalkannya. Ketidakpastian future juga fail closed; tidak diasumsikan offset0.
HTTP429/418 mematuhi Retry-After. Request clock terpisah dari bucket Lighter40/55;
actual Binance X-MBX-USED-WEIGHT-1M dicatat termasuk traffic futures lain.
Guard gap5s, coverage600s/480, FX, threshold8bps, buffer5bps dan risiko tidak berubah.

## Pemuatan izin sesi tanpa restart

`LOAD_SESSION_PERMISSION` melalui command API/CSRF lokal memuat izin yang
sudah diberikan pengguna dan staged pada config + record BoundedSession
authorized baru. Tanpa record baru yang sah, config `allow_live_orders=true`
saja tidak cukup. Jangan memanggil authorize/START_LIVE pada tugas tanpa izin.
Izin pra-aktivasi expires20menit sejak authorize; record lama tanpa expiry,
closed/revoked/used ditolak. Reload tidak mengganti UUID, kuota, expiry atau
deadline. Identity/risk/config hash, flat, fresh account, full history, kas
dan bukti fee diperiksa sebelum engine memuat flag. Coverage dan timestamp
observasi tetap dipertahankan. LOAD tidak mengaktifkan sesi atau mengirim order.
Config flags yang dimuat mempersiapkan izin; entry tetap stopped sampai
START_LIVE yang terpisah dan seluruh preflight lolos.

Supervisor sesi yang diluncurkan dengan otorisasi eksplisit memakai LOAD,
preflight diagnostik, lalu maksimal satu START_LIVE. Audit-before-every-entry
dan TTL5s tetap wajib; peluang dievaluasi ulang setelah audit. Restart fisik
tetap RECONCILING dan tidak memulihkan loaded permission atau entry otomatis.
STOP_ENTRIES atau LOAD dengan configOFF mencabut izin melalui engine,
menutup unused authorization atau memulai ending/cleanup bila sedang aktif.
Supervisor tidak boleh menulis ulang state/config/kuota untuk revocation.

Untuk diagnosis feed, gunakan `market_processing` dan Recorder enqueue stamp:
receipt aplikasi sebelum parse, sumber exchange asli, enqueue, processing,
quote stamp yang diterapkan. Pesan baru tidak selalu berarti depth baru;
old/duplicate sequence dan heartbeat/ticker tidak menyegarkan book.

Kurs pembanding memakai snapshot resmi Binance Spot USDCUSDT (USDT perUSDC),
bukan last trade atau kurs1:1. Bootstrap exchangeInfo sekali, lalu ticker
bid/ask tiap deadline2s dengan total request2s, freshness15s tetap.
Status `fx_diagnostic` memisahkan request/receipt/verification/price-change,
outcome/phase dan quota Binance Spot. Harga sama dari respons baru boleh
menyegarkan observasi, heartbeat tidak. Source exchange time/update IDNULL
karena REST ini tidak menyediakannya. Data terlambat/cached/invalid gagal.
Bobot ticker2 (sekitar60/menit), bootstrap20; localcap100weight/60s dan
limit/header IP/Retry-After resmi tetap diperiksa. Bucket ini terpisah dari
Lighter. Signal memakai mid; pembanding bid/ask executable memakai FXask
untuk menjual BTC lalu membeliUSDC, FXbid untuk membeli BTC dari danaUSDC.
Lihat AUDIT_FX_OBSERVATION_2026-10-05.md. Otorisasi order tidak berubah.

Otorisasi sesi `f4645c06-b55d-45f4-b642-66b5e8c23864` sudah ditutup
4 Oktober 2026 pukul 21:53:18 WIB setelah timeout rekonsiliasi; nol entry.
Izin order dan policy reported fee sesi mati. Sisa kuota tidak dapat dipakai.
Lihat AUDIT_STRATEGY_SESSION_2026-10-04.md. Sesi baru memerlukan izin baru.

Pengguna memberi izin terpisah pada 4 Oktober untuk SATU sesi strategi setelah
preflight, maksimum 1800 detik/tiga percobaan entry. Lihat
SESSION_POLICY_2026-10-04.md. Ini menggantikan larangan START_LIVE hanya untuk
UUID sesi tersebut; tidak mengizinkan rearm probe atau sesi lanjut otomatis.
Policy reported fee sesi mempunyai flag terpisah, default false, dan memerlukan
record otorisasi persisten yang belum dipakai. Kuota disimpan atomic sebelum
signer, termasuk penolakan/failure. Restart menghentikan entry untuk sisa sesi,
mempertahankan deadline/kuota, dan tetap memulihkan posisi. Setelah ending,
flat/global order bersih diverifikasi sebelum IDLE dan izin order false.
Fee exact NULL tetap terpisah dari reported; sebelum setiap entry berikutnya,
ekspor per fill dan kas/dana segar harus cocok. Metadata pending tidak menahan
proteksi/exit dan tidak boleh memperpanjang izin entry sesudah kuota/deadline.

Hasil terbaru ada di AUDIT_PROTECTION_PROBE_2026-10-04.md: satu entry baru sudah
dipakai, stop terkonfirmasi aktif di exchange, close reduce-only terisi,
posisi/order nol, dan audit net kas cocok. Mesin IDLE dan izin order false.
Penerimaan stop terbukti; pemicuan stop tidak diuji. Jangan rearm tanpa izin baru.

Fee API empat fill tetap NULL; ekspor resmi akun memberi reported fee 0.0000 pada empat
desimal per Trade ID yang cocok. Simpan laporan/presisi/sumber terpisah,
jangan mengubah fee exact menjadi nol. Audit net kas dapat cocok walaupun
rincian fee exact belum diketahui. Guard strategi tetap ketat. Kebijakan pada
FEE_POLICY_PROPOSAL_2026-10-04.md sudah disetujui khusus preflight satu probe:
`probe_accept_reported_fees=true` hanya sah pada scope `probe_only`.
Setiap fee unknown wajib memiliki bukti ekspor resmi yang cocok; audit kas
segar, fingerprint fill/dana dan equity harus cocok dengan sesi izin.
Preflight membekukan fingerprint/equity, dan dispatch memeriksanya lagi.
Perubahan tak terduga membatalkan entry. Persetujuan kebijakan bukan rearm.
Setelah fill, fee/audit pending tidak menahan proteksi/close/cancel/timer.
Flat + seluruh order bersih + audit kas segar cocok menyelesaikan operasi,
sementara fee exact tetap NULL. Backfill otomatis maksimal
tiga history check berhasil berjarak minimal 60 detik, lalu berhenti persisten.
Jangan mengulang polling tanpa batas atau mereset counter demi READY.

Untuk coverage, baca status.coverage dan feed_channels: koneksi tersambung
berbeda dari quote segar dan sampel cukup. Minimal 600 detik, 480 sampel valid
dalam 600 detik terakhir, tanpa gap >5 detik tetap berlaku untuk probe maupun
strategi. Restart memulai buffer baru; reconnect mempertahankan sampel tetapi
gap lama tetap menahan coverage. Observasi pasca-fix dibatasi 660 detik;
jika gagal, simpan nilai/count/gap/channel/alasan lalu laporkan penghambat.
Jangan memutus jaringan untuk menguji kegagalan saat posisi mainnet terbuka.

Sebelum probe setelah perubahan parameter signing, jalankan
`uv run --frozen pytest -q tests/test_sdk_signing.py`. Regresi ini memeriksa
pin distribusi resmi, melewati adapter entry/stop/cancel/close dan signer native
yang sesungguhnya dengan key sementara; nonce lookup dan transport diganti,
seluruh I/O jaringan diblokir. IOC lama expiry -1 harus ditolak sebelum
transport, IOC expiry 0 dan stop helper resmi harus berhasil ditandatangani.
Ini bukti lokal, bukan bukti penerimaan atau fill mainnet.

Pada percobaan kedua, entry/fill dan close darurat reduce-only mainnet terbukti,
tetapi stop tidak dikirim: API trade tidak memberi fee dan ledger menahan
rekonsiliasi. Lihat AUDIT_PROBE_2026-10-03_SECOND.md dan
AUDIT_PROTECTION_FIX_2026-10-04.md. Fee yang hilang sekarang disimpan sebagai
unknown/NULL dan tidak menahan worker proteksi, cancel, posisi, atau close;
audit fee tetap pending dan memblokir entry berikutnya. Jangan mengisi fee null
dengan nol tanpa bukti. Fill yang sudah diaudit hanya dapat didedup jika ID,
ukuran dan harga cocok. Percobaan kedua tidak membuktikan stop; sesi baru pada
audit terbaru membuktikan penerimaan stop, bukan pemicuannya. START_LIVE tetap
tidak diizinkan.

Izin baru untuk satu percobaan hanya boleh mengaktifkan ulang flag sekali-pakai
setelah intent lama terminal dan ketidakpastian broadcast terselesaikan serta
account-check terbaru membuktikan posisi/order nol. Simpan backup dan event
PROBE_REARM_AUTHORIZATION; jangan hapus intent, fill, counter ID, atau audit
lama. Tidak ada rearm otomatis sesudah entry gagal/ambigu. EXIT_DISPATCH
mencatat harga acuan dan batas slippage sebelum close reduce-only dikirim.

IOC entry/exit harus mengirim order_expiry=0. Percobaan pertama 3 Oktober berhenti
di signing lokal karena adapter memakai default SDK -1; tidak ada broadcast,
fill, stop atau close mainnet. Penolakan lokal yang definitif dicatat REJECTED,
hasil network/API yang tidak pasti tetap UNKNOWN. probe_used tidak direset
dan tidak ada entry kedua sesudah fix. Lihat AUDIT_PROBE_2026-10-03.md.

Izin satu probe memakai `live_entry_scope: "probe_only"` (default). Walaupun
`allow_live_orders=true`, backend menolak START_LIVE dan dispatch ENTRY strategi;
PROBE tetap sekali-pakai melalui flag persisten. Jangan mengubah scope ke
`strategy` untuk tugas uji tunggal. Catat `source_funding_spent_usd` dari jumlah
sumber yang telah dikeluarkan, termasuk biaya. Pada akun ini nilainya 50 dan
sisa budget sumber nol; inflow collateral 49,43 tidak memberi izin deposit 0,57.

Sesudah flat/order bersih terverifikasi, tulis `allow_live_orders=false` pada
config lokal lalu kirim STOP_ENTRIES melalui mekanisme resmi. Perintah itu
hanya menyerap pencabutan izin dari config, tidak bisa mengaktifkan izin, dan
tidak mereset buffer feed. Penyelesaian probe juga membersihkan close_requested;
seluruh history tetap disimpan. Jika dilakukan restart setelah perbaikan,
observasi warm-up tetap mengikuti syarat 600/480/5 dengan izin order false.

Pagu normal tetap 40 dan transaksi proteksi/cancel/close tetap 55 per 60 detik.
Pembacaan urgent probe dibatasi 47 agar menyisakan delapan slot transaksi;
preflight juga menyisakan kapasitas pembacaan setelah entry. Ini lebih rendah
dari batas Standard 60 pada [dokumen rate limit resmi](https://apidocs.lighter.xyz/docs/rate-limits).
Probe menutup normal hanya setelah stop reduce-only terverifikasi dari status
bursa. Stop gagal/tidak pasti meminta close darurat; deadline konfirmasi delapan
detik sejak exposure diketahui tetap berlaku meskipun pembukuan gagal. Batas
pemasangan stop 3 detik terpisah dari konfirmasi 8 detik dan hold 180 detik.
Worker risiko berjalan tanpa lock rekonsiliasi; GET posisi/status maksimum
2 detik per panggilan. Polling posisi terlindungi memakai pagu normal 40,
pemulihan mendesak memakai 47; delapan slot tersisa untuk transaksi di pagu 55.
Antrean lock signer, nonce dan submission stop/cancel/close bersama-sama
dibatasi 3 detik; timeout menjadi UNKNOWN, bukan bukti order tidak terkirim.
Hold proses aktif memakai monotonic clock, pengiriman close diminta mulai
detik 177 agar ada ruang 3 detik sebelum rencana 180. Setelah restart, deadline
lama dianggap jatuh tempo dan exposure dibaca ulang sebelum close. Tidak ada
jaminan konfirmasi fill pada detik 180; proses tetap memantau sampai flat dan
order bersih. Snapshot posisi aktual diperlukan, bukan ukuran tebakan.
Stop tidak dikirim ulang pada
hasil ambigu. Pending probe bertahan sampai cleanup dan audit dana lengkap,
termasuk ketika posisi telah flat sebelum restart.

Status 2026-10-03 setelah percobaan kedua: akun Standard dan setting BTC Cross
1x diverifikasi di UI/API nyata. Saldo 49,430576 USDC, posisi/order nol, mode
lokal `live_capable` dengan `allow_live_orders=false`, entry tetap berhenti.
Koreksi 4 Oktober: dua fee unknown, recorded_fees null, audit net kas cocok;
pending fee melarang probe berikutnya sampai ada bukti biaya yang sah.
Feed Binance/Lighter, entry/fill dan close darurat mainnet telah diverifikasi;
proteksi exchange belum dibuktikan. Percobaan tambahan membutuhkan izin baru
dan tetap memakai jalur probe khusus seperti catatan di bawah.
Bukti jaringan sebelum perbaikan ada di `AUDIT_2026-09-30.md`; hasil perbaikan ada di
`LOCAL_FIX_2026-09-30.md`. Jangan menyebut dashboard siap live sampai
`live_blockers` kosong, hasil `account-check` cocok dengan akun dedicated, dan
satu probe telah direkonsiliasi. Bila development runner sudah aktif, hentikan
dari terminal yang menjalankannya sebelum memakai unit systemd; single-instance
lock menolak engine kedua.

## Jalankan lokal

```bash
cd ~/lighter-live-desk
uv sync --frozen --extra dev --python 3.12
cd frontend && npm ci && npm run build && cd ..
cp -n config.example.json config.json
uv run --frozen lldesk check
./dev.sh
```

Dashboard development: `http://127.0.0.1:5173`; API: `http://127.0.0.1:8765`. Engine adalah proses terpisah. Menutup tab browser tidak menghentikan engine. Jika heartbeat lebih tua dari tiga detik, API menandai `DISCONNECTED`.

Layanan user tanpa root:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/lighter-live-desk-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start lighter-live-desk-engine.service lighter-live-desk-api.service
```

Buka `http://lighter.localhost:8765` atau `http://127.0.0.1:8765`. Unit tidak di-*enable* otomatis. Jika nanti diaktifkan saat login, engine selalu mulai dengan entry berhenti dan rekonsiliasi dahulu. Lihat log melalui `journalctl --user -u lighter-live-desk-engine.service -f`; jangan menyalin log berisi data akun ke ruang publik.

Pilihan DNS hanya di aplikasi: `binance_dns_mode="system"` memakai DNS laptop,
sedangkan `"doh"` mengambil jawaban A lewat HTTPS Cloudflare/Google untuk dua
host feed Binance. TTL dihormati sampai maksimal 60 detik dan alamat nonpublik
ditolak. Mode ini tidak mengganti resolver router, `/etc/hosts`, DNS laptop,
hostname bursa, ataupun pemeriksaan TLS. Konfigurasi laptop ini memakai `doh`
setelah REST dan kedua stream resmi berhasil diuji.

Unit memakai `~/lighter-live-desk/.venv/bin/lldesk` yang dibuat oleh `uv sync` di atas.
Status sebelum perbaikan adalah `inactive` dan `disabled`. Setelah perbaikan,
layanan dijalankan dalam mode observasi tanpa mengubah pengaturan autostart.

## Idle dengan akun tersambung

Dengan `mode=live_capable`, account index/tier/trading mode yang sudah
diverifikasi, dan `allow_live_orders=false`, restart mulai dari RECONCILING.
Sesudah warm-up wajib 600 detik dan pemeriksaan sehat, mesin dapat masuk
IDLE untuk memantau akun/harga. IDLE mensyaratkan akun flat, tidak ada
order/intent/sesi aktif, data segar, jurnal sehat dan tidak ada flag insiden.
Jika syarat itu berubah, IDLE ditahan. Entry tetap berhenti.

API dapat tidak mengembalikan setting margin BTC ketika akun flat. Hal itu
tidak menghalangi pemantauan IDLE; semua guard margin, izin order dan
preflight tetap berlaku sebelum live. Jangan mengisi expected margin
dengan perkiraan, menaikkan leverage, atau mengirim order untuk memaksa IDLE.

Profil akun saat ini memakai2GET per pembaruan state dan8GET per audit
lengkap. State dijadwalkan dari umur komponen tertua:15s dikurangi bound
durasi awal2s/high-water dan margin0,5s. Timeout state mengikuti sisa umur
komponen, full history maksimum8s; retry audit gagal12s, retry state2s.
Failure tidak memperbarui freshness. Setelah bootstrap, full history dibaca
dahulu dan account dekat akhir; state refresh tidak membaca funding history.
Kebijakan dua lane disetujui 5 Oktober 2026: keadaan akun/order masing-masing
wajib segar ≤15s, tetapi riwayat lengkap tidak lagi mengikuti interval itu.
`account` membuktikan posisi semua market, collateral/equity/available, tier,
mode/margin dan total/pending order count; `accountActiveOrders` tanpa market
membuktikan seluruh order. Timestamp masing-masing berasal dari responsnya;
GET posisi saja, heartbeat dan socket idle tidak menyegarkan order/history.
Count tidak cocok, komponen kosong/basi atau cursor order yang tidak dapat
diikuti SDK membatalkan kesiapan entry.

Audit lengkap wajib pada startup/recovery, tepat sebelum SETIAP percobaan
entry, setelah lifecycle selesai dan setelah perubahan dana/state/aktivitas
tak terjelaskan. Saat flat, periodik **120s**: perubahan ekonomi/count atau
transaction_time pada dua GET dan hint trade/reconnect mempercepat audit;
aktivitas net-zero yang luput dari hint tetap dicari saat periodik/sebelum
entry. Transaction_time hanya petunjuk perubahan, bukan bukti kontinuitas.
Funding pagination tetap lengkap, trade checkpoint/deduplikasi tetap ada.
Audit gagal/truncation tidak lulus; fee NULL tetap terpisah dari ekspor resmi.

Preflight memegang reservasi atomik audit lengkap + send2 (saat ini8+2),
menyertakan dua GET keadaan akun dalam8, tanpa menghitungnya dua kali.
Admission juga memeriksa audit8 + envelope entry/priority-read14 ≤47
(used≤25 sebelum audit8), serta normal≤40 agar tersisa minimal15 menuju55.
Lease mengubah slot belum terpakai menjadi konsumsi; hanya sisa dilepas saat
batal. Pagination/retry bertambah membatalkan preflight, tidak meminjam slot
send secara diam-diam. Worker risiko tidak mengambil lease preflight.

Bukti terikat boot/process, satu UUID percobaan, akun, risk parameter hash,
checkpoint/fingerprint ledger, state akun, bukti fee dan referensi kas.
TTL monotonic paling lama **5s**, dipotong sisa freshness komponen−0,5s.
TTL tidak diperpanjang oleh pembaruan data yang ekonominya sama. Dispatch
memeriksa ulang binding, freshness, kapasitas, sinyal, dan izin sesi; bukti
dikonsumsi sebelum signing. Restart/gap/reconnect/perubahan state membatalkan.
Audit/fee pending sesudah fill hanya memblokir entry berikutnya; risk worker
proteksi, close, cancel dan timer tetap independen.

Status membedakan `diagnostic_observation.technical_ready` (evaluasi +
kapasitas biaya preflight yang belum dijalankan) dari
`preflight.ready_to_send_technically` (audit baru selesai dan bukti belum
kedaluwarsa). `DIAGNOSE_PREFLIGHT` hanya menjalankan GET yang sama dengan
reserve send2 yang tidak dikonsumsi; bukti sendable=false, tidak membuat
intent/signing/broadcast, tidak mengaktifkan sesi/fee policy/order. Jalannya
background agar coordinator tetap memproses pasar. Jangan menyamakan bukti
ini dengan order diterima exchange atau latensi fill.

Batch memakai reservasi atomik normal40/min; status memisahkan pemakaian
dari reservasi yang belum terpakai. Pemanggil audit bersamaan berbagi task,
sementara worker proteksi/exit tetap tidak menunggu lock/network pembukuan.
Jadwal lama8GET per pembaruan diganti2GET + audit lengkap periodik/barrier.
Histori panjang tetap dapat menghabiskan admission; pagu tidak dinaikkan.
[Batas resmi Standard](https://apidocs.lighter.xyz/docs/rate-limits)
adalah 60 request per rolling minute dan dapat dibagi dengan klien lain.
Batas freshness akun tetap 15 detik. Kesalahan rekonsiliasi tetap menghentikan
entry sampai pemeriksaan berikutnya berhasil; umur data tidak diperbarui
secara palsu dan pagu tidak dinaikkan untuk menyamarkan polling yang gagal.

## Pendanaan dan key lokal

Probe memeriksa ketersediaan dua request transaksi sebelum mengonsumsi flag
sekali-pakai dan menolak mulai ketika rekonsiliasi lain masih berjalan.
Polling rutin tidak mendahului task pengiriman probe yang sedang menunggu.
Saat probe/posisi/order masih aktif, akun, order dan fill dibaca dengan
prioritas exit (profil eksperimen empat GET, termasuk satu query status).
Riwayat dana dari audit lengkap terakhir dipertahankan sementara; pembacaan
posisi ini **tidak** memperbarui `last_reconcile_ok`/umur audit dana dan tidak
dapat mengakhiri probe. Baseline dana/PnL juga menunggu audit lengkap.
Setelah posisi flat serta order/proteksi bersih, riwayat deposit, withdrawal
dan transfer wajib dibaca ulang dengan prioritas exit sebelum probe selesai.
Pagu tetap 40 request biasa dan 55 request prioritas exit per rolling minute.
Audit gagal/kuota habis menjaga probe tertahan dan entry strategi berhenti.

Untuk akun saat ini, pengguna menyimpan BTC **Cross 1x** di UI resmi pada
3 Oktober 2026. Account-check kemudian membaca `BTC_margin_mode: 0` dan
`BTC_initial_margin_fraction: 100.00` dengan posisi/order tetap nol.
Konfigurasi expected margin harus memakai nilai respons akun yang nyata
(`100.00` pada akun ini), bukan menebak `1` dari angka leverage atau memakai
skala basis point milik transaksi signer. Nilai ini berlaku untuk akun yang
diverifikasi; jangan menyalinnya ke akun lain tanpa pemeriksaan ulang.

1. Siapkan akun/subakun Lighter **Standard** khusus eksperimen, kosong dari posisi, order, dan aset lain. Pastikan wilayah, akun, dan akses pengguna memenuhi aturan platform. Konfirmasi jenis akun, margin konservatif, market BTC perpetual, biaya aktual, dan batas min order di UI serta `uv run --frozen lldesk market-check`.
2. Dana pengguna USDT adalah sumber budget, sedangkan collateral perpetual Lighter adalah **USDC**. [Dokumen deposit resmi](https://apidocs.lighter.xyz/docs/deposits-transfers-and-withdrawals) menyebut deposit langsung USDC via Ethereum dengan minimum 1 USDC; rute Universal Deposit Address (UDA) menerima USDT pada jaringan yang didaftarkan (misalnya Ethereum, Arbitrum, Base, Polygon, Solana, Tron) dan mengkreditkan USDC setelah konversi. Minimum UDA untuk akun baru $5 di Ethereum/EVM/Solana/Tron; akun lama $3 di EVM/Solana dan $5 di Tron. Rute legacy USDC CCTP punya minimum lain. Periksa **rute yang benar-benar dipilih** di UI resmi, alamat/jaringan/aset, quote bersih, biaya/slippage dan status deposit sebelum mengirim. API UDA builder memerlukan kunci builder terpisah; aplikasi ini tidak memakainya. Total USDT yang dikeluarkan termasuk biaya konversi/transfer maksimum setara US$50. Aplikasi tidak melakukan swap, bridge, deposit, atau withdrawal.
3. Di UI resmi, buka **Tools > API Keys > Generate API Key** untuk akun dedicated itu. Pilih indeks kosong **4–254**; [dokumentasi API key resmi](https://apidocs.lighter.xyz/docs/api-keys) mencadangkan indeks 0–3 untuk web/mobile, sedangkan 255 hanya untuk query semua key. Jangan menimpa key yang sudah dipakai aplikasi lain. Registrasi memerlukan tanda tangan wallet pemilik akun. Jangan kirim seed phrase/private key melalui chat. Simpan hanya **API private key khusus Lighter** di `~/lighter-live-desk/secrets.json` dengan bentuk `{"api_private_key":"ISI_SENDIRI","api_key_index":N}`. Jalankan `umask 077` di terminal lokal, buat file dengan editor yang tidak menaruh nilainya ke shell history, lalu `chmod 600 secrets.json`. Jangan menjalankan `cat` pada file itu atau menyalin isinya ke tiket/log. File diabaikan Git, hanya dibaca engine, dan harus dimiliki user saat ini. `account-check` menangkap error SDK tanpa menampilkan nilai kunci.
4. Edit `config.json`: set `mode` ke `live_capable`, isi `account_index` dedicated, tetapi biarkan `allow_live_orders: false`. Jalankan `uv run --frozen lldesk account-check`; cocokkan account index, `account_type_code`, `account_trading_mode`, `BTC_margin_mode`, `BTC_initial_margin_fraction`, equity, posisi, aset, order aktif dan gross history inflow dengan UI resmi. Konfigurasikan BTC margin konservatif di UI, tanpa menaikkan leverage untuk memenuhi minimum order. Isi keempat `expected_*` dari nilai akun yang diverifikasi; aplikasi menolak live bila BTC margin setting tidak tersedia atau berubah. Pastikan equity dan **jumlah seluruh inflow historis** tidak melebihi pagu, termasuk biaya sumber dana yang tidak terlihat oleh aplikasi. Riwayat deposit, withdrawal dan transfer harus cocok; bila tidak, hentikan setup dan audit secara manual. Tetap pertahankan `allow_live_orders: false` setelah pemeriksaan akun. Mengaktifkan order memerlukan instruksi jelas pengguna untuk uji transaksi, lalu restart engine; restart tetap tidak membuka entry dengan sendirinya.
5. Untuk satu probe, tunggu status READY, entry strategi berhenti, kedua feed valid, snapshot akun segar, warm-up sekurangnya 10 menit, dan `probe_blockers` kosong. Klik langsung **Probe 1×**; tidak perlu menekan **Mulai sesi live**. Perintah melewati preflight yang sama dengan live, lalu membuka state PROBING dengan entry strategi tetap berhenti dan satu task IOC minimum legal. Window probe maksimum `max_hold_seconds` (180 detik pada konfigurasi ini); dispatch memeriksa lagi mode order, data, akun/tier/margin, dana/cadangan, loss/insiden dan epoch pembatalan sebelum signer. Stop/cancel/close tetap responsif saat task menunggu signer. Flag sekali-pakai disimpan sebelum pengiriman; jangan ulangi probe atau menghapus flag jika hasil tidak pasti. Pantau fill, stop, fee, penutupan dan ledger sampai bursa mengonfirmasi posisi flat serta seluruh order/proteksi selesai. Akhir probe menjaga entry strategi berhenti; restart merekonsiliasi tanpa mengulang probe. PROBE tidak masuk statistik posisi strategi. Sesi strategi maksimum 30 menit/tiga posisi adalah tahap terpisah setelah uji transaksi dan validasi strategi.

Kode adapter membaca riwayat deposit, withdrawal dan transfer akun via SDK. Status selain `completed`, aset selain USDC, transfer selain inflow/outflow perps yang dikenali, riwayat terlalu panjang, atau respons tidak tersedia menghentikan entry untuk peninjauan. Jika arus dana baru muncul setelah sesi live pernah dimulai, flag persisten `funding_change_pending` menghentikan entry. Trade yang bukan milik akun atau tanpa intent lokal memicu `account_incident_pending`. Fee kosong disimpan unknown dan memblokir entry lewat pending_fee_count; proteksi dan close berjalan terpisah. Keduanya memerlukan audit manual; jangan hapus flag atau database untuk memaksa live. Kelengkapan dan kemungkinan overlap antar riwayat SDK belum dapat diverifikasi tanpa akun nyata.

## Stop, close, dan insiden

- `Hentikan entry` mencegah order baru. `Batalkan entry` mengirim permintaan cancel; tunggu status bursa yang terkonfirmasi. `Minta tutup posisi` mengirim IOC reduce-only dan membutuhkan konfirmasi posisi flat dari bursa. Tombol dapat diklik di dashboard; respons awal hanya `QUEUED`.
- Jika dashboard/engine terputus saat posisi terbuka, lihat posisi dan stop di UI Lighter resmi. Kelola posisi di sana bila engine tidak dapat mengirim exit. Setelah koneksi pulih, restart engine untuk rekonsiliasi; engine tetap memblokir entry. Jangan menganggap cancel/close berhasil hanya dari HTTP 200 atau hash transaksi.
- Exit otomatis memakai maksimal tiga IOC dengan toleransi harga 30, 60, lalu 100 bps setelah status sebelumnya definitif tetapi posisi masih ada. Jika tiga upaya habis atau book Lighter invalid, engine berhenti entry dan menampilkan alasan; operator harus memeriksa/menutup melalui UI resmi. Stop bursa tidak menjamin harga fill pada gap pasar. Posisi kecil di bawah minimum legal mungkin perlu penanganan manual.
- Jika jurnal gagal ditulis, entry berhenti. Jangan hapus database untuk menghilangkan UNKNOWN; cocokkan client ID/order/fill dengan akun resmi lebih dulu.

## Backup dan pemulihan

```bash
cd ~/lighter-live-desk
mkdir -p state/backups
uv run --frozen lldesk backup state/backups/desk-$(date +%Y%m%d-%H%M).db
```

Backup memakai API SQLite agar konsisten dengan WAL. Folder `state/` dan rahasia tidak masuk Git. Untuk restore: hentikan unit engine/API, simpan salinan `state/desk.db` saat ini, ganti dengan backup setelah `PRAGMA integrity_check`, lalu start engine. Rekonsiliasi akun tetap wajib; jangan memulihkan snapshot lama dengan asumsi posisi masih sama. Gunakan izin file 0600 untuk DB dan 0700 untuk direktori state. Rekaman `state/market/*.jsonl.gz` diputar per jam untuk debugging dan dapat dihapus terpisah saat engine mati setelah disalin jika perlu ruang disk.

## Pindah ke VPS kelak

Belum ada deploy. VPS memerlukan verifikasi wilayah/jaringan, sinkronisasi jam, penyimpanan rahasia yang terlindungi, firewall/SSH, backup terenkripsi, dan audit akses. API sekarang sengaja bind `127.0.0.1`; gunakan tunnel SSH tepercaya bila perlu akses jarak jauh, tanpa mengubah bind ke `0.0.0.0` secara langsung. Jangan otomatis mengaktifkan sesi trading setelah boot.
