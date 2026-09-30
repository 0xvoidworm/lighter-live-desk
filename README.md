# Lighter Live Desk

Local BTC perpetual desk. The engine and dashboard API run as separate processes. The initial configuration is **observe only**; it can display public live data without any key or funds.

## Quick start

```bash
cd ~/lighter-live-desk
cp config.example.json config.json
uv sync --extra dev --python 3.12
cd frontend && npm ci && cd ..
./dev.sh
```

Open `http://127.0.0.1:5173`. The engine is the only order sender. Restarting the browser or dashboard does not create another engine.

See [RUNBOOK.md](RUNBOOK.md) for user services, keys, funding, stop and recovery. See [PROGRESS.md](PROGRESS.md) for verified status and [AUDIT_2026-09-30.md](AUDIT_2026-09-30.md) for the Binance and order-path evidence. Mainnet orders remain untested.
