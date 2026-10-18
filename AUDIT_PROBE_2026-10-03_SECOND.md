# Audit percobaan mainnet kedua — 3 Oktober 2026

**Lifecycle proteksi belum lolos.** Satu entry baru dibroadcast dan terisi;
stop tidak sempat dipasang karena rekonsiliasi menolak respons trade tanpa fee.
Close darurat reduce-only terisi penuh. Posisi dan seluruh order aktif kemudian
terverifikasi nol; ledger kas cocok. Tidak ada entry kedua, START_LIVE, atau
deposit tambahan dalam izin baru ini.

## Sebelum broadcast

Instruksi repo, runbook, audit lama dan commit `91ffbb3`/`2eb278a` diperiksa.
Intent lama client 1 tetap REJECTED, hash null, tanpa fill. Penolakan expiry -1
direproduksi oleh signer native SDK pinned: wrapper mengembalikan error sebelum
send_tx. Query accountTxs yang dahulu mendapat 403 bukan bukti absensi transaksi.
Account-check baru membuktikan akun 751745/key 4, Standard/Unified, margin
Cross 1x (0/100.00), equity/available 49,43/49,430000 USDC, posisi/order/aset
lain nol, satu inflow 49,43. Sumber 50 USDT telah terpakai, sisa nol.

SDK distribusi 1.1.5 terpasang dari commit resmi
`78db23f632de8deca9a232a61bbd3bf6e79fc522`. Tiga regresi memakai key sementara
dan signer native sesungguhnya melalui adapter; nonce lookup dan transport
diganti, seluruh I/O jaringan diblokir. Entry IOC expiry 0, stop helper resmi,
cancel exchange index, dan close IOC expiry 0 reduce-only lolos signing.
IOC lama expiry -1 gagal sebelum transport. Ini bukti lokal, bukan mainnet.
193 tes, Ruff, format, dan mypy lulus sebelum entry (`fa95fb9`).

Backup dibuat sebelum rearm. Event PROBE_REARM_AUTHORIZATION merekam izin baru
untuk satu entry; intent/audit lama dan counter ID dipertahankan. Mesin menjalani
warm-up 600 detik tanpa pelonggaran guard. READY teramati dengan blocker kosong,
akun/audit dana <15 detik, kedua feed valid, kedua stream Binance tersambung,
kuota lokal terpakai 32/60 detik, 23 slot transaksi tersisa. Pagu normal/urgent/
transaksi tetap 40/47/55, di bawah [batas Standard resmi](https://apidocs.lighter.xyz/docs/rate-limits).

Preview disimpan sebelum perintah PROBE_ONCE (command 2). Mekanisme lama memilih
BUY tetap untuk lifecycle, bukan sinyal strategi. Ukuran minimum 0,00012 BTC;
dispatch aktual memakai ask 84.539,1, IOC limit 84.556,1, notional 10,146732 USD,
rencana risiko 0,0375429084 USD. Batas entry 2 bps, stop direncanakan 35 bps
dari fill dengan limit 30 bps setelah trigger; maksimal hold 180 detik.
Batas notional 25, exposure 1x, risiko 0,50, loss harian 5 tetap aktif.

## Bukti mainnet

| Tahap | Bukti |
| --- | --- |
| Entry | Client 2, order 844421373195178, trade 33141524453; BUY 0,00012 BTC pada 84.539,0; status filled |
| Proteksi | Tidak dikirim; tidak ada bukti stop diterima atau terpicu |
| Close darurat | Client 3, order 562953474977251, trade 33141644233; SELL 0,00012 BTC pada 84.543,8; status filled dan reduce_only=true |
| Cleanup | Account REST dan seluruh accountActiveOrders: qty 0,00000, order aktif 0; kedua intent terminal, tidak ada ketidakpastian tersisa |
| Ledger | 49,430000 + 0,00012 × (84.543,8 − 84.539,0) = 49,430576 USDC; arus dana tetap satu inflow, snapshot funding 0 |

Hash entry:
`c4530690d1f3f1495d771a799aa39d11e9a5b381448c89a7510742aa52227e30ba6005ec7d9a6c26`.
Hash close:
`0df1411fa4e223ecc11f2645ab19ff5abec1ee4900a6786e1661135ee525cb1d8d1616a905307f26`.
Slippage terhadap quote dispatch: entry −0,0118288461 bps (menguntungkan),
close 0 bps terhadap bid 84.543,8; close limit 84.290,1 (30 bps).

Perubahan kas bersih +0,000576 USDC. Selisih antara gross PnL fill dan perubahan
kas adalah 0,000000, tetapi **ini bukan bukti fee per trade nol**. API maker_fee/
taker_fee kosong pada kedua trade. Koreksi 4 Oktober menyimpan kedua fee sebagai
unknown/NULL; audit net kas tetap cocok, audit fee belum lengkap. Pencatatan nol
berdasarkan audit kas pada laporan/event lama telah dikoreksi melalui migration
3 dan event FEE_PROVENANCE_CORRECTION, tanpa menghapus bukti historis.

Dispatch lokal entry 23:13:17,135 WIB, close 23:16:20,802 WIB. Raw timestamp
exchange entry 1791020396773, close 1791020597030: durasi fill-to-close 200,257
detik, **melewati rencana 180 detik**. Timestamp exchange berbeda dari jam UTC
lokal sekitar 6h33m; penyebab belum diketahui. Kedua clock disimpan tanpa
penyesuaian buatan. Jangan memakai salah satunya seolah clock lain tersinkron.

## Kegagalan dan perbaikan

Respons trade valid memuat account/client ID dan fill, tetapi kedua field fee
tidak tersedia. `_record_trade` memicu ValueError/account_incident_pending;
rekonsiliasi berhenti sebelum mempublikasikan snapshot posisi dan memasang stop.
Dashboard saat itu masih mempunyai snapshot flat yang basi; posisi tidak aman.
Engine dihentikan untuk mengambil alih nonce secara tunggal, akun aktual dibaca,
dan close darurat memakai ukuran aktual serta quote REST segar. ACK close tidak
dianggap selesai: status filled, saldo, posisi nol, seluruh order nol, serta
audit dana penuh diverifikasi sesudahnya.

Commit `285727b` menambahkan error khusus fee yang hilang. Rekonsiliasi sekarang
tetap mempublikasikan exposure aktual untuk pengurangan risiko, membekukan entry,
dan memakai jalur close darurat sekali dalam flight. Audit tetap gagal sampai
fee terselesaikan; tidak ada fee nol otomatis atau pengakuan full reconciliation.
Fill lama yang sudah diaudit hanya dapat didedup ketika ID, ukuran dan harga
cocok. Regresi menguji exit tunggal/ukuran aktual, pending audit, dan perubahan
ekonomi fill lama. Seluruh 195 tes, Ruff/format, mypy dan SQLite check lulus.

Audit kas manual hanya diselesaikan setelah full account/funding/trade/order
check membuktikan dua trade milik intent lokal, kedua order filled, qty sama,
tidak ada posisi/order/aset lain, inflow tidak berubah, dan selisih saldo persis
sama dengan PnL fill. Event AUDITED_FILL, FEE_CASH_AUDIT dan PROBE_COMPLETION_AUDIT
menyimpan dasar penyelesaian kas saat itu; penyimpulan fee nol tidak sah dan
dikoreksi pada 4 Oktober. probe_used=1 dipertahankan; probe_pending=0 dan
account incident/close request baru diselesaikan berdasarkan bukti tersebut.

Perbaikan pemisahan proteksi/pembukuan dan deadline sesudah audit ini berada di
AUDIT_PROTECTION_FIX_2026-10-04.md. Tidak ada entry baru dalam tugas perbaikan.

Enam timeout pada proses sebelumnya tercatat 22:08:41,311–22:09:41,456 WIB
(span error 60,144342 detik). Waktu pemulihan tepat tidak dicatat log; pembacaan
akun baru membuktikan pemulihan. Penyebab jaringan dasar belum ditetapkan.

## Penutupan tugas

Izin order sudah false, scope tetap probe_only, entry strategi berhenti.
Engine kode terakhir dimulai ulang 23:24:05 WIB untuk warm-up wajib.
IDLE terverifikasi 23:34:59 WIB: PID 86819, izin order false, entries_stopped
true, probe_used 1/probe_pending 0, posisi/order/proteksi/exit nol, saldo
49,430576 USDC. Umur full audit dana 11 detik; kedua feed valid, reconnect dan
error runtime proses akhir nol. Unit aktif, autostart disabled, NRestarts 0.
Snapshot akhir dan hasil cleanup disimpan dalam JSON audit.

Bukti: [JSON audit](audits/probe-20261003-second.json) dan
`~/Workspace/outputs/lighter-mainnet-probe-20261003-second/`.
Key, token, cookie dan signature tidak disimpan dalam bukti atau commit.

Entry/fill dan close reduce-only mainnet telah dibuktikan. Stop yang diterima
exchange, pemicuan stop, serta lifecycle penuh yang terlindungi belum dibuktikan.
Percobaan tambahan memerlukan izin baru. Hasil PnL kecil ini tidak membuktikan
profitabilitas strategi.
