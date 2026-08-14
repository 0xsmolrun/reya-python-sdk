"""Position sizing, bracket placement and the circuit breakers.

The strategy decides *where* to trade; this module decides *whether* and *how
much*. It is deliberately the only place that can authorise an entry, so every
guard rail lives in one auditable spot.
"""

from typing import List, Optional

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from bot.config import RiskConfig
from bot.exchange.base import MarketMeta
from bot.strategy.fvg import Signal, SignalSide
from bot.utils.numbers import ZERO, round_price, round_qty_down, safe_div

logger = logging.getLogger("bot.strategy.risk")

HUNDRED = Decimal(100)


@dataclass
class TradePlan:
    """A fully specified, risk-approved trade."""

    symbol: str
    side: SignalSide
    qty: Decimal
    entry_price: Decimal
    stop_price: Decimal
    take_profit: Decimal
    #: Currency amount at risk if the stop fills exactly.
    risk_amount: Decimal
    #: ``qty * entry_price``.
    notional: Decimal
    #: Realised reward-to-risk multiple of the plan.
    risk_reward: Decimal
    #: Account equity used for sizing.
    equity: Decimal
    signal: Signal
    created_at: float = field(default_factory=time.time)

    @property
    def is_buy(self) -> bool:
        """Whether opening this plan means buying."""
        return self.side.is_buy

    @property
    def stop_distance(self) -> Decimal:
        """Absolute distance from entry to stop."""
        return abs(self.entry_price - self.stop_price)

    def describe(self) -> str:
        """One-line summary for logs and alerts."""
        return (
            f"{self.side.value} {self.qty} {self.symbol} @ {self.entry_price} "
            f"SL {self.stop_price} TP {self.take_profit} "
            f"(risk {self.risk_amount:.2f}, {self.risk_reward:.2f}R)"
        )


@dataclass
class RiskState:
    """Mutable day-scoped risk counters, exposed to the UI."""

    day: str = ""
    day_start_equity: Decimal = ZERO
    daily_pnl: Decimal = ZERO
    daily_trades: int = 0
    consecutive_losses: int = 0
    cooldown_until: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    last_entry_at: float = 0.0
    wins: int = 0
    losses: int = 0

    @property
    def daily_loss_pct(self) -> Decimal:
        """Day's loss as a positive percentage of the day's starting equity."""
        if self.day_start_equity <= ZERO or self.daily_pnl >= ZERO:
            return ZERO
        return (-self.daily_pnl / self.day_start_equity) * HUNDRED

    @property
    def in_cooldown(self) -> bool:
        """Whether the loss-streak cooldown is still running."""
        return time.time() < self.cooldown_until

    @property
    def cooldown_remaining_s(self) -> int:
        """Seconds left on the cooldown, or 0."""
        return max(0, int(self.cooldown_until - time.time()))


class RiskManager:
    """Sizes trades and enforces the daily / streak / exposure limits."""

    def __init__(self, config: RiskConfig) -> None:
        """Create a risk manager.

        Args:
            config: Risk limits and sizing parameters.
        """
        self.config = config
        self.state = RiskState()
        #: Why the last :meth:`build_plan` or :meth:`check_gates` said no.
        self.last_rejection: str = ""

    # ------------------------------------------------------------------
    # Day / streak bookkeeping
    # ------------------------------------------------------------------
    def sync_equity(self, equity: Decimal, now: Optional[float] = None) -> None:
        """Roll the day over if needed and seed the day's starting equity.

        Args:
            equity: Current account equity.
            now: Override for the current epoch time (used by the backtester).
        """
        moment = now if now is not None else time.time()
        day = datetime.fromtimestamp(moment, tz=timezone.utc).strftime("%Y-%m-%d")
        if day != self.state.day:
            logger.info("Risk day rollover -> %s (equity %s)", day, equity)
            self.state.day = day
            self.state.day_start_equity = equity
            self.state.daily_pnl = ZERO
            self.state.daily_trades = 0
            self.state.halted = False
            self.state.halt_reason = ""
        elif self.state.day_start_equity <= ZERO:
            self.state.day_start_equity = equity

    def record_entry(self, now: Optional[float] = None) -> None:
        """Note that an entry was submitted, for the per-day and spacing limits."""
        self.state.daily_trades += 1
        self.state.last_entry_at = now if now is not None else time.time()

    def record_result(self, pnl: Decimal, now: Optional[float] = None) -> None:
        """Fold a closed trade's PnL into the day's counters and loss streak.

        A loss streak of ``consecutive_loss_limit`` starts a cooldown; crossing
        ``max_daily_loss_pct`` halts trading until the next UTC day.

        Args:
            pnl: Realised profit (positive) or loss (negative).
            now: Override for the current epoch time.
        """
        moment = now if now is not None else time.time()
        self.state.daily_pnl += pnl

        if pnl < ZERO:
            self.state.losses += 1
            self.state.consecutive_losses += 1
            if self.state.consecutive_losses >= self.config.consecutive_loss_limit:
                self.state.cooldown_until = moment + self.config.cooldown_minutes * 60
                logger.warning(
                    "%d consecutive losses -> cooling down for %d minutes",
                    self.state.consecutive_losses,
                    self.config.cooldown_minutes,
                )
        else:
            if pnl > ZERO:
                self.state.wins += 1
            self.state.consecutive_losses = 0

        if self.state.daily_loss_pct >= self.config.max_daily_loss_pct > ZERO:
            self.state.halted = True
            self.state.halt_reason = (
                f"daily loss {self.state.daily_loss_pct:.2f}% >= limit {self.config.max_daily_loss_pct}%"
            )
            logger.error("Trading halted for the day: %s", self.state.halt_reason)

    def halt(self, reason: str) -> None:
        """Manually halt new entries (operator action or a critical error)."""
        self.state.halted = True
        self.state.halt_reason = reason
        logger.error("Trading halted: %s", reason)

    def resume(self) -> None:
        """Clear a manual halt and any running cooldown."""
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.cooldown_until = 0.0
        self.state.consecutive_losses = 0
        logger.info("Risk limits manually reset")

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------
    def check_gates(
        self,
        open_positions: int,
        symbol_has_position: bool,
        now: Optional[float] = None,
    ) -> Optional[str]:
        """Test every pre-trade limit.

        Args:
            open_positions: Number of positions currently open across symbols.
            symbol_has_position: Whether this symbol is already in a position.
            now: Override for the current epoch time.

        Returns:
            ``None`` when trading is allowed, otherwise the blocking reason.
        """
        moment = now if now is not None else time.time()

        if self.state.halted:
            return f"halted: {self.state.halt_reason}"
        if self.state.in_cooldown:
            return f"cooldown ({self.state.cooldown_remaining_s}s left)"
        if symbol_has_position:
            return "already in position for this symbol"
        if open_positions >= self.config.max_open_positions:
            return f"max open positions ({self.config.max_open_positions}) reached"
        if 0 < self.config.max_daily_trades <= self.state.daily_trades:
            return f"daily trade limit ({self.config.max_daily_trades}) reached"
        if self.state.daily_loss_pct >= self.config.max_daily_loss_pct > ZERO:
            return f"daily loss limit ({self.config.max_daily_loss_pct}%) reached"
        return None

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------
    def stop_for(self, signal: Signal) -> Decimal:
        """Stop price for a signal: beyond the gap's far edge plus an ATR buffer.

        The buffer keeps the stop out of the noise that usually surrounds the
        edge of an imbalance; a separate floor guarantees the stop is never
        closer than ``min_stop_atr_mult`` ATRs, which would otherwise produce an
        enormous position size.
        """
        buffer = self.config.sl_atr_buffer_mult * signal.atr
        floor = self.config.min_stop_atr_mult * signal.atr
        entry = signal.entry_price

        if signal.side is SignalSide.LONG:
            stop = signal.fvg.far_edge - buffer
            return min(stop, entry - floor)
        stop = signal.fvg.far_edge + buffer
        return max(stop, entry + floor)

    def target_for(self, signal: Signal, entry: Decimal, stop: Decimal) -> Decimal:
        """Take-profit price for a signal.

        With ``tp_mode: rr`` this is a fixed multiple of the stop distance. With
        ``tp_mode: opposing_fvg`` the nearest unfilled opposing gap is preferred,
        but only when it pays at least ``min_risk_reward``; otherwise the fixed
        multiple is used so the trade is never taken at a worse-than-configured
        reward.
        """
        distance = abs(entry - stop)
        fixed = entry + distance * self.config.risk_reward * signal.side.sign

        if self.config.tp_mode == "opposing_fvg" and signal.opposing_target is not None:
            candidate = signal.opposing_target
            candidate_rr = safe_div(abs(candidate - entry), distance)
            if candidate_rr >= self.config.min_risk_reward:
                return candidate
        return fixed

    def build_plan(
        self,
        signal: Signal,
        equity: Decimal,
        meta: MarketMeta,
    ) -> Optional[TradePlan]:
        """Turn a signal into a sized, bracketed trade plan.

        Sizing is risk-first: the quantity is whatever puts exactly
        ``risk_per_trade_pct`` of equity between entry and stop, then trimmed by
        the notional and leverage caps and rounded *down* onto the market's
        quantity step so realised risk never exceeds the budget.

        Args:
            signal: The entry candidate.
            equity: Account equity to size against.
            meta: Market tick size, step size and limits.

        Returns:
            The plan, or ``None`` with :attr:`last_rejection` set.
        """
        self.last_rejection = ""

        if equity <= ZERO:
            self.last_rejection = "no equity"
            return None

        sizing_equity = equity
        if self.config.equity_cap > ZERO:
            sizing_equity = min(sizing_equity, self.config.equity_cap)

        entry = round_price(signal.entry_price, meta.tick_size)
        stop = round_price(
            self.stop_for(signal),
            meta.tick_size,
            rounding=ROUND_DOWN if signal.side is SignalSide.LONG else ROUND_UP,
        )
        stop_distance = abs(entry - stop)
        if stop_distance <= ZERO:
            self.last_rejection = "degenerate stop distance"
            return None

        # The stop must be on the correct side of the entry, otherwise a rounding
        # edge case would invert the trade.
        if (signal.side is SignalSide.LONG and stop >= entry) or (
            signal.side is SignalSide.SHORT and stop <= entry
        ):
            self.last_rejection = "stop on the wrong side of entry"
            return None

        target = round_price(
            self.target_for(signal, entry, stop),
            meta.tick_size,
            rounding=ROUND_DOWN if signal.side is SignalSide.LONG else ROUND_UP,
        )
        realised_rr = safe_div(abs(target - entry), stop_distance)
        if realised_rr < self.config.min_risk_reward:
            self.last_rejection = f"R:R {realised_rr:.2f} below minimum {self.config.min_risk_reward}"
            return None

        risk_pct = min(self.config.risk_per_trade_pct, self.config.max_risk_per_trade_pct)
        risk_amount = sizing_equity * risk_pct / HUNDRED
        raw_qty = risk_amount / stop_distance

        # Exposure caps: never let a tight stop translate into silly leverage.
        caps: List[Decimal] = []
        if self.config.max_notional_pct > ZERO:
            caps.append(sizing_equity * self.config.max_notional_pct / HUNDRED / entry)
        if meta.max_leverage > 0:
            caps.append(sizing_equity * Decimal(meta.max_leverage) / entry)
        if caps:
            raw_qty = min(raw_qty, min(caps))

        qty = round_qty_down(raw_qty, meta.qty_step_size)
        if qty <= ZERO:
            self.last_rejection = "size rounds to zero at this risk budget"
            return None
        if qty < meta.min_order_qty:
            self.last_rejection = (
                f"size {qty} below market minimum {meta.min_order_qty} "
                f"(raise risk.risk_per_trade_pct or fund the account)"
            )
            return None

        return TradePlan(
            symbol=signal.symbol,
            side=signal.side,
            qty=qty,
            entry_price=entry,
            stop_price=stop,
            take_profit=target,
            risk_amount=qty * stop_distance,
            notional=qty * entry,
            risk_reward=realised_rr,
            equity=equity,
            signal=signal,
        )


__all__ = ["RiskManager", "RiskState", "TradePlan"]
