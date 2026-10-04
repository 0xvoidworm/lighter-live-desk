# Kebijakan satu sesi strategi mainnet terbatas

Pengguna mengizinkan satu aktivasi START_LIVE setelah preflight: maksimum
1800 detik dan tiga percobaan entry, tanpa deposit atau sesi lanjutan otomatis.
Persetujuan reported fee diperluas hanya untuk UUID sesi yang tersimpan.
Hasil eksekusi akan dicatat dalam audit sesi; dokumen ini menjelaskan guard.

## Otorisasi, kuota, dan deadline

`BoundedSession` menyimpan otorisasi, waktu aktivasi/deadline, kuota dan daftar
client ID di SQLite. START_LIVE memerlukan record `authorized` sekali pakai,
scope strategy, config izin live, policy sesi, preflight bersih dan cadangan
request. Aktivasi mengonsumsi otorisasi; START_LIVE kedua ditolak. Tidak ada
otorisasi sesi baru otomatis. Flag probe dan semua riwayat lama dipertahankan.

Sebelum signer, transaksi SQLite atomic mencatat kuota dan state SENT. Signing
ditolak, timeout atau kegagalan sesudah titik ini tetap menghitung percobaan;
guard yang membatalkan sebelum titik ini tidak mengirim order. Satu entry task,
satu intent belum final, dan satu posisi saja diperbolehkan. Dispatch memeriksa
lagi cash/fingerprint yang dibekukan, sinyal, data, margin, loss dan request.

Deadline mulai saat aktivasi. Proses memakai monotonic clock dan juga menolak
wall deadline yang sudah lewat. Reconnect tidak mengubah keduanya. Restart
mempertahankan deadline/kuota, mengakhiri izin entry untuk sisa sesi, dan
memulihkan/menutup exposure. Deadline posisi setelah restart tetap konservatif.

Kuota habis mengakhiri entry begitu posisi terakhir terverifikasi flat/order
bersih, termasuk bila metadata fee pending. Deadline, proteksi gagal/ambigu,
ketidakpastian pengiriman, mismatch exposure/kas, insiden dana atau loss
mengakhiri entry untuk sisa sesi. Ending tetap menjalankan risk worker,
cancel/reconcile kemungkinan late fill, stop, dan close reduce-only.
Monitoring tidak dihentikan sebelum flat/global order bersih dibuktikan.

## Fee dan kas

Default policy sesi false. Bila flag sesi true, semua fee exact NULL harus
mempunyai laporan ekspor resmi yang cocok dengan akun, market, Trade ID,
client/order/tx, timestamp, sisi, ukuran, harga, trade value dan role.
Reported fee, presisi, sumber SHA-256 CSV dan waktu pengambilan disimpan
terpisah. URL ekspor bertanda tangan dan auth token tidak disimpan/log.
Fee exact tetap NULL; tier Standard atau kesamaan kas tidak mengubahnya ke nol.

Aktivasi terikat fingerprint historis dan equity yang telah diaudit. Sebelum
entry berikutnya, posisi/global order/intents harus bersih, full funding
reconcile segar, setiap fee unknown berlapor cocok, serta net kas cocok tanpa
residual. Hanya fill milik sesi dan dana yang tidak berubah boleh memperbarui
binding audit. Perubahan lain tetap memblokir entry atau menghentikan sesi.

Worker audit ekspor hanya berjalan setelah flat, terpisah dari worker risiko
dan signer. Maksimum tiga percobaan ekspor per jumlah fill, berjarak minimal
20 detik dan tunduk pada pagu read 40; kegagalan data menahan entry. Stop,
cancel, close, timer dan pembacaan exposure tidak menunggu fee atau lock audit.
Ending dapat menyelesaikan operasi setelah flat/global order bersih walaupun
metadata/audit biaya masih pending; status kas pending harus dilaporkan jujur,
bukan dianggap cocok. Tidak ada entry lanjut setelah otorisasi ditutup.

Risiko sizing memakai rate taker metadata platform yang divalidasi, bukan
fee fill NULL. Net cash/PnL memakai exact bila tersedia atau reported per fill
yang tervalidasi sesuai presisi yang disetujui; tidak ada fallback NULL→0.

## Strategi dan parameter yang dibekukan

Kode `DeviationStrategy` tidak diubah. Binance BTCUSDT dibandingkan terhadap
Lighter BTC USDC dengan `comparator = Binance.mid / USDCUSDT.mid`.
Raw deviation = `(Lighter.mid / comparator - 1) × 10000` bps. Kurs harus dalam
10 bps dari parity dan segar; kedua book/feed harus valid, quote <1500 ms.

Basis = median sampel deviasi dalam buffer maksimal 1800 detik, mengecualikan
detik terbaru. Scale = median absolute deviation × 1.4826. Residual = raw −
basis; threshold = max(8 bps, 4×scale). BUY bila residual <−threshold, SELL
bila >threshold. Scale nol atau coverage gagal tidak memberi sinyal.
Coverage tetap >=600 detik, >=480 sampel dalam window 600, tanpa gap >5 detik.

Filter tambahan: gerakan pembanding dalam 10 detik tidak melawan sisi >5 bps;
order flow lokal early/late 15 detik dalam window 30 mempunyai >=3 trade
masing-masing, tekanan jual/beli sesuai sisi melemah ke <=70%; depth sisi
pasif pulih >=1.2×minimum 30 detik dengan >=20 sampel; residual mencukupi
spread+5 bps. Sebelum entry, residual juga harus melebihi impact+spread+5.
Tidak ada perubahan threshold atau entry probe/manual untuk sesi ini.

Ukuran selalu minimum legal dengan ruang pembulatan; batas notional 25 USDC,
exposure <=1×equity, rencana risiko <=0.50 USDC, daily loss pause 5 USDC,
exit reserve 2 USDC, sumber funding 50 sudah habis dialokasikan.
IOC entry bound 2 bps. Stop dari harga/ukuran posisi aktual: jarak 35 bps,
stop-limit bound 30 bps, reduce-only. Konfirmasi mencocokkan state exchange,
identitas, sisi, ukuran, trigger dan reduce-only; ACK saja tidak cukup.
Install 3 detik dan konfirmasi 8 detik terpisah dari hold maksimum 180 detik.

Exit normal ketika residual <=2 bps setelah proteksi terverifikasi; deadline
hold meminta close mulai detik 177. Exit juga dipicu akhir sesi/risiko/pemulihan.
Close IOC reduce-only dengan bound bertahap 30/60/100 bps dari book segar dan
ukuran aktual, hanya setelah hasil percobaan sebelumnya direkonsiliasi.
Target risiko/deadline pengiriman bukan jaminan harga atau waktu fill exchange.

Config serta hash kode strategi/risk diikat ke sesi; perubahan membatalkan
entry. Source seluruh engine yang benar-benar dijalankan dicatat pada
preflight dan audit. Toolchain Python 3.12/TypeScript dipertahankan karena
perubahan menyatu dengan engine yang ada dan SDK signer resmi yang dipin.

## Bukti dan batas kesimpulan

Audit merekam observasi sinyal satu detik sekali, alasan penolakan, attempt
persisten, fill/order/tx ID, quote/fill/slippage, stop exchange, exit/cleanup,
durasi berdasarkan clock yang sama, ekspor dan kas. Jumlah sinyal teramati
dibedakan dari percobaan entry dan jumlah posisi selesai. Nol sinyal/transaksi
tetap hasil yang sah. Tidak ada perpanjangan sesi untuk mengejar transaksi.

SDK native entry/stop/cancel/close diuji offline tanpa broadcast. Regresi sesi
meliputi tiga penolakan sebelum sendTx, restart/quota/deadline, perubahan cash
sebelum signer, partial fill dengan fee unknown, stop ambigu dan priority exit,
late fill/cleanup tertahan, fee reported nonnol tanpa mengubah NULL, bukti CSV
tidak cocok, serta kuota habis dengan metadata pending. Sampel paling banyak
tiga percobaan tidak menetapkan profitabilitas strategi atau jaminan stop fill.

Sumber resmi yang diperiksa: [rate limits](https://apidocs.lighter.xyz/docs/rate-limits)
(Standard 60 request/rolling minute; pagu lokal 40 normal/47 urgent read/55
transaksi tetap dipertahankan) dan [trade export](https://apidocs.lighter.xyz/reference/export)
(`aggregate=false`, trade ID/economics terpisah, auth diperlukan).
