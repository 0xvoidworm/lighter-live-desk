# Diagnosis fee dan coverage READY — 4 Oktober 2026

Melanjutkan `7786369`. Tugas ini hanya perbaikan dan pembacaan akun, tanpa
entry, broadcast order, START_LIVE, perubahan margin, rearm probe, atau deposit.
Izin order tetap false, scope probe_only, probe_used=1 dan probe_pending=0.
Perilaku proteksi yang memisahkan risiko dari pembukuan tetap dipertahankan.

## Fee: bukti resmi tersedia, presisi exact belum tersedia

Trade 33141524453/client 2/order 844421373195178 dan
33141644233/client 3/order 562953474977251 cocok dengan akun 751745, BTC,
ukuran 0,00012, harga 84539,0 dan 84543,8 serta hash transaksi lama.
Pembacaan authenticated `/trades` menggunakan SDK resmi terpasang 1.1.5,
pin `78db23f632de8deca9a232a61bbd3bf6e79fc522`, dilakukan sebelum parsing SDK.
Respons HTTP terbaru menghilangkan kedua key maker_fee/taker_fee. Model Trade
memiliki Optional StrictInt=None dan mempertahankan integer yang disediakan;
dua fixture respons asli teredaksi menguji hal ini. Jadi bukan kehilangan
field oleh parser. Null pada artefak SDK sebelumnya adalah hasil normalisasi,
bukan bukti bahwa body HTTP literal mengirim null.
Referensi: [model SDK pada pin proyek](https://github.com/elliottech/lighter-python/blob/78db23f632de8deca9a232a61bbd3bf6e79fc522/lighter/models/trade.py)
dan [schema OpenAPI resmi pada pin yang sama](https://github.com/elliottech/lighter-python/blob/78db23f632de8deca9a232a61bbd3bf6e79fc522/openapi.json).

Query `/tx?by=hash` untuk kedua transaksi berhasil dan identitas akun/key/order
cocok. event_info mempunyai field ringkas tf/mf, tetapi schema resmi hanya
menjelaskan event_info sebagai string opaque. Tanpa definisi dan unit resmi,
field tersebut tidak dipakai untuk menetapkan fee exact. Signature order tidak
disimpan. Pembacaan ulang beberapa jam setelah transaksi tetap tidak memberi
fee di `/trades`; keterlambatan biasa belum terbukti sebagai penyebab. Kesimpulan
dibatasi pada dua transaksi ini dan endpoint/versi yang diperiksa.

[Ekspor akun resmi](https://apidocs.lighter.xyz/reference/export),
`GET /api/v1/export`, type=trade, aggregate=false, BTC, hari UTC 3 Oktober,
memberi dua baris. Trade ID, tanggal exchange, sisi Open Long/Close Long,
harga, ukuran, nilai trade dan role Taker cocok. **Fee dilaporkan 0.0000 USDC
untuk masing-masing trade, pada empat desimal.** Presisi lebih kecil daripada
0,0001 USDC dan aturan pembulatan tidak dibuktikan. Nilai laporan dipersistenkan
terpisah bersama sumber, identitas dan presisi; fills.fee tetap SQL NULL.
URL download bertanda tangan dan token tidak disimpan. Ini ekspor akun GET,
bukan layanan historicalTrades berbayar.

Audit kas pada presisi enam desimal cocok: 49,430000 → 49,430576 USDC,
perubahan +0,000576; nilai close dikurangi entry = +0,000576. Selisih net
yang belum dijelaskan adalah 0,000000. Funding/deposit tunggal tetap cocok.
Kesamaan kas tidak membuktikan fee individual exact nol. NET_CASH_AUDIT
disimpan terpisah dari OFFICIAL_FEE_REPORT; perubahan fill/dana atau posisi
terbuka membuat referensi kas membutuhkan audit baru.

Pencarian fee lama kini maksimal tiga pembacaan history yang berhasil, paling
cepat sekali per 60 detik; counter persisten melewati restart. Kegagalan
request tidak dianggap hasil fee, unknown tidak dianggap nol, dan pembacaan
transaksi baru/posisi/proteksi tetap berjalan. Setelah tiga hasil tanpa fee,
backfill otomatis berhenti; pembacaan manual bersumber baru masih dapat
mengisi fee yang benar-benar diberikan API.

Guard exact fee tetap aktif. Diagnosis dan bukti laporan fee selesai, tetapi
penghambat entry exact fee **belum dihapus**. Usulan presisi ekspor khusus satu
probe, konsekuensi dan syaratnya ada di FEE_POLICY_PROPOSAL_2026-10-04.md;
belum disetujui atau diterapkan. Audit kas dapat selesai dengan bukti ini;
audit rincian fee mikro tetap pending. Tidak ada pengecualian coverage probe.

## Coverage: bug reconnect terbukti dan diperbaiki

Saat tugas dimulai, engine lama IDLE dan kedua quote/stream valid; kegagalan
coverage pada audit sebelumnya tidak dianggap keadaan sekarang. Kode strategi
memakai monotonic clock, bukan timestamp exchange yang mempunyai offset.
Reconnect mempertahankan sampel; restart proses memulai buffer baru.

Log complete gzip member dari engine 93565 mencatat sembilan gangguan Binance
ConnectionError. Receipt-to-snapshot lokal: 4,262; 4,433; 8,435; 16,450;
30,429; 30,448; 30,510; 30,463; 30,404 detik. Backoff sebelumnya memakai
counter lifetime yang dibagi public dan market, tidak direset setelah sesi
sehat. Pola tersebut cocok dengan jeda 2/4/8/16/30 dari kode lama. Gap >5
detik memblokir coverage sampai keluar dari window 600 detik. Log lama tidak
memuat channel atau detail transport; penyebab gangguan jaringan awal belum
ditetapkan. Member gzip sebelumnya terpotong, tidak dipakai untuk kesimpulan
timeline ini; data asli tidak diubah.

Counter lifetime tetap tersedia sebagai telemetri. Backoff baru mengikuti
kegagalan berurutan per channel dan direset hanya sesudah 30 detik sesi sehat
dengan pesan valid. Disconnect channel market/aggTrade kini mempertahankan
public book; sebelumnya semua disconnect Binance menghapus book sehingga
delta public berikutnya dapat memicu resync kedua feed. Sampling tetap
memerlukan **kedua** channel Binance pulih, book bridged, Lighter valid, quote
segar, FX valid dan antrean di bawah batas. Normal EOF Lighter dicatat sebagai
gangguan dan memakai backoff, bukan reconnect loop tanpa jeda.

Dashboard menampilkan status koneksi tiap channel, umur pesan/quote, durasi,
jumlah sampel, gap, sisa waktu dan alasan buffer/reset/gangguan. Penghalang
probe dan strategi tampil terpisah; keduanya tetap memakai coverage strategi
yang sama dalam desain saat ini: minimal 600 detik, 480 sampel total,
480 sampel valid dalam 600 detik terakhir, tanpa gap antar sampel >5 detik.
Tidak ada persyaratan yang diturunkan agar READY tercapai.

## Verifikasi dan batas bukti

225 tes lulus; 14 regresi diagnosis mencakup respons SDK asli, duration/count
terpisah, gap aging, reconnect per channel, book terpisah, backfill terbatas
dan persisten, kecocokan metadata laporan, audit kas dan guard fee tetap aktif.
Regresi proteksi/signing native SDK tanpa broadcast tetap lulus. Ruff,
format, mypy dan build UI lulus. Browser lokal membuktikan progres/bukti fee
tampil dan tombol probe/START_LIVE nonaktif, tanpa menekan tombol transaksi.

Hasil observasi terbatas, snapshot akun/order akhir, batas request dan bukti
tanpa rahasia disimpan di audits/ready-diagnosis-20261004.json. Artefak lengkap
lokal berada di ~/Workspace/outputs/lighter-ready-diagnosis-20261004.
Observasi 600,4 detik (pagu 660) berakhir 07:15:03 WIB: umur buffer 629,8
detik, 622 sampel total, 592 dalam window 600, gap maksimum 1,061 detik,
tanpa gap >5 dan tanpa reconnect. Coverage lolos; engine IDLE karena izin
entry dimatikan. Request normal engine teramati maksimal 40/60 detik,
cadangan transaksi minimal 15 slot dari 55. Syarat coverage tidak dilonggarkan.

Pembacaan resmi akun dan accountActiveOrders tanpa filter market pada
07:17:28 WIB membuktikan Standard/Unified, BTC Cross 1x, equity/available
49,430576, posisi dan seluruh order/aset lain nol. Intent belum final nol;
fee exact pending dua, reported fee dua, audit net cocok. Snapshot engine
terakhir IDLE, entries_stopped=true, allow_live_orders=false, probe_used=1.
Tidak ada intent atau event dispatch baru selama tugas ini; antrean command
pending juga kosong. Dua GET verifikasi akhir dicatat terpisah dari pemakaian
normal engine; tidak ada transaksi atau signing order baru.

Tidak ada bukti baru bahwa stop pernah diterima atau terpicu di mainnet.
Probe proteksi berikutnya tetap memerlukan penyelesaian kebijakan/data fee,
izin entry baru yang eksplisit, rearm sekali-pakai dan preflight segar; tugas
ini tidak memberikan atau mengaktifkan izin tersebut.
