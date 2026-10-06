# Satu sesi START_WHEN_READY — 6 Oktober 2026

**Alur kontrol berhasil; sesi berhenti karena insiden rekonsiliasi. Nol transaksi.**
Menunggu, aktivasi atomic sekali dengan guard/preflight aktual, evaluasi strategi,
penghentian entry, audit dan cleanup terverifikasi. Lifecycle fill/stop/close
tidak diuji kembali karena tidak ada kandidat strategi yang memenuhi syarat.

## Baseline dan otorisasi

Dibaca AGENTS.md, RUNBOOK.md, SESSION_POLICY_2026-10-04.md dan
AUDIT_START_WHEN_READY_CONTROL_2026-10-06.md. Repo awal bersih pada audit
`5c462f7`; produksi **`777f68e66fa1850cc1c21f7f82dfc20d03dc7ea5`**, engine
PID **378689**, API PID 378690. Hash source cocok dengan pemuatan sebelumnya
dan tetap sama sampai selesai. Tidak ada edit kode, tuning, build/test ulang,
restart, deposit, probe atau rearm dalam tugas ini.

Sebelum otorisasi, status engine dan SQLite read-only membuktikan akun 751745,
Standard/Unified (tier 0, trading mode 1, margin mode 0), saldo 49,430756 USDC,
flat, seluruh order/intent bersih, jurnal sehat, audit kas/riwayat cocok.
Otorisasi lama closed dan tidak ada activation request yang hidup. Empat fill
exact NULL mempunyai bukti ekspor resmi yang cocok; exact tidak diisi nol.

Satu UUID baru **`713be53b-5d75-48c0-8b75-e5c0558afd1f`** dibuat melalui
BoundedSession.authorize. Hanya flag izin order dan policy reported fee sesi
yang di-stage; parameter strategi/risiko lainnya tetap. Riwayat/flag lama
dipertahankan. Supervisor produksi menyimpan binding sebelum satu POST.

| Peristiwa | WIB |
|---|---|
| Otorisasi persisten | 18:50:25,974316 |
| LOAD_SESSION_PERMISSION, command 20 | sekali, DONE |
| START_WHEN_READY, command 21 | 18:50:27,094282 |
| Aktivasi aktual | 18:50:28,638336 |
| Penghentian entry | 19:08:57,030156 |
| Flat/cleanup/audit, otorisasi closed | 19:09:08,582155 |
| Expiry persiapan asli | 19:10:25,974316 |
| Deadline aktif asli | 19:20:28,638336 |

Request ID **`3d86925e-4835-4f4c-8bf0-6e9402b0166f`** terikat UUID, akun,
parameter hash dan expiry asli. Persiapan **2,664019 s**, waiting sejak request
**1,544053 s**. Aktif sampai entry dihentikan **1.108,391820 s**
(18 menit 28,392 detik), cleanup **11,552 s**. Tidak ada START_LIVE, POST
aktivasi kedua, UUID pengganti atau STOP dari supervisor; engine menghentikan
sesi melalui kebijakan insiden. Deadline/kuota tidak diperpanjang.

## Assessment yang menentukan aktivasi

Journal mencatat QUEUED/WAITING dan kebutuhan preflight request-bound, lalu
satu STRATEGY_SESSION_STARTED/ACTIVATION_CONTROL_DECISION atomic. Waiting
berlangsung terutama saat audit dijalankan. Penantian panjang karena clock
tidak terjadi pada request ini; jangan menganggap pengujian offline sebelumnya
sebagai kejadian runtime sesi ini.

Assessment di dalam transaksi, setelah write lock diperoleh, mencatat semua
guard passed: otorisasi/binding, READY, coverage, kedua pemeriksaan sumber,
akun/order/intent, metadata/sizing, risiko, fee/kas, audit, request reserve dan
insiden. Quote dan timestamp wall/monotonic asli tersimpan. Tidak memakai
assessment tampilan sesudahnya sebagai pembenaran aktivasi terdahulu.

Proof latest/selected pada tiga pemeriksaan sumber adalah
`1791287366412:173048721125026` (latest fallback). Pada data_valid:
umur evidence 62,137037 s dari TTL tetap 120 s; interval offset setelah drift
sekitar **[-90,461; +130,244] ms**, uncertainty **110,353 ms** dari batas
250 ms; interval umur sumber **[57,766; 278,471] ms**, batas tetap 1.500 ms.
Pemeriksaan langsung dan recheck preflight juga passed; nilai/timestamp
masing-masing tersimpan terpisah di summary, bukan dihitung ulang.

Audit aktivasi **8 GET aktual**, reservasi send **2**, durasi **1.351,583 ms**;
sendable=false, broadcast=false. Proof terikat attempt
`activation:3d86925e-4835-4f4c-8bf0-6e9402b0166f:4e5723b92efb4518b16f86851fb63fff`,
TTL 5 s; sisa **4,842896 s** pada assessment akhir. Tidak ada kandidat entry
untuk menjalankan audit dispatch baru. Bukti aktivasi bukan izin dispatch.

## Kesiapan dan evaluasi selama aktif

Supervisor merekam **3.911 snapshot**, 0 error API lokal. Dalam interval aktif
ada 3.862 snapshot diagnostik, gap pengamatan maksimum **0,420484 s**.
Integrasi memakai state diagnostik pada interval ke snapshot berikutnya,
dipotong pada waktu aktivasi/penghentian entry. Ini estimasi dari sampling,
bukan pembuktian guard kontinu di setiap nanodetik atau izin mengirim order.

Denominator aktif **1.108,391820 s**; siap teknis **71,512978 s (6,451958%)**,
interval siap terpanjang **7,389322 s**. Closed/OFF tidak dihitung sebagai
kegagalan teknis. Interval cleanup dihitung terpisah.

| Blocker | Detik dalam interval aktif |
|---|---:|
| basis_scale | 1.034,856996 |
| coverage | 954,898313 |
| source_freshness | 297,484988 |
| market_data | 297,484988 |
| reconciliation_idle | 14,314381 |
| fx_normalization | 1,966768 |

Durasi **bertumpang tindih dan tidak dijumlahkan**. Kelompok overlap terbesar:
coverage+basis_scale 714,248949 s; ditambah market_data+source_freshness
228,031227 s; market_data+source_freshness+basis_scale tanpa coverage
64,106254 s. Seluruh kelompok eksklusif lengkap di summary menjumlah tepat
ke denominator, termasuk 71,512978 s tanpa blocker.

Penolakan sumber diagnostik: proof expired **180,212334 s**, future uncertain
**96,525002 s**, indikator umur sumber melewati guard **20,747652 s**.
Gap sumber menahan coverage; basis_scale sebagian besar blocker turunan ketika
keputusan terakhir belum mempunyai basis/scale karena source/coverage gagal.
Tidak ada bukti kegagalan numerik estimasi basis/scale. Timestamp/gap/buffer
dipertahankan; tidak memakai proof yang baru tersedia kemudian untuk keputusan
historis atau meluluskan sampel yang terlambat.

Request rolling teramati mencapai **18/40 normal**, jauh di bawah 55 transaksi;
blocker request reserve **0 s**. Umur account maksimum **14,223153 s**, active
orders **14,382741 s** (batas 15 s). Binance dan Lighter counter reconnect
sebelum vs terakhir aktif sama; satu reconnect Lighter kumulatif yang tampak
berasal dari sebelum sesi, bukan reconnect baru pada interval ini.

## Peluang dan hasil eksekusi

**3.335 keputusan unik teramati**, 1.905 data_valid; 222 mempunyai residual
basis, **216** mempunyai data_valid plus snapshot teknis cocok. Ini jumlah
yang tertangkap supervisor, bukan klaim counter seluruh invocation engine.
Keputusan yang hanya muncul sebelum aktivasi tidak dimasukkan. Assessment
sumber yang tidak tercatat ditandai demikian, bukan diisi timestamp/proof baru.

Filter keputusan: DATA_TIDAK_VALID 1.430; COVERAGE_DATA_KURANG 1.683;
DEVIASI_KURANG 222; BIAYA_DAN_BUFFER_MELEBIHI_PELUANG 222. Dua filter terakhir
menolak keputusan yang sama dan tidak boleh dijumlah sebagai kandidat berbeda.

Residual eligible bertanda: min **-1,089177**, median **-0,065630**, p95
**+0,749015**, max **+1,507156 bps**. Absolut median **0,478290**, p95
**0,945180**, max **1,507156 bps**, semuanya di bawah threshold minimum
8 bps (threshold efektif tetap max(8, 4×scale)). Sinyal valid teramati **0**, kandidat
preflight entry **0**, peluang gugur selama preflight entry **0**.

Ukuran minimum produksi yang dihitung untuk kedua sisi **0,00012 BTC**,
notional estimasi **10,350024–10,359492 USDC**, tanpa memakai maksimum 25.
Spread Lighter median **0,057940 bps**, p95 **0,814225**, max **1,054540**;
impact ukuran minimum median **0**, max **0,556320 bps**. Batas exposure 1×,
planned risk 0,50, daily loss 5, stop/deadline dan seluruh guard tetap.

Analisis memakai helper Decimal proyek: dua VWAP lokal dengan ukuran sama,
asumsi book exit bergeser paralel menuju basis. Spread/impact sudah dalam
VWAP dan tidak dikurangi lagi. Rate fee metadata saat rekaman 0,0000 bps
adalah **asumsi model**, terpisah dari fee individual NULL. Buffer asumsi
tetap **5 bps**. Estimasi setelah dua book legs/fee metadata/buffer:
median **-4,849379**, p95 **-4,261283**, terbaik **-3,516637 bps**;
estimasi positif **0/216**. Harga exit hipotetis belum tentu executable;
latensi fill, slippage dan biaya eksekusi aktual tidak terukur tanpa fill.

Percobaan entry **0/3**, intent baru **0**, signing order/broadcast **0**, fill **0**,
stop/close baru **0**, posisi selesai **0**. Tidak ada entry probe/manual.
Penerimaan stop, fill, slippage dan close reduce-only bukan bukti baru sesi ini.
Dominan: kesempatan evaluasi terbatas akibat clock/coverage. Pada kesempatan
yang layak, deviasi dan estimasi biaya juga tidak memenuhi aturan. Nol entry
tidak membuktikan strategi gagal, menguntungkan, atau perlu threshold lebih rendah.

## Penghentian, cleanup, dan keadaan akhir

Pada **19:08:57,030 WIB**, audit rutin gagal dengan **ClientConnectorDNSError**.
Trace menunjuk GET **`/api/v1/deposit/history`**, host
**`mainnet.zklighter.elliot.ai`**, alasan account_reconciliation,
last_phase **dns**. Request pertama 6,115 ms, fase DNS 3,824 ms; satu HTTP
attempt lokal, weight lokal 1. Tidak ada bukti respons HTTP diterima atau
kegagalan yang disebabkan kuota. Ini membuktikan fase kegagalan, bukan
menentukan ISP/router/upstream sebagai penyebabnya.

Engine menghentikan entry untuk sisa sesi. Empat audit recovery selanjutnya
gagal pada DNS pada 19:08:59,075; 19:09:01,089; 19:09:03,171;
19:09:05,236 WIB. Rekonsiliasi lalu berhasil; pada **19:09:08,582** flat/global
order/intent dan kas terverifikasi, izin OFF dan sesi closed. Tidak ada posisi
yang perlu close atau order yang perlu cancel; worker risiko/rekonsiliasi
tetap berjalan sampai bukti cleanup ada.

Verifikasi akhir tersimpan **19:32:34,031 WIB**, engine tetap PID 378689:

- Saldo/collateral **49,430756 USDC**, posisi/order aktif/pending/posisi market
  lain dan unresolved intent **nol**. Kas matched, perubahan kas sesi dan
  selisih audit **0,000000 USDC**. Riwayat lengkap; jurnal sehat.
- **IDLE, order OFF, entries stopped, loaded permission NULL, sesi closed**.
  Request tetap **ACTIVATED sebagai riwayat**, pending request nol; UUID sudah
  consumed/closed dan tidak dapat dipakai kembali. Tidak ada otorisasi tersisa.
- Empat fee exact historis tetap **NULL**; setiap ekspor resmi reported
  **0,0000 USDC pada empat desimal**, identity/economy cocok. Tidak ada fee
  transaksi baru; reported nol tidak diklaim exact nol.

## Artefak dan keputusan

Ringkasan aman: `audits/start-when-ready-session-20261006/summary.json` dan
`manifest.json`. **24 file privat** berada di
`/home/yuri/Workspace/outputs/lighter-start-when-ready-session-20261006-185025`
(0700/0600): authorization/binding, supervisor/gzip, journal/evidence,
backup DB, status dan analisis offline. Hash manifest privat SHA-256
`4b84f23432a6b5644470d6cc753c56a6f82fdacd000bdbb1cecea6b32c6b3682`.
Raw/DB/config/key/token tidak masuk Git. Analisis memakai helper Python
existing untuk Decimal dan ekonomi; tidak mengimpor engine/signer atau
membuat request exchange. Log pass analisis awal yang gagal karena field
assessment nullable juga dipertahankan; field itu akhirnya dilabeli tidak
tercatat, bukan diganti bukti baru. Bukti historis tidak diubah.

**Keputusan:** alur START_WHEN_READY diterima berdasarkan bukti runtime
aktivasi tunggal dan penutupan aman ini. Gangguan DNS audit dan clock/coverage
tetap penghambat konkret. Langkah berikut yang didukung bukti adalah diagnosis
read-only dari trace kegagalan DNS/kalibrasi yang telah disimpan, sebelum
mempertimbangkan otorisasi live baru. Tidak ada perubahan strategi atau sesi
pengganti otomatis; sampel ini tidak cukup menilai profitabilitas.
