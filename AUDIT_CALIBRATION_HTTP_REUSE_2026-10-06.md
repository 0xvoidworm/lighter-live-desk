# Kalibrasi HTTP reuse dan diagnosis DNS Lighter — 6 Oktober 2026

Tugas tanpa order. Dibaca AGENTS.md, RUNBOOK.md, audit sesi START_WHEN_READY,
summary, manifest dan rekaman privat. Awal repo bersih `c58fe5f`, kode runtime
`777f68e`, PID378689. Bukti historis:24 file,3.911 snapshot; semua ukuran/hash
cocok dengan manifest SHA256
`4b84f23432a6b5644470d6cc753c56a6f82fdacd000bdbb1cecea6b32c6b3682`.
Tidak ada perubahan atau penggantian hash bukti lama.

## Hubungan waktu dari sesi sebelumnya

Persiapan18:50:25,974316–18:50:28,638336:2,664019s. Aktif sampai kebijakan
menghentikan entry19:08:57,030156:1.108,391820s. Cleanup sampai closed
19:09:08,582155:11,551999s. Tidak ada fill/entry dalam sesi historis.

19 kalibrasi unik pada authorization→entry-stop:11 diterima,8 ditolak hanya
karena RTT>400ms; semua HTTP200,host synchronized dan validasi lainnya lolos.
Seluruhnya membuat koneksi baru,0 reuse. RTT median349,937ms,p95753,696ms.
Fase resolver534,813ms pada18:50:26 adalah fase resolver aplikasi DoH,
termasuk lock/cache/lookup HTTP; bukan pengukuran535ms DNS OS.
Connection-create578,475ms saat itu mencakup resolver/TCP/TLS. Subfase TLS,
lock dan HTTP DoH historis tidak direkam; tidak diarang dari selisih angka.

| Mulai WIB | Diterima | RTT penuh ms | Resolver ms | Create ms termasuk resolver |
|---|---:|---:|---:|---:|
| 18:50:26 | tidak | 672.931 | 534.813 | 578.475 |
| 18:51:26 | tidak | 422.177 | 149.232 | 321.988 |
| 18:52:26 | ya | 193.793 | 0.015 | 85.297 |
| 18:53:26 | tidak | 550.573 | 121.206 | 457.068 |
| 18:54:26 | ya | 296.162 | 73.763 | 205.842 |
| 18:55:26 | ya | 309.611 | 151.219 | 220.086 |
| 18:56:26 | tidak | 516.104 | 318.404 | 399.220 |
| 18:57:26 | tidak | 753.696 | 425.284 | 561.655 |
| 18:58:26 | tidak | 539.538 | 146.495 | 444.566 |
| 18:59:26 | ya | 339.602 | 101.651 | 187.731 |
| 19:00:26 | tidak | 510.590 | 153.624 | 389.536 |
| 19:01:26 | ya | 372.787 | 126.057 | 281.127 |
| 19:02:26 | tidak | 692.547 | 444.891 | 513.272 |
| 19:03:26 | ya | 284.122 | 47.865 | 105.546 |
| 19:04:26 | ya | 281.276 | 57.806 | 98.818 |
| 19:05:26 | ya | 321.601 | 162.568 | 229.504 |
| 19:06:26 | ya | 247.964 | 59.283 | 136.887 |
| 19:07:26 | ya | 278.307 | 49.191 | 176.928 |
| 19:08:26 | ya | 349.937 | 66.051 | 258.716 |

Kalibrasi18:50 dan18:51 ditolak; latest proof dari18:49 kedaluwarsa.
Penolakan18:56,18:57,18:58 menyusul latest accepted18:55 dan membuat expiry
kedua. Dua interval expiry diagnostic yang benar-benar diamati:
18:51:26,522208–18:52:26,574645 (60,052437s) dan
18:57:26,801119–18:59:26,961016 (120,159897s),total180,212334s.
Batas observasi diskret sekitar0,3s; TTL asli120s dihitung dari monotonic
receipt proof, bukan dari waktu tampilan. Rejection RTT mempertahankan latest
sampai TTL aslinya habis; cache tidak meluluskan latest yang expired.

Tidak ada sampel valid selama source guard menolak, tetapi delta depth tetap
menjaga urutan book. Setelah proof pulih, gap sampel60,896s dan120,551s tetap
tersimpan. Coverage blocker teramati18:52:26,574645–19:08:21,472957,
954,898313s. Gap pairwise baru terlihat ketika sampel valid berikutnya masuk;
tidak adanya blocker coverage pada awal expiry bukan bukti continuity valid.
Pada19:07:27 gap besar sudah keluar dari window600s:long_gap_count0,
maxgap4,836s,tetapi427 sampel valid<480. Pada19:07:50 baru449;19:08:21 mencapai
480 dan coverage lolos. Tidak ada reset buffer selama interval aktif.

Source blocker297,484988s terdiri expired180,212334s,future-uncertain
96,525002s,source-late20,747652s. Ini bukan tiga pengukuran latensi fill.
Coverage954,898313s dan basis_scale1.034,856996s bertumpang tindih dengannya;
tidak dijumlahkan sebagai downtime. Kesiapan aktif71,512978s (6,451958%),
interval kontinu terpanjang7,389322s. Overlap eksklusif ada dalam summary.
Penolakan future-uncertain tetap masalah berbeda dari expiry:interval clock
melintasi batas future; reuse tidak mengubah toleransi itu. Counter reconnect
Binance/Lighter tidak bertambah selama aktif;1 reconnect Lighter adalah
historis sebelum otorisasi, bukan kejadian sesi ini.

## Lifecycle dan perubahan terbatas

aiohttp terpasang3.14.3,DefaultResolver=ThreadedResolver. Worker clock membuka
satu ClientSession/connector sepanjang run, bukan session baru tiap menit.
Akan tetapi default idle keepalive15s dan pemeriksaan _get() menolak reuse
sesudah timeout itu; cadence60s memastikan koneksi lama tidak dipakai ulang.
Resolver Binance produksi modeDoH mempunyai ClientSession HTTP tersendiri,
juga default keepalive15s. Answer cache memakai TTL provider dengan cap60s;
cache hit pada satu kalibrasi pun tidak mencegah koneksi Binance baru.

Kode `8d785cb` memberi **75s keepalive hanya context kalibrasi**, outer HTTP
Binance serta HTTP DoH milik resolver itu:60s cadence+2s budget request+13s
ruang idle/penjadwalan. Tidak menjamin upstream mempertahankan koneksi.
Koneksi yang sudah hidup tidak di-resolve ulang hanya karena TTL nama habis;
resolusi baru tetap memakai TTL provider semula. Jalur lain memakai default
15s, tanpa fixed IP atau perubahan DNS/TLS/SNI/proxy.

Trace header-send menghitung automatic retry idempotent GET aiohttp;
request_start sendiri tidak menghitung retry itu. Bobot konservatif minimal1,
plus pengiriman ulang; koneksi attempt dicatat tersendiri. Nested DoH attempts
bounded4 dan frame trace24 dengan counter overflow; tidak menambah lookup atau
retry. Cancellation dicatat, tetap menutup session dan tidak memperbarui proof.
RTT tetap seluruh request termasuk resolver,headers,body,decode/handoff.
Cadence60s,TTL120s,RTT400ms,uncertainty250ms,host/wall/future guards,selection
proof,coverage,strategi8bps/buffer5bps,risk,fee,quota dan backoff tetap.

## DNS Lighter: bukti dan batas kesimpulan

/api/v1/deposit/history pada mainnet.zklighter.elliot.ai gagal19:08:57,029,
kemudian19:08:59,19:09:01,19:09:03,19:09:05. Trace awalClientConnectorDNSError,
fase akhirdns,request6,115ms,resolver3,824ms. Ini gagal saat resolusi sebelum
HTTP response, bukan exchange menolak deposit request. Installed SDK memakai
connector/resolver sistem bawaan; berbeda dari opt-in DoH khusus Binance.

Audit lengkap pertama yang berhasil selesai19:09:08,565560 (8GET,pagination
tersedia),lalu sesi ditutup19:09:08,582156. Recovery routine mempertahankan
kebijakan incident dan tidak membuka sesi pengganti. Bukti response-fresh tidak
membuktikan sumber DNS awal atau alasan jaringan pulih.

TransportTrace lama dan journal hanya menyimpan kelas exception, bukan
os_error/cause/errno. Log NetworkManager,systemd-resolved,chronyd yang tersedia
19:05–19:12 kosong. Jadi EAI_AGAIN/NXDOMAIN,cache OS,server DNS,ISP maupun
upstream **unknown**. Tidak mengganti resolver Lighter atau DNS OS. Field
pengamatan minimum bila berulang:errno/os_error type,cause type,hostname,
resolver-start/end dan koneksiID; tanpa URL query/header/token atau perubahan
transport. Tidak menyimpulkan sebab dari kegagalan beberapa milidetik.

## Tes dan satu observasi

503 tes produksi lulus (3 warning deprecation dependency),Ruff src/tests/scripts
lulus,mypy23 source files lulus. Test lokal menggunakan aiohttp terpasang,
HTTP clock dan DoH benar-benar reuse sesudah60,1s nyata. Force-close server,
stale-connection automatic retry(2 header sends/1 logicalGET),timeout,
cancellation,HTTP500,RTT reject tidak memperbarui proof;TTL120s tetap expired.
Suite tetap membuktikan assessment keputusan,entry recheck dan proteksi/exit
independen dengan transport palsu,tanpa broadcast. Python mengikuti integrasi
proyek/lockfile dan pipeline analisis yang sudah tersedia; bukan migrasi.

Kriteria ditetapkan sebelum mulai di
CALIBRATION_REUSE_VERIFICATION_2026-10-06.md.
Satu restart hanya engine; API dipertahankan. Pemuatan `8d785cb` PID391484,
recording21:17:20,998706–21:37:20,998706WIB tepat1.200s termasuk warm-up.
Tidak ada LOAD_SESSION_PERMISSION/START/order/extra exchange polling.

**Runtime:**4.798 snapshot,0 collector error,durasi monotonic1.200,000280s
(kelebihan0,000280s hanya penutupan file; window statistik tepat1.200s).
Config/source hash tetap. Kalibrasi pertama dimulai0,097850s sebelum marker
pengamatan,selesai0,304743s sesudah marker:RTT402,592ms,ditolak; bukan expiry.

**20 kalibrasi yang dimulai dalam window** semuanya accepted dan reuse,
RTT min91,679ms,median94,758ms,p95/max133,997ms,mean98,230ms. Ada20 header
send,0 automatic retry. Snapshot hanya memuat19 kalibrasi lengkap;id21 selesai
pada detik1.199,993920 dan event transport direkam sebelum deadline,tetapi belum
masuk tick status0,25s terakhir. Raw event membuktikan attempt ke-20 tanpa
memakai proof itu untuk meluluskan keputusan sebelumnya. Tidak ada request
kalibrasi tambahan; first+20 mengikuti cadence produksi.

Pada20 reuse tersebut tidak ada hook resolver/create baru:field fase absent,
bukan hasil pengukuranDNS0ms. Waiting headers median94,532ms,p95133,804ms;
body/decode masing-masing sekitar0,02ms. Bootstrap:resolver265,375ms,
connection-create308,618ms,headers93,686ms. Trace DoH baru memperlihatkan
resolusi nama provider0,733ms,connection-create gabungan254,044ms,headers10,968ms.
Jadi setup koneksi DoH mendominasi bagian resolver bootstrap yang teramati;
TLS tersendiri dan lock/cache belum terukur. Ini tidak menjelaskan subfase
535ms pada kejadian18:50:26 secara retrospektif.

Proof expired **0s**. Sebelum proof pertama sah,not_verified59,255794s dan
missing timestamp0,580856s. Future_uncertain **95,937965s** dan source_late
**26,812636s** tetap muncul;proof valid tidak otomatis meluluskan quote.
Pemanasan dari awal pengamatan660,266102s (600s sejak sampel valid pertama).
Setelah warm-up denominator539,733898s:ready33,349998s (**6,178970%**),
interval kontinu terpanjang5,272805s. Full window ready2,779167%.

Blocker setelah warm-up:coverage496,912329s,basis_scale506,383900s,
source+market58,969228s,rekonsiliasi3,994714s. Source overlap terdiri
future_uncertain55,175601s dan source_late3,793627s. Overlap eksklusif di summary
berjumlah tepat denominator; tidak menjumlahkan blocker turunan.
Coverage full window1.157,178431s termasuk warm-up. Gap sampel terbesar12,860s
teramati pada detik569,846,setelah episode source_late sekitar557,906–569,846
(11,940s). Contoh quote:receipt age149,234ms,indikator sumber5.173,798ms,
interval umur terkoreksi5.080,028–5.211,761ms,uncertainty65,866ms,proof age27,377s.
Ini indikator waktu dengan evidence clock,bukan pengukuran pasti latensi wire
atau fill. Tidak ada sampel sintetis/gap yang dibuang;coverage baru pulih
sekitar detik1.157,178. Ketiadaan expiry belum menyelesaikan sumber-late atau
future_uncertain,dan tidak menjamin expiry tidak akan terjadi lagi.

Umur akun max12,870s,active-orders12,979s (batas15s). Rolling request Lighter
median10,p95/max18;held-unconsumed max7,used+held max18,
sisa kapasitas total minimal37 (reserve exit15 tetap). Semua quota checks teknis lolos,
clock GET memakai0slot Lighter. Sebanyak257 GET Lighter dimulai dalam window
(routine refresh/audit yang sudah ada),0 request error/0 DNS error. Exit reserve
15 dan cap normal40/total55 tetap. Audit startup/periodik lengkap,akhir matched.

Binance public/market dan Lighter market **0 reconnect** dalam window.
Dua koneksi account WS dirotasi aplikasi untuk auth pada detik490,336 dan
980,972,cause application_auth_rotation; ini bukan kegagalan DNS atau market
reconnect. Bootstrap snapshot2/bridge1/source-valid1 tercatat. Tidak menguji
reconnect paksa; recovery kegagalan transport tetap bukti localhost/offline.

Callback trace clock20 request total0,140797ms;bootstrap0,017377ms,callback DoH
bootstrap0,013194ms. Tidak menghitung ini sebagai overhead seluruh aplikasi.
Websocket hook total sekitar1.132,942338ms sepanjang proses~20menit,
recorder queue peak7/512,maxage6,994ms,0 overflow/0 write error. Loop status
p95 4,1ms,max17,6ms;queue pasar p950,max19. Hook timer pasif
mencatat max76,055ms pada skala berbeda dari lag tick evaluator. Collector lokal
median17,055ms,p9528,409ms,max72,022ms. Tidak ada bukti tracing menahan feed
selama beberapa detik; korelasi ini bukan benchmark kausal tanpa instrumentasi.

## Keadaan akhir, bukti dan keputusan

Verifikasi21:40:11WIB dari status engine/DB read-only:PID391484,IDLE,orderOFF,
entries stopped,sesi closed,loaded permissionNULL,pending activation0. Saldo
collateral/equity49,430756USDC,semua posisi/order/pending/intent0,kas matched,
selisih dan perubahan kas0,000000USDC;history complete. Empat exact fee tetap
NULL dengan official reported0,0000USDC/4desimal yang cocok,tidak disebut exact0.
Jurnal sehat,NRestarts0. Tidak ada order/signing order/broadcast/otorisasi baru.
Counter intents6,fills4,commands21,activation requests1 tetap sama sebelum/sesudah.
API tetap proses lama;kode engine yang benar-benar dimuat8d785cb,tidak ada
perbaikan produksi yang belum dimuat.

Raw/status/SQLite/trace/logs berada di
`/home/yuri/Workspace/outputs/lighter-calibration-reuse-20261006` (0700/0600).
Prefix gzip market/transport yang masih terbuka disalin,setiap JSON lengkap
sudah dipertahankan;tail tanpa trailer ditandai. Prefix mencakup sebagian data
sesudah deadline;statistik dipotong pada timestamp window asli. Tidak ada reset
atau perekaman tambahan. Manifest dan summary aman di
audits/calibration-http-reuse-20261006;raw/config/key/token tidak masuk Git.

**Keputusan:perbaikan keepalive terbatas diterima.** Mekanisme idle15s versus
cadence60s terbukti di implementasi installed library dan tes transport nyata;
runtime membuktikan reuse20GET dan0expiry dalam window ini. Perbandingan dua
interval pasar berbeda tidak membuktikan sebab seluruh perubahan RTT/readiness
secara mutlak,dan tidak membuktikan masalah DNS lama atau clock future selesai.
Langkah berikut paling berguna:telusuri episode source_late11,940s pada raw depth
serta frame library/receipt/processing yang sudah diarsipkan,untuk membedakan
stempel transaksi tua dari keterlambatan penerimaan/pemrosesan,sebelum sesi baru.
Tidak menurunkan guard/threshold atau membuka trading. Sampel ini tidak menilai
profitabilitas strategi.


Seal manifest privat SHA256 `708c54a269b5b439874587a264610fab4a3ba4bf3191b4107b9e328d189395a4`; 39 file privat.
Pemeriksaan terminal 2026-10-06T21:50:30.279996+07:00: IDLE, order OFF, sesi closed,
flat/order/intent/activation pending nol, kas matched. Source hash tetap versi
8d785cb; commit audit tidak mengubah kode engine atau konfigurasi.
