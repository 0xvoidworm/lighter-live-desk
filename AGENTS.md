# Lighter Live Desk local rules

- This repo is a single account, single market experiment. No order may be sent unless `RUNBOOK.md` live preflight is satisfied.
- Never copy, print, log, or commit API keys. `secrets.json`, `config.json`, `state/`, and market recordings stay untracked.
- An API acknowledgement is not a fill or a completed cancel. On uncertainty, freeze new entries and reconcile with Lighter.
- Keep the engine separate from the API and UI. Restart always begins in RECONCILING and never resumes live entry by itself.
- Use the pinned official Lighter SDK for signing. Never implement custom signing.
- Changes to risk guards need meaningful failure tests. Keep all market values as `Decimal` or scaled integers.
- Do not modify Binance Desk or any other project while working here.
