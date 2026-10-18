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
SDK terpasang memakai `Configuration().retries=None`; RetryClient hanya
dibuat jika retries dikonfigurasi. Tidak ada retry GET tersembunyi yang
diasumsikan gratis. Retry aplikasi harus claim lagi sebelum request.

State2GET tidak membaca dana/trade dua kali. Audit8GET saat ini terdiri dari
account1, global active-orders1, trades setelah checkpoint1, deposit2,
withdraw2, transfer1. Halaman terminal kosong dengan cursor echo tetap
dibaca; belum ada dasar menghapusnya. Dana tetap diaudit lengkap; checkpoint
trade dan deduplikasi ledger dipertahankan. Pagination bertambah tidak
dilabeli selesai atau mengurangi kapasitas proteksi.

TTL5s membatasi umur bukti untuk mulai dispatch, bukan janji fill5s.
Pemeriksaan ulang berada di callback setelah mendapat lock signer; bukti
yang kedaluwarsa saat menunggu lock tidak boleh mulai signing. TTL juga
dibatasi sisa freshness komponen tertua dikurangi margin0,5s. Audit120s
adalah deadline mulai audit saat flat dari penyelesaian sebelumnya, bukan
timestamp state baru; saat audit berjalan, guard reconciliation-idle menahan
entry, dan setiap entry tetap mengaudit lagi.

## Pengujian dan verifikasi

Regresi menguji setiap komponen basi/missing/count/cursor, position-only
read, perubahan dana/checkpoint/config/kas, reconnect/restart/TTL/attempt,
audit gagal/23page, pembatalan lease, biaya8+send2 dan envelope14, audit
lama bukan bukti dispatch, serta exit ketika accounting lock/network macet.
Native signer entry/stop/cancel/close tanpa broadcast tetap di suite.
Tes legacy umur riwayat>15s diperbarui ke audit-required, bukan menghapus
pengujian data akun basi. Dashboard dibangun dengan toolchain/lockfile lama.
Python3.12 dipakai untuk integrasi engine/SDK yang sudah ada; tidak migrasi.

Implementasi dan tes di `9028794`. Seluruh344tes lulus dalam18,37s;
3warning dependency, mypy18source lulus, Ruff/format37file lulus.
Frontend `npm run build` (tsc/vite) lulus1,43s. Analyzer offline tambahan
diverifikasi Ruff/format dan dijalankan pada rekaman nyata; agregatnya
dicocokkan secara independen dengan event SQLite persisten.

## Satu verifikasi runtime, tanpa order

Window **14:20:23.313–14:40:23.314 WIB**, 5 Oktober2026, tepat1200s monotonic
(kelebihan waktu penutupan file0,000945s). PID217981 menjalankan `9028794`;
hash semua18module engine dan konfigurasi tetap sama sepanjang window.
4798snapshot lokal, error kolektor0, gap sampling maksimum0,255111s.
Mesin sudah bootstrap sekitar55s sebelum marker rekaman; warm-up yang
tersisa dan semua gangguan tetap termasuk1200s, tanpa pengulangan.

| Ukuran | Baseline di c10d26f (runtime2db23db) | Sesudah9028794 |
|---|---:|---:|
| GET rata-rata/menit | 35,60 | 13,20 |
| Puncak GET rolling60s | 40 | 24 |
| Puncak lokal consumed+lease | 40 | 26 |
| Umur state akun maksimum | 13,7s | 12,697s |
| Umur global active-orders maksimum | Belum dicatat terpisah | 12,836s |
| Terblokir entry-request reserve | 616,019s | 1,001s |
| Semua guard teknis evaluasi lolos | 277,561s | 465,475s (7m45s) |
| Interval lolos kontinu terpanjang | 6,752s | 120,055s |
| Audit history lengkap berhasil | 89/89 | 11/11 |
| Kapasitas tersisa menuju transaksi55, termasuk lease | ≥15 | ≥29 |

P95 umur account11,952s, orders12,080s; tidak ada sampel komponen≥15s.
History-age P95 115,194s/max121,132s: history lama tidak dilabeli baru ketika
dua GET state selesai.11audit mencakup10audit berkala dan1preflight
diagnostik, masing-masing8GET;88refresh state masing-masing2GET.
Total264request dimulai dalam window, bobot lokal264, seluruhnya HTTP200.
Retry/timeout/error/429 teramati0; tidak ada lease bocor atau audit terpotong.
Median/max durasi audit1,107/1,178s; state0,246/0,338s. Accounting-lock wait
median0,009/max0,014ms. Phase transport, endpoint, alasan panggil dan
overlap blocker disimpan dalam JSON; tidak ada antrean pool/lock panjang
yang terbukti pada window ini.

Pagu entry evaluasi menghitung audit8 yang belum dijalankan. Satu blocker
reserve1,001s terjadi saat lease preflight diagnostik sendiri dimiliki;
seluruh sampel gagal quota berada di interval itu. Bukan pola audit rutin
yang menghabiskan quota. Left-hold memperpanjang atribusi sesudah finish
sekitar0,079s; ini di bawah ketidakpastian sampling0,255s, bukan bukti
blocker kuota baru. Tidak ada sampel quota-gagal di luar rehearsal.

Durasi blocker individual saling tumpang tindih: coverage717,970s,
basis728,725s, reconciliation-idle11,255s, market-data11,255s, FX10,755s,
reserve1,001s. Warm-up tersisa sekitar545,324s. Tabel kombinasi dalam JSON
mencakup1200s;0,048s pertama belum teramati. Jangan menjumlahkan blocker
individual. Definisi history guard berubah dengan izin eksplisit pengguna,
dan kondisi feed/startup kedua window berbeda; kenaikan kesiapan total
bukan estimasi kausal murni scheduler.

## Preflight lengkap tanpa pengiriman

Command7 `DIAGNOSE_PREFLIGHT`,14:31:53.361 WIB, hanya sekali. Selesai
14:31:54.626 WIB dengan outcome `complete_no_send`,8GET aktual dalam
1,212244s, blocker kosong. Reservasi atomik10slot mencakup read8/send2;
dua slot send tidak digunakan dan dilepas setelah audit. Tidak membuat
intent, signing, transaksi, perubahan session, atau membuka izin order.

Attempt `diagnostic:eb78258a5267419795e6682f06dd10fb`, generation0,
binding `1d580383ef09f61a4c9734ed57e20e2c3ef5fde8bec824a21c3eed412144046b`.
Bukti cocok dengan kas/state/ledger/fee historis, TTL5s, `sendable=false`.
Kesiapan teknis bukti sebelum send teramati sekitar5,002s (resolusi0,255s),
dibedakan dari465,475s siap mengevaluasi. Send authorization selalu false.
Kebutuhan nonce/send2 dan protection envelope14 adalah perhitungan/simulasi
kapasitas saja; durasi signing, broadcast dan fill tidak diukur tugas ini.

## Coverage dan gangguan yang tetap terlihat

Binance public/market dan Lighter reconnect0, overflow/disconnect/error
Recorder0. Tidak ada reset buffer; alasan tetap `engine_start`, stamp asli
dipertahankan. Book Binance maksimum jeda receipt0,504s dan Lighter0,565s.
FX USDCUSDT memiliki jeda update maksimum25,600s. Sekitar14:37:19.864–
14:37:30.619 WIB FX tidak valid karena umur data melewati TTL yang ada;
kedua book tetap segar. Engine tidak menyegarkan FX dengan heartbeat.

Tidak adanya FX membuat sampling basis berhenti; setelah FX pulih, gap
sampel11,303s>5s menahan coverage untuk sisa window. Riwayat valid tidak
dibuang atau diberi timestamp baru. Penyebab eksternal diamnya kanal FX
belum dibuktikan oleh rekaman; reconnect/overflow bukan penyebab teramati.
Guard tetap bekerja, tanpa menaikkan umur data atau melonggarkan coverage.

## Keputusan dan keadaan akhir

Konflik kuota cadence lama selesai **untuk profil akun tenang8GET saat ini**:
dua GET menjaga state<15s, full history tetap lengkap pada barrier yang
diwajibkan, kapasitas membayar full preflight tersedia dan reserve exit
minimal15 dipertahankan. Rehearsal tidak membuktikan broadcast atau exit
mainnet baru. History/pagination panjang, retry dan pekerjaan recovery
lebih padat tetap dapat menahan entry secara sah; tidak menjanjikan semua
permintaan dapat dijalankan serentak tanpa batas.

Pemeriksaan **15:20:13.375 WIB**: account751745/key4, Standard/Unified/Cross,
initial margin100%, equity/available/collateral49,430756USDC; umur account
9,337s dan active-orders9,451s. Seluruh posisi/global active/pending order
dan unresolved intent0. History lengkap, kas matched/difference0 dan
perubahan kas selama tugas0. Intent/fill baru sejak marker0. Empat fee
exact tetapNULL, masing-masing reported0,0000USDC pada4desimal dengan
sumber dan waktu ekspor resmi tersimpan; tidak disamakan dengan exact0.

MesinIDLE, entry stopped, allow_live_orders=false, kedua flag pengecualian
fee false, send authorization false, sesi closed/attempt kosong. Kedua
service aktif untuk observasi tetapi autostart disabled. Tidak ada deposit,
START_LIVE, rearm, dummy order atau transaksi baru.

Tindak lanjut paling didukung bukti: diagnosis kanal kurs USDCUSDT dan
hubungannya dengan gap sampling basis, memakai trace yang ada terlebih
dahulu. Itu penghambat kesiapan yang tersisa setelah quota diperbaiki;
threshold strategi atau klaim profitabilitas tidak dapat ditentukan dari
window ini.

Bukti aman: `audits/split-account-history-runtime-20261005.json`,
`audits/split-account-history-final-account-20261005.json`. Rekaman mentah
private beserta SHA256 manifest berada di
`~/Workspace/outputs/lighter-split-audit-20261005/recording`;
tidak di-commit. Reproduksi agregat offline:

```sh
.venv/bin/python scripts/analyze_split_audit.py \
  ~/Workspace/outputs/lighter-split-audit-20261005/recording \
  audits/split-account-history-runtime-20261005.json \
  --market-directory state/market \
  --baseline audits/audit-scheduler-after-20261005.json
```
