# Rancangan profiling produksi — 7 Oktober 2026

Status: **siap ditinjau, belum dijalankan**. Tahap ini hanya pemeriksaan
read-only dan dokumentasi. Tidak ada pemasangan paket, attach profiler,
capture, pembanding baru, restart, pemuatan izin, atau transaksi.

Revisi reviewer: semua batas scheduling/syscall disimpan tanpa filter durasi;
overlap per episode mempunyai kategori unknown eksplisit. Sampler dipisah
dari supervisor, dan profiler root dimiliki pengendali tersendiri dengan
lease, deadline absolut, pidfd, serta fallback systemd. Command dan skenario
uji di bawah menggantikan kontrak versi awal. Ini masih spesifikasi, bukan
implementasi atau hasil tes.

Perubahan penting:

- Retensi **all-edges-v1**, record OS 96 byte, tanpa pembuangan edge <2 ms;
  pagu 2 juta event/512 MiB, tanpa overwrite atau downsampling saat cap.
- Overlap per episode memisahkan C/S/R/**unknown**; syscall menjadi axis
  terpisah dan data hilang tidak pernah dijadikan on-CPU.
- Supervisor, sampler process dan pengendali root mempunyai ownership,
  progress/lease, pidfd dan unit/cgroup terpisah; worker IO tidak menahan kontrol.
- Deadline absolut dan INT→TERM→KILL diverifikasi lewat exit/cgroup;
  watchdog manager menangani supervisor/pengendali mati atau macet.
- Command systemd serta matriks uji retensi, loss, crash dan cleanup diperbarui;
  seluruh tes implementasi tersebut masih pending.

## Kesimpulan kelayakan

Profiling tanpa restart tampak memungkinkan, tetapi belum dibuktikan lewat
attach. Pilihan yang disarankan adalah **sampler eksternal Rust berbasis
library py-spy 0.4.1 dalam mode nonblocking, ditambah bpftrace untuk scheduling
dan durasi syscall main thread**. Kombinasi ini memberi stack Python dan
interval OS yang bisa dikorelasikan memakai CLOCK_MONOTONIC, tanpa locals,
argumen fungsi/syscall, isi buffer, maupun kredensial.

Ada dua prasyarat yang belum tersedia: pemasangan bpftrace dan pembuatan serta
pengujian sampler/supervisor kecil tersebut. Ini rancangan dengan kontrak
command yang eksplisit; **binary supervisor belum ada**, sehingga command
capture di bawah belum dapat dieksekusi. Tidak ada klaim attach atau overhead
produksi sudah terverifikasi. Langkah berikutnya yang disarankan adalah
menyiapkan dan menguji collector secara terisolasi sebelum meminta observasi
produksi sesuai rancangan ini.

## Baseline dan pemeriksaan aktual

Pemeriksaan lokal terakhir saat penyusunan: **2026-10-07 01:58:54 WIB**.
API yang dibaca adalah status cache lokal `127.0.0.1:8765/api/status`, ditambah
SQLite read-only, procfs, metadata systemd, metadata tracepoint kernel, dan
sumber kode. Tidak ada tambahan polling exchange.

- Repo bersih sebelum penambahan dokumen, HEAD
  `a352a52973db4a7c263f4259a5ad37078bdd5f9b`.
- Runtime PID **405247**, run ID `6e870e45fd1a4a198f65eb06a9b684a5`, produksi
  `776bec4a6b476d7cd8bd49ffbb71a1b7611fffc5`. Seluruh **24 hash source** cocok
  dengan manifest baseline comparator; hash `config.json` juga cocok.
- Interpreter nyata: uv CPython **3.12.14**, Linux
  `7.2.8-200.fc44.x86_64`. Start ticks procfs PID: **19195208**.
- **17 OS thread**. Main TID = PID **405247**; CLI menjalankan
  `asyncio.run(run_engine())` pada main thread. Nama OS seluruh thread saat
  diperiksa adalah `lldesk`; nama itu tidak cukup untuk memetakan worker.
  Thread lain belum dipetakan secara semantik melalui attach. Beberapa thread
  native mungkin tidak mempunyai stack Python.
- UID engine dan pemeriksa **1000**, `TracerPid=0`, engine
  `NoNewPrivileges=1`, tanpa effective capabilities. Shell tidak mempunyai
  effective capabilities; kebijakan sudo read-only menunjukkan
  `NOPASSWD: ALL`. Itu ketersediaan akses, bukan izin menjalankan capture kini.
- `ptrace_scope=0`; metadata `/proc/PID/mem` dapat diakses menurut permission
  check. **process_vm_readv belum dicoba**. SELinux enforcing; konteks engine
  dan pemeriksa `unconfined_t`. Pemeriksaan statis ini tidak menjamin LSM akan
  menerima operasi attach yang sebenarnya.
- `perf_event_paranoid=2`; lockdown `[none]`. Kernel mempunyai BPF, BPF_EVENTS,
  TRACEPOINTS, FTRACE, SCHED_INFO, SCHEDSTATS, dan BTF. Format tracepoint
  `sched_switch`, `sched_wakeup`, `raw_syscalls/sys_enter`, dan
  `raw_syscalls/sys_exit` berhasil dibaca dengan sudo tanpa mengaktifkan probe.
  Tracefs memerlukan akses root; tidak ada perubahan permission/sysctl.
- `sched_schedstats=0`, tetapi `/proc/PID/task/TID/schedstat` yang terbaca
  berisi penghitung nonzero. Ini tidak membuktikan kualitas setiap delta;
  collector harus memeriksa konsistensinya, dan tidak mengaktifkan schedstats.
- Cgroup engine: `cpu.max=max 100000`; `nr_throttled=0` pada snapshot ini.
  Ini tidak menyingkirkan penundaan scheduler atau throttling di ancestor.
- GDB **17.2-4.fc44** tersedia. `py-spy`, `bpftrace`, `perf`, `strace`,
  `pidstat`, `samply`, dan `trace-cmd` tidak ditemukan di PATH; pemeriksaan RPM
  juga tidak menemukan paket profiler terkait yang diperiksa.
- Akun **751745**: equity/collateral **49.430756 USDC**, posisi global nol,
  active/pending order nol, unresolved intent nol, pending activation nol.
  Komponen akun lengkap; usia account/orders **10.809/10.916 detik**.
  Kas `matched`, selisih **0.000000 USDC**, audit riwayat lengkap.
  **IDLE, order OFF, entries_stopped=true, sesi closed, loaded permission null**,
  jurnal sehat. Empat fee exact tetap NULL; kebijakan reported tidak berubah.

Angka PID, umur akun, dan keadaan di atas harus diperiksa ulang saat capture
kelak diotorisasi. Tidak boleh menjadi izin entry atau bukti kesiapan di waktu
lain.

## Pertanyaan pengukuran dan kandidat kode

Audit `AUDIT_DEPTH_SAME_PEER_2026-10-07.md` pada commit `a352a52` menemukan
212 event lag timer >25 ms, maksimum window **243.236550 ms**. Pada pasangan
depth ekstrem positif, production receipt tertinggal **328.727110 ms**;
callback lag dicatat **3.241 ms** setelah receipt, queue 26. Receipt sampai
recorder hanya **3.389344 ms** untuk update tersebut. Hubungan waktu ini
menunjukkan perlunya mengukur main thread, bukan membuktikan penyebab.
Gangguan sumber 5 detik sebelumnya tidak terulang pada eksperimen itu.

| Jalur aktual | Jenis pekerjaan yang perlu dibedakan | Bukti yang dibutuhkan |
|---|---|---|
| `engine.py:357` `_loop`, `:470` `on_market`, `:546` `_apply_market` | Batch sinkron penerapan depth; recorder dipanggil pada `:549` sebelum penerapan book | Stack main thread, runtime CPU, interval scheduling dan ID update |
| `market.py:191` `Recorder.write` | JSON, kompresi gzip, write; flush setiap ≥1 detik dan rotasi per jam, berjalan sinkron | Stack JSON/gzip/flush + CPU versus syscall/off-CPU yang tumpang tindih |
| `market.py` Book, `:143` quote dan `:151` price_for | Decimal, update dictionary, pengurutan/pencarian harga dan kedalaman | Stack callback + main thread benar-benar on-CPU |
| `engine.py:750` decide; `strategy.py:44` coverage dan `:80` evaluate | Traversal window, median/MAD dan kalkulasi sinyal | Stack dan durasi CPU; tidak mengubah threshold/jadwal evaluator |
| `engine.py:435–436`, `:3520` status; `db.py:300` save_status | Pembentukan status, JSON dan SQLite sinkron; DB timeout 5 detik | Stack status/SQLite, syscall atau futex, durasi off-CPU |
| `websocket_trace.py:221` `_write`, `:231` `_writer` | JSON/gzip pada thread `ws-telemetry`, antrean terbatas | Stack worker/GIL snapshot dan CPU worker; belum diasumsikan menahan main thread |
| Feed parsing dan hook transport | Parsing JSON, enqueue, callback library | Stack + telemetry receipt/enqueue/dequeue; tidak menyamakan receipt aplikasi dengan waktu wire |
| `source_time.py:41` chrony_proof, `:312` pemanggil | subprocess sinkron **sudah lewat `await asyncio.to_thread(...)`** | Stack executor jika relevan; bukan bukti blocking langsung main loop |

REST yang sedang berlangsung bisa berkorelasi dengan stall, tetapi `await`
HTTP tidak dengan sendirinya menahan event loop. Ini daftar kandidat,
bukan penetapan akar masalah. Risk/protection worker tetap tidak disentuh.

## Mengapa bukan profiler yang tersedia atau output CLI standar

GDB attach menghentikan proses; tidak dipilih. Broad strace berpotensi
merekam argumen/buffer dan mengubah timing lewat ptrace; tidak dipilih.
Perf on-CPU saja tidak memisahkan sleeping dari runnable delay, dan stack
Python interpreter saat ini tidak otomatis terbaca sebagai fungsi Python.
Tidak akan mengaktifkan perf support dengan restart atau mengambil raw
user-stack/register dump.

py-spy menyediakan API `PythonSpy::new` dan `get_stack_traces()` dengan
`Config.blocking=LockingStrategy::NonBlocking`; output kita hanya memakai
allowlist fungsi/file/baris, native TID bila tersedia, serta flags
active/owns_gil. Config `native=false`, `dump_locals=0`, `subprocesses=false`,
`gil_only=false`. Tidak memakai fungsi dump, ProcessInfo/argv, env, core dump,
atau serialisasi seluruh StackTrace. [Dokumentasi py-spy](https://github.com/benfred/py-spy/tree/v0.4.1),
[API library](https://github.com/benfred/py-spy/blob/v0.4.1/src/lib.rs),
[config](https://github.com/benfred/py-spy/blob/v0.4.1/src/config.rs).

CLI py-spy tetap berguna untuk agregat, tetapi tidak dipilih untuk capture
ini: exporter speedscope memakai bobot nominal `1/rate`; chrometrace memakai
waktu relatif exporter saat pengolahan sampel, tanpa anchor monotonic absolut
per read. `--duration` pada 0.4.1 dihitung lewat jumlah interval sampling;
deadline eksternal tetap diperlukan. Karena itu rancangan memakai sampler
Rust langsung dengan bracket monotonic setiap pengambilan stack, bukan
mengklaim timestamp CLI cocok persis dengan trace engine.
[Speedscope](https://github.com/benfred/py-spy/blob/v0.4.1/src/speedscope.rs),
[chrometrace](https://github.com/benfred/py-spy/blob/v0.4.1/src/chrometrace.rs),
[record loop](https://github.com/benfred/py-spy/blob/v0.4.1/src/main.rs).

Nonblocking dapat menghasilkan read yang tidak atomik, stack parsial/error,
dan tidak memberikan call-entry/exit tracing. Native unwinding tidak dipakai;
fungsi C yang menjalankan kompresi/SQLite dapat terlihat sebagai frame
pemanggil Python. Tidak boleh menganggap stack yang tidak berubah sebagai
durasi pasti sebuah fungsi. [Batas nonblocking](https://github.com/benfred/py-spy#how-can-i-avoid-pausing-the-python-program).

## Persiapan tool — belum diotorisasi/dikerjakan pada tahap ini

Satu paket sistem yang diperlukan: **bpftrace** beserta dependency RPM-nya
(LLVM/BCC sesuai paket Fedora). Fedora 44 saat dicek menawarkan
**0.24.2-3.fc44**. Setelah izin pemasangan terpisah, command:

```bash
sudo -n dnf install bpftrace
rpm -q bpftrace
sudo -n /usr/bin/bpftrace --info
sudo -n /usr/bin/bpftrace -lv 'tracepoint:sched:sched_switch'
sudo -n /usr/bin/bpftrace -lv 'tracepoint:sched:sched_wakeup'
sudo -n /usr/bin/bpftrace -lv 'tracepoint:raw_syscalls:sys_enter'
sudo -n /usr/bin/bpftrace -lv 'tracepoint:raw_syscalls:sys_exit'
```

Ini hanya command rencana. Tidak memerlukan penurunan ptrace/perf/SELinux,
perubahan DNS/TLS, peningkatan capabilities engine, atau perubahan konfigurasi
engine. Versi lain harus direview ulang terhadap script sebelum capture.
[Paket resmi Fedora](https://packages.fedoraproject.org/pkgs/bpftrace/bpftrace/),
[bpftrace CLI 0.24](https://bpftrace.org/docs/release_024/cli).

Sampler/supervisor dibuat sebagai proyek Rust terpisah, mengikuti workbench
yang telah dibaca, tanpa dependency baru di `.venv`/lockfile produksi:

```bash
rust-workbench new lldesk-profile-capture
cd ~/Workspace/projects/lldesk-profile-capture
cargo add 'py-spy@=0.4.1' --no-default-features
# Implementasi supervisor sesuai kontrak di bawah diperlukan sebelum build.
cargo test --locked
rust-workbench check
cargo build --release --locked
```

Crate py-spy adalah Rust; pin 0.4.1 dan Cargo.lock, tanpa fitur `unwind`.
Ketergantungan build dan kompatibilitas nyata terhadap CPython uv belum
dibuktikan; tidak ada build/install saat ini. Verifikasi pada proses Python
3.12 sintetis terlebih dahulu. Jangan mencoba attach berulang ke produksi
untuk mencari kombinasi yang lolos.

Revisi memerlukan satu binary dengan subcommand `supervise`, `sample-worker`,
dan `root-controller`, serta script BPF. Setelah implementasi/tes disetujui,
binary dan script untuk jalur root dipasang **root-owned, tidak writable oleh
UID pengguna**, misalnya `/usr/local/libexec/lldesk-profile-capture` (0755)
dan `/usr/local/share/lldesk-profile-capture/main-thread.bt` (0444). Hash
keduanya dipatok saat review. Jangan mengeksekusi source/script yang masih
dapat diubah pengguna melalui sudo. Pemasangan tersebut, seperti bpftrace,
belum dilakukan. Systemd terpasang yang diperiksa saat revisi adalah
**259.9-1.fc44**; dukungan properti unit dan notifikasinya wajib diuji di
lingkungan sintetis, tanpa service engine.

## Satu rancangan capture, batas total 900 detik

T0 ditetapkan CLOCK_MONOTONIC **sebelum attach apa pun**. Tidak ada reset,
pengulangan, perpanjangan, reconnect paksa, atau comparator.

| Waktu sejak T0 | Kegiatan |
|---|---|
| 0–60 detik | Baseline dengan procfs 10 Hz dan status cache lokal 1 Hz; profiler belum attach |
| 60–840 detik | Satu attach nonblocking, stack target 50 Hz, BPF scheduling/syscall main TID; waktu attach termasuk interval ini |
| 840–850 detik | Hentikan pengukuran profiler, eskalasi signal bila perlu, dan verifikasi detach |
| 850–890 detik | Observer mencatat pemulihan setelah detach yang terverifikasi; jika belum, terminal cleanup-unverified |
| 890–900 detik | Final check lokal, tutup writer dan manifest; deadline tidak bergantung selesai serialization |

Jika attach lebih dari 10 detik atau gagal, capture terminal. Tidak mencoba
ulang. Durasi profiling riil maksimal 780 detik dan dapat lebih pendek.
Sampling tidak mengejar tick yang terlewat; catat scheduled/start/end dan
jumlah missed tick. Stop pada deadline asli, termasuk bila scheduler terlambat.
Supervisor memakai BOOTTIME juga untuk memutus bila suspend/discontinuity
terdeteksi; bukan memperpanjang window setelah suspend.

### Arsitektur pemilik proses dan deadline

| Aktor | Kepemilikan dan pekerjaan | Batas ketika aktor macet/mati |
|---|---|---|
| Supervisor UID 1000, unit user per UUID capture | Pemilik T0, binding target, state machine, lease dan stop condition; control loop nonblocking 100 ms | Watchdog systemd 1 s dan batas lifetime; root mendeteksi lease hilang atau pidfd supervisor exit |
| Worker sampler, proses terpisah dalam cgroup supervisor | Satu `PythonSpy::new`, kemudian read nonblocking 50 Hz; progress dikirim setelah read selesai | Read selesai terakhir >250 ms atau worker exit memicu abort; `pidfd_send_signal` dan kill-cgroup untuk worker milik capture |
| Worker observer/writer dalam cgroup supervisor | Procfs/status cache, parsing dan disk/serialization lewat antrean bounded | IO tidak ada di control loop; request/write in-flight >2 s, overflow, atau worker exit memicu abort |
| Pengendali root, unit system terpisah per UUID | Pemilik child bpftrace dan pidfd-nya; control loop 25 ms mengawasi supervisor, lease, deadline dan eskalasi | Watchdog systemd 1 s; jika main pengendali mati, systemd menghentikan **seluruh cgroup root** |
| bpftrace serta worker drain/writer root | Emit/parse batas OS dan tulis raw privat; tidak memiliki kebijakan lease | Worker/output macet menghentikan pengukuran; tidak dapat memperpanjang deadline pengendali |

Control loop tidak menjalankan read stack, HTTP, syscall disk, kompilasi BPF,
`wait()` blocking, flush atau fsync. Pekerjaan tersebut ada di proses worker;
writer thread saja tidak cukup sebagai jaminan pemisahan. Loop membaca IPC
nonblocking dan timerfd absolut. Notifikasi WATCHDOG dikirim hanya dari loop
kontrol yang benar-benar membuat progress, bukan thread heartbeat independen
yang tetap hidup saat loop macet.

T0 berisi pasangan monotonic/boottime dan boot ID, dibuat sekali di awal
supervisor sebelum precheck/attach. Deadline worker/root berasal dari T0 yang
sama: attach maksimal T0+70 s, pengukuran profiler berhenti T0+840 s, root
terminal maksimal T0+848 s, fallback manager root diberi ruang sampai sekitar
T0+850 s, pengukuran observer/artefak berhenti paling lambat T0+900 s.
Timer baru, lease, reconnect, spawn lambat, SIGCONT atau receipt command tidak
menggeser deadline. Startup pada deadline yang sudah lewat ditolak tanpa
attach. BOOTTIME membatasi elapsed termasuk suspend; perbedaan baru antara
elapsed BOOTTIME dan MONOTONIC memicu abort, bukan kelanjutan capture.

Batas root T0+848/850 adalah target deadline pengendali/cleanup normal.
Fallback `RuntimeMaxSec` manager bersifat relatif terhadap activation unit;
ini **bukan** bukti cutoff tepat T0+850. Dengan peluncuran sejak T0+60,
startup maksimal 10 s dan perhitungan remaining saat permintaan, fallback
manager dapat terlambat sampai sekitar T0+860 termasuk stop grace 2 s.
Jika ACK startup tidak ada pada T0+70, supervisor meminta stop unit yang
sama; root yang baru mulai sesudah itu menolak BIND/attach. Initial/start
deadline, active-enter manager, runtime fallback yang dihitung, dan cutoff
absolut direkam terpisah. Tidak ada jalur yang sengaja menunggu fallback
untuk meneruskan sampling melewati T0+840.

Worker sampler memasang `PR_SET_PDEATHSIG(SIGKILL)` dan memeriksa parent PID
sesudah pemasangan untuk menutup race parent sudah mati. Ini lapisan tambahan;
cleanup tetap mengandalkan unit/cgroup dan pidfd. Pengendali root adalah anak
system manager, sehingga PDEATHSIG terhadap peluncur sudo **tidak** dipakai
sebagai pengganti lease/pidfd supervisor.
[Parent-death signal](https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html).

Supervisor menunggu readiness worker/pengendali sampai deadline attach asli,
bukan memakai `timeout 780s` yang dimulai sesudah handshake. Kegagalan atau
exit tidak memulai worker/pengendali pengganti. Kondisi terminal irreversible.
Progress worker adalah sequence **read yang telah selesai**; heartbeat dari
worker yang sedang berada dalam satu read macet tidak memperbaruinya. Ambang
250 ms berlaku setelah sampler ready; initialization tetap dibatasi deadline
attach 10 s. Heartbeat observer/writer juga memuat sequence operasi selesai
dan waktu operasi in-flight, bukan sekadar thread masih ada.

### Binding, lease, dan pengendali root

Sebelum root boleh memulai bpftrace, pengendali menerima satu `BIND` melalui
socket AF_UNIX SOCK_SEQPACKET privat dan memvalidasi SO_PEERCRED, UUID capture,
UID/PID/start ticks supervisor, boot ID, PID/start ticks/run/source target,
hash tool/script, serta seluruh deadline absolut. Root membuka pidfd untuk
supervisor dan child miliknya; pemeriksaan start ticks diulang untuk menutup
race sebelum binding. Metadata engine hanya referensi, **bukan target signal**.
Socket EOF saja tidak cukup karena FD mungkin masih diwarisi worker; pidfd
supervisor tetap dipantau. FD kontrol harus close-on-exec dan tidak diwariskan
ke worker lain.

`HEALTH` setiap 100 ms mempunyai sequence monotonik, phase, progress worker,
dan stop-condition bits. Lease root maksimal **500 ms**, dibatasi deadline
asli. Pesan duplikat/out-of-order, masa depan, binding berbeda, atau message
yang hanya mengatakan alive tanpa progress tidak mengesahkan kelanjutan.
Saat ACTIVE, root juga menguji progress sampler maksimum 250 ms. ATTACHING
boleh belum mempunyai sampel pertama, hanya sampai deadline attach T0+70 s.
`STOP` idempotent; hanya mempercepat penutupan. Tidak ada pesan EXTEND/REARM.

Jika supervisor exit, lease expired, progress worker basi, output worker
macet, atau stop condition tercapai, root menetapkan `stop_at` sekali:

```text
stop_at = min(deadline_profiler, waktu_abort_yang_teramati)
SIGINT  pada stop_at
SIGTERM pada stop_at + 1 detik bila child belum exit
SIGKILL pada stop_at + 2 detik bila child belum exit
verifikasi pidfd exit/reap dan owned-cgroup paling lambat stop_at + 3 detik
```

Deadline eskalasi juga absolut dan tidak diulang setelah signal. Signal
dikirim lewat pidfd yang terikat child; tidak memakai angka PID/PGID yang
dapat dipakai ulang, `pkill`, atau nama engine. Pengendali tidak menunggu ACK
SIGINT, marker END, EOF, maupun flush tanpa batas. Jika bpftrace mengabaikan
SIGINT/SIGTERM, ia di-KILL; raw prefix dipertahankan dan evidence disebut
partial, walaupun detach akhirnya terverifikasi. `kill()` berhasil belum
menjadi bukti exit/detach.

Cleanup verified memerlukan owned pidfd exit/reap, seluruh owned descendant
exit, root cgroup tidak populated, serta penutupan FD pemilik attach tanpa
pin/duplikasi ke luar cgroup. Unit inactive saja tidak cukup. Prefix yang
drain/END-nya terpotong tetap partial. Bila pengendali mati, bukti dari manager
dan pidfd/cgroup dipakai jika tersedia; tidak mengarang ACK pengendali.

Kedua unit memakai `Restart=no`, `ExitType=main`, `KillMode=control-group`,
`SendSIGKILL=yes`, `TimeoutStopSec=2s`, watchdog 1 s dengan
`WatchdogSignal=SIGKILL`, dan `LimitCORE=0`. Tidak ada daemon yang keluar
cgroup atau pin BPF permanen. Engine tetap di cgroup servicenya sendiri.
Jika supervisor di-KILL, user manager membersihkan sampler/writer; root
pidfd/lease memicu cleanup root. Jika pengendali root di-KILL atau SIGSTOP,
system manager/ watchdog membersihkan bpftrace dan worker root tanpa menunggu
supervisor. RuntimeMaxSec manager adalah fallback independen, bukan pengganti
deadline absolut dalam pengendali.
[Properti service/watchdog](https://github.com/systemd/systemd/blob/v259/man/systemd.service.xml),
[kill-cgroup](https://github.com/systemd/systemd/blob/v259/man/systemd.kill.xml),
[pidfd](https://man7.org/linux/man-pages/man2/pidfd_open.2.html).

Supervisor dan pengendali merekam scheduled/actual time tiap tindakan stop,
signal, exit, EOF dan verifikasi. Kernel/manager yang tidak mendapat CPU atau
proses di uninterruptible sleep tidak bisa dijamin berhenti tepat pada suatu
nanodetik oleh rancangan userspace. Jangan mengklaim deadline fisik terpenuhi
bila callback terlambat: ukur overshoot dan tandai `CLEANUP_UNVERIFIED` jika
exit/FD release belum terbukti. Sampling baru tidak dilanjutkan setelah batas;
penanganan kill oleh manager tetap berlaku. Capture state, completeness
evidence, dan hasil cleanup adalah tiga status terpisah. Manifest tidak
lengkap karena supervisor mati juga tidak boleh disebut sukses.

### Kontrak command tunggal supervisor

**Command di bawah merupakan spesifikasi CLI binary yang harus dibuat dan
diuji lebih dulu, bukan tool yang sudah terpasang.** Pelaksanaan memerlukan
otorisasi capture baru setelah review. Supervisor resolve PID dari service,
mengikat PID + start ticks + run ID + source hash, dan menolak perubahan
identitas. Jangan menyalin PID historis sebagai target tanpa pemeriksaan.

```bash
umask 077
CAPTURE_UUID='<satu UUID capture baru; dipilih sekali>'
systemd-run --user --collect --service-type=exec \
  --unit="lldesk-profile-${CAPTURE_UUID}" \
  --property=Restart=no --property=ExitType=main \
  --property=KillMode=control-group --property=SendSIGKILL=yes \
  --property=TimeoutStopSec=2s --property=TimeoutStartSec=10s \
  --property=RuntimeMaxSec=898s --property=RuntimeRandomizedExtraSec=0 \
  --property=WatchdogSec=1s --property=WatchdogSignal=SIGKILL \
  --property=NotifyAccess=main --property=LimitCORE=0 --property=UMask=0077 \
  /usr/local/libexec/lldesk-profile-capture supervise \
  --capture-uuid "$CAPTURE_UUID" \
  --service lighter-live-desk-engine.service \
  --status-url http://127.0.0.1:8765/api/status \
  --expected-source-commit 776bec4a6b476d7cd8bd49ffbb71a1b7611fffc5 \
  --duration-s 900 --baseline-s 60 --detach-at-s 840 --finalize-at-s 890 \
  --stack-rate-hz 50 --other-thread-record-rate-hz 5 --proc-rate-hz 10 \
  --status-rate-hz 1 --sample-mode nonblocking \
  --root-controller /usr/local/libexec/lldesk-profile-capture \
  --bpf-script /usr/local/share/lldesk-profile-capture/main-thread.bt \
  --os-retention all-edges-v1 --max-bpf-events 2000000 \
  --max-private-bytes 536870912 --stack-queue-capacity 512 \
  --root-event-queue-capacity 8192 --worker-progress-timeout-ms 250 \
  --root-lease-ms 500 --sigterm-after-ms 1000 --sigkill-after-ms 2000 \
  --output "~/Workspace/outputs/lighter-production-profile-${CAPTURE_UUID}"
```

Placeholder UUID harus diganti sekali, bukan UUID izin trading. Directory
harus baru; jika sudah ada, abort. Folder 0700, file 0600. Path produksi hanya
dibaca; engine bukan child dan tidak berada di cgroup/process group collector.
Systemd-run hanya peluncur: terminal/launcher mati tidak diasumsikan supervisor
mati. Jika supervisor masih hidup, lease dan deadline yang sama tetap berlaku;
jika supervisor mati, kontrak cleanup di atas berlaku.

Pada T0+60 supervisor memulai tepat satu unit pengendali root. Variabel di
bawah berasal dari binding yang sudah disimpan. `ROOT_RUNTIME_S` adalah floor
remaining BOOTTIME sampai T0+848 s saat permintaan unit, dan wajib positif;
tidak diganti 780 s setelah command terlambat. Pengendali tetap menolak
startup/attach yang melewati T0+70 s. RuntimeMaxSec relatif manager dapat
dimulai terlambat; deadline absolut dalam binding tetap otoritatif.

```bash
sudo -n /usr/bin/systemd-run --collect --service-type=exec \
  --unit="lldesk-profile-root-${CAPTURE_UUID}" \
  --property=Restart=no --property=ExitType=main \
  --property=KillMode=control-group --property=SendSIGKILL=yes \
  --property=TimeoutStopSec=2s --property=TimeoutStartSec=10s \
  --property="RuntimeMaxSec=${ROOT_RUNTIME_S}s" --property=RuntimeRandomizedExtraSec=0 \
  --property=WatchdogSec=1s --property=WatchdogSignal=SIGKILL \
  --property=NotifyAccess=main --property=LimitCORE=0 --property=UMask=0077 \
  /usr/local/libexec/lldesk-profile-capture root-controller \
  --capture-uuid "$CAPTURE_UUID" --control-socket "$CONTROL_SOCKET" \
  --owner-uid "$SUPERVISOR_UID" --owner-pid "$SUPERVISOR_PID" \
  --owner-start-ticks "$SUPERVISOR_START_TICKS" \
  --deadline-monotonic-ns "$ABSOLUTE_DETACH_MONOTONIC_NS" \
  --deadline-boottime-ns "$ABSOLUTE_DETACH_BOOTTIME_NS" \
  --hard-deadline-monotonic-ns "$ABSOLUTE_ROOT_HARD_MONOTONIC_NS" \
  --hard-deadline-boottime-ns "$ABSOLUTE_ROOT_HARD_BOOTTIME_NS" \
  --lease-ms 500 --sigterm-after-ms 1000 --sigkill-after-ms 2000
```

Sesudah BIND/lease valid, root controller meluncurkan child berikut, dengan
env yang disanitasi dan path/hash script tetap; stdout/stderr masuk drain
worker bounded, bukan pipe blocking dalam loop kontrol:

```bash
/usr/bin/bpftrace -q /usr/local/share/lldesk-profile-capture/main-thread.bt \
  "$ENGINE_MAIN_TID" "$ABSOLUTE_DETACH_MONOTONIC_NS"
```

Command launcher yang responsnya ambigu diperiksa dengan UUID/unit yang sama;
tidak diulang atau dibuat unit pengganti. Root menolak binding berbeda/expired
sebelum attach. Artefak dibuka lewat directory FD yang divalidasi, tanpa
mengikuti symlink. Root tidak menerima arbitrary command/path/args dari socket.
Seluruh jalur capture tidak memuat akun/key, signer atau engine sebagai library.

### Kontrak BPF, tanpa argumen/payload

Script dipatok ke **main TID**. Tidak mencetak comm/argumen task lain, stack
native/user memory, pathname, FD, buffer, header, syscall argument array,
atau return value. Hook global tetap mempunyai biaya kernel pada event
non-target; overhead tersebut harus diukur, bukan diasumsikan nol.

1. `tracepoint:sched:sched_switch`: **emit setiap edge** switch-out/in target,
   waktu sumber, raw `prev_state` dan CPU. Tidak hanya emit pasangan yang
   durasinya melebihi threshold. State awal/batas attach tetap unknown.
2. `tracepoint:sched:sched_wakeup`: emit setiap wakeup target; pemilihan wakeup
   pertama yang relevan dan pemisahan sleep/runqueue dilakukan offline.
3. `tracepoint:raw_syscalls:sys_enter`/`sys_exit`, predicate `tid==target`:
   emit setiap enter/exit, hanya syscall **ID** dan monotonic edge timestamp.
   Jangan membaca `args.args[]`. ID dipetakan offline ke tabel syscall
   arsitektur yang dipatok; tidak melakukan tebakan dari durasi.
4. Semua timestamp eksplisit `nsecs(monotonic)`, **bukan `nsecs` default boot**.
   **Tidak ada filter retensi 2 ms**. Count/total/max hanya turunan tambahan,
   bukan pengganti timestamp event pendek. Rekam heartbeat tracer 1 Hz,
   attachment-ready dan detach marker serta counter emitted/lost per jenis.
   Interval terbuka di awal/akhir disebut censored, tidak diberi durasi lengkap.
5. Map state tetap bounded untuk satu TID; batas output **2.000.000 event**.
   Tidak memakai `system`, `signal` dari BPF, uprobe engine, atau `--unsafe`.
   Kehilangan event/ringbuffer dan error stderr disimpan serta menghentikan
   capture; tidak diabaikan. Script tidak boleh menunggu writer engine.

Format kernel yang benar-benar dibaca menunjukkan state mask `0xff` dan
preemption bit `0x100`; pertahankan raw value. Kernel berbeda wajib divalidasi
ulang. R/preempted menunjukkan runnable, bukan blocking I/O. Sleep S/D tidak
dengan sendirinya membuktikan disk/network. Jadwal BPF dan wall time bukan
timestamp penerimaan wire.
[Semantik timestamp bpftrace](https://bpftrace.org/docs/release_024/stdlib#nsecs).

### Format retensi waktu dan volume — all-edges-v1

Raw OS disimpan privat sebagai header epoch + chunk record **fixed 96 byte,
little-endian**. Header memuat UUID capture, boot ID, run/PID/start ticks,
hash script/schema dan bracket attach/detach. Enum mencakup SWITCH_IN,
SWITCH_OUT, WAKEUP, SYS_ENTER, SYS_EXIT, READY, LOSS dan END. Format record:

| Field | Ukuran |
|---|---|
| schema_version / kind / flags | u16 / u16 / u32 |
| kernel_monotonic_ns / collector_receive_monotonic_ns | u64 / u64 |
| collector_seq / provider_seq | u64 / u64 |
| cpu / target_tid | u32 / u32 |
| state_raw / syscall_id | i64 / i64 |
| aux0 / aux1 / reserved / crc64 | u64 / u64 / u64 / u64 |

Flags menandai field tidak tersedia; bukan angka nol sebagai timestamp valid.
Aux hanya enum/counter/metadata waktu yang dipatok, tanpa argumen syscall.
`provider_seq` null bila provider tidak menyediakannya; jangan menyamakan
collector_seq dengan deteksi loss kernel. BPF counter emitted per jenis/CPU
dicocokkan dengan jumlah raw parsed setelah drain. Lost callbacks/stderr,
queue drop, count mismatch, chunk CRC/hash gagal, atau END/counter akhir
yang tidak tersedia menghasilkan batas completeness eksplisit. Count cocok
hanya membuktikan retensi event yang diamati probe, bukan latensi wire atau
tidak adanya semua kemungkinan miss di kernel.

Transport BPF→drain berupa baris angka/enum yang dibatasi **384 byte**, kemudian
dinormalisasi ke record fixed; parser menolak baris terlalu panjang/terpotong.
Output syscall argument array/comm/payload tetap dilarang. Chunk maksimal
4.096 event dengan CRC setiap record dan SHA-256 chunk; chunk parsial terakhir
tetap disimpan sebagai partial. Urutan write/receipt tidak menggantikan waktu
event kernel. Urutan lintas CPU disusun offline menurut timestamp/kausalitas;
tie yang tidak bisa diselesaikan tetap ambiguous.

Tidak ada downsampling edge, penggabungan interval pendek, atau rotasi yang
menghapus bagian awal ketika ruang habis. Batas volume revisi:

| Bagian | Pagu hard |
|---|---|
| Raw OS | 2.000.000 × 96 = **192.000.000 byte**, plus header/chunk; file ≤192 MiB |
| Stack allowlist | ≤192 MiB |
| Procfs / status cache / control-loss-manifest | masing-masing ≤16 MiB |
| Seluruh artefak privat | ≤512 MiB, termasuk reserve sampai 80 MiB |
| Queue stack / queue event root | 512 / 8.192 item bounded |

2.000.000 event dalam 780 s berarti envelope rata-rata **2.564 event/s**,
bukan estimasi workload yang telah diukur. Wire maksimal baris 384 byte
berarti envelope sekitar 0,94 MiB/s; biaya parse/write harus diuji. Pagu
collector/sampler RSS tetap 256 MiB; RSS pengendali+bpftrace+drain dilaporkan
terpisah dan dibatasi 1 GiB. Jika volume/rate/overhead tidak layak, abort dan
revisi metode, **bukan** kembali ke agregasi pendek yang kehilangan overlap.
Alert kapasitas pada 90% memulai cleanup sambil menyisakan tail; event kedua
juta/512 MiB adalah hard cap. Cap atau queue loss membuat epoch partial.

Setiap sampel stack yang dilewati/error mempunyai scheduled time dan bracket
error/progress. Jangan mengisi interval antarsampel dengan fungsi terakhir.
Checkpoint periodik berisi parsed counts, byte offset/hash, producer/loss
counters yang tersedia, dan timestamp observasi. Checkpoint bukan klaim
bahwa seluruh event sebelum wall/monotonic tersebut sudah tiba; count pada
checkpoint bukan watermark lintas CPU yang dipaksakan.

### Timestamp, identitas, dan volume sampler

Sampler Rust memakai satu CLOCK_MONOTONIC host, bukan waktu relatif yang
dianggap sama begitu saja. Untuk setiap read stack:

```text
capture_uuid, engine_run_id, pid, proc_start_ticks
scheduled_monotonic_ns, read_start_monotonic_ns, read_end_monotonic_ns
wall_ns, boottime_ns, native_tid|null, python_thread_id
active, owns_gil, frames[{function,file,line}], error_code|null
```

Hasil stack adalah observasi dalam **bracket read_start–read_end**, bukan titik
atomik. `native_tid` tidak tersedia menjadi null, tanpa menebak dari urutan
thread. Main TID disahkan melalui frame entrypoint/main loop; thread lain
dipetakan hanya saat native ID tersedia. Simpan main thread 50 Hz dan thread
Python lain 5 Hz, meski API mengambil seluruh stack thread pada setiap read;
biaya read seluruh thread tetap dihitung. Jangan filter hanya pemilik GIL
karena itu akan menyembunyikan kasus I/O. Flag GIL adalah observasi yang bisa
berubah selama nonblocking read, bukan bukti eksklusivitas interval.

Gunakan bounded queue 512, writer terpisah, payload stack dibatasi 32 KiB.
Queue penuh/error/disk cap menghentikan capture dengan counter drop; tidak
menahan engine. Jangan mencetak objek StackTrace lengkap: `locals`,
`process_info`, argv/env, bytes memori atau konstanta code tidak diserialisasi.

## Korelasi dengan bukti produksi yang sudah dikonsumsi

Gunakan file transport/market produksi yang ada, offset awal/akhir, run ID,
dan prefix lengkap saat gzip belum ditutup. Tidak menambah subscription,
REST snapshot, kalibrasi clock, ping, atau polling akun.

- Lag timer: tautkan `scheduled_ns` sampai actual callback `monotonic_ns`,
  event ID, queue/context dan active REST. Pulse last-frame periodik bukan
  bukti waktu setiap frame.
- Depth: identitas connection ID + `U/u/pu/E/T` dan payload hash; receipt,
  queued, recorded monotonic. Pasangkan marker application processing yang
  benar-benar tersedia. Durasi processing yang hanya ada sebagai last-value
  tidak boleh diterapkan kepada seluruh update historis.
- `recorded_monotonic_ns` ditetapkan sebelum I/O recorder selesai. Jangan
  menganggap receipt→recorded sudah mengukur seluruh write/flush.
- Connection metadata handshake mengikat run/channel/socket-lifetime;
  reconnect membuat epoch baru. Jangan menggabungkan event dari connection
  lain hanya karena FD atau peer sama.
- Status cache lokal direkam dari T0, allowlist `decision_inputs`/guard,
  quotes, clock proof, processing, queues, logger counters, akun/budget,
  activation/session/permissions. Simpan hanya perubahan unik dan timestamp
  status asli. Polling cache 1 Hz dapat melewatkan keputusan engine 4 Hz;
  jangan menganggapnya seluruh populasi keputusan.
- Wall hanya untuk tampilan; kalkulasi interval memakai monotonic. Clock
  source dipakai dari proof yang tersedia saat itu dengan TTL/drift asli.
  Replay source freshness dibedakan dari keputusan produksi sebenarnya.

### Aturan klasifikasi hasil

| Hipotesis | Bukti yang diperlukan | Yang belum boleh diklaim |
|---|---|---|
| Callback sinkron/CPU menahan loop | Stack main thread berulang pada jalur sinkron, delta CPU tinggi dan interval scheduled-on-CPU yang dibuktikan edge lengkap | Frame pemanggil Python tidak mengidentifikasi instruksi native tertentu; scheduled-on-CPU juga bisa mencakup interrupt/kernel, bukan durasi fungsi Python pasti |
| Blocking I/O/wait lokal | Stack write/flush/SQLite/lock atau pemanggilnya, syscall beririsan, dan sleeping/off-CPU sebelum wakeup; bedakan epoll/select asyncio normal | Syscall panjang dapat mencakup preemption; futex tidak otomatis disk I/O; sleeping tidak otomatis network |
| Keterlambatan scheduling OS | Target runnable sejak preemption atau wakeup tetapi baru switch-in jauh kemudian, didukung delta runqueue wait/cgroup/CPU | Tidak membuktikan penyebab host/ISP/upstream tertentu, atau siapa kompetitor CPU |
| Belum dapat dipisah | Stack parsial, read bracket terlalu lebar, trace lost, interval boundary, atau kasus tidak muncul | Jangan memilih salah satu sebab berdasarkan lag saja |

Untuk setiap episode lag, window **L=[scheduled_ns, callback_actual_ns)**
terikat run/event/connection/update ID yang memang tersedia. Buat empat set
interval disjoint dari edge lengkap: C=scheduled-on-CPU, S=sleep sebelum
wakeup, R=runnable menunggu switch-in, U=unknown. **C dibentuk dari switch-in
dan switch-out yang sah; bukan `L minus off-CPU yang tersimpan`.** Initial
state, edge pendek yang hilang, timestamp tidak tersedia dan boundary tidak
ditutup masuk U. Tidak memakai procfs snapshot/CPU counter/GIL/heartbeat
untuk mengisi U atau antarsampel stack.

```text
C_L = union(C intersect L); S_L = union(S intersect L)
R_L = union(R intersect L); U_L = L minus union(C_L, S_L, R_L)
|C_L| + |S_L| + |R_L| + |U_L| = |L|  (integer nanodetik sebelum pembulatan)
```

Kondisi salah urutan/overlap yang tidak dapat diselesaikan mengubah wilayah
yang terdampak menjadi U, bukan memaksa persamaan lewat label CPU. Syscall
adalah axis lain: laporkan union syscall∩L serta potongannya dengan C/S/R/U;
jangan menambahkan syscall duration kepada jumlah state scheduler. Banyak
sleep pendek tetap dimasukkan pada posisi aslinya. Agregat per detik/minute
tidak mempunyai informasi untuk menghitung overlap per episode.

Aturan completeness dan ambiguous:

- State machine dimulai unknown. Re-anchor hanya dengan transisi valid yang
  baru; attachment READY atau pulse saja tidak mengesahkan state sebelumnya.
- Missing enter/exit atau ID syscall tidak cocok: interval syscall tersebut
  unknown/censored. Ini tidak otomatis menghapus state scheduler yang
  independen dan terbukti lengkap; loss stream yang scope-nya tidak jelas
  tetap membatalkan attribution gabungan.
- Loss yang waktunya bisa dibatasi secara sah membatalkan wilayah batas itu
  sampai re-anchor; batasnya tidak dibuat dari waktu penerimaan laporan loss.
  Jika loss hanya diketahui lewat count akhir/perf report tanpa batas sumber,
  **seluruh epoch terpengaruh menjadi ambiguous untuk attribution OS**.
  Event individual yang masih ada tetap merupakan observasi, bukan proof
  bahwa tidak ada edge lain di antaranya. END/counter yang hilang saat KILL
  tidak boleh disebut epoch complete.
- Episode dengan U>0 dilaporkan partial/ambiguous, beserta durasi known,
  unknown dan alasannya. Jangan menyimpulkan satu penyebab dominan dari
  sisa interval yang belum terukur. Baseline tanpa BPF berarti OS attribution
  unknown, bukan on-CPU selama 60 detik.
- Stack bracket yang melintasi beberapa state, >20 ms, partial/error atau
  tidak ada sampel: fungsi penyebabnya ambiguous. Jangan last-value-hold frame
  sampai sampel berikutnya. Bahkan U=0 tidak otomatis membuktikan callback
  tertentu bila stack/native evidence tidak cukup.
- Censored start/end, chunk terpotong, tie lintas CPU, target/epoch berubah,
  atau denominator decision cache tidak lengkap dicatat sebagai keterbatasan
  terpisah, bukan dibuat lolos melalui replay yang mengisi lubang.

Epoll tidur ketika tidak ada kerja adalah idle normal; relevansi terhadap
lag memerlukan deadline/kerja yang benar-benar tertunda. Sampling 50 Hz dapat
melewatkan callback pendek. Ketiadaan sampel bukan ketiadaan blocking, dan
root/controller callback yang terlambat juga tidak membuktikan OS sebagai
akar masalah tanpa evidence scheduler yang sesuai.

## Overhead dan penghentian

Procfs 10 Hz mengambil main-thread stat/schedstat/state/wchan, task-list
identity 1 Hz, total CPU/context-switch/I/O counter engine dan collector,
serta cgroup cpu.stat. Tidak membaca cmdline/environ/mem/stack melalui procfs.
Penghitung per-thread menambah informasi terhadap total process. `schedstat`
adalah counter kumulatif CPU/runqueue/timeslice, bukan attribution callstack;
validasi delta dan ketersediaan sebelum memakai nilainya.
[Dokumentasi kernel](https://docs.kernel.org/scheduler/sched-stats.html).

Bandingkan baseline 60 s, profiling, dan pasca-detach: CPU engine/main thread
dan collector dalam unit core, context switches, lag p50/p95/max, missed
observer ticks, backlog pasar/account, telemetry queue/write/drop, stack read
duration/error, BPF event/loss, dan byte/RSS collector. Pisahkan baseline
observer-only dari biaya profiler tambahan. CPU/kernel overhead tidak seluruhnya
dibebankan ke process profiler; periksa CPU host agregat/cgroup juga.
Perbedaan fase pasar bukan eksperimen kausal sempurna.

Kriteria penghentian capture berikut hanya untuk keselamatan pengukuran;
tidak mengubah guard trading:

- T0+840: stop sampling profiler dan mulai detach; ikuti deadline eskalasi
  absolut. T0+900: seluruh pengukuran berakhir, tanpa memperpanjang.
- Identitas PID/start ticks/run/source berubah; account state tidak segar
  atau tidak dapat diverifikasi; exposure/order/intent/pending activation
  muncul; sesi tidak closed/order tidak OFF; atau journal tidak sehat.
- Attach gagal/tidak selesai dalam 10 s, permintaan elevated access ditolak,
  stack main thread tidak dapat dipetakan, atau schema tracepoint tidak cocok.
- Collector/trace kehilangan event, writer error, queue penuh, stack error
  >5% pada window 10 s, atau p95 read stack >10 ms pada window 10 s.
- CPU total collector/profiler >0.10 core selama 10 s; RSS collector/sampler
  >256 MiB atau pengendali/bpftrace/drain >1 GiB; mulai cleanup pada 90% pagu
  artefak/event, hard cap 512 MiB/2.000.000 event; ruang kosong <1 GiB.
- Event-loop lag baru >500 ms, atau ≥3 event >250 ms dalam 10 s selama attach;
  market queue >128 pada dua status cache berurutan; logger produksi mulai
  drop/error. Catat bahwa penghentian dapat dipicu workload asli juga, bukan
  langsung membuktikan profiler penyebabnya.
- Status cache tidak diperbarui >2 s, observer terlambat >1 s, suspend,
  discontinuity wall/monotonic, atau child profiler tidak sehat.
- Progress sampler >250 ms sesudah ready, observer/write in-flight >2 s,
  lease >500 ms, pidfd supervisor exit, atau watchdog manager expired.
  Heartbeat tersendiri tidak membatalkan kondisi tersebut.

Stop hanya collector/profiler; tidak mematikan engine/risk worker, mengirim
cancel/close/order, memulihkan izin, atau melakukan restart. Jika akun tidak
bersih, laporkan dan biarkan mekanisme risiko produksi berjalan. Tugas capture
kelak tetap tidak memberi izin trading.

## Uji sebelum produksi dan keluaran review

Belum ada tes profiler dijalankan dalam tahap ini karena belum ada collector
dan kode engine tidak berubah. Sebelum capture harus lolos tes terisolasi:

1. Python 3.12 sintetis: busy callback, blocking local pipe/file wait, normal
   epoll idle dan runnable scheduling delay; tanpa akun/SDK/signer engine.
2. Synthetic argv/locals/buffer mengandung canary secret; seluruh output,
   error, stderr dan manifest harus bebas canary/argv/locals. Tidak menyimpan
   core atau memory dump untuk pemeriksaan.
3. TID mapping, missing metadata, thread exit, PID reuse; nonblocking tidak
   mengirim SIGSTOP/ptrace-stop. Uji attach gagal tidak menyalakan fallback
   blocking/native atau retry produksi.
4. Deadline absolut, setup lambat, cancellation, BPF loss, slow writer,
   overflow, output cap, shutdown timeout; tidak ada signal ke target engine.
5. Timestamp bracket nyata, missed ticks dan censored interval. BPF
   S/R/preempt/wakeup→switch-in classification memakai format kernel aktual.
6. Offline overhead pada workload sintetis yang sama dengan/ tanpa collector;
   hasil ini bukan overhead engine produksi. BPF `--dry-run` juga **attach**,
   sehingga jangan dijalankan pada tahap read-only ini.

### Kontrak uji retensi/overlap — belum dijalankan

| Kasus sintetis | Hasil wajib |
|---|---|
| Episode 10 ms dengan sleep 0,4 ms dan runnable-wait 0,2/0,3 ms | Edge pendek tetap tersimpan; C=9,1 ms, S=0,4 ms, R=0,5 ms, U=0 |
| Hapus satu edge; loss scope tidak dapat dibatasi | Episode/epoch ambiguous; tidak mengubah area hilang menjadi CPU meski count procfs naik |
| Loss range terpercaya 2,4–5,3 ms pada fixture 10 ms | Area itu U=2,9 ms; tetap C=6,7 ms, S=0,4 ms, R=0; semua state menjumlah 10 ms |
| Syscall 1–3 ms pada fixture utuh | Syscall=2 ms, potongan C=1,4/S=0,4/R=0,2 ms; tidak double-count terhadap 10 ms state |
| Banyak off-CPU <2 ms dalam episode panjang | Output raw mempertahankan seluruh timestamp; hasil sama dengan ground truth, tidak hanya count/total |
| Out-of-order delivery lintas CPU, duplicate, timestamp tie | Normalisasi idempotent bila identitas pasti; tie/duplikasi yang tidak pasti tetap ambiguous, bukan reordering tebakan |
| Drop kernel/perf buffer, stdout, parser atau writer | Counter/CRC/END dibandingkan; stop, prefix dipertahankan dan scope completeness disebut eksplisit |
| File cap, item kedua juta, raw line >384 B, partial chunk | Tidak overwrite/rotate-away/downsample; abort dan unknown/censored sesuai scope |
| Stack error/missed tick/bracket lintas sleep, native TID null | Tidak memperpanjang fungsi terakhir atau mengarang TID; scheduler evidence dan stack ambiguity terpisah |
| Attach/detach di tengah syscall atau off-CPU | Boundary unknown/censored; tidak membentuk state sebelum first valid edge |

Fixture numerik utuh memakai edge: IN 0; OUT-S 2; WAKE 2,4; IN 2,6;
OUT-R 5; IN 5,3; OUT 10 (ms). Ini data sintetis, bukan runtime. Test integer
nanodetik harus menguji union/intersection sebelum display rounding. Untuk
kasus loss-range, range berasal dari generator/marker yang diketahui saat
itu; algoritma tidak boleh membuat range tersebut dari bukti masa depan.

### Kontrak uji ownership/deadline/cleanup — belum dijalankan

Jalankan deterministic fake-clock/state-machine tests dahulu, lalu uji
integrasi proses dan unit pada host terisolasi dengan **target Python dummy**
dan dummy profiler/root controller, bukan PID engine. BPF test terpisah
menarget dummy yang sama; tidak ada impor account, signer atau key.

| Failure injection | Hasil wajib |
|---|---|
| Read stack berhenti kembali / worker SIGSTOP | Progress tidak maju meski heartbeat lain hidup; abort setelah ambang 250 ms teramati, root STOP, worker direap; target dummy tidak menerima signal |
| Writer/procfs/cache request macet | Loop supervisor masih hidup; operasi >2 s mengakhiri capture; queue tidak menahan kontrol |
| Supervisor SIGKILL setelah root attach | Pidfd/lease root memulai eskalasi; user manager membersihkan worker; root unit/cgroup akhirnya kosong |
| Supervisor SIGKILL sebelum BIND atau saat startup | Root tidak attach; pidfd/credential gagal/EOF membatalkan; tidak ada pengganti |
| Supervisor SIGSTOP sementara worker/root tetap hidup | Lease 500 ms atau watchdog mengakhiri capture; SIGCONT tidak melanjutkan sampling |
| Root controller SIGKILL/SIGSTOP saat profiler hidup | System manager/watchdog kill root cgroup; sampler dihentikan supervisor, bukan dilanjutkan tanpa OS tracing |
| Profiler mengabaikan INT; lalu mengabaikan INT+TERM | Signal times berturut stop/stop+1/stop+2 s; KILL dan pidfd exit benar-benar diverifikasi, bukan ACK; prefix partial |
| Child root fork descendant / non-root worker fork descendant | KillMode=control-group membersihkan seluruh descendant; tidak tersisa FD pemilik attach; cgroup engine dummy lain tetap utuh |
| Startup terlambat 9/11 s, command ACK ambigu/duplicate | 9 s memakai sisa waktu asli; 11 s tidak attach; lookup UUID/unit yang sama, tidak spawn/extend/retry |
| PID reuse, socket masih diwarisi child, lease reorder/future | Pidfd/credentials tetap menunjuk pemilik asli; tidak tertipu PID/EOF/heartbeat palsu |
| Deadline ketika syscall/serialization sedang macet | Timer supervisor/root independen; stop memakai deadline asli, drain bounded, hasil incomplete bila END/hash final hilang |
| Wall step/suspend/resume atau seluruh loop kontrol terlambat | Abort, simpan scheduled/actual dan overshoot; tidak menyebut semua tindakan tepat waktu, tidak reset budget |
| SIGKILL belum menghasilkan exit/reap pada deadline verifikasi | Simulasi cleanup-unverified; tidak klaim detach/manifest sukses; manager terus menegakkan kill tanpa sampling baru |
| Foreground shell/systemd-run launcher mati, supervisor masih hidup | Tidak salah menganggap launcher sebagai owner; UUID dan deadline asli tetap, tanpa peluncuran ulang |

Assertions integrasi: identitas pidfd, state monotonic irreversible, urutan
signal, `cgroup.events populated=0` atau unit sudah GC dengan semua owned
pidfd exited, tidak ada BPF pin/FD yang diwariskan, dan tidak ada restart
otomatis. Unit notification/watchdog dan properti yang tidak didukung harus
gagal sebelum capture. Uji perhitungan fallback manager secara terpisah dari
deadline pengendali: callback/manager lateness terukur tidak diluluskan lewat
perbandingan timer nominal saja. Canary test mencakup output unit/journal
yang mungkin dibuat manager, selain file raw dan stderr collector.

Hasil capture nanti: manifest versi tool/Rust/lockfile/binary/script hash,
baseline source/config hash, run/PID/start ticks, wall/mono/boot anchor,
denominator yang benar-benar terukur, per-phase overhead, klasifikasi tiap
lag beserta C/S/R/U dan syscall overlap, stack uncertainty, update/connection
ID, counter/loss scope, actor/unit/pidfd identity, signal/deadline actual,
cleanup status dan alasan stop. Tidak menggabungkan evidence lengkap dengan
cleanup yang sukses: keduanya harus dilaporkan terpisah.
Raw stack, procfs, tuple socket jika dirujuk, dan status privat berada di
`Workspace/outputs` 0700; hanya ringkasan aman dan hash masuk repo. Tidak
mengunggah profile ke layanan visualisasi eksternal.

Keputusan tahap ini: **metode disarankan untuk persiapan tooling terisolasi,
belum disetujui sebagai capture produksi yang sudah siap jalan**. Tidak ada
penyebab stall ditetapkan. Revisi hanya dokumen; versi/akun di bawah merupakan
pemeriksaan terakhir sebelumnya, bukan verifikasi runtime baru saat revisi.
Tidak ada perubahan guard atau kebijakan empat fee exact NULL.

Pemeriksaan penutup **02:03:34 WIB**: PID/run ID sama, 24 hash source dan
config tetap cocok; umur status cache 0,193 detik, komponen akun/orders
1,386/1,492 detik, akun flat, order/intent/pending activation nol, kas matched
tanpa selisih, saldo **49.430756 USDC**, IDLE/order OFF/session closed.
Jurnal tetap enam intent dan 21 command; tidak ada command kontrol baru.
Hanya dokumen rancangan ini ditambahkan; belum di-commit agar dapat ditinjau.

Revisi reviewer tidak memasang/membangun tool, menjalankan test harness,
membuat transient unit, attach, capture, atau polling runtime baru. Repo
baseline tetap a352a52; command di dokumen belum dijalankan. Langkah berikut
tetap implementasi dan uji collector terisolasi atas kedua kontrak revisi ini.

Validasi revisi dokumen: syntax lima blok shell diperiksa dengan `bash -n`
tanpa mengeksekusi command; layout record berjumlah 96 byte, pagu bagian
menyisakan reserve 80 MiB, dan aritmetika fixture overlap/unknown konsisten.
Tidak ada diagnostik whitespace. Ini validasi spesifikasi, **bukan** uji
sampler, BPF, unit systemd atau cleanup proses yang telah dijalankan.
