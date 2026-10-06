# Audit aktivasi menunggu kesiapan — 6 Oktober 2026

**Diterima untuk alur kontrol. Tidak ada otorisasi trading dalam tugas ini.**
Kode `777f68e66fa1850cc1c21f7f82dfc20d03dc7ea5` telah dimuat sekali dengan
order OFF dan sesi lama tetap closed. Aktivasi menunggu hanya dibuktikan pada
fixture akun 77 dan transport palsu; tidak dicoba pada akun mainnet.

## Baseline dan kebutuhan

Dibaca: AGENTS.md, RUNBOOK.md, SESSION_POLICY_2026-10-04.md, dan
AUDIT_STRATEGY_PROOF_SELECTION_PREPARATION_2026-10-06.md. Repo awal bersih pada
`8fb448f`; engine awal PID 363683 memakai produksi `24455ff`. API awal PID
255515. Akun aktual diperiksa melalui status engine dan SQLite read-only,
bukan account-check/polling exchange tambahan.

Sesi sebelumnya tidak aktif: satu START_LIVE ditolak guard clock. Kesiapan
pada tampilan sesudahnya tidak membuktikan guard saat command seharusnya lolos.
Kebutuhan yang disetujui adalah satu permintaan menunggu di pemilik state,
bukan pengulangan POST atau penghapusan penolakan tersebut.

## Perilaku yang diimplementasikan

- START_LIVE tetap langsung: gagal guard berarti ditolak, tidak menjadi waiting.
  START_WHEN_READY eksplisit mengikat request/command ID, UUID otorisasi, akun,
  hash parameter, dan expiry persiapan asli. Maksimal satu pending dan satu
  request per otorisasi; duplikat identik mengembalikan record lama.
- QUEUED/WAITING belum aktif: entries stopped, tidak ada intent, signing atau
  order. Deadline aktif dan kuota entry belum berjalan/terpakai. Engine yang
  sama menilai readiness; tidak ada supervisor yang mengulang START_LIVE.
- Guard lokal diperiksa maksimal tiap 250 ms. Audit read-only memakai jalur
  preflight/scheduler/reservasi produksi, hanya saat guard lain lolos, dengan
  jarak retry minimal 30 detik. Tidak ada audit per tick. Biaya nominal satu
  preflight 8 GET + reservasi 2 pengiriman; pagination tetap dihitung dan
  kapasitas tetap dibatasi 40/55 dengan cadangan exit. Cooldown tidak
  memperpanjang TTL lima detik.
- Transaksi SQLite memeriksa ulang binding, command pembatalan, guard aktual,
  izin config dan preflight setelah memperoleh write lock. Perubahan request,
  pemakaian otorisasi, session aktif, command DONE dan evidence aktivasi
  di-commit bersama. Aktif maksimal 1.800 detik sejak titik aktivasi; counter
  tiga percobaan entry tetap mekanisme lama, sebelum signer/pengiriman.
- Audit aktivasi `sendable=false`, terikat request dan attempt audit sendiri.
  Entry selanjutnya tetap melakukan audit/proof baru, TTL maksimal lima detik,
  lalu recheck sinyal, quote, risiko, data dan izin saat dispatch.
- Blocker sementara dikenali boleh menunggu. Identitas/parameter/izin berubah,
  insiden, dan blocker tidak dikenal terminal. STOP/CANCEL/CLOSE, OFF, revoke,
  expiry dan restart membatalkan waiting; restart tidak melanjutkannya.
  Recovery exposure aktif tetap independen dan mengikuti aturan lama.
- Assessment kontrol menyimpan guard yang benar-benar menentukan hasil,
  timestamp wall/monotonic, identitas quote dan proof, interval/umur, serta
  pemeriksaan passed/failed/skipped. Kedua assessment sumber yang memang
  digunakan guard legacy tetap tercatat, tanpa menghitung ulang sebagai alasan
  historis. Pembatalan menyatakan readiness skipped; assessment lama diberi
  peran last_readiness_only. Perubahan alasan waiting dicatat paling cepat
  tiap lima detik; peristiwa terminal/aktivasi selalu dicatat.
- GET `/api/activation/{request_id}` dan command journal menyelesaikan respons
  ambigu dengan ID/UUID sama. Dashboard memisahkan waiting, active, cancelled;
  supervisor menyimpan binding sebelum satu POST. Tidak membuat UUID pengganti.
- Disarm izin mempertahankan edit parameter pengguna pada config disk yang
  valid. Config invalid tidak ditimpa; runtime OFF/otorisasi closed tetap
  mencegah aktivasi kembali.

Tidak ada perubahan pada strategy.py, risk.py, source_time.py, network.py,
config.py, preflight.py, account.py atau market.py dibanding `24455ff`.
Threshold 8 bps, buffer 5 bps, coverage, toleransi clock, TTL, timeout,
heartbeat/backoff, batas risiko dan fee policy tetap. Python mengikuti
integrasi engine/SQLite/FastAPI yang sudah ada, tanpa migrasi toolchain.

## Pengujian offline

Hasil final sebelum pemuatan, log privat beserta hash tersedia:

| Pemeriksaan | Hasil |
|---|---|
| Modul activation request baru | 32 kasus |
| Activation + permission reload + supervisor | 52 passed, 1,39 detik |
| Suite produksi lengkap | 494 passed, 36,18 detik |
| Ruff src/tests/scripts | passed |
| mypy src | 23 source files, tanpa error |
| TypeScript + Vite build UI | passed, Vite 936 ms |
| git diff --check | passed |

Regresi memakai **Engine.data_valid/source_freshness produksi** dengan
ClockEvidence fixture, akun/transport palsu. Boundary terkontrol dapat menolak
pada assessment pertama dan lolos pada assessment langsung berikutnya; kedua
hasil asli tetap tersimpan, keputusan keseluruhan ditolak.

Diuji: tampilan siap lalu clock menolak saat processing; blocker sementara
lalu aktivasi sekali; stale preflight/kapasitas request tertutup; expiry wall
dan monotonic, revoke, OFF, identity/config, incident/HALTED/journal;
STOP/CANCEL/CLOSE; duplikasi/conflict; respons API hilang lalu lookup ID sama;
restart QUEUED/WAITING; crash injection sebelum commit dan setelah commit
sebelum adopsi state; pembatalan masuk di batas transaksi; incident baru pada
callback final; write lock SQLite nyata 120 ms membuat proof kedaluwarsa dan
recheck menolak; fee reported hilang/kas mismatch; pembatalan saat audit lambat
melepaskan reservasi; edit config pengguna dipertahankan.

Tidak ada entry selama waiting. Setelah aktivasi fixture, satu entry **palsu**
menggunakan audit entry baru, bukan proof aktivasi. Suite lengkap juga
mempertahankan regresi proteksi/deadline/close terpisah dari guard entry.
Tiga warning deprecation library tidak menggagalkan pemeriksaan. Tidak ada
broadcast atau koneksi exchange pada pengujian kontrol baru.

## Pemuatan dan smoke runtime

Sebelum pemuatan: akun 751745 Standard/Unified, margin mode 0, saldo
49,430756 USDC; posisi global/active/pending order dan unresolved intent nol;
audit riwayat lengkap, kas matched, IDLE, OFF, entries stopped, sesi closed,
permission loaded NULL. Tidak ada command belum selesai. Backup SQLite
konsisten disimpan privat sebelum restart.

Satu operasi restart terkontrol engine + API melalui unit systemd user pada
**18:03:24 WIB** memuat `777f68e`. Engine PID **378689**, API PID **378690**;
restart selesai dalam 0,205 detik. NRestarts otomatis keduanya nol. Hash source
pada pemuatan dicatat dan diperiksa lagi; config disk tidak berubah.

Smoke lokal: GET status segar; lookup request yang tidak ada HTTP 404 dari
route baru; index UI yang disajikan identik dengan hasil build. Tidak ada POST
kontrol, LOAD_SESSION_PERMISSION, authorize, START_LIVE, START_WHEN_READY,
probe atau rearm pada runtime nyata. Tabel request kosong, command counts dan
empat fill historis tidak berubah. Tidak ada observasi pasar baru 20 menit.

Verifikasi tersimpan **18:06:09,477 WIB**:

- Saldo/collateral **49,430756 USDC**; posisi 0; active/pending/global-other
  order/posisi dan unresolved intent 0. Kas matched, selisih 0,000000 USDC,
  perubahan kas tugas 0,000000; audit riwayat complete.
- **IDLE, order OFF, entries stopped, loaded permission NULL, sesi closed**
  UUID lama `a269dd7f-a216-4f21-b641-5bc909d5ba39`. Tidak ada request pending.
- Umur account 3,151 s / active orders 3,261 s, batas tetap 15 s.
  Bucket Lighter 16/40 normal, reservasi 0, kapasitas transaksi tersisa 39/55;
  cadangan exit tidak dipakai untuk pengujian.
- Warm-up belum selesai: 162,5/600 s, 159/480 sampel; sisa durasi 437,5 s,
  max gap 1,094 s, tanpa long gap pada snapshot. Sumber saat snapshot lolos
  SOURCE_FRESH_WITH_CLOCK_BOUND. Ini bukan bukti READY atau aktivasi mainnet.
- Empat fee exact tetap NULL; masing-masing reported **0,0000 USDC pada empat
  desimal**, bukti official_account_export cocok. Metadata exact belum lengkap
  dan tidak diganti nol berdasarkan saldo atau tier. Tidak ada perluasan izin
  pengecualian fee dalam tugas ini.

## Bukti dan keputusan

Artefak aman: `audits/start-when-ready-control-20261006/checks.json`,
`runtime.json`, `private-evidence-manifest.json`. Raw 18 file, termasuk log tes,
status sebelum/sesudah/final dan backup DB, berada di
`/home/yuri/Workspace/outputs/lighter-start-when-ready-20261006-1802` (direktori
0700, file privat 0600). Manifest privat SHA-256
`4a2ebcf711c4bee0e4633ca463008ff128d078435eff27310286d8f0c792ddf0`.
Raw/state/config/secret tidak masuk Git. Bukti historis tidak diubah.

**Keputusan:** alur kontrol baru diterima berdasarkan regresi dan pemuatan
read-only. Waiting/aktivasi mainnet belum dicoba karena tidak diotorisasi;
kualitas sinyal, eksekusi transaksi, dan profitabilitas tidak dinilai.
Langkah berikutnya hanya bila pengguna memberi otorisasi trading baru:
buat UUID baru melalui mekanisme resmi, LOAD tanpa restart, lalu satu
START_WHEN_READY terikat expiry persiapan asli. Jangan memakai izin lama atau
memperpanjang deadline. Semua guard dan preflight entry tetap wajib.
