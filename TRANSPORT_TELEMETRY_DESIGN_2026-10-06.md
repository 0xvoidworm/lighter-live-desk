# Bukti minimum instrumentasi transport pasif

Baseline 1ecb4ef/31813c8, runtime818b508 PID301678. Sebelum rollout akun
751745 diperiksa melalui status engine: equity49,430756USDC, posisi/order/intent0,
kas matched, IDLE/OFF/closed. Library aktual aiohttp3.14.3 dan websockets16.1.1.

Audit sebelumnya sudah mempunyai receipt/source/enqueue/recorded timestamp,
snapshot/delta U/u/pu, exception chain, backoff, REST phase dan snapshot status.
Belum mempunyai connection identity yang persisten, urutan close pertama,
jadwal/callback heartbeat, parsed/dequeued control frames, atau transisi coverage
pascareconnect. Data lama tidak cukup untuk memilih penyebab host/jaringan/upstream.

Hook yang digunakan dan batas maknanya:

- aiohttp response subclass: callback heartbeat/timeout tetap memanggil metode
  asli sekali; reader/writer proxy mendelegasikan operasi asli. Reader.read
  mengamati dequeue sebelum autoping menyembunyikan PONG/CLOSE dan EOF sebelum
  cleanup close. Transport chunk callback bukan parsed frame/wire timestamp.
- websockets create_connection subclass: process_event mengamati protocol frame
  sebelum aplikasi, termasuk PONG matched dengan pending ping asli; EOF,
  connection_lost dan close tetap mendelegasikan metode asli. Tidak mencatat
  payload, header, auth, atau token. Account socket memakai hook sama; rotasi
  auth480s tetap dan diberi sebab application_auth_rotation.
- keepalive template identik control flow library16.1.1, diverifikasi AST dan
  SHA256 sumber. Sleep, latency compensation, timeout dan protocol.fail tetap.
  Native asyncio.Timeout memakai wrapper callback per-instance yang memanggil
  callback aslinya sekali. Jika hash berubah, inherited keepalive tetap berjalan,
  dan jadwal/callback yang tidak teramati dinyatakan unavailable.
- Heartbeat reset yang sering terjadi dikoaleskan ke counter/due-time terakhir;
  callback mencatat jadwal aktual handle dan due heartbeat. Tidak ada ping baru,
  perubahan autoping, subscription, timeout, backoff, DNS, TLS/SNI atau guard.

Satu run UUID + connection counter + event counter mengikat handshake, urutan
close awal/tambahan, disconnect/retry, snapshot, bridge, receipt/source valid,
dan coverage observed. Connection baru menunjuk connection sebelumnya di channel
yang sama. Observasi coverage berasal dari pembacaan status yang sudah ada;
waktu guard coverage menjadi benar persis di antara pembacaan tidak diinventarisasi.
Data/gap historis tidak dibuang dan source guard tidak diubah.

Logger baru: queue512 event, put_nowait; daemon thread sendiri menulis gzip,
tidak memegang lock order/account. Overflow/write error dihitung, tidak menunggu
disk. Tidak ada log per data frame: counter bytes/messages dan last-frame
coalesced; pulse5s. Recent32 event tanpa nested pulse, connection summary32.
Timer observasi0,5s mengukur latenya terhadap deadline monotonic; tidak
menggeser evaluator. Context aman mengikat queue, active REST request/phase,
dan write-time recorder pasar yang memang diukur. Tidak ada polling exchange baru.

Kriteria satu observasi20menit (termasuk warm-up, tanpa ulang/perpanjangan):
kode/PID sesuai commit; OFF/closed, posisi/order/intent0; bootstrap dan bridge
tercatat; decision data_guard valid konsisten; timestamp sumber/receipt asli;
ukur drop/error/overhead logger, callback/loop lag, blocker/overlap, readiness
pascawarm-up dan account/request budget. Bila disconnect terjadi, urutan pemicu
dan recovery dilaporkan sampai coverage observed atau batas window. Bila tidak,
recovery kegagalan tetap hanya bukti offline. Tidak mensyaratkan readiness100%
atau jaringan bebas reconnect selamanya untuk tahap persiapan sesi berikutnya.
