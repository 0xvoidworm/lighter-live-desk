# Specification v1

One Standard Lighter BTC perpetual account, one position maximum, Binance USD-M BTCUSDT as public comparator. The backend is Python 3.12; the UI is React/TypeScript. The dashboard only binds to loopback. No API key enters the browser.

State machine: STARTING → RECONCILING → WARMING_UP → READY → LIVE; feed/account uncertainty or an equity loss stop goes to PAUSED or RECOVERING; journal failure or exhausted bounded exit attempts goes to HALTED. Restart always re-enters RECONCILING. An acknowledgement is separate from acceptance, fill, and cancel completion.

Risk limits: funding ≤$50 equivalent including transfer, one BTC position, entry ≤$25 notional and ≤1× dedicated equity, planned risk ≤$0.50 including exit reserve, daily equity drawdown ≥$5 stops entries until next Jakarta day, total experiment loss ≥$50 halts. Pending entries count toward capacity. No automatic top-up, compounding, martingale, or leverage increase to meet order minimum.

Signal is a conservative, transparent local deviation heuristic. At least 10 minutes of valid paired data are needed, with rolling historical median/MAD up to 30 minutes. Zero variation or stale/gapped data blocks entry. The strategy is `HEURISTIC_UNVALIDATED` until separately evaluated. No signal is acceptable.

Entry intent is committed before sending an IOC limit order. Unknown outcomes freeze entries. Position and protection derive from confirmed exchange state, not request success. An emergency close is a request with a recorded result, not proof of closure. Exit attempts are bounded at three and escalate to HALTED if exhausted.

Every real-money session has a 30 minute wall clock maximum and three completed positions maximum. A single tagged execution probe may be used after preflight and is excluded from strategy performance.
