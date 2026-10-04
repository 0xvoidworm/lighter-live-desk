# Audit satu probe proteksi mainnet — 4 Oktober 2026

Satu percobaan entry yang diizinkan pengguna selesai secara operasional:
broadcast entry → fill → stop aktif terkonfirmasi dari exchange → close
reduce-only → seluruh order bersih → audit net kas cocok. START_LIVE tidak
digunakan dan tidak ada deposit baru. Izin entry sudah habis. Mesin akhir
IDLE, izin order false, posisi dan semua order nol.

Bukti terstruktur: [JSON](audits/protection-probe-20261004.json),
[CSV resmi](audits/protection-probe-20261004-trades.csv), dan
[hasil tes](audits/protection-probe-20261004-tests.txt).
Sesi `d980862d-fda4-4876-97c0-b5d6f82b950c`, akun 751745,
Standard/Unified, key indeks 4, BTC margin mode 0, fraksi awal 100.00.
Riwayat percobaan lama dipertahankan; rearm dicatat PROBE_REARM_AUTHORIZATION.

## Kebijakan dan preflight

Pengguna menyetujui presisi ekspor resmi khusus preflight satu probe.
Default tetap ketat. Flag `probe_accept_reported_fees` hanya sah untuk
`probe_only`; strategi tetap memerlukan metadata exact dan tidak mendapat
pengecualian. Setiap fill unknown harus mempunyai ekspor resmi cocok dengan
ID trade/client, tx hash, waktu exchange, sisi, ukuran, harga, akun, market,
role, nilai, presisi dan digest sumber. Fee exact tetap NULL.

Audit net kas, fingerprint fill/dana dan equity terikat UUID izin. Preflight
membekukan fingerprint/equity lalu memeriksanya lagi tepat sebelum signing.
Perubahan fill/dana/equity, bukti tidak cocok, selisih kas atau audit basi tetap
memblokir entry. Guard ini tidak dijalankan pada proteksi/close/cancel/timer.
Sesudah fill baru, audit fee pending memblokir entry berikutnya saja.

Preflight READY: akun/order flat; dua fee historis berlapor resmi; audit net
kas historis cocok; full funding reconcile berusia 8,8 detik, posisi 8,8 detik;
Binance book/market dan Lighter tersambung tanpa reconnect, quote valid/segar.
Coverage 790,3 detik, 592 sampel dalam 600 detik, gap maksimum 1,065 detik,
tanpa gap >5. Syarat 600 detik/480 sampel/5 detik tidak dilonggarkan.
Budget engine 32/60 detik, 23 slot transaksi tersisa dari pagu 55.

Ukuran minimum dengan ruang pembulatan: BUY 0,00012 BTC, quote 84.794,7,
limit IOC 84.811,7 (2 bps), worst notional 10,177404 USDC, rencana risiko
0,0376563948 USDC. Minimum quote 10, base 0,00007, size 5 desimal, price 1.
Sisi BUY mengikuti mekanisme probe tetap, bukan sinyal strategi baru.
Preview stop memakai limit entry; stop aktual dihitung dari harga fill.
Batas tetap: notional 25, exposure 1× equity, rencana risiko 0,50 per trade,
jeda loss harian 5 USDC. Risiko rencana bukan jaminan harga eksekusi.
Proteksi install 3 detik, konfirmasi 8 detik, hold maksimum 180 detik.

## Bukti exchange

| Tahap | Client / order exchange | Bukti |
| --- | --- | --- |
| Entry | 4 / 844421372216756 | BUY 0,00012 terisi 84.794,7; trade 33170714739 |
| Stop | 5 / 562953476061212 | pending stop-loss SELL, reduce_only=true, remaining 0,00012, filled 0, trigger 84.497,9, limit 84.244,4 |
| Close | 6 / 562953476061280 | SELL reduce-only 0,00012 terisi 84.796,2; trade 33170717917 |

PROTECTION_CONFIRMED menyimpan respons status order exchange segar beserta
snapshot posisi. Pencocokan mencakup client ID, order ID positif, market,
status pending/open, jenis stop, arah, sisa ukuran, trigger dan reduce-only.
Ini melampaui respons sendTx. Close dikirim sesudah konfirmasi tersebut,
tanpa menunggu 180 detik atau sengaja menguji pemicuan stop.

Order entry/close terminal filled; stop terminal canceled-reduce-only dengan
filled_base_amount=0 dan trigger_time=0. Exchange membersihkan stop setelah
posisi ditutup; tidak perlu broadcast cancel. Pembacaan accountActiveOrders
seluruh market membuktikan nol. Tidak ada entry kedua atau blind retry.

## Timeline dan keterbatasan clock

| Peristiwa | Waktu lokal UTC, 4 Oktober |
| --- | --- |
| Entry dispatch | 01:14:49.030178 |
| Fill entry pertama diamati | 01:14:49.547097 |
| Stop dispatch | 01:14:51.206844 |
| Stop exchange terkonfirmasi | 01:14:53.281517 |
| Close dispatch | 01:14:53.340911 |
| Fill close diamati | 01:14:53.836993 |
| Audit kas/order batch selesai | 01:16:35.768982 |
| Operasional probe selesai | 01:16:44.472306 |
| Pemeriksaan akhir SDK | 01:38:34.610494 |

Monotonic exposure-known → proteksi terkonfirmasi **3,731308 detik**;
observasi fill → stop dispatch 1,659746 detik. Ini jendela tanpa proteksi
terkonfirmasi menurut proses lokal, bukan waktu pasti tanpa stop di exchange.
Timestamp placement exchange berpresisi milidetik tidak tersedia dalam bukti
ini. Satu lookup opsional tx stop ditolak HTTP 400 dan tidak diulang.

Timestamp fill exchange 1791055841985 → 1791055846698 memberikan durasi posisi
**4,713 detik**, dihitung dari clock exchange yang sama. Clock exchange dan
laptop berbeda; jangan mencampur timestamp keduanya. Fill lokal → fill lokal
4,289895 detik. Deadline proses memakai monotonic clock; target pengiriman
close tidak menjamin kapan exchange menyelesaikan fill.

Sesudah close, request reserve sempat membuat pembaruan snapshot/ledger
menunggu slot; ini menunda bukti cleanup, bukan penutupan posisi yang sudah
terisi. Pemakaian engine teramati maksimum 48/60 detik termasuk transaksi,
di bawah 55; pagu normal 40 dan pembacaan urgent 47 tetap berlaku. Pembacaan
SDK manual dicatat terpisah, termasuk inisialisasi yang tidak masuk counter
engine; jangan menganggap counter engine mewakili seluruh proses.
Batas Standard resmi: [rate limits](https://apidocs.lighter.xyz/docs/rate-limits).

## Kas dan fee

Saldo sebelum **49,430576**, sesudah **49,430756 USDC**; perubahan kas
**+0,000180**. Gross PnL fill **+0,000180**. Audit net pada presisi kas enam
desimal mempunyai residual **0,000000**, tanpa perubahan dana masuk/keluar.
Entry slippage 0 bps; adverse close slippage dihitung terhadap quote 84.796,3
dalam JSON (fill 84.796,2), jauh di bawah bound 30 bps.

CSV resmi melaporkan **0.0000 USDC per trade pada empat desimal**, termasuk
dua fill baru. SHA-256 CSV:
`f786875c5e15705556b3d2b2ba10f9344e3f1c8aeb4c206692d01c8c1226d663`.
Keempat fee exact tetap SQL NULL. Kas net cocok dan reported fee tersedia
tidak membuktikan fee mikro nol. Metadata fee exact masih belum lengkap;
lookup otomatis berhenti setelah tiga history check sukses berjarak.
Operasional selesai, metadata exact pending, strategi otomatis tetap diblokir.

## Perbaikan cleanup dan validasi

Sesudah cleanup terverifikasi, runtime probe awal masih menyisakan
close_requested=1: worker risiko sudah melihat flat sebelum rekonsiliasi
pembukuan melihat exposure. Perintah resmi CLOSE_POSITION pada snapshot flat
menghapus permintaan lama dan menghasilkan “tidak ada posisi”; tanpa signing
atau broadcast tambahan. Perbaikan final membersihkan flag itu bersama
PROBE_OPERATIONAL_COMPLETE setelah flat/order bersih/kas cocok, dengan regresi.
Hash kode runtime mainnet sebelum perbaikan flag dan hash final disimpan
terpisah di JSON. Perbaikan flag terakhir dibuktikan lokal, bukan probe baru.

251 tes lulus, termasuk signer native SDK resmi offline entry/stop/cancel/close;
Ruff/format, mypy dan build UI lulus. Regresi khusus menguji bukti fee hilang/
berbeda, selisih kas, perubahan fingerprint, binding sesi, race sebelum
signing, fee baru unknown dengan partial fill, stop ambigu/exit darurat,
cleanup dengan audit kas pending, dan pencabutan izin tanpa reset feed.

Izin dicabut via config lokal false + STOP_ENTRIES. Restart final dilakukan
saat flat, tanpa entry, untuk memuat perbaikan flag. Observasi terbatas 600,5
detik: buffer 647,8 detik, 591 sampel terbaru, gap maksimum 1,057 detik, nol
gap panjang/reconnect. Mesin IDLE; tombol probe dan START_LIVE nonaktif.
Pemeriksaan akhir resmi read-only membuktikan saldo 49,430756, posisi/order
nol, probe_used=1/probe_pending=0, close_requested=0, intent ambigu nol.

Stop diterima exchange sudah terbukti. Stop terpicu, harga eksekusi stop dalam
gangguan pasar, dan profitabilitas strategi belum dibuktikan. Tidak ada izin
probe atau strategi berikutnya melalui hasil audit ini.
