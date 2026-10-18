# Satu pembanding depth dengan kontrol peer

Rencana ditetapkan sebelum upaya koneksi, berdasarkan instruksi pengguna.
Produksi tetap `776bec4`; source/config tidak diubah, tanpa restart, trading,
pemuatan izin, snapshot/kalibrasi tambahan atau polling akun exchange.

- Ambil peer public dari event handshake connection ID produksi yang masih aktif;
  cocokkan dengan tuple/inode/FD OS read-only baru. Pemeriksaan awal menemukan
  `13.112.27.226:443`; binding harus diverifikasi lagi tepat sebelum percobaan.
- Satu helper, satu koneksi, satu IP; deadline monotonic **900s sejak sebelum
  upaya koneksi**, termasuk handshake. Tidak reconnect atau memperpanjang.
- Override resolver hanya untuk `fstream.binance.com` pada connector pembanding
  yang baru, dipulihkan ketika scope berakhir. Hostname lain tidak dioverride.
  URL resmi `/public/stream?streams=btcusdt@depth@100ms` tetap; aiohttp3.14.3,
  IPv4, trust_env false, autoping, heartbeat20 dan timeout/factory produksi sama.
  TLS/SNI memakai hostname asli, cert validation tetap wajib. Tidak ada IP tetap
  pada produksi atau perubahan DNS/routing sistem.
- Setelah handshake, peer actual443 dan SNI/check_hostname/CERT_REQUIRED harus
  terverifikasi. Kegagalan/mismatch mengakhiri eksperimen, tanpa IP pengganti.
- Pengamatan socket OS tambahan bersifat lokal/pasif, tanpa account polling.
  Pisahkan lifetime produksi jika reconnect; hanya receipt/payload yang berada
  dalam epoch dengan binding peer cocok boleh dibandingkan sebagai controlled.
- Pasangkan U/u/pu,E/T dan SHA256 JSON payload; key duplikat atau mismatch tidak
  dipasangkan secara arbitrer. Ukur signed production-minus-comparator receipt
  monotonic p50/p95/p99/max, kehilangan pasangan dan sequence continuity.
- Replay source guard memakai hanya event proof yang tersedia saat receipt,
  TTL/drift/validasi/cache asli. Bedakan pasti terlambat (lower>=1500ms), interval
  ambigu, future uncertainty dan perbedaan receipt tanpa koreksi clock.
- Kesiapan evaluasi produksi dihitung dari assessment keputusan yang tercatat
  pada waktu keputusan; replay receipt tidak dianggap kesiapan atau izin entry.
  Korelasikan enqueue/recorded processing, library/application pulse dan loop lag
  yang sudah ada. Snapshot last-frame bukan timestamp setiap frame.
- Peer IP sama tidak membuktikan backend atau jalur jaringan identik. Empat fee
  exactNULL/official reported policy, threshold8bps, buffer5bps dan risiko tetap.
- Raw/alamat lokal hanya di `~/Workspace/outputs`; manifest/hash dan
  ringkasan aman masuk Git. Tutup comparator, verifikasi akun/izin dan hash/PID
  produksi akhir. Tidak ada percobaan/observasi pengganti otomatis.

Pertanyaan utama: apakah update **identik** menunjukkan keterlambatan pasti pada
dua koneksi, hanya produksi, hanya pembanding, atau tidak cukup/ tidak terulang?
