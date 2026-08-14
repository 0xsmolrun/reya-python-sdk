"""Event-driven backtester for the 15m FVG strategy.

It walks the candle series one bar at a time, feeding the *same*
:class:`~bot.strategy.fvg.FVGStrategy` and :class:`~bot.strategy.risk.RiskManager`
the live engine uses, so detection, filtering and sizing cannot drift between
research and production.

What it models
--------------
* Entries as resting limits inside the gap: filled when a bar's range covers the
  entry price.
* Stops and targets checked bar by bar, with the **stop taking precedence** when
  a single bar spans both. Real tape resolves that intrabar; assuming the loss
  is the conservative reading.
* Taker fees on both sides, and fixed slippage on entries.
* The full risk stack: daily loss limits, trade counts and loss-streak cooldowns,
  driven by candle timestamps rather than the wall clock.

What it does not model
----------------------
Funding, partial fills, order rejection, queue position, liquidations, or the
possibility that a limit inside a fast-moving gap simply never fills. Treat
results as an upper bound, not a forecast.
"""

from typing import List, Optional, Sequence, Tuple

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal

from bot.config import BotConfig
from bot.exchange.base import MarketMeta
from bot.metrics import PerformanceStats, compute_stats
from bot.state import TradeRecord
from bot.strategy.fvg import FVGStrategy, SignalSide
from bot.strategy.indicators import Candle
from bot.strategy.risk import RiskManager, TradePlan
from bot.utils.numbers import BPS, ZERO, apply_bps

logger = logging.getLogger("bot.backtest")


@dataclass
class BacktestResult:
    """Outcome of a backtest run."""

    symbol: str
    timeframe: str
    trades: List[TradeRecord] = field(default_factory=list)
    equity_curve: List[Tuple[int, Decimal]] = field(default_factory=list)
    starting_equity: Decimal = ZERO
    ending_equity: Decimal = ZERO
    #: Signals the strategy produced, including those risk declined.
    signals: int = 0
    #: Signals rejected by a risk gate or by sizing.
    rejected: int = 0
    bars: int = 0
    first_timestamp: int = 0
    last_timestamp: int = 0

    @property
    def stats(self) -> PerformanceStats:
        """Performance statistics for the closed trades."""
        return compute_stats(self.trades, self.starting_equity)

    @property
    def return_pct(self) -> Decimal:
        """Total return over the run, as a percentage of starting equity."""
        if self.starting_equity <= ZERO:
            return ZERO
        return (self.ending_equity - self.starting_equity) / self.starting_equity * Decimal(100)

    def summary(self) -> str:
        """Multi-line human-readable report."""
        stats = self.stats
        span = ""
        if self.first_timestamp and self.last_timestamp:
            start = time.strftime("%Y-%m-%d %H:%M", time.gmtime(self.first_timestamp))
            end = time.strftime("%Y-%m-%d %H:%M", time.gmtime(self.last_timestamp))
            span = f"{start} -> {end} UTC"

        lines = [
            f"Backtest {self.symbol} {self.timeframe}  {span}",
            f"  bars              {self.bars}",
            f"  signals           {self.signals} ({self.rejected} declined by risk)",
            f"  trades            {stats.trades}",
            f"  win rate          {stats.win_rate_pct:.1f}%",
            f"  net PnL           {stats.net_pnl:.2f}",
            f"  return            {self.return_pct:.2f}%",
            f"  profit factor     {'inf' if stats.profit_factor == Decimal('Infinity') else f'{stats.profit_factor:.2f}'}",
            f"  expectancy        {stats.expectancy:.2f} ({stats.expectancy_r:.2f}R)",
            f"  avg win / loss    {stats.average_win:.2f} / {stats.average_loss:.2f}",
            f"  max drawdown      {stats.max_drawdown:.2f} ({stats.max_drawdown_pct:.2f}%)",
            f"  longest streaks   {stats.max_consecutive_wins}W / {stats.max_consecutive_losses}L",
            f"  fees paid         {stats.total_fees:.2f}",
            f"  equity            {self.starting_equity:.2f} -> {self.ending_equity:.2f}",
        ]
        return "\n".join(lines)


@dataclass
class _OpenTrade:
    """A position held by the simulator."""

    plan: TradePlan
    entry_price: Decimal
    qty: Decimal
    opened_at: int
    entry_fee: Decimal

    @property
    def side(self) -> SignalSide:
        """Direction of the position."""
        return self.plan.side

    def r_multiple(self, exit_price: Decimal) -> Decimal:
        """Result at ``exit_price`` in units of initial risk."""
        risk = abs(self.entry_price - self.plan.stop_price)
        if risk <= ZERO:
            return ZERO
        return (exit_price - self.entry_price) * self.side.sign / risk


class Backtester:
    """Replays a candle series through the live strategy and risk code."""

    def __init__(
        self,
        config: BotConfig,
        symbol: str,
        market_meta: Optional[MarketMeta] = None,
    ) -> None:
        """Create a backtester.

        Args:
            config: Full bot configuration; strategy, risk and paper sections
                are used.
            symbol: Symbol being tested (labels the output).
            market_meta: Tick and step constraints. Defaults to a conservative
                placeholder, which is fine for research but will not match the
                venue's real minimum order size.
        """
        self.config = config
        self.symbol = symbol
        self.meta = market_meta or MarketMeta.fallback(symbol)
        self.strategy = FVGStrategy(symbol, config.strategy)
        self.risk = RiskManager(config.risk)

    def run(self, candles: Sequence[Candle]) -> BacktestResult:
        """Replay ``candles`` and return the result.

        Args:
            candles: Closed bars in chronological order.

        Returns:
            The trades, equity curve and statistics for the run.

        Raises:
            ValueError: If there are too few bars to warm up the indicators.
        """
        warmup = max(self.config.strategy.atr_period + 3, self.config.strategy.bias_ma_period + 3)
        if len(candles) <= warmup + 1:
            raise ValueError(
                f"Need more than {warmup + 1} candles to backtest; got {len(candles)}. "
                "Lower strategy.atr_period / strategy.bias_ma_period or load more history."
            )

        equity = self.config.paper.starting_equity
        result = BacktestResult(
            symbol=self.symbol,
            timeframe=self.config.strategy.timeframe,
            starting_equity=equity,
            ending_equity=equity,
            bars=len(candles),
            first_timestamp=candles[0].timestamp,
            last_timestamp=candles[-1].timestamp,
        )
        open_trade: Optional[_OpenTrade] = None

        # Bar `i` is the last closed bar the strategy may look at; bar `i + 1`
        # is the one being traded. This ordering is what prevents look-ahead.
        for i in range(warmup, len(candles) - 1):
            history = candles[: i + 1]
            next_bar = candles[i + 1]
            now = float(next_bar.timestamp)

            self.strategy.update_candles(history)
            self.risk.sync_equity(equity, now=now)

            if open_trade is not None:
                exit_price, reason = self._check_exit(open_trade, next_bar)
                if exit_price is not None:
                    equity = self._book(result, open_trade, exit_price, reason, next_bar, equity)
                    open_trade = None

            if open_trade is None:
                open_trade, equity = self._maybe_enter(result, next_bar, equity, now)

            result.equity_curve.append((next_bar.timestamp, equity))

        # Mark any position still open at the end of the data to its last close.
        if open_trade is not None:
            equity = self._book(result, open_trade, candles[-1].close, "end of data", candles[-1], equity)

        result.ending_equity = equity
        return result

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------
    def _maybe_enter(
        self,
        result: BacktestResult,
        candle: Candle,
        equity: Decimal,
        now: float,
    ) -> Tuple[Optional[_OpenTrade], Decimal]:
        """Try to open a position on ``candle``.

        Returns:
            ``(open_trade_or_None, equity)``.
        """
        signal = None
        for gap in self.strategy.tradeable_gaps:
            entry = gap.entry_price(self.config.strategy.entry_zone_ratio)
            if not candle.low <= entry <= candle.high:
                continue  # the bar never traded at the limit price
            # Re-run the real evaluation at that price so every live filter
            # (bias, far-edge rejection, ranking) applies identically here.
            signal = self.strategy.evaluate(entry)
            if signal is not None:
                break

        if signal is None:
            return None, equity

        result.signals += 1

        blocked = self.risk.check_gates(open_positions=0, symbol_has_position=False, now=now)
        if blocked:
            result.rejected += 1
            self.strategy.forget_signal(signal.fvg.key)
            return None, equity

        plan = self.risk.build_plan(signal, equity, self.meta)
        if plan is None:
            result.rejected += 1
            self.strategy.forget_signal(signal.fvg.key)
            return None, equity

        fill_price = apply_bps(plan.entry_price, self.config.paper.slippage_bps, plan.is_buy)
        fee = fill_price * plan.qty * self.config.paper.fee_bps / BPS
        self.risk.record_entry(now=now)

        trade = _OpenTrade(
            plan=plan,
            entry_price=fill_price,
            qty=plan.qty,
            opened_at=candle.timestamp,
            entry_fee=fee,
        )

        # Paying the entry fee is what "opening" costs; PnL accrues from there.
        equity -= fee

        # The same bar can stop the trade out. Because the fill happened partway
        # through the bar, testing the whole bar's range can close a trade at a
        # level that printed before the entry — deliberately pessimistic.
        exit_price, reason = self._check_exit(trade, candle)
        if exit_price is not None:
            return None, self._book(result, trade, exit_price, reason, candle, equity)

        return trade, equity

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------
    def _check_exit(self, trade: _OpenTrade, candle: Candle) -> Tuple[Optional[Decimal], str]:
        """Decide whether ``candle`` closed the trade, and at what price.

        The stop is tested first: when one bar covers both levels there is no
        way to know which came first, so the worse outcome is assumed.
        """
        stop = trade.plan.stop_price
        target = trade.plan.take_profit

        if trade.side is SignalSide.LONG:
            if candle.low <= stop:
                return stop, "stop loss"
            if candle.high >= target:
                return target, "take profit"
        else:
            if candle.high >= stop:
                return stop, "stop loss"
            if candle.low <= target:
                return target, "take profit"
        return None, ""

    def _book(
        self,
        result: BacktestResult,
        trade: _OpenTrade,
        exit_price: Decimal,
        reason: str,
        closing_bar: Candle,
        equity: Decimal,
    ) -> Decimal:
        """Record a closed trade and return the updated equity."""
        gross = (exit_price - trade.entry_price) * trade.qty * trade.side.sign
        exit_fee = exit_price * trade.qty * self.config.paper.fee_bps / BPS
        fees = trade.entry_fee + exit_fee
        pnl = gross - fees

        record = TradeRecord(
            symbol=self.symbol,
            side=trade.side,
            qty=trade.qty,
            entry_price=trade.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            r_multiple=trade.r_multiple(exit_price),
            opened_at=float(trade.opened_at),
            closed_at=float(closing_bar.timestamp),
            reason=reason,
            fees=fees,
        )
        result.trades.append(record)
        self.risk.record_result(pnl, now=float(closing_bar.timestamp))

        logger.debug(
            "%s %s %s @ %s -> %s (%s, PnL %s)",
            record.side.value,
            record.qty,
            self.symbol,
            record.entry_price,
            record.exit_price,
            reason,
            record.pnl,
        )
        # The entry fee was already deducted when the position opened.
        return equity + gross - exit_fee


__all__ = ["BacktestResult", "Backtester"]
