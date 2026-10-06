# Kriteria verifikasi kalibrasi HTTP — 6 Oktober 2026

Satu restart engine setelah akun aktual flat/bersih, IDLE, order OFF, session
closed dan tidak ada activation pending. Tidak memuat izin trading. API tetap
berjalan. Satu perekaman status lokal tepat 1.200 detik termasuk warm-up, melalui
`scripts/record_diagnostics.py`, tanpa polling exchange tambahan atau pengulangan.

Sebelum pemuatan: suite produksi, Ruff dan mypy harus lolos. Tes localhost
membuktikan reuse setelah **60,1 detik nyata** untuk HTTP clock dan DoH, server
close, retry idempotent terukur, timeout/cancel, HTTP gagal, TTL 120 detik tetap,
serta default connector jalur lain dan TTL DNS tidak berubah. Proof selection,
source entry/sampling, worker proteksi/exit, dan kontrol sesi diuji dalam suite.

Keputusan transport didasarkan pada reuse yang benar-benar diamati, fase/RTT,
accepted/rejected, interval expired, coverage dan kesiapan setelah warm-up.
Bootstrap sebelum marker pengamatan dilaporkan terpisah bila ada. Tidak ada
tuntutan readiness 100%, hasil sama dengan sesi historis, atau reconnect harus
muncul. Server boleh menutup koneksi; tes recovery offline dipisahkan dari
kejadian runtime. Invariant akun/izin dipantau sampai akhir dan diverifikasi
melalui status engine serta ledger read-only. Gangguan/collector error/gap
tetap dihitung, bukan dihapus atau diganti pengamatan baru.

Audit akhir menyimpan denominator dan overlap, beban callback/recorder, raw
privat dan hash. Dua interval waktu berbeda tidak membuktikan kausalitas mutlak
antara keepalive dan keseluruhan readiness. RTT tetap mengukur seluruh request;
komponen resolver tidak disubtraksi dari validasi. Tidak ada perubahan strategi,
clock guard, coverage, fee, risiko, batas request, DNS/TLS atau backoff.
