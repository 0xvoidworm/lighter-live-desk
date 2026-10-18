# Audit pemetaan socket produksi — 7 Oktober 2026

**Keputusan: diterima.** Empat handshake produksi mempunyai metadata dari transport
yang benar-benar tersambung, dan masing-masing cocok unik dengan dua pengamatan OS.
Ini menutup kekurangan pemetaan channel/socket untuk **koneksi baru ini**. Peer public
pada eksperimen lama tetap **unknown**; bukti baru tidak diterapkan retrospektif.
Tidak ada trading, pemuatan izin, rearm, perubahan strategi atau kebijakan reconnect.

## Baseline dan implementasi

- Repo awal bersih di `ffcadd6`; runtime terverifikasi `8d785cb`, PID391484.
  AGENTS.md, RUNBOOK.md, AUDIT_DEPTH_COMPARATOR_2026-10-06.md, hook transport,
  market/account dan helper pembanding diperiksa sebelum perubahan.
- Kode yang dimuat: **`776bec4`**, PID **405247**, run ID
  `6e870e45fd1a4a198f65eb06a9b684a5`. Hash24 source produksi dan konfigurasi
  disimpan dalam summary; hash identik sebelum/sesudah restart dan pemeriksaan akhir.
- Library terpasang: aiohttp3.14.3 (`_writer.transport`, termasuk proxy hook lama),
  websockets16.1.1 (`transport`). Tidak mengambil peer dari DNS atau urutan socket.
- Fungsi bersama mengambil sockname/peername, family, FD dan inode dari transport
  tersebut. SO_COOKIE tidak diekspos Python terpasang: **null**, alasan eksplisit;
  tidak menebak nilai atau memasang opsi socket. Wall/monotonic, PID/run/connection
  ID dan channel menyertai event handshake yang sudah ada.
- Capture sekali per connection ID. Reconnect/rotasi account memperoleh ID baru.
  Kegagalan metadata nonfatal, hasil null dengan alasan berdasarkan jenis exception;
  exception text, auth/header/payload serta userinfo/query/fragment URL tidak dicatat.
  Logger queue512, kesehatan/fail-closed clock dan hook penerimaan tetap sama.
- Tuple lengkap/alamat lokal hanya di bukti privat. API/status tidak menampilkan
  metadata socket; menampilkan ketersediaan/durasi capture. Tidak ada perubahan UI.
  Helper pembanding memakai hook bersama untuk penggunaan mendatang; tidak dijalankan.

Python dipakai untuk integrasi dengan transport/library produksi yang sama,
tanpa SDK/signer pada utilitas diagnosis. Subscription depth-only `/public`,
aggTrade `/market`, resolver, TLS, heartbeat, backoff, proof/guard, threshold8bps,
buffer5bps, fee policy dan seluruh batas risiko/request dipertahankan.

## Pengujian sebelum rollout

- Regresi terarah: **38 passed**, dua peringatan SDK deprecation lama.
- Suite produksi: **529 passed**, tiga peringatan deprecation lama,101,93s.
- Ruff: **All checks passed**; mypy: **24 source files**, tanpa masalah;
  `git diff --check` bersih. UI tidak berubah.
- Tes baru memakai server localhost dan transport nyata kedua library: empat channel
  serentak, pesan pertama tetap diterima utuh, pengikatan socket tidak tertukar,
  rotasi/cancellation pada `LighterAccount.stream` produksi dengan auth palsu,
  penggunaan ulang FD/inode baru, capture lama tidak tertimpa, socket tertutup,
  metadata gagal/null, pencocokan OS ambigu, IPv4/IPv6 procfs dan queue penuh.
  Marker kredensial tidak muncul pada trace/status. Tidak ada constructor signer,
  broadcast atau koneksi exchange dalam tes baru.
- Suite juga menjalankan regresi assessment keputusan produksi, sumber terlambat
  diterapkan untuk sequence tetapi tidak menambah coverage, serta source unknown
  yang tidak menahan stop/timer/close. Ini bukti pengujian offline.

## Satu rollout dan smoke test

Sebelum restart: state akun segar/complete, Standard/Unified akun751745/BTC market1,
saldo/collateral49,430756USDC, posisi global/order/pending/intent nol, kas matched,
riwayat lengkap, jurnal sehat, orderOFF, entries_stopped dan sesi closed.
Tidak ada command/pending activation atau izin yang dimuat.

Satu `systemctl --user restart lighter-live-desk-engine.service`, waktu mulai unit
**00:04:29WIB**. API tidak direstart. `NRestarts=0` sesudah rollout; PID baru tetap
405247. Smoke mencakup startup: **00:04:29,829536–00:07:29,829695WIB**,
180,000159s (deadline180s; bookkeeping melewati159µs),36 snapshot status lokal.
Tidak ada polling exchange, socket pembanding, ping tambahan atau reconnect paksa.

Suffix connection ID di bawah terikat run ID di atas. Semua **AF_INET**;
FD/inode bukan identitas historis mandiri. Kedua pengamatan OS (~45s/~150s)
memakai PID, tuple/family, inode/FD dan lifetime connection ID yang masih aktif;
hasil masing-masing unik. Tuple lokal dan raw procfs tetap privat.

| Channel | Connection suffix | Peer aktual | FD | Inode | OS cocok |
|---|---:|---|---:|---:|---|
| Lighter market (`lighter`) | :1 | 3.168.203.20:443 |22|1778353|2/2|
| Lighter account | :2 | 3.168.203.15:443 |14|1778354|2/2|
| Binance public depth | :3 | 13.112.27.226:443 |20|1778359|2/2|
| Binance market aggTrade | :4 | 18.179.166.158:443 |21|1778360|2/2|

Bootstrap: dua snapshot applied, satu bridge U/u/pu dan satu receipt/source-valid
tercatat. Tidak ada close/reconnect selama smoke. Metadata mengambil
0,094113–0,138769ms/handshake; total **0,449153ms**, tambahan JSON metadata1071byte
untuk empat event yang sudah ada, tanpa disk I/O pada hook.
Logger154event ditulis, **drop0/error0**, puncak queue3/512, umur queue maksimum
8,672ms; write maksimum2,933ms. Hook total164,926ms mencakup seluruh instrumentasi
lama+baru, bukan biaya incremental metadata. Lag timer maksimum36,861ms;
tidak membuktikan sebab host/jaringan atau atribusi kepada logger.

Engine akhir **IDLE**, tetapi kesiapan teknis **false** karena warm-up:
coverage178,1/600s,175/480sampel, sisa421,9s, gap maksimum1,074s, long gap0.
Coverage dan basis_scale belum lolos; tidak diperpanjang untuk menunggu readiness.
Pada akhir smoke, guard receipt/source/FX, akun/audit, reserve dan flat-clean lolos.
Snapshot blocker overlap dipertahankan sebagai hitungan snapshot, bukan dijumlahkan
menjadi durasi. Kesiapan setelah warm-up tidak diukur pada tugas ini.

## Penutupan, bukti dan batas

Pemeriksaan akhir terpisah mengonfirmasi PID/hash/config tetap, **IDLE**,
49,430756USDC, posisi/order/intent nol, pending activation0, orderOFF,
entries_stopped, loaded permissionnull, sesi closed. Kas matched,
selisih/perubahan kas **0,000000USDC**. Empat fee exact historis tetapNULL;
bukti reported terpisah dan kebijakannya tidak diubah. Tidak mengklaim exact fee nol.
Counter persisten tidak berubah: intents6, fills4, commands21, activation_requests1.
Row aktivasi lama berstatus terminalACTIVATED tetap dipertahankan, sesi kanonik
closed; itu bukan permintaan pending atau otorisasi yang dapat dipakai kembali.

Raw, tuple lokal, procfs, status privat dan log tes:
`~/Workspace/outputs/lighter-socket-peer-rollout-20261006` (nama folder
dibuat sebelum pergantian tanggal; waktu rollout WIB7Oktober).26file dihash;
prefix gzip recorder aktif disegel, EOF yang diharapkan dinyatakan eksplisit,
154row JSON lengkap juga disimpan/di-hash. Ringkasan aman:
[summary](audits/socket-peer-20261007/summary.json),
[manifest](audits/socket-peer-20261007/manifest.json).
SHA256 manifest: `5183fe567d82944b163c7462401f7d7445dd2b5f2a2f4fc23a4e6f939c7f1564`.
Manifest eksperimen lama tetap SHA256
`e00499cd8531326a8e84bfd1242ecbab934699d165ed2acb1b273d9f51ed5fad`.

Tidak terjadi disconnect/rotasi saat runtime; recovery, cancellation dan FDreuse
dibuktikan offline. Tidak menyimpulkan reconnect/keterlambatan lama selesai, strategi
menguntungkan, atau mesin siap live setelah smoke pendek ini.

**Satu langkah berikutnya:** pada analisis/pembandingan depth berikutnya,
gunakan binding connection ID→peer aktual ini sebagai faktor pembeda jalur,
bersama identitas update dan waktu; jangan mengulangi atribusi memakai daftar DNS
atau socket yang belum terikat channel. Eksperimen/trading baru tetap memerlukan
tugas/otorisasi tersendiri; tidak ada aktivasi lanjutan otomatis.
