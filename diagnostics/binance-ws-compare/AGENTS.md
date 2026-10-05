# Panduan proyek Rust

- Utamakan Rust; ikuti `rust-toolchain.toml` dan simpan `Cargo.lock`.
- Dependency aplikasi hanya ditambahkan di `Cargo.toml` proyek sesuai kebutuhan.
- Jalankan `rust-workbench check` setelah perubahan. Jika tool itu tidak tersedia, ikuti perintah pada README.
- Tambahkan test untuk perilaku aplikasi yang penting. Template awal belum memiliki test aplikasi.
- Jalankan coverage saat perlu mengukur cakupan pengujian, bukan sebagai pengganti test yang bermakna.
- Pipeline default memakai `--all-features`; sesuaikan bila aplikasi mempunyai fitur yang saling eksklusif.
