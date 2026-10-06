# Satu pembanding depth read-only

Window tetap **900 detik sejak marker perekaman**, termasuk handshake. Hanya
satu attempt koneksi publik pembanding, tanpa reconnect otomatis atau snapshot
REST. Kegagalan tetap menjadi hasil; window tidak diulang/diperpanjang.
Produksi tidak direstart atau diubah. Order OFF, sesi closed, tanpa izin baru.

Pembanding memakai Python 3.12 proyek, aiohttp terpasang, factory
`binance_session(doh)`, IPv4, trust_env false, TLS/SNI dan validasi hostname,
autoping default, heartbeat20, serta hook socket pasif produksi yang sama.
URL persis `wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms`.
Tidak mengimpor engine/account/signer/key. Writer bounded512, nonblocking;
drop/error, loop timer0,5s, pulse5s dan TCP_INFO pasif dicatat.

Pasangan harus identik U/u/pu, E/T **dan SHA256 payload kanonis**. Duplicate yang
ambigu tidak dipasangkan sewenang-wenang. Selisih receipt memakai monotonic
host yang sama, bertanda produksi minus pembanding; p50/p95/min/max dan absolut
dilaporkan. Proof diputar ulang kausal hanya dari event source_clock produksi
yang sudah tersedia saat receipt, memakai validator produksi dan TTL/drift/
anchor/clock bounds aslinya. Penilaian ini replay saat receipt, bukan izin entry
atau klaim latensi wire/fill. T tetap sumber utama; E terpisah.

Kriteria terlambat pasti: proof sah dan lower bound umur T >=1500ms. Upper bound
melintasi batas saja adalah ketidakpastian. Missing/future/expired proof tidak
dianggap normal atau terlambat pasti. Kategori: kedua koneksi terlambat pasti;
hanya produksi (pembanding source-valid); hanya pembanding (produksi source-valid);
tidak direproduksi/pasangan tidak cukup. Seluruh kasus uncertain dilaporkan.
Durasi burst adalah span receipt pertama–terakhir sampel terlambat berurutan;
bukan asumsi seluruh waktu antarobservasi pasti terlambat.

IP peer pembanding diperoleh dari socket. Peer produksi hanya dipetakan jika ada
bukti hubungan channel/socket; daftar socket PID tidak cukup membedakan public,
market, clock atau kurs. IP berbeda/tidak terpetakan adalah confound eksplisit.
Pulse library last-frame bukan timestamp setiap frame; tidak menyingkirkan semua
buffering. Receipt→enqueue→record dan loop/queue/pulse dipakai sesuai granularitas.

Account awal/akhir diperiksa melalui status/journal yang sudah diterbitkan engine;
tidak menambah polling exchange. Raw tetap privat di Workspace/outputs, manifest
hash dan ringkasan tanpa account payload/key saja masuk Git. Produksi/izin harus
tetap sama setelah eksperimen; pembanding ditutup sebelum laporan akhir.
