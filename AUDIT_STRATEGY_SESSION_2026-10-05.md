# Satu sesi strategi terbatas — 5 Oktober 2026

Otorisasi pengguna baru: maksimum1800detik sejak aktivasi dan3percobaan entry,
mana lebih dulu, satu UUID saja, tanpa deposit, tuning, probe/manual entry,
perpanjangan atau sesi lanjutan. UUID `5c2672c7-bdec-4561-922b-8f344f0938b0`.
Persiapan memakai batas konservatif10:54:50–11:14:50UTC (17:54:50–18:14:50WIB).
START_LIVE hanya melalui API lokal setelah seluruh guard dan preflight lolos.
Rekaman privat berada di Workspace/outputs/lighter-strategy-session-20261005.

## Kode, keadaan awal, dan kebutuhan restart

Basis repo283ab14/5dfd09b;19hash modul engine direkam sebelum aktivasi.
Keadaan aktual sebelum perubahan: akun751745/key4 Standard0/Unified1/Cross0,
initial margin100%, equity/available/collateral49.430756USDC; posisi,
seluruh order aktif/pending dan intent belum terselesaikan nol; cash matched,
history lengkap, empat fee exactNULL dengan ekspor resmi0.0000/DP4 cocok.
Sumber50USDT sudah teralokasi seluruhnya; tidak ada dana baru.

Proses239076 memiliki coverage sah, tetapi konfigurasi orderOFF/fee exception
false tersimpan di memori. Kode yang tersedia tidak memiliki enable/reload
permission command: START_LIVE hanya membaca self.config, STOP_ENTRIES hanya
bisa mencabut izin. Karena itu satu restart saat flat diperlukan untuk
memuat policy sesi; tidak dilakukan hot injection atau pelonggaran guard.
Restart11:00:30UTC memakai service lighter-live-desk-engine, PID244409.
Coverage baru wajib memenuhi600detik/480sampel/gap≤5detik; tidak menganggap
buffer sebelum restart sebagai observasi baru. Deadline aktif belum dimulai.

BoundedSession.authorize dipakai untuk record persisten baru; record/event
sesi lama tetap ada. Config lokal allow_live_orders/session_accept_reported_fees
true hanya mempersiapkan UUID ini; proses tetap entries_stopped sampai aktivasi.
Hash parameter `e998b3743ca8decd904b7a81d076a865e0b98c7bb46f02e39036023890b97842`.

## Parameter yang dibekukan

Strategi local-deviation-v1-HEURISTIC_UNVALIDATED tetap. Comparator=
Binance BTCUSDT.mid / USDCUSDT.mid; raw=(Lighter.mid/comparator−1)×10000bps;
basis median buffer1800detik dengan detik terbaru dikecualikan;
scale=MAD×1.4826; residual=raw−basis; threshold=max(8bps,4×scale).
BUY residual<−threshold, SELL residual>threshold, hanya setelah konfirmasi
flow/depth/comparator dan residual mencukupi spread+impact+buffer5bps.
Angka decision.deviation_bps adalah raw, bukan residual; analisis mengurangi
basis. Tidak ada perubahan sumber strategy/risk atau threshold selama sesi.

Konfirmasi flow: early/late masing-masing≥3trade; tekanan sisi melemah≤70%;
depth pasif pulih≥1.2×minimum30detik dengan≥20sampel; gerakan comparator10detik
tidak melawan sisi>5bps. Quote max1500ms; kurs observasi conservative max15s,
parity max10bps, cadence REST2s/total timeout2s. Bukan asumsi USDT=USDC.
Ukuran minimum legal dengan headroom pembulatan (metadataBTC minquote10USDC,
minbase0.00007,5size decimals/1price decimal); dihitung ulang sebelum send.
Notional≤25USDC/exposure≤1×equity/rencana risiko≤0.50/daily pause5/exit reserve2.
Entry IOC2bps; stop35bps/limit30bps reduce-only; install3s/confirm8s;
hold≤180s, close dispatch mulai177s, normal exit residual≤2bps.
Stop acceptance harus cocok identitas/sisi/trigger/ukuran/reduce-only exchange.

State akun duaGET dengan freshness independen≤15s; history8GET baseline
startup/recovery/pre-entry/post-lifecycle/perubahan dana dan120s saat flat.
Entry full audit wajib dengan lease8read+2send/envelope14 dan proof≤5s,
terikat boot/generation/config/account/fingerprint/attempt. Setelah audit,
signal/quote/depth/size/risk diperiksa ulang sebelum signer. Kuota SQLite
dikonsumsi sebelum signing; local rejection juga dihitung, maksimal tiga.
Pagu Lighter normal40/urgent47/transaksi55, cadangan exit≥15, Standard resmi60
rolling minute; Binance Spot bucket terpisah100weight/rolling60s.

Fee exactNULL tidak menjadi nol; semua unknown historis harus mempunyai
ekspor yang cocok beserta precision/source/captured_at. Setelah fill baru,
fee/audit pending memblokir entry berikutnya, bukan proteksi/close/timer.
Cash harus cocok tanpa residual. SESSION_POLICY_2026-10-04.md tetap berlaku
untuk UUID baru saja. Restart saat aktif mengakhiri entry, tidak mengulang
deadline/kuota. Engine menangani cleanup dan tetap memantau sampai flat.

## Pemeriksaan sebelum aktivasi

88tes terarah lulus (4.56s,2warning dependency): bounded session, split audit,
protection priority, SDK signing asli tanpa broadcast, reported fee probe.
Tidak ada kode engine yang diubah. Supervisor menggunakan model DB/config
Python proyek karena integrasi dengan engine/SDK yang sudah ada; ia hanya
merekam status lokal dan memanggil DIAGNOSE_PREFLIGHT/START_LIVE/STOP_ENTRIES,
tidak mempunyai exchange/order/signer client. Tidak menambah polling exchange.
Satu marker privat mencegah rekaman diulang; latching sebelum START_LIVE POST
mencegah retry command ambigu. Batas waktu persiapan tidak memakan1800s aktif.

## Hasil

Pending: warm-up, preflight lengkap, aktivasi satu kali, observasi dan cleanup.
Audit ini akan dilengkapi dari snapshot status, Recorder yang sudah dikonsumsi
engine, SQLite order/fill/session events, fee exports, dan audit kas.
Sampel paling banyak tiga percobaan tidak membuktikan profitabilitas.

Dokumentasi resmi diperiksa5Oktober: [Lighter limits](https://apidocs.lighter.xyz/docs/rate-limits)
dan [official trade export](https://apidocs.lighter.xyz/reference/export).
