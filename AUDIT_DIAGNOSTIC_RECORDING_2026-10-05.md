# Satu rekaman diagnostik tanpa order, 5 Oktober 2026

## Lingkup dan bukti

Dari `ca8c2b6`, instrumentasi pasif `ea46fbfc728efda7ef26a3b4675347814f86fa9a`
dijalankan oleh PID146477. Rekaman **07:49:48.429–08:09:48.430 WIB**
(00:49:48.429–01:09:48.430 UTC), tepat 1.200 detik analisis;
monotonic elapsed1.200,000632 detik. Tidak diperpanjang untuk warm-up,
gangguan, sinyal, atau follow-up. 4.797 snapshot lokal sekitar4Hz,
kesalahan kolektor0, interval maksimum0,257132 detik. Interval awal0,198267
detik belum diamati dicatat unknown, bukan diasumsikan siap.

Kolektor hanya membaca `/api/status` lokal. Statistik memakai quote, depth,
audit akun, trace, dan Recorder yang sudah dikonsumsi engine; tidak menambah
polling exchange. Konfigurasi dan seluruh hash source engine sama dari
awal sampai akhir rekaman. Setelah rekaman selesai, perbaikan pembagian
giliran event dibuat di `b493ac0`; hasil rekaman tidak digabung dengan
verifikasi singkat perbaikan.

Seluruh snapshot: `allow_live_orders=false`, sesi sebelumnya closed,
percobaan entry0, posisi/order/intent0. Tidak ada START_LIVE, rearm, entry,
broadcast, cancel, close, atau deposit. Kebijakan fee dan kontrol proteksi
tidak diubah. Pemeriksaan kesiapan teknis mengabaikan izin yang sengaja
mati, stateIDLE, otorisasi sesi closed, dan keberadaan sinyal; bukti fee
historis tetap divalidasi tanpa mengaktifkan kebijakan entry sesi baru.

Konfigurasi beku: akun751745 Standard/Unified, Cross100%; notional≤$25,
exposure≤1×equity, planned risk≤$0,50, daily loss≤$5, sumber$50 sudah
terpakai, tambahan deposit0; umur quote<1.500ms, umur FX/akun/audit<15s.
Warm-up600s,≥480 sampel valid pada600s terakhir, gap≤5s. Threshold asli
`max(8 bps, 4×scale)`, IOC2bps, stop35bps, hold180s dan deadline proteksi/
exit tetap mengikuti kode sebelum rekaman. Minimum quantity dihitung ulang,
sekitar0,00012BTC/$10,39; tidak menggunakan maksimum$25.

## Kesiapan dan blocker

Durasi adalah estimasi interval snapshot yang menahan status terakhir
hingga snapshot berikutnya; kontinuitas di bawah0,257s tidak dapat dibuktikan.
Seluruh guard teknis lolos **136,324s =2m16,324s (11,36%)**.
Interval lolos kontinu terpanjang **4,253s**.

| Blocker/keadaan | Detik | Penjelasan |
|---|---:|---|
| Cadangan request entry | 795,341 | Terbesar; local used umumnya40, syarat entry≤33 |
| Coverage / basis belum tersedia | 738,606 | 600,540 warm-up awal +138,066 gap setelah warm-up |
| Rekonsiliasi sedang berjalan | 105,311 | Audit lengkap serial; semua berhasil |
| Data pasar tidak valid/segar | 41,517 | Quote Binance invalid/basi41,017s; Lighter awal0,500s |
| FX awal belum segar | 0,750 | Tidak berulang sepanjang rekaman setelah awal |
| Akun/margin/kas/dana/ukuran awal belum siap | 1,250 | Blocker startup saling tumpang tindih |
| Kolektor belum mengamati awal | 0,198 | Tidak dianggap siap |

**Jangan menjumlahkan baris tersebut.** Cadangan request dan coverage
tumpang tindih475,477s; reserve dan rekonsiliasi87,798s. Waktu ketika
reserve menjadi satu-satunya blocker284,340s. Kombinasi lengkap dan setiap
durasi blocker tersedia dalam JSON/CSV; kombinasi menjumlah tepat1.200s.
Sesi closed/izin mati tidak masuk blocker teknis.

Coverage pertama lolos08:02:07.234 WIB, detik738,805 sejak rekaman. Gap
sampel maksimum11,539s; ada tiga gap>5s pada bagian awal. Gap lama harus
keluar dari rolling600s; ini menjelaskan tambahan138s setelah warm-up,
bukan reset buffer. `buffer_reset_reason=engine_start` sepanjang rekaman;
interupsi mencatat141 dan662 sampel dipertahankan. Tidak ada sampel
sintetis. Age quote berasal dari Stamp asli; flush/reconnect tidak membuat
book tertahan terlihat baru.

## Request, cadangan, dan gangguan

93 rekonsiliasi lengkap berhasil, masing-masing8 GET. Ada743 GET yang
mulai dan selesai teramati dalam window, ditambah1 GET akun carry-in yang
mulai90ms sebelum rekaman dan selesai di dalam window. Tidak ada request
mulai dalam window yang masih aktif pada sampel terakhir; mikro-interval
setelah sampel terakhir tidak diekstrapolasi. Semua HTTPattempt1/retry0,
respons200, timeout0, 429=0, bobot lokal1, alasan `account_reconciliation`.

| Endpoint Lighter | Request mulai di window | Median / maks durasi ms |
|---|---:|---:|
| account | 92 (+1 carry-in) | 115,609 /193,250 |
| accountActiveOrders | 93 | 112,154 /184,522 |
| deposit/history | 186 | 126,900 /493,461 |
| withdraw/history | 186 | 122,495 /338,921 |
| transfer/history | 93 | 120,738 /368,993 |
| trades | 93 | 122,556 /426,363 |

Cadence audit12s ditambah kerja transport menghasilkan sekitar12,9s per
siklus. Median durasi audit1.062,824ms, P951.239,783ms, maksimum1.804,770ms.
Wait lock maksimum0,024713ms; antrean pool SDK0. Fase durasi terbesar
menunggu headers; tidak ada fase timeout pada window ini. TCP/TLS digabung
oleh aiohttp, bukan dua pengukuran terpisah. Lag event loop maksimum41,6ms
pada pengamatan yang tersedia, bukan jaminan tidak ada lag di antaranya.

Pagu lokal tetap40 normal /47 urgent-read /55 transaksi per60s.
Used maksimum dan median40; minimum ruang transaksi55−used=15. Urgent
reads dibatasi47 agar8 slot transaksi tetap ada. Entry meminta ruang
normal2 dan urgent-read14, sehingga used harus≤33. **Ruang transaksi15
bukan izin entry:** keduanya merupakan syarat berbeda. Lima audit8request
pada rolling60s memenuhi40 tetapi berulang kali gagal cadangan entry.
Kapasitas dicadangkan belum berarti sudah dikonsumsi. Rate limit Standard
resmi60 request/minute tidak berbobot; Premium memakai skema lain.
[Dokumentasi resmi rate limit](https://apidocs.lighter.xyz/docs/rate-limits).
Konsumsi dari klien/IP lain tidak dapat dilihat oleh trace engine ini.

Deposit: satu halaman berisi1data dan satu terminal kosong. Withdrawal:
halaman awal kosong mengembalikan cursor; halaman kedua kosong mengulang
cursor. Ini bukan retry. Tes `test_empty_advancing_pages_do_not_hide_later_funding`
membuktikan halaman kosong bercursor dapat mendahului dana pada halaman
selanjutnya. [Schema SDK resmi](https://github.com/elliottech/lighter-python/blob/main/docs/TransactionApi.md)
tidak menjamin halaman kosong pertama adalah terminal. Jadi tidak aman
menghapus request kedua atau memperlambat audit tanpa memperhitungkan
syarat freshness15s. Jadwal audit lengkap perlu optimasi berdasarkan biaya
request yang terukur; guard/cursor/cadence tidak dilonggarkan pada tugas ini.

Tambahan request yang **sudah merupakan kerja feed**:3 REST Binance
`/fapi/v1/depth` (200,124,291/136,396/130,339ms),2 handshake reconnect
`/public/stream` (101,346,194/313,695ms), serta115 respons sukses FX
USDCUSDT dari loop normal10s. HTTPX FX tidak mempunyai trace attempt,
durasi, fase, atau failure lengkap;115 adalah sukses yang direkam, bukan
jaminan semua attempt. Startup metadata/key-check berada sebelum window;
trace ini bukan pencacah seluruh traffic laptop.

Dua reconnect Binance public berbeda:

- **07:52:41.560 WIB:** socket close1006, `ClientConnectionResetError`.
  Tidak membuktikan pihak mana yang menutup atau masalah ISP/router/exchange.
- **08:01:31.540 WIB:** Recorder mencatat `queue overflow` pada kedua venue;
  diikuti Binance `GapError`/resync, snapshot baru08:01:34.217 WIB.
  Ini kehilangan kapasitas antrean pasar lokal, **bukan timeout HTTP dan
  bukan antrean pool request SDK**. Binance market/Lighter reconnect0.

Antrean pasar bounded2048. `await emit()` lama memakai `put_nowait`; saat
socket memiliki burst yang sudah dibuffer, await itu tidak wajib memberikan
giliran ke konsumen. Regresi4096 event mereproduksi overflow pada kode lama
walau konsumen siap. Perbaikan memberi giliran `asyncio.sleep(0)` setiap
maksimal64 event (lebih sedikit untuk antrean kecil), tanpa mengganti Stamp,
kapasitas, freshness, request, atau perilaku invalidate/resync saat overflow
benar-benar terjadi. FIFO dan Stamp asli dibuktikan oleh regresi setelah fix.
Tidak tersedia jejak occupancy/OS kontinu untuk memastikan seluruh faktor
burst kejadian asli; pengujian membuktikan jalur starvation dan perbaikannya.
Penyebab eksternal reset pertama tetap belum pasti.

## Residual dan peluang

`raw = (mid Lighter/(mid Binance/USDCUSDT)−1)×10.000`.
Basis median history yang lebih tua dari1s (maks1800s); scale MAD×1,4826.
Residual=raw−basis, bps; negatif calonBUY, positif calonSELL. Tidak memakai
data masa depan. Follow-up di bawah hanya analisis retrospektif.
Threshold8bps merupakan floor heuristik konfigurasi, belum kalibrasi empiris;
tidak diubah. Filter asli tetap termasuk gerak Binance10s, pelemahan flow
lokal, pemulihan depth, serta spread+buffer5bps.

Basis/residual tersedia461,195s; tidak tersedia738,805s. Jangan mengisi
residual unknown dengan0 atau memakai raw warm-up sebagai residual.

| Sampel dengan basis | n /detik | Signed min /median /max bps | Absolute median /P95 /P99 /max bps |
|---|---:|---:|---:|
| Semua | 1844 /461,195 | −1,761901 /+0,004868 /+1,963771 | 0,346398 /0,873617 /1,268327 /1,963771 |
| Guard teknis lolos | 545 /136,324 | −1,761901 /−0,000168 /+1,963771 | 0,359276 /0,927846 /1,390868 /1,963771 |
| Guard teknis terblokir | 1299 /324,871 | −1,332149 /+0,005117 /+1,838835 | 0,340278 /0,845533 /1,176920 /1,838835 |

Abs residual>1bps selama14,006s total (run maksimum1,501s), atau5,253s
pada guard lolos (run maksimum1,000s). >2/5/8bps:0s. Sinyal valid yang
teramati0; tidak mengklaim snapshot4Hz menghitung setiap evaluasi internal
engine20Hz. Raw semua4.631 sampel mencapai−9,929938bps pada07:52:07.194,
tetapi basis belum tersedia/coverage warm-up dan reserve terblokir;
Binance age1.372,493ms vs Lighter50,128ms. Itu bukan peluang residual valid.

Deviasi residual terbesar saat lolos: **08:08:45.440 WIB**, +1,963771bps,
raw+1,953208, basis−0,010563, threshold8; ditolak `DEVIASI_KURANG` dan
`BIAYA_DAN_BUFFER_MELEBIHI_PELUANG`.

| Input serentak keputusan | Binance | Lighter |
|---|---:|---:|
| Bid /ask | 86537,90 /86538,00 USDT | 86542,9 /86546,9 USDC |
| Size bid /ask BTC | 6,707 /1,203 | 0,27793 /0,00231 |
| Spread bps | 0,011556 | 0,462188 |
| Age ms | 497,167 | 19,728 |

FX1,000115 USDT/USDC, age9,185s. Receipt skew477,445ms; kedua book cocok
dengan input keputusan yang sama, tetapi bukan sinkronisasi event exchange
sempurna. Timestamps receipt/exchange/monotonic tersimpan pada JSON.
Ukuran kedua sisi0,00012BTC; notionalBUY10,387716 /SELL10,383060USDC,
planned risk sekitar0,0384USDC; kedalaman cukup, VWAP=BBO dan impact0.
Binance opposite VWAP hanya pembanding ukuran sama, bukan hedge yang
dapat dieksekusi: minimum order Binance tidak divalidasi untuk ukuran ini.

Model terpisah dari residual mid menggunakan dua VWAP Lighter ukuran sama:
entry saat ini, lalu hipotesis book exit bergeser proporsional ke target basis
dengan Binance/FX tetap. Spread/depth kedua sisi sudah masuk VWAP; **tidak
dikurangi spread lagi**. Di antara545 sampel lolos, edge terbaik setelah
dua sisi book1,501280bps; setelah asumsi roundtrip metadata fee0 dan buffer
konfigurasi5bps menjadi **−3,498720bps**, median−4,995822bps. Positif setelah
asumsi0 sampel; tidak ada kandidat strategi valid. Fee metadata0 adalah
asumsi model, bukan fee exact fill. Future exit/reversion tidak dijamin.

Pada puncak tersebut residual+1s=+1,125897,+5s=−0,690034,+10s=−0,064315,
+30s=−0,166930. Deviasi menyeberang dalam5s; sebagian follow-up diblokir
reserve. Tidak membuktikan waktu fill, slippage, atau keuntungan nyata.
Age quote/RTT tidak dianggap latensi fill; tanpa transaksi latensi fill tidak
terukur. Metadata fee exact historis tetap4 NULL; ekspor resmi0,0000 DP4
disimpan terpisah. Tidak ada fee/PnL trade baru; perubahan kas window0.

## Verifikasi, keputusan, dan tindak lanjut

Tes penuh setelah perbaikan **307 passed**,3warning dependency deprecation;
mypy16source, Ruff dan format lulus. Tes terarah90passed. Regresi baru
menguji burst sebelum/sesudah fix, urutan/timestamp, dua VWAP tanpa spread
ganda, dampak depth/fee/buffer, dan exact feeNULL terpisah dari asumsi0.
Tes pagination/proteksi/signing lokal tanpa broadcast tetap lulus.
Python3.12 dipertahankan karena utilitas mengintegrasikan engine/SDK/Decimal
proyek yang ada; tidak melakukan migrasi atau memasang dependency baru.

Penghambat terbesar adalah **penjadwalan audit8request yang memenuhi pagu
normal tetapi sering menutup reserve entry**; warm-up dan gap memperkecil
window lagi. Queue starvation terbukti dapat direproduksi dan sudah diperbaiki.
Tidak ada bukti untuk menurunkan threshold; residual eligible jauh di bawah8
dan model setelah buffer negatif.

**Satu langkah berikutnya:** desain dan uji scheduler audit yang sadar biaya
8request serta freshness15s, dengan worst-case RTT/lock dan reserve exit
tetap utuh. Buktikan secara lokal kelengkapan pagination, audit dana sebelum
entry, tidak ada double polling, serta snapshot segar sebelum observasi
bounded berikutnya. Jangan sekadar menghapus halaman kosong, memperlambat
timer, mengurangi reserve, atau meminta sesi trading untuk mengejar sinyal.

Hasil verifikasi runtime45s dan pemeriksaan akun akhir disimpan terpisah
di `audits/diagnostic-fix-verification-20261005.json`; bukan tambahan durasi
rekaman utama dan bukan bukti bahwa reconnect berikutnya mustahil.

Verifikasi **08:39:54.971–08:40:40.013 WIB**,45,042s termasuk pembacaan
akhir, PID160336 memakai source`b493ac0`:44snapshot,32 GET SDK teramati
berhasil, nol timeout/retry teramati. Binance public mengalami1 reset socket
1006/ClientConnectionResetError; bukan GapError/queue overflow. Channel
lain reconnect0 dan ketiganya tersambung pada akhir pemeriksaan. Coverage
baru44,9s/600s setelah restart, bukan READY dan tidak menunggu warm-up baru
untuk memperpanjang verifikasi. Pengujian burst lokal membuktikan perbaikan
starvation;45s produksi belum membuktikan bahwa semua overload akan hilang.

Akun saat verifikasi: equity/available49,430756USDC, posisi0, seluruh order
exchange0, posisi lain0, unresolved intent0; snapshot age3,1s dan audit
dana2,2s. Kas matched, selisih0, perubahan kas0;4 exact fee tetapNULL.
Entry stopped, izin orderfalse, sesi closed, attempts0, autostartdisabled.
Pemeriksaan terakhir sebelum commit dicatat terpisah pada artefak final.

**08:45:33.334 WIB**, PID160336: stateIDLE, saldo49,430756USDC,
posisi/order/intent0, kas matched tanpa selisih; snapshot/audit segar<15s,
fee exact4NULL dengan semua bukti reported resmi cocok. Izin orderfalse,
entry stopped, probe/session fee-policy flagsfalse, sesi closed/attempts0,
autostartdisabled. Bukti `audits/diagnostic-final-account-20261005.json`
memuat pemeriksaan, sumber/hash kode aktif, reported fee/presisi/sumber/waktu,
dan hasil tes. Penutupan otorisasi sebelumnya tetap berlaku.

## Artefak dan reproduksi offline

- `audits/diagnostic-20m-20261005.json`: konfigurasi/hash, guard/overlap,
  endpoint/fase/pagination, distribusi, paired books/follow-up/timeline.
- `audits/diagnostic-20m-20261005.csv`: interval siap/blocker/residual.
- `audits/market-overflow-evidence-20261005.json`: event overflow/resync asli.
- `audits/diagnostic-fix-verification-20261005.json`: verifikasi45s terpisah.
- `audits/diagnostic-final-account-20261005.json`: keadaan akun/izin akhir.
- Private lokal0700/0600: `~/Workspace/outputs/lighter-diagnostic-20m-20261005/`;
  snapshot gzip dan log tes. Tidak di-commit. Recorder mentah tetap di state.

```bash
.venv/bin/python scripts/analyze_diagnostic_recording.py \
  --source ~/Workspace/outputs/lighter-diagnostic-20m-20261005 \
  --output-prefix audits/diagnostic-20m-20261005
```

Perintah itu hanya analisis offline. Marker `started.json` melarang merekam
ulang ke lokasi yang sama. Dua puluh menit ini tidak cukup untuk menyimpulkan
profitabilitas maupun kegagalan strategi.
