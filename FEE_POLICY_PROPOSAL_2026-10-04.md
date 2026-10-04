# Usulan kebijakan fee untuk probe — belum diterapkan

Sumber resmi `GET /api/v1/export`, akun 751745, BTC, 3 Oktober, aggregate=false
memuat trade 33141524453 dan 33141644233. ID, waktu, harga, ukuran, sisi dan
role Taker cocok dengan fill/intent. Kolom Fee masing-masing 0.0000 USDC.
Ekspor menampilkan empat desimal; fee individual dari endpoint trades tetap
absen, dan schema transaksi tidak mendokumentasikan makna field ringkasnya.
Ini bukti fee yang dilaporkan per trade, bukan bukti presisi mikro atau dasar
untuk mengubah fee NULL menjadi nol. Audit kas pada enam desimal cocok.

Default: pertahankan guard ketat. Fee exact tetap unknown, ekspor ditampilkan
terpisah, pencarian ulang otomatis dibatasi, dan probe tetap diblokir.

Alternatif: izinkan bukti fee ekspor resmi pada presisi yang tersedia **khusus
preflight satu probe**, hanya jika seluruh fill unknown mempunyai bukti ekspor
dengan ID/ekonomi yang cocok, akun flat/order bersih, audit kas/dana segar
cocok tanpa selisih, dan semua guard lain lolos. Ledger exact tetap NULL;
dashboard/audit menyebut reported precision, tidak mengklaim fee mikro nol.
Entry strategi tetap membutuhkan fee exact; START_LIVE tetap dilarang.

Konsekuensi alternatif: metadata biaya tersedia pada presisi laporan resmi,
tetapi rincian fee mikro tidak bisa diaudit. Perubahan fill/dana atau selisih
kas membatalkan kelayakan bukti tersebut; tidak ada rearm entry otomatis.
Ini perubahan kebijakan guard, sehingga menunggu pilihan pengguna. Tidak ada
izin entry/broadcast baru melalui usulan atau pilihan kebijakan ini; izin
order tetap false dan probe_used tetap 1.
