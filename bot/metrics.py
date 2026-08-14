"""Performance statistics computed from closed trades.

Used by the UI's metrics panel and printed at the end of a backtest. Everything
is derived from :class:`~bot.state.TradeRecord`, so live and backtested runs are
scored by exactly the same code.
"""

from typing import List, Sequence

from dataclasses import dataclass
from decimal import Decimal

from bot.state import TradeRecord
from bot.utils.numbers import ZERO, safe_div


@dataclass(frozen=True)
class PerformanceStats:
    """Summary statistics for a set of closed trades."""

    trades: int = 0
    wins: int = 0
    losses: int = 0
    #: Fraction of trades that closed in profit, 0..1.
    win_rate: Decimal = ZERO
    gross_profit: Decimal = ZERO
    gross_loss: Decimal = ZERO
    net_pnl: Decimal = ZERO
    #: Gross profit divided by gross loss. Infinite with no losing trades.
    profit_factor: Decimal = ZERO
    #: Average PnL per trade.
    expectancy: Decimal = ZERO
    #: Average result in units of risk.
    expectancy_r: Decimal = ZERO
    average_win: Decimal = ZERO
    average_loss: Decimal = ZERO
    largest_win: Decimal = ZERO
    largest_loss: Decimal = ZERO
    #: Deepest peak-to-trough decline of the cumulative PnL curve.
    max_drawdown: Decimal = ZERO
    max_drawdown_pct: Decimal = ZERO
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    total_fees: Decimal = ZERO
    average_duration_s: float = 0.0

    @property
    def win_rate_pct(self) -> Decimal:
        """Win rate as a percentage."""
        return self.win_rate * Decimal(100)

    def as_rows(self) -> List[tuple]:
        """Label/value pairs ready for a two-column table."""
        profit_factor = "inf" if self.profit_factor == Decimal("Infinity") else f"{self.profit_factor:.2f}"
        return [
            ("Trades", str(self.trades)),
            ("Win rate", f"{self.win_rate_pct:.1f}%"),
            ("Net PnL", f"{self.net_pnl:.2f}"),
            ("Profit factor", profit_factor),
            ("Expectancy", f"{self.expectancy:.2f}"),
            ("Expectancy (R)", f"{self.expectancy_r:.2f}R"),
            ("Avg win", f"{self.average_win:.2f}"),
            ("Avg loss", f"{self.average_loss:.2f}"),
            ("Max drawdown", f"{self.max_drawdown:.2f}"),
            ("Max win streak", str(self.max_consecutive_wins)),
            ("Max loss streak", str(self.max_consecutive_losses)),
            ("Fees", f"{self.total_fees:.2f}"),
        ]


def compute_stats(trades: Sequence[TradeRecord], starting_equity: Decimal = ZERO) -> PerformanceStats:
    """Compute performance statistics for ``trades``.

    Args:
        trades: Closed round trips, ideally in chronological order (drawdown is
            path-dependent, so order matters).
        starting_equity: Used to express the drawdown as a percentage. Zero
            leaves ``max_drawdown_pct`` at zero.

    Returns:
        The statistics; an empty input yields an all-zero result.
    """
    if not trades:
        return PerformanceStats()

    wins = [trade for trade in trades if trade.pnl > ZERO]
    losses = [trade for trade in trades if trade.pnl < ZERO]

    gross_profit = sum((trade.pnl for trade in wins), ZERO)
    gross_loss = -sum((trade.pnl for trade in losses), ZERO)
    net_pnl = sum((trade.pnl for trade in trades), ZERO)
    total_fees = sum((trade.fees for trade in trades), ZERO)

    profit_factor = Decimal("Infinity") if gross_loss == ZERO and gross_profit > ZERO else safe_div(
        gross_profit, gross_loss
    )

    # Drawdown from the cumulative PnL curve, measured against its running peak.
    equity = starting_equity
    peak = starting_equity
    max_drawdown = ZERO
    for trade in trades:
        equity += trade.pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    best_win_streak = 0
    best_loss_streak = 0
    win_streak = 0
    loss_streak = 0
    for trade in trades:
        if trade.pnl > ZERO:
            win_streak += 1
            loss_streak = 0
        elif trade.pnl < ZERO:
            loss_streak += 1
            win_streak = 0
        else:
            win_streak = loss_streak = 0
        best_win_streak = max(best_win_streak, win_streak)
        best_loss_streak = max(best_loss_streak, loss_streak)

    count = Decimal(len(trades))
    return PerformanceStats(
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=safe_div(Decimal(len(wins)), count),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_pnl=net_pnl,
        profit_factor=profit_factor,
        expectancy=safe_div(net_pnl, count),
        expectancy_r=safe_div(sum((trade.r_multiple for trade in trades), ZERO), count),
        average_win=safe_div(gross_profit, Decimal(len(wins))) if wins else ZERO,
        average_loss=-safe_div(gross_loss, Decimal(len(losses))) if losses else ZERO,
        largest_win=max((trade.pnl for trade in wins), default=ZERO),
        largest_loss=min((trade.pnl for trade in losses), default=ZERO),
        max_drawdown=max_drawdown,
        max_drawdown_pct=safe_div(max_drawdown, starting_equity) * Decimal(100),
        max_consecutive_wins=best_win_streak,
        max_consecutive_losses=best_loss_streak,
        total_fees=total_fees,
        average_duration_s=sum(trade.duration_s for trade in trades) / len(trades),
    )


__all__ = ["PerformanceStats", "compute_stats"]
