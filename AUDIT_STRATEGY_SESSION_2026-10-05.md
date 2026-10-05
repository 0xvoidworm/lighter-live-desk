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

## Hasil: persiapan terblokir; sesi tidak diaktifkan

Otorisasi ditutup11:13:12.215476UTC/18:13:12.215476WIB. Persiapan konservatif
18menit22.215476detik, di bawah maksimum20menit. Tidak ada START_LIVE,
DIAGNOSE_PREFLIGHT, entry intent, signing transaksi/order, broadcast, fill,
posisi baru, atau stop/close/cancel baru. Durasi aktif0detik, percobaan0,
fill0 dan posisi selesai0. Command baru hanya8 STOP_ENTRIES, DONE.
UUID ditandai session_used=1 meskipun belum diaktifkan: tidak ada otorisasi
tersisa dan tidak bisa dipakai ulang. Tidak ada sesi lanjutan otomatis.

Warm-up durasi600detik terpenuhi sekitar11:10:35UTC, tetapi satu gap sampel
valid5.551s melebihi maksimum5s, sehingga coverage tidak lolos. Pada11:11:01,
613total/587recent samples sudah melampaui minimum480, warmup_complete=true,
coverage_complete=false, gap_wait344s. Pemulihan paling dini11:16:45UTC/
18:16:45WIB, melewati deadline persiapan11:14:50UTC. Menunggu sampai20menit
tidak dapat mengubah fakta ini; izin ditutup lebih awal tanpa melonggarkan guard.
Buffer tidak di-reset saat gap, dan tidak diisi pengamatan sintetis.

### Gangguan yang terbukti

Snapshot kurs request187 di /api/v3/ticker/bookTicker (USDCUSDT,2weight)
mulai11:06:45.093977UTC; timeout11:06:47.095457UTC setelah2001.475ms.
Trace menunjukkan request headers/body sudah terkirim pada koneksi yang ada;
failure_phase=http11.receive_response_headers, menunggu header respons2000ms.
Bukan bukti DNS/connect/TLS failure. Kurs di-invalidasi sesuai fail-closed;
observasi baru pulih sekitar11:06:49.977UTC. Market guard juga gagal karena
book Binance/Lighter basi pada interval yang tumpang tindih. Umur maksimum
teramati Binance4.430s dan Lighter6.348s (>1500ms), tanpa reconnect pada dua
feed. FX conservative age maksimum3.776s pada observasi yang masih valid,
bukan15s freshness expiry; kurs menjadi unavailable setelah snapshot gagal.
Queue saat transisi0, event-loop lag teramati saat transisi2.6–2.8ms dan
peak rekaman11.6ms. Bukti belum menentukan penyebab eksternal yang sama
untuk HTTP timeout dan keheningan book, maupun ISP/router/exchange tertentu.

### Rekaman dan peluang evaluasi

Rekaman status lokal11:03:53.788674–11:13:12.432052UTC,2064snapshot/error0,
558.626s interval terukur. Bagian awal persiapan543.789s tidak direkam pada
cadence ini; pembacaan awal dan event authorization/restart tetap tersedia.
Semua19hash modul unchanged, config strategi/risk tidak berubah. Konsol
supervisor versi yang berjalan menamai raw deviation sebagai residual;
analisis memakai decision_inputs/decision asli dan formula raw−basis, bukan
label konsol itu. Label diperbaiki untuk penggunaan berikutnya.

Evaluasi berbeda yang tertangkap berdasarkan decision_inputs.monotonic_ns:
1899; ini jumlah evaluasi teramati, bukan total panggilan evaluate engine.
1345 WARMUP_BELUM_LENGKAP,531 COVERAGE_DATA_KURANG,23 DATA_TIDAK_VALID;
signal valid teramati0. Seluruh guard teknis lolos0s/interval kontinu0s pada
rekaman pascarestart; seluruh durasi aktif juga0s karena tidak pernah aktif.
Distribusi residual eligible tidak tersedia (0sampel), bukan distribusi nol.
Tidak ada preflight kandidat ataupun peluang yang hilang selama preflight;
nilai peluang hilang tidak dapat diukur karena preflight tidak dijalankan.
Penyebab tanpa entry adalah kesiapan teknis, bukan bukti pasar kurang peluang
atau aturan threshold terlalu ketat. Tidak ada usulan menurunkan threshold.

Durasi blocker coverage dan basis masing-masing558.626s, tumpang tindih.
Market data6.760s, FX unavailable2.704s, reconciliation idle5.402s juga
tumpang tindih dengan coverage. Jangan menjumlahkan durasi tersebut.
Quota/account freshness/historical fee/cash/minimum sizing guard tidak pernah
gagal pada rekaman. Peak Lighter consumed+lease18, capacity menuju55 minimum37
(reserve minimum15 dipertahankan). Account component age max12.664s,
orders12.790s. Tidak ada retry transaksi atau polling exchange tambahan untuk
rekaman. Satu timeout FX teridentifikasi, tanpa eksperimen jaringan disengaja.

### Keuangan, cleanup, dan keterbatasan

Pemeriksaan saat penutupan: account751745 equity/available/collateral
49.430756USDC; seluruh posisi/global active/pending order dan unresolved
intent0; history lengkap dan audit kas matched/difference0/change0.
Tidak ada transaksi baru sehingga slippage, proteksi, durasi posisi, exit,
fee baru dan per-trade PnL tidak berlaku; perubahan kas aktual0.000000USDC.
Empat fee historis exact tetapNULL, official reported0.0000 masing-masing
pada4desimal; source/evidence_sha256/captured_at tetap disimpan terpisah.
Tidak mengubah unknown menjadi nol berdasarkan tier maupun perubahan kas.

IDLE/entries_stopped=true/allow_live_orders=false/sessionclosed, disk config
allow_live_orders dan session_accept_reported_fees false. Boolean fee policy
pada proses belum di-reload oleh STOP_ENTRIES, tetapi record closed dan UUID
tombstoned memblokir penggunaannya; tidak ada izin order atau entry tersisa.
Proses tidak direstart lagi untuk kosmetik, agar coverage berikutnya tetap
dipertahankan. Autostart tetap disabled. Cash cocok adalah bukti operasional,
bukan kelengkapan exact fee atau bukti profitabilitas strategi.

Empat tes tambahan memverifikasi unused authorization tidak dapat dipakai
ulang, tidak mengubah ledger/feeNULL, mempertahankan otorisasi lama, dan
supervisor tidak mempunyai jalur command probe/order langsung. Hasil tes
final dan pemeriksaan akun terbaru dicatat pada bukti pendamping.

Final suite365tes lulus/18.36s/3warning dependency;4tes supervisor lulus0.09s,
mypy19source lulus, Ruff/format lulus. Tidak ada frontend/dependency/toolchain
atau kode engine/strategi/risk yang berubah.
Pemeriksaan ulang11:22:19.652125UTC/18:22:19.652125WIB membuktikan49.430756USDC,
flat/globalactive/pending/intents0, account age4.641s/orders4.752s,
history complete/age93s, kas matched/difference0/change0, IDLE/orderOFF dan
sessionclosed/UUIDretired. Coverage sudah pulih pada pemeriksaan ini tetapi
entry tidak dilanjutkan; sesi tidak diaktifkan ulang. Engine/API active,
autostartdisabled. Bukti audits/strategy-preparation-final-account-20261005.json.

Bukti publik: audits/strategy-preparation-20261005.json, analyzer offline
scripts/analyze_bounded_preparation.py; snapshot privat SHA256
142695584d7636cbfd94da6e5eb36844d489130bbf2006fa0e0c9d4a5164b0f7.
Keterbatasan kontrol konkret: izin baru saat ini memerlukan restart; jalur
rearm resmi yang memuat hanya otorisasi sesi saat flat tanpa menghapus buffer
akan menghindari warm-up tambahan pada sesi yang kelak diizinkan. Ini belum
diimplementasikan, bukan izin untuk sesi lanjutan pada tugas ini.

Dokumentasi resmi diperiksa5Oktober: [Lighter limits](https://apidocs.lighter.xyz/docs/rate-limits)
dan [official trade export](https://apidocs.lighter.xyz/reference/export).
