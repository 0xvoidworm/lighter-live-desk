# Keadaan akun segar dan audit riwayat terpisah — 5 Oktober 2026

Lingkup dari `c10d26f`/`2db23db`: perubahan kebijakan guard disetujui pengguna.
Tidak ada izin order, probe, START_LIVE, rearm, sesi baru atau deposit.
Threshold 8 bps, buffer 5 bps, max notional25, exposure1x, risk0,50,
daily-loss5, deadline proteksi/exit, kebijakan fee exactNULL tidak berubah.

## Kebijakan yang diterapkan

- State minimum2GET: `account` (akun, tier, mode/margin, collateral/equity/
  available, seluruh posisi/aset, total/pending order count, transaction_time)
  dan `accountActiveOrders` tanpa market (seluruh order). Count tidak cocok,
  cursor order yang SDK tidak dapat ikuti, data kosong/basi tidak lulus.
  Timestamp wall/monotonic setiap respons disimpan sendiri. GET posisi
  mempertahankan timestamp order lama; heartbeat tidak menyegarkan data.
- State deadline15s, bound awal2s/high-water + margin0,5s. Audit lengkap pada
  startup/recovery, setiap percobaan entry, setelah posisi/cleanup dan ketika
  dana/state/hint aktivitas berubah. Saat flat tanpa perubahan: **120s**.
  Dua GET mendeteksi perubahan ekonomi/count/marker konservatif; perubahan
  net-zero yang tidak terdeteksi hint dicari pada audit periodik/barrier entry.
  Marker bukan kontrak kontinuitas WS. Tidak memakai WS sebagai otoritas baru.
- Full history tetap seluruh deposit/withdraw/transfer pages, deduplikasi,
  checkpoint trade dan pencocokan kas/fee resmi. Timeout8s, page limit atau
  failure tidak sukses. Audit pending sesudah fill tidak memblokir proteksi,
  timer, cancel, close atau pemulihan posisi. Risk worker tetap di lane sendiri.
- Setiap entry baru melakukan audit lengkap **lagi**, dengan satu reservasi
  audit+send. Saat ini8GET termasuk2GET state + send nonce/submit2 =10slot;
  tidak menambah2GET yang sama. Admission juga menghitung8+envelope14≤47,
  sehingga used≤25 sebelum audit; normal≤40 menyisakan ≥15 menuju55.
  Dua slot send dalam lease juga termasuk envelope14, bukan biaya tambahan
  di atasnya. Urgent recovery tetap≤47 dan transaksi≤55; reserve15 adalah
  kapasitas normal yang boleh dipakai pekerjaan risiko, bukan request fisik.
- Lease campuran tetap bertipe preflight setelah claim: konsumsi tidak
  dihitung dua kali. Request dimulai tidak di-refund; pembatalan melepas sisa.
  Pagination/retry melebihi rencana membatalkan bukti tanpa signing ulang.
  Routine task tidak dijalankan bersamaan dengan preflight; risk worker tidak
  menunggu lock/signature audit. Pemanggil full audit berbagi owner.
- Bukti process-local, boot UUID + generation, satu attempt UUID, akun,
  hash risiko, ledger/checkpoint, state ekonomi, fee reports dan referensi
  kas. TTL monotonic min(5s,15−umur komponen tertua−0,5s); tidak diperpanjang.
  Perubahan binding/gap/reconnect/restart/failure membatalkan. Dispatch
  memeriksa lagi freshness, fee/risiko, sinyal dan otorisasi lalu mengonsumsi
  bukti sebelum signing. Bukti diagnostik selalu `sendable=false`.

`technical_ready` sekarang kesiapan **evaluasi** + kemampuan membayar audit
lengkap yang belum dijalankan. `ready_to_send_technically` memerlukan bukti
audit khusus percobaan yang masih berlaku; itu berbeda dari izin order.
`DIAGNOSE_PREFLIGHT` dijalankan background, tanpa dummy intent/signing/
broadcast. Ia mengalokasikan send2 tetapi tidak mengonsumsi kedua slot itu.

## Perhitungan sebelum runtime

Jadwal2GET setiap kira-kira12–13s: puncak5batch/60s=10read. Satu full audit
8read per120s memberi bound18 dalam rolling window tenang, tidak menghitung
penggantian refresh state oleh audit sebagai diskon. Pada used18, full
preflight8+send2 memberi28≤40 dan envelope18+8+14=40≤47, reserve55−28=27.
Jadi konflik cadence lama5×8=40 vs admission33 dapat diatasi untuk profil8GET.
Ini bound/simulasi kebutuhan pengiriman, bukan bukti broadcast atau exchange
selalu selesai sesuai deadline. Pagination panjang/timeout/retry/aktivitas
lebih banyak tetap bisa menutup admission; guard tidak dilonggarkan.

[Limit resmi Standard](https://apidocs.lighter.xyz/docs/rate-limits) tetap
60 request rolling minute, termasuk sendTx; pagu lokal40/47/55 tetap lebih
ketat. SDK resmi1.1.5 tetap digunakan. Traffic klien lain serta bootstrap
native signer di luar trace rutin tidak dibuktikan oleh metrik GET rekaman.
Entry tetap menunggu coverage≥600s sehingga call bootstrap keluar dari
window60s sebelum entry mungkin. Counter lokal dihitung sebelum await,
termasuk request in-flight/failure dan reservasi yang belum terpakai.

## Pengujian dan verifikasi

Regresi menguji setiap komponen basi/missing/count/cursor, position-only
read, perubahan dana/checkpoint/config/kas, reconnect/restart/TTL/attempt,
audit gagal/23page, pembatalan lease, biaya8+send2 dan envelope14, audit
lama bukan bukti dispatch, serta exit ketika accounting lock/network macet.
Native signer entry/stop/cancel/close tanpa broadcast tetap di suite.
Tes legacy umur riwayat>15s diperbarui ke audit-required, bukan menghapus
pengujian data akun basi. Dashboard dibangun dengan toolchain/lockfile lama.
Python3.12 dipakai untuk integrasi engine/SDK yang sudah ada; tidak migrasi.

Satu verifikasi1200s akan memakai engine dari commit implementasi yang
dibekukan, termasuk warm-up/gangguan, orderOFF dan sesi closed. Kolektor
hanya status lokal; satu rehearsal preflight tanpa pengiriman akan dicatat
secara terpisah dari audit berkala, tanpa membuat intent. Tidak ada sesi
ulang otomatis. Hasil runtime ditambahkan setelah window selesai.
