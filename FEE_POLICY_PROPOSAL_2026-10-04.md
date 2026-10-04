# Kebijakan fee untuk satu probe — disetujui dan diterapkan

Pengguna memilih alternatif presisi ekspor resmi pada 4 Oktober 2026 dan
mengizinkan satu percobaan entry baru. Izin itu sudah dipakai oleh sesi
`d980862d-fda4-4876-97c0-b5d6f82b950c`; hasilnya ada di
[audit probe proteksi](AUDIT_PROTECTION_PROBE_2026-10-04.md).
Tidak ada izin entry berikutnya atau rearm otomatis. Order kini dinonaktifkan.

Sumber resmi `GET /api/v1/export`, akun 751745, BTC, 3 Oktober, aggregate=false
memuat trade 33141524453 dan 33141644233. ID, waktu, harga, ukuran, sisi dan
role Taker cocok dengan fill/intent. Kolom Fee masing-masing 0.0000 USDC.
Ekspor menampilkan empat desimal; fee individual dari endpoint trades tetap
absen, dan schema transaksi tidak mendokumentasikan makna field ringkasnya.
Ini bukti fee yang dilaporkan per trade, bukan bukti presisi mikro atau dasar
untuk mengubah fee NULL menjadi nol. Audit kas pada enam desimal cocok.

Default konfigurasi tetap ketat (`probe_accept_reported_fees=false`). Fee exact
tetap unknown, ekspor ditampilkan terpisah, pencarian ulang otomatis dibatasi,
dan entry dengan fee exact pending tetap diblokir tanpa pengecualian eksplisit.

Alternatif yang disetujui: izinkan bukti fee ekspor resmi pada presisi yang tersedia **khusus
preflight satu probe**, hanya jika seluruh fill unknown mempunyai bukti ekspor
dengan ID/ekonomi yang cocok, akun flat/order bersih, audit kas/dana segar
cocok tanpa selisih, dan semua guard lain lolos. Ledger exact tetap NULL;
dashboard/audit menyebut reported precision, tidak mengklaim fee mikro nol.
Entry strategi tetap membutuhkan fee exact; START_LIVE tetap dilarang.
Flag ini hanya sah pada scope `probe_only`. Bukti memuat Trade ID, client ID,
tx hash, timestamp exchange, sisi, ukuran, harga, akun, market, role, nilai,
presisi, dan SHA-256 CSV resmi. Setiap fill unknown harus cocok dengan ledger.
Audit kas terikat pada fingerprint fill/dana, equity, dan UUID sesi; rekonsiliasi
lengkap harus segar. Fingerprint dan equity dibekukan saat preflight dan
diperiksa kembali sebelum signing. Perubahan sebelum entry membatalkan izin.

Konsekuensi alternatif: metadata biaya tersedia pada presisi laporan resmi,
tetapi rincian fee mikro tidak bisa diaudit. Perubahan fill/dana atau selisih
kas membatalkan kelayakan bukti tersebut; tidak ada rearm entry otomatis.
Sesudah entry terisi, fee/audit pending hanya menghalangi entry selanjutnya.
Proteksi, close reduce-only, cancel, timer, dan pemulihan tetap berjalan.
Penyelesaian operasional membutuhkan flat, seluruh order bersih, dan audit kas
segar cocok; metadata fee exact tetap pending sampai ada sumber yang sah.
Probe selesai dengan empat fee exact NULL (dua historis, dua baru), empat
laporan resmi 0.0000 pada empat desimal, dan selisih net kas 0.000000 USDC.
