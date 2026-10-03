# Progress

## Terbaru — 3 Oktober 2026, entry terisi dan close darurat selesai

Izin baru satu entry digunakan setelah audit percobaan lama, regresi signer
native tanpa jaringan, 193 tes, dan preflight READY lolos. Entry client 2 BUY
0,00012 BTC terisi 84.539,0. API trade tidak memberi fee, sehingga rekonsiliasi
berhenti sebelum stop dipasang. Lifecycle proteksi gagal; close darurat client 3
reduce-only terisi penuh pada 84.543,8. Account/order REST sesudahnya membuktikan
posisi dan seluruh order nol, ledger kas cocok, saldo 49,430576 USDC, PnL kas
+0,000576 dan biaya net agregat nol. Field fee per trade API tetap tidak tersedia;
provenance fee ledger adalah audit kas, bukan fee API. Durasi posisi 200,257
detik melampaui rencana 180; clock exchange/lokal berbeda sekitar 6h33m dan
keduanya dipertahankan di audit.

Commit fa95fb9 memverifikasi signing IOC/stop/cancel/close sesungguhnya dengan
key sementara tanpa broadcast dan mencatat quote close. Commit 285727b memastikan
fee hilang tidak menahan pengurangan exposure: snapshot posisi segar dipakai
untuk close darurat sekali dalam flight, audit tetap gagal, fee nol tidak diisi
otomatis. 195 tes/lint/format/mypy/SQLite lulus. Izin order dimatikan, probe_used
tetap 1, probe_pending 0. IDLE terverifikasi 23:34:59 WIB setelah restart/warm-up:
PID 86819, saldo tetap, posisi/order/proteksi/exit nol, umur full audit dana
11 detik, kedua feed valid, error/reconnect proses akhir nol. Tidak ada entry
kedua, START_LIVE atau deposit tambahan. Stop diterima exchange masih belum
dibuktikan; tidak ada kesimpulan strategi profit.

Detail: AUDIT_PROBE_2026-10-03_SECOND.md dan audits/probe-20261003-second.json.

## Terbaru — 3 Oktober 2026, percobaan probe ditolak sebelum broadcast

Pengguna mengizinkan satu percobaan entry dengan proteksi/cancel/close, melarang
START_LIVE/strategi dan deposit tambahan. Scope backend probe_only menolak
strategi bahkan saat izin order aktif. Account-check aktual cocok: akun 751745,
Standard/Unified, key 4, margin Cross 1x (0/100.00), 49,43 USDC, posisi/order/
aset lain nol dan satu inflow 49,43. Sisa budget sumber diperbaiki menjadi nol;
50 USDT sumber telah dipakai termasuk biaya. Riwayat timeout dianalisis menjadi
tiga episode dengan waktu audit akun pulih yang terdokumentasi, tanpa menebak
ISP/router/exchange. Detail: AUDIT_PROBE_2026-10-03.md.

Perbaikan lifecycle meliputi konfirmasi stop sebelum close normal, close
darurat bila stop gagal/ambigu, delapan slot transaksi yang dilindungi dari
GET urgent (pagu 40/47/55), polling probe dua detik dengan admission kapasitas,
pending persisten sampai full audit, dan race fill sesudah snapshot flat.
189 tes/build/lint/format/mypy lulus sebelum percobaan; commit 91ffbb3.

Preflight runtime lulus setelah warm-up 600 detik dan cadangan cukup. Satu
perintah PROBE_ONCE diterima 21:29:14 WIB, client ID lokal 1, BUY 0,00012 BTC,
ask acuan 84.541,6 dan limit IOC 84.558,6 (notional 10,147032 USD). Signer
menolak OrderExpiry is invalid sebelum sendTx: adapter memakai default -1
untuk IOC, yang membutuhkan 0. Diagnosis lokal mereproduksi jalur error
SDK pinned; signing expiry 0 valid tetapi tidak dibroadcast. Query tambahan
accountTxs ForbiddenException tidak dipakai sebagai bukti absensi transaksi.

Tidak ada transaksi/order/trade ID bursa atau fill. Intent UNKNOWN diaudit
menjadi REJECTED berdasarkan bukti penolakan sebelum pengiriman, tanpa reset
probe_used dan tanpa entry kedua. Engine menyelesaikan audit dana lengkap serta
cleanup pada 21:38:45 WIB. Fee/PnL transaksi nol; equity/available tetap
49,43/49,430000 USDC. IOC entry/exit kini mengirim expiry 0 dan penolakan lokal
yang definitif mempunyai jalur REJECTED; hasil network ambigu tetap UNKNOWN.
190 tes lulus setelah fix; Ruff/format, mypy dan whitespace lulus.

Izin order dimatikan, engine restart 21:38:49 WIB. IDLE teramati 21:49:37 dan
21:51:23 WIB; engine/API PID 80858/50780 aktif, NRestarts=0, autostart disabled.
Posisi/order/proteksi/exit nol, probe_used=1, probe_pending=0, entry berhenti,
saldo tetap. Dua feed valid; error rekonsiliasi/setup/jurnal dan reconnect nol
pada proses akhir. UI IDLE, budget sumber $0, tombol live/probe nonaktif.

Siklus mainnet belum dibuktikan: entry/fill, stop diterima/terpicu, close
reduce-only dan slippage aktual belum terjadi. Tidak ada kesimpulan profit
strategi. Batas satu percobaan yang ditolak menghentikan retry otomatis;
percobaan berikutnya membutuhkan izin baru. Bukti tanpa key/token:
audits/probe-20261003.json dan Workspace/outputs/lighter-mainnet-probe-20261003.

## Terbaru — 3 Oktober 2026, laporan lanjutan pukul 20:20 WIB

Pemeriksaan read-only 2026-10-03 20:20:46 WIB: engine masih PID 63783 dan
API PID 50780; kedua unit aktif, NRestarts=0, autostart disabled. State IDLE,
equity/available 49,43 USDC, posisi BTC/order nol, entry strategi berhenti,
probe belum dipakai dan allow_live_orders=false. Margin tetap 0/100.00.
Quote Binance 92 ms dan Lighter 5 ms valid; umur akun 5.8 detik,
audit dana 5 detik, backlog 0. Ada blocker kuota entry pada
sampel ini; probe harus menunggu kuota dan seluruh preflight lulus.

Temuan baru untuk periode panjang sejak restart 02:37:46 WIB: log memuat
130 kegagalan rekonsiliasi, terdiri dari 129 TimeoutError dan satu
ClientConnectorDNSError. Rentang error 03:53:16–06:49:28 WIB; tidak ada
error rekonsiliasi yang lebih baru dalam query sampai 20:20 WIB. Counter
reconnect Binance 95 dan Lighter 42; kedua stream Binance kini aktif dan
quote Lighter segar. Setup akun/jurnal error nol. Penyebab dasar timeout,
DNS dan reconnect belum dipastikan; jangan menyimpulkan ISP/router/Binance
atau menyatakan sambungan stabil sepanjang periode ini.

Bukti nol error/reconnect pada catatan sebelumnya tetap benar hanya untuk
interval uji awal 02:48:06–02:51:01 WIB. Mesin pulih menjadi IDLE saat
pemeriksaan terbaru, tetapi kestabilan jaringan jangka panjang belum tuntas.
Tidak ada kode/config/akun yang diubah, restart dilakukan, atau order dikirim
pada follow-up ini. 179 tes dan hasil build sebelumnya berlaku pada versi
kode yang sama; tidak dijalankan ulang untuk perubahan dokumentasi.

Pendanaan sumber 50 USDT sudah terpakai: biaya withdrawal 0,40, 49,60 USDT
terkirim lalu 49,43 USDC masuk perpetual. Selisih tahap konversi belum
diuraikan biaya/spread. Field funding_budget_remaining 0,570000 menghitung
sisa terhadap inflow Lighter, bukan sisa budget sumber untuk deposit baru.

Bukti sanitasi:
`/home/yuri/Workspace/outputs/lighter-probe-only-20261003/followup-runtime.json`.
Satu probe nyata beserta fill/stop/close/cleanup/ledger dan validasi strategi
tetap belum dilakukan. Semua pemeriksaan pada follow-up ini read-only.

## Terbaru — 3 Oktober 2026, probe terpisah dari strategi dan IDLE terverifikasi

Masalah sebelumnya: Probe memerlukan START_LIVE sehingga entry strategi bisa
terbuka sebelum uji tunggal. Jalur baru READY → PROBING menjadwalkan tepat satu
entry minimum legal, tetap menghentikan entry strategi, menolak probe ganda dan
START_LIVE selama probe. Perintah Stop/Cancel/Close dapat membatalkan entry yang
masih antre; semua guard diperiksa ulang sebelum signer mengirim order.
Status probe yang belum selesai bertahan saat restart dan tidak diputar ulang.
Selesai probe tidak membuka strategi atau menambah hitungan posisi strategi.

Preflight memeriksa kuota entry dan rekonsiliasi yang sedang berjalan. Selama
eksposur probe, empat GET berprioritas exit membaca akun/order/fill memakai
riwayat dana lengkap yang sebelumnya terverifikasi. Ini tidak memperbarui
timestamp audit dana atau baseline PnL. Probe baru boleh selesai setelah akun
flat, stop/order bersih dan audit dana lengkap baru berhasil. Pagu tetap
40 request normal dan 55 dengan cadangan exit per 60 detik.

179 tes lulus, termasuk signer-wait/cancellation, restart dengan hasil belum
pasti, cleanup stop dan profil kuota nyata pada adapter mock. Ruff, format
19 file, mypy 12 source file, config/SQLite, TypeScript/Vite dan whitespace
diff lulus. Kode Python/TypeScript dan SDK resmi tetap mengikuti toolchain
proyek; tidak ada migrasi atau perubahan pin SDK.

Engine versi akhir direstart 02:37:46 WIB, PID 63783; API tetap PID 50780.
Kedua unit aktif, autostart disabled dan NRestarts=0. Setelah warm-up 600
detik, IDLE sehat teramati 02:48:06–02:51:01 WIB (174.75 detik).
140 sampel: equity tetap 49,43 USDC, posisi/order nol, entry strategi
berhenti dan probe belum dipakai. Quote invalid nol; quote Binance maksimum
520 ms, Lighter 277 ms, backlog nol dan reconnect nol.

Ada 1 sampel akun stale 15.1 detik saat WARMING_UP; guard tetap
memblokir sampai data segar. Sepanjang interval IDLE sehat, umur akun maksimum
13.7 detik dan audit dana lengkap 12.7 detik, di bawah batas 15 detik.
Log PID akhir menunjukkan nol error rekonsiliasi/setup akun/jurnal. DOM
dashboard IDLE; tombol live dan probe nonaktif karena allow_live_orders=false.

Preview perhitungan tanpa order: BUY BTC 0,00012, IOC limit 84.187,5,
nilai maksimum entry 10,1025 USD (pagu config 25 USD). Harga perlu disegarkan
saat pelaksanaan. Tidak ada order mainnet yang dikirim. Bukti tanpa key/token:
`/home/yuri/Workspace/outputs/lighter-probe-only-20261003/validation.json`
dan `/home/yuri/Workspace/outputs/lighter-probe-only-20261003/runtime.json`.

Sisa pekerjaan: instruksi jelas pengguna untuk satu probe mainnet, lalu
verifikasi fill, stop, close, pembatalan sisa order dan ledger sampai flat.
Strategi masih HEURISTIC_UNVALIDATED; kesiapan monitoring bukan validasi
profit atau bukti transaksi nyata. Setelah uji tunggal, izin order harus
dimatikan lagi dan IDLE diverifikasi.

## Terbaru — 3 Oktober 2026, margin cocok dan IDLE terverifikasi

Pengguna mengklik Confirm pada draft BTC Cross 1x di UI resmi. Account-check
lulus dan membaca margin_mode 0, initial_margin_fraction 100.00, tier 0 dan
trading mode 1 untuk akun 751745. UI menampilkan Cross 1x. Posisi/order/aset
lain tetap nol, equity/available 49,43 USDC dan gross inflow 49,43 USDC.
Nilai expected margin lokal diisi dari respons nyata: 0 dan "100.00".
allow_live_orders tetap false; tidak ada order yang dikirim oleh Codex.

Config/SQLite lulus. Engine direstart pada 00:15:10 WIB, PID 57096;
API tetap PID 50780. Kedua unit aktif, autostart disabled, NRestarts=0.
Warm-up 600 detik selesai dan blocker margin hilang. IDLE sehat teramati
00:25:24–00:27:56 WIB (152,45 detik), lalu pemeriksaan tambahan masih IDLE.
130 sampel: seluruh posisi/order nol dan margin cocok; snapshot akun
maksimum 13,9 detik, quote Binance 651 ms dan Lighter 167 ms; nol sampel
quote invalid, nol kegagalan fetch, nol reconnect setelah restart.
Backlog pasar sesaat maksimum 143 (ambang engine tetap 1.500), dan nol
sepanjang interval IDLE sehat yang diperiksa. Log PID engine menunjukkan
nol error rekonsiliasi/setup akun/jurnal. DOM dashboard menampilkan IDLE,
49,43 USDC dan tombol live/probe nonaktif.

Kode implementasi/guard tidak diubah pada tahap ini; perubahan hanya config
lokal dan dokumentasi. git diff --check lulus. Bukti sanitasi tanpa key/token:
`/home/yuri/Workspace/outputs/lighter-preflight-20261003/confirmed-runtime.json`.

Masih ada pekerjaan sebelum probe mainnet pertama: jalur PROBE_ONCE saat ini
memerlukan LIVE dan UI mengharuskan START_LIVE lebih dulu. Perintah itu juga
membuka entry strategi; dua klik terpisah dapat membuka entry strategi sebelum
probe. Buat dan uji jalur probe atomik yang tidak membuka entry strategi sebelum
izin order diaktifkan. Catatan RUNBOOK diperbarui agar alur itu tidak dipakai
untuk uji satu transaksi. Order/fill, stop, close dan ledger mainnet tetap
belum diuji; strategi tetap HEURISTIC_UNVALIDATED.

## Terbaru — 3 Oktober 2026, pemeriksaan margin BTC

Account-check kembali lulus untuk akun Standard 751745, trading mode 1,
equity/available 49,43 USDC, posisi/order/aset lain nol, satu inflow historis
49,43 USDC. Respons account dengan active_only=false masih positions=[];
margin_mode dan initial_margin_fraction BTC belum tersedia.

UI resmi BTC menampilkan Cross 20x. Pemeriksaan kode publik UI membuktikan
selector leverage memakai default_initial_margin_fraction market jika
baris akun belum ada, dan selector margin juga memiliki fallback market.
Jadi tampilan tersebut bukan bukti setting akun BTC yang sudah tersimpan.
Confirm pada modal leverage mengirim transaksi UpdateLeverage ke Lighter.
Draft Cross 1x telah diisi di UI (input 1.0, range 1, Confirm aktif), tetapi
belum dikonfirmasi; menunggu instruksi jelas pengguna untuk perubahan akun
eksternal. expected margin tetap kosong dan allow_live_orders tetap false.

Market-check lulus: BTC market 1/perp, min base 0,00007, min quote $10,
5 size decimals, 1 price decimal, taker fee 0. Tidak ada order dikirim.

Pemantauan menemukan PAUSED/COVERAGE_DATA_KURANG dan satu reconnect Binance.
Setelah tersambung kembali, status WARMING_UP menunggu coverage pulih.
20 sampel berikutnya menunjukkan kedua quote valid, stream tersambung,
saldo tetap, posisi/order nol. Status IDLE pada laporan 2 Oktober adalah
bukti pada interval tersebut, bukan jaminan status terus-menerus.
Tidak ada guard freshness/coverage yang dilonggarkan atau kode yang diubah.

Bukti pemeriksaan tanpa key/token:
`/home/yuri/Workspace/outputs/lighter-preflight-20261003/check.json`.

## Terbaru — 2 Oktober 2026, idle terverifikasi

Pembacaan nyata satu rekonsiliasi flat menggunakan 8 REST request dalam
1,09 detik. Interval lama 10 detik dapat melebihi pagu baca lokal 40/min;
interval diubah menjadi 12 detik tanpa menaikkan pagu/cadangan exit atau
melonggarkan batas freshness akun 15 detik.

Status IDLE diperkenalkan untuk pemantauan dengan allow_live_orders=false,
entry berhenti, sesi tidak aktif, akun flat tanpa order/intent tertunda,
akun dan data pasar segar, rekonsiliasi berhasil, jurnal sehat, warm-up
selesai dan tanpa flag insiden. Setting margin BTC yang tidak dikembalikan
API saat flat tetap menghalangi live; nilainya tidak ditebak/dibypass.
Rekonsiliasi gagal, snapshot basi, perubahan posisi/order/tier/mode akun,
masalah jurnal/data atau flag audit mengeluarkan mesin dari IDLE.

96 tes lulus; Ruff lint/format, mypy, config/SQLite dan whitespace diff
lulus. Pengujian mencakup profil request selama 10 menit waktu simulasi,
cadangan exit, penolakan live/order dari IDLE, serta kondisi insiden dan
staleness. Kode/SDK Python proyek tetap dipertahankan sesuai toolchain.

Unit engine/API direstart: PID 50779/50780, aktif dengan autostart tetap
disabled dan NRestarts=0. Warm-up 600 detik selesai; IDLE teramati sejak
23:10:00 WIB dan bertahan sekurangnya 120 detik sampai 23:12:00 WIB.
80 sampel runtime: snapshot akun maksimum 13,8 detik (batas tetap 15),
quote Binance maksimum 975 ms dan Lighter 147 ms (batas tetap 1.500),
kedua stream Binance aktif, antrean data/order nol dan posisi BTC nol.
Saldo tetap 49,43 USDC; entry berhenti, allow_live_orders=false.

Pemeriksaan setelah pemantauan masih IDLE dengan account age 11,1 detik
dan event-loop lag 2,1 ms. Log PID baru menunjukkan nol error rekonsiliasi,
setup akun dan jurnal. DOM browser menampilkan IDLE/saldo $49,43 dan tombol
mulai live/probe nonaktif. Capture screenshot browser timeout; verifikasi
tampilan memakai DOM dan tidak ada gambar yang dijadikan bukti.

Data pemantauan dan hasil akhir (tanpa key/token):
`/home/yuri/Workspace/outputs/lighter-idle-20261002/runtime.json`.
Tidak ada order atau perubahan leverage/margin akun dikirim. Mesin dibiarkan
nyala memantau sesuai asumsi idle; live masih memerlukan verifikasi margin
BTC serta instruksi pengguna berikutnya.

## Terbaru — 2 Oktober 2026, API terverifikasi dan akun tersambung

Deposit UI resmi berstatus Completed pada 18:20:09 WIB: 49,43 USDC untuk
perpetuals, berasal dari budget sumber 50 USDT. Penarikan Binance via Arbitrum
One mengirim 49,60 USDT setelah biaya 0,40 USDT; selisih berikutnya belum
diuraikan menjadi biaya/spread konversi. Pengguna melakukan konfirmasi dan
autentikasi sendiri.

Pengguna mendaftarkan key indeks 4 dan menyimpan API private key di editor
lokal. SDK resmi memverifikasi kecocokan key dengan account index 751745;
private key tidak ditampilkan dalam konteks model, chat, maupun log.

Account-check terautentikasi lulus (exit 0): account_type 0,
account_trading_mode 1, equity 49.43, available 49.430000, posisi BTC 0,
order aktif 0, posisi/aset lain 0. Satu deposit completed USDC tercatat;
gross_history_inflow 49.430000, withdrawal dan transfer kosong. Hasil cocok
dengan UI Standard/Unified dan deposit sebelumnya. BTC_margin_mode serta
BTC_initial_margin_fraction masih None; jangan menebak nilainya saat flat.

Pemeriksaan nyata menemukan halaman terakhir withdrawal kosong dengan
cursor yang mengulang cursor permintaan. Adapter sebelumnya menolaknya
sebagai funding pagination loop. Perbaikan menerima hanya halaman kosong
dengan cursor yang sama; halaman kosong dengan cursor maju tetap dibaca,
loop nonempty, siklus cursor, respons gagal dan batas lima halaman tetap
ditolak. Enam kasus regresi gagal sebelum patch; setelah patch, seluruh
76 tes lulus (3 warning deprecation dependency yang sudah ada). Ruff lint/
format dan mypy lulus. Implementasi tetap memakai SDK Python proyek yang
sudah dipin; tidak ada implementasi signing baru.

config.json memakai account_index 751745, expected_account_type_code 0,
expected_account_trading_mode 1, mode live_capable, dan tetap
allow_live_orders=false. Kedua file lokal berizin 0600 dan diabaikan Git;
config/SQLite serta diff whitespace check lulus.

Runner development engine/API lama dihentikan dan diganti unit systemd user
yang memakai konfigurasi baru; unit aktif, autostart tetap disabled.
Engine PID 47938, API PID 47939. Browser dashboard menampilkan equity dan
saldo tersedia $49,43 dengan posisi BTC 0. State PAUSED, entries_stopped=true,
tidak ada intent tertunda. Tidak ada order atau transaksi uji dikirim.

Penghalang preflight: setting margin BTC belum terverifikasi, warm-up belum
lengkap saat pemeriksaan, serta refresh akun sempat berumur 16,2–19,0 detik
sehingga snapshot akun basi/rekonsiliasi belum segar memblokir live. Refresh
berikutnya pulih ke umur 1,2 detik. Penyebab jeda refresh belum didiagnosis;
verifikasi dan bereskan stabilitas rekonsiliasi sebelum mengaktifkan live.

## 1 Oktober 2026, perbaikan lokal setelah audit

Dashboard berjalan lewat `http://lighter.localhost:8765` dalam mode `observe`.
Layanan engine/API user aktif; autostart tetap disabled. Nama lokal mengarah ke
loopback. Konfigurasi laptop memilih `binance_dns_mode=doh`; resolver hanya
menangani dua host feed publik Binance, dengan hostname/TLS tetap diverifikasi.
REST, depth Futures dan aggTrade kini tersambung bersama feed Lighter.

Sembilan temuan audit direproduksi ulang pada sumber terbaru dengan mock yang
disesuaikan, lalu diperbaiki. Uji feed nyata menemukan dua masalah tambahan:
pemeriksaan U Futures setelah bridge yang keliru dan backlog dari status SQLite
pada setiap event. Keduanya diperbaiki, dengan regresi untuk urutan pu serta
burst 2.000 event. Engine memproses event secara berurutan dalam batch terbatas
dan menulis status maksimal empat kali per detik. UI menandai quote basi tidak
valid. CSRF diperoleh saat pengguna mengirim perintah, tanpa retry POST otomatis.

Validasi akhir: 55 tes lulus, Ruff lint/format dan mypy lulus, build TypeScript/
Vite lulus, config/SQLite serta pemeriksaan whitespace diff lulus. Browser
menampilkan harga kedua venue, engine terhubung, tanpa error/warning console.
Pemantauan 25 sampel sekitar 25 detik sesudah patch throughput: backlog/gap nol,
kedua stream Binance tersambung, umur maksimum Binance 89 ms dan Lighter 282 ms.
Snapshot browser berikutnya juga segar (15 ms/2 ms). Tidak ada kredensial dibaca,
API akun nyata dipanggil, atau order dikirim. Warm-up 600 detik tidak dilewati;
integrasi akun, proteksi dan fill mainnet tetap belum diverifikasi.

Pemeriksaan akhir 00:18:50 WIB: warm-up sudah selesai, state `READY` dalam mode
`observe`, entry tetap berhenti, akun belum tersambung. Kedua stream Binance
tersambung, age Binance/Lighter 53/36 ms, backlog/gap/reconnect nol. Engine PID
83706 dan API PID 82658 aktif, masing-masing NRestarts otomatis nol.

Rincian perubahan, batas bukti, dan rollback: `LOCAL_FIX_2026-09-30.md`.
Laporan ekspor: `/home/yuri/Workspace/outputs/lighter-live-desk-fix-20260930/LAPORAN.md`.

## Riwayat 30 September sebelum perbaikan ini

2026-09-30: Local Fedora repository initialized. Backend, engine, public feeds, account/order adapter, SQLite journal/migrations/backup, risk gates, strategy, local API, React dashboard, user service units and runbook implemented. `uv` Python 3.12 and npm lockfiles pinned. Public Lighter BTC metadata verified again: market ID 1, min base 0.00007, min quote $10, 5 size decimals, 1 price decimal, taker fee 0. Lighter book and Binance Spot USDCUSDT quote observed live. Binance USD-M REST/WS currently fail at TCP before TLS/HTTP; therefore paired feed and strategy on actual comparator data cannot be verified. See `AUDIT_2026-09-30.md` for exact network evidence. User systemd units are disabled/inactive. A separate development engine/API/Vite runner already exists in observe mode; a second engine was refused by the single-instance lock and the attempted unit was stopped without disturbing that runner.

Audit lanjutan memperbaiki rute `aggTrade` Binance ke `/market`, bridge snapshot Binance, rekonsiliasi trade/fill dari riwayat REST resmi, pemeriksaan akun dan fee trade, transfer dalam ledger pendanaan, serta pemeriksaan margin akun/BTC sebelum live. Perubahan arus dana setelah sesi live memicu flag persisten dan blok entry. Tes deterministik saat ini 28 lulus; Ruff, mypy, frontend TypeScript/Vite build, config/SQLite check lulus. Account response, order acknowledgement/fill, perlindungan stop, fee tier, margin, dan kelengkapan riwayat dana masih memerlukan akun nyata. Tidak ada kredensial dibaca, API akun tidak dipanggil, tidak ada pendanaan/order, dan mainnet fill **belum diuji**.

Next required user-side step: dapatkan akses sah ke endpoint Binance USD-M melalui jaringan biasa yang tersedia bagi laptop ini, lalu siapkan akun dedicated Lighter Standard, API key lokal dan collateral dalam pagu sumber US$50 menurut RUNBOOK. Engine tetap observe sampai pemeriksaan itu lulus. Setelah tersedia, cocokkan API akun/fee/margin/riwayat arus dana dengan UI resmi, lalu satu probe terawasi dan maksimal satu sesi strategi 30 menit/3 posisi.

Pemeriksaan setelah restart aplikasi: runner development baru aktif, dashboard/API HTTP 200; engine `observe`, `entries_stopped=true`, kedua rute Binance masih terputus. Saran pengguna tentang Binance Desk diperiksa read-only: API key-nya dikelola KWallet aplikasi lain dan tidak diperlukan untuk feed publik; konektor publik proyek itu memakai candle REST dengan fallback DNS tersendiri, bukan depth/trade WebSocket. Uji URL resmi dari network namespace engine masih gagal sebelum handshake. Bukti tambahan ada di `AUDIT_2026-09-30.md`; tidak ada perubahan rute jaringan atau pengaktifan trading.
