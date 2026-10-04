from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from .models import Quote


@dataclass(frozen=True)
class Decision:
    side: str | None
    deviation_bps: Decimal | None
    basis_bps: Decimal | None
    scale_bps: Decimal | None
    reasons: tuple[str, ...]
    version: str = "local-deviation-v1-HEURISTIC_UNVALIDATED"


class DeviationStrategy:
    def __init__(self, warmup_seconds: int = 600):
        self.samples: deque[tuple[float, Decimal, Decimal, Decimal]] = deque()
        self.warmup_seconds = warmup_seconds
        self.last_sample_at = 0.0
        self.local_sell_flow: deque[tuple[float, Decimal]] = deque()
        self.last_binance_mid: Decimal | None = None
        self.comparator_moves: deque[tuple[float, Decimal]] = deque()
        self.started_epoch = time.time()
        self.last_interruption: dict | None = None

    def interrupted(self, venue: str, channel: str, reason: str) -> None:
        # Reconnects interrupt valid sampling; they do not reset historical samples.
        self.last_interruption = {
            "venue": venue,
            "channel": channel,
            "reason": reason,
            "at": time.time(),
            "samples_preserved": len(self.samples),
        }

    def coverage(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        elapsed = now - self.samples[0][0] if self.samples else 0.0
        recent = [x[0] for x in self.samples if now - x[0] <= 600]
        gaps = [(a, b) for a, b in pairwise(recent) if b - a > 5]
        warm = len(self.samples) >= self.warmup_seconds * 8 // 10 and elapsed >= self.warmup_seconds
        covered = len(recent) >= 480 and not gaps
        return {
            "duration_required_s": self.warmup_seconds,
            "duration_observed_s": round(elapsed, 1),
            "duration_remaining_s": round(max(0, self.warmup_seconds - elapsed), 1),
            "total_samples_required": self.warmup_seconds * 8 // 10,
            "total_samples": len(self.samples),
            "window_s": 600,
            "recent_samples_required": 480,
            "recent_samples": len(recent),
            "max_sample_gap_s": round(max((b - a for a, b in pairwise(recent)), default=0), 3),
            "allowed_gap_s": 5,
            "long_gap_count": len(gaps),
            "gap_wait_remaining_s": round(max((a + 600 - now for a, _ in gaps), default=0), 1),
            "last_sample_age_s": round(now - self.samples[-1][0], 1) if self.samples else None,
            "warmup_complete": warm,
            "coverage_complete": covered,
            "ready": warm and covered,
            "buffer_started_at": self.started_epoch,
            "buffer_reset_reason": "engine_start",
            "last_interruption": self.last_interruption,
        }

    def trade(self, price: Decimal, size: Decimal, taker_buy: bool) -> None:
        now = time.monotonic()
        signed = size if taker_buy else -size
        self.local_sell_flow.append((now, signed))
        while self.local_sell_flow and now - self.local_sell_flow[0][0] > 30:
            self.local_sell_flow.popleft()

    def evaluate(
        self, lighter: Quote | None, binance: Quote | None, valid: bool, fx: Decimal | None
    ) -> Decision:
        now = time.monotonic()
        reasons: list[str] = []
        if not valid or lighter is None or binance is None:
            return Decision(None, None, None, None, ("DATA_TIDAK_VALID",))
        if fx is None or abs((fx - 1) * 10000) > 10:
            return Decision(None, None, None, None, ("KURS_USDC_USDT_TIDAK_TERVERIFIKASI",))
        comparator = binance.mid / fx
        raw = (lighter.mid / comparator - 1) * 10000
        if self.last_binance_mid is not None:
            move = (binance.mid / self.last_binance_mid - 1) * 10000
            self.comparator_moves.append((now, move))
        self.last_binance_mid = binance.mid
        while self.comparator_moves and now - self.comparator_moves[0][0] > 10:
            self.comparator_moves.popleft()
        while self.samples and now - self.samples[0][0] > 1800:
            self.samples.popleft()
        # One valid observation per second. Gaps are not filled with synthetic samples.
        if now - self.last_sample_at >= 1:
            self.samples.append((now, raw, lighter.bid_size, lighter.ask_size))
            self.last_sample_at = now
        coverage = self.coverage(now)
        if not coverage["warmup_complete"]:
            return Decision(None, raw, None, None, ("WARMUP_BELUM_LENGKAP",))
        if not coverage["coverage_complete"]:
            return Decision(None, raw, None, None, ("COVERAGE_DATA_KURANG",))
        history = [x[1] for x in self.samples if x[0] < now - 1]
        basis = Decimal(str(statistics.median(history)))
        abs_dev = [abs(x - basis) for x in history]
        scale = Decimal(str(statistics.median(abs_dev))) * Decimal("1.4826")
        if scale <= 0:
            return Decision(None, raw, basis, scale, ("VARIASI_NOL",))
        residual = raw - basis
        threshold = max(Decimal(8), scale * 4)
        side = "buy" if residual < -threshold else "sell" if residual > threshold else None
        if side is None:
            reasons.append("DEVIASI_KURANG")
        moves = sum((x[1] for x in self.comparator_moves), Decimal(0))
        if side == "buy" and moves < -5 or side == "sell" and moves > 5:
            reasons.append("PASAR_PEMBANDING_BERGERAK_KUAT")
        early = [x[1] for x in self.local_sell_flow if now - x[0] >= 15]
        late = [x[1] for x in self.local_sell_flow if now - x[0] < 15]
        if side is not None:
            if len(early) < 3 or len(late) < 3:
                reasons.append("ORDER_FLOW_KURANG")
            elif side == "buy":
                early_sells = -sum((x for x in early if x < 0), Decimal(0))
                late_sells = -sum((x for x in late if x < 0), Decimal(0))
                if early_sells <= 0 or late_sells > early_sells * Decimal("0.7"):
                    reasons.append("PENJUALAN_LOKAL_BELUM_MELEMAH")
            else:
                early_buys = sum((x for x in early if x > 0), Decimal(0))
                late_buys = sum((x for x in late if x > 0), Decimal(0))
                if early_buys <= 0 or late_buys > early_buys * Decimal("0.7"):
                    reasons.append("PEMBELIAN_LOKAL_BELUM_MELEMAH")
            recent_depth = [
                x[2] if side == "buy" else x[3] for x in self.samples if now - x[0] <= 30
            ]
            if len(recent_depth) < 20 or recent_depth[-1] < min(recent_depth) * Decimal("1.2"):
                reasons.append("LIKUIDITAS_LOKAL_BELUM_PULIH")
        spread_bps = (lighter.ask - lighter.bid) / lighter.mid * 10000
        if abs(residual) < spread_bps + 5:
            reasons.append("BIAYA_DAN_BUFFER_MELEBIHI_PELUANG")
        if not self.local_sell_flow:
            reasons.append("BELUM_ADA_TRADE_LOKAL")
        if reasons:
            side = None
        return Decision(side, raw, basis, scale, tuple(reasons or ("KONDISI_TERPENUHI",)))
