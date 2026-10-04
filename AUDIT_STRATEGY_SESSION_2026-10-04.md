# Audit satu sesi strategi mainnet terbatas — 4 Oktober 2026

Sesi `f4645c06-b55d-45f4-b642-66b5e8c23864` selesai secara operasional,
tanpa percobaan entry maupun broadcast order. Otorisasi sudah ditutup;
START_LIVE tidak boleh diulang menggunakan sesi atau sisa kuota ini.

## Waktu dan hasil

- Aktivasi: **21:30:23,527826 WIB** (14:30:23,527826 UTC).
- Deadline semula: 22:00:23,527826 WIB; maksimal tiga percobaan entry.
- Entry dihentikan permanen: **21:53:12,791424 WIB**, alasan
  `rekonsiliasi gagal: TimeoutError`.
- Cleanup terverifikasi dan otorisasi ditutup: **21:53:18,790612 WIB**.
  Durasi sesi **1.375,262786 detik / 22 menit 55,263 detik**.
- Percobaan entry **0/3**, fill baru **0**, posisi selesai **0**.
  Tidak ada sesi pengganti, entry probe/manual, deposit, atau autostart baru.

Sebanyak **1.351 evaluasi** tersimpan sekitar satu kali per detik: nol sisi
valid dan nol episode sinyal valid. Penolakan: `DEVIASI_KURANG` 728,
`BIAYA_DAN_BUFFER_MELEBIHI_PELUANG` 728, `COVERAGE_DATA_KURANG` 568,
`DATA_TIDAK_VALID` 55. Dua alasan pertama muncul bersamaan, sehingga hitungan
alasan tidak boleh dijumlahkan sebagai jumlah sinyal berbeda. Residual maksimum
absolut dengan basis valid pada evaluasi tersimpan **2,945796 bps**, di bawah
threshold minimum tetap **8 bps**. Tidak ada per-transaksi yang perlu dilaporkan:
harga fill/slippage, penerimaan stop, durasi posisi, dan exit baru tidak tersedia.

## Preflight dan parameter tetap

Preflight segar 21:30:23,484596 WIB lolos: akun **751745**, key **4**,
Standard/Unified, BTC cross, initial margin fraction 100%, equity/available
49,430756 USDC, seluruh posisi/order nol, tanpa intent ambigu. Audit kas dan
dana cocok, empat fee NULL memiliki ekspor resmi yang cocok, kedua feed valid.
Coverage 630,0 detik, 581 sampel dalam 600 detik, gap maksimum 1,815 detik;
syarat 600 detik / 480 sampel / gap 5 detik tetap berlaku. Request terpakai 32,
tersisa 23 slot transaksi saat aktivasi.

Ukuran minimum yang dihitung ulang **0,00012 BTC**; metadata minimum quote
10 USDC, minimum base 0,00007 BTC, langkah ukuran 0,00001 dan harga 0,1.
Preview BUY: quote 85.156,5, limit IOC 85.173,6, notional maksimum 10,220832
USDC, risiko rencana 0,0378170784. SELL: quote 85.156,4, limit 85.139,3,
notional 10,216716, risiko 0,0378018492. Preview tidak dikirim sebagai entry.

Rumus sinyal, normalisasi Binance BTCUSDT / USDCUSDT, median/MAD,
threshold, filter orderflow/depth, serta aturan stop/exit tercatat dalam
[SESSION_POLICY_2026-10-04.md](SESSION_POLICY_2026-10-04.md).
Parameter dan semua 14 file sumber engine tetap selama sesi; hash tersimpan.
Batas 25 USDC, exposure 1× equity, risiko rencana 0,50, loss harian 5,
IOC entry 2 bps, stop 35 bps, deadline proteksi 3/8 detik dan posisi 180 detik
tidak dilonggarkan. Kuota dicatat sebelum signing, deadline persisten,
restart menghentikan entry, dan hanya satu intent/posisi diperbolehkan.

## Gangguan yang terbukti

Gap sampel **10,532 detik** membuat coverage gagal antara 21:35:17,246930
dan 21:45:06,489695 WIB, sekitar **589,243 detik**. Snapshot menunjukkan
quote Binance melewati 1.500 ms meski frame channel masih diterima;
buffer tidak dihapus dan syarat coverage tidak diturunkan. Saat sesi masih
diotorisasi, Binance public reconnect dua kali (`ConnectionError`), sedangkan
Binance market dan Lighter masing-masing nol. Pemakaian lokal normal maksimum
40/60 detik, dengan cadangan transaksi minimum 15 dari cap 55; read darurat
cap 47 tetap terpisah.

`safe_reconcile` dibatasi delapan detik. Timeout pada 21:53:12 menghentikan
entry untuk sisa sesi. Pembacaan lengkap kemudian pulih, dan dalam sekitar
enam detik cleanup/audit terverifikasi. Bukti ini tidak menentukan endpoint
yang lambat atau membuktikan ISP/router/exchange sebagai penyebab.
Reconnect tambahan dan gap setelah otorisasi ditutup dipisahkan dari statistik
sesi; diperlukan diagnosis transport lebih terarah sebelum mempertimbangkan
sesi lain, tanpa mengubah guard atau menambah dana.

## Kas, fee, dan keadaan akhir

Saldo awal/akhir **49,430756 USDC**; perubahan kas sesi **0,000000**,
PnL kas sesi **0,000000**, selisih audit **0,000000** pada enam desimal.
Riwayat dana tetap satu deposit 49,430000 USDC. Gross historis +0,000756
USDC berasal dari probe terdahulu, bukan hasil sesi strategi ini.

Empat fee exact historis tetap **NULL**. Ekspor resmi yang diambil
21:06:12 WIB melaporkan **0,0000 USDC per fill, empat desimal**;
identitas, sisi, jumlah, harga, nilai, waktu, role dan transaksi dicocokkan.
Nilai reported, presisi, sumber, waktu pengambilan dan SHA disimpan terpisah.
Tidak ada fill atau fee transaksi baru; fee exact historis tidak diubah menjadi
nol. Fee pending hanya memblokir entry, tidak proteksi/cancel/close/timer.
Kebijakan ekspor sesi telah dimatikan setelah penyelesaian.

Pembacaan independen SDK resmi **22:01:41,754543 WIB** memverifikasi akun,
seluruh order semua market, posisi dan dana: flat, nol order/posisi lain,
equity/available 49,430756, audit cocok. Engine dihentikan sesudah cleanup dan
jendela request lama dibiarkan habis sebelum tujuh request baca tersebut;
inisialisasi SDK terpisah, tanpa signing order atau broadcast.

Setelah sesi selesai ditemukan status WARMING_UP dapat menimpa IDLE karena
coverage feed. Perbaikan **sesudah sesi ditutup** memisahkan status operasional
closed/disarmed/flat dari kesiapan feed, dengan snapshot global segar, jurnal,
rekonsiliasi dan semua guard akun tetap wajib. Snapshot stale/order aktif tetap
menghalangi IDLE; guard START_LIVE/probe tidak dilonggarkan. Hash perubahan
sesudah sesi ditandai terpisah dari kode yang benar-benar menjalankan sesi.
Restart terkontrol dan browser membuktikan **IDLE**, entry berhenti,
izin order **false**, sesi **closed**, tombol live/probe nonaktif. Kedua service
tetap disabled untuk autostart; pemantauan akun tetap berjalan.

Pengumpul status lokal terhenti saat payload STARTING belum memuat akun ketika
restart **sesudah cleanup**. Hal ini tidak terjadi selama sesi diotorisasi;
data sesi tetap lengkap. Pemeriksaan SDK independen dan pembaca status akhir
yang menangani payload startup kosong melengkapi bukti IDLE, tanpa entry ulang.

## Pengujian dan bukti

Sebelum aktivasi: **281 tes lulus**; setelah regresi status akhir: **282 tes
lulus**, Ruff/check format, mypy 14 file dan build UI lulus. Pemeriksaan
signer SDK resmi lokal tanpa broadcast termasuk dalam suite; ini tidak
menjadi bukti transaksi mainnet baru. Regresi mencakup bukti fee salah/hilang,
selisih kas/dana, partial fill dan NULL baru, stop ambigu, restart tanpa
proteksi, kuota sebelum signer, intent tunggal, deadline monotonic, late fill,
serta penghentian kuota meski fee/audit belum lengkap.

- [Audit JSON](audits/strategy-session-20261004.json): waktu, counter persisten,
  preflight, metadata, hash sumber, event, rekonsiliasi SDK dan keadaan akhir.
- [Evaluasi strategi CSV](audits/strategy-session-20261004/strategy-evaluations.csv):
  sinyal/reason, threshold, quote, coverage dan budget per evaluasi.
- [Ekspor resmi](audits/strategy-session-20261004/official-export-f786875c5e15705556b3d2b2ba10f9344e3f1c8aeb4c206692d01c8c1226d663.csv).
- [Tes terakhir](audits/strategy-session-20261004/pytest-after-session.txt).

Sesi membuktikan evaluasi mainnet, guard penolakan dan penghentian aman pada
timeout saat flat. Tidak ada bukti fill/stop/close baru dari sesi ini; bukti
probe sebelumnya tetap di audit probe. Tidak ada klaim stop pasti tereksekusi
atau strategi menguntungkan. Sesi baru membutuhkan izin baru dan preflight baru.
