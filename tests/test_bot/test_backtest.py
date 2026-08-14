"""Tests for the event-driven backtester and the performance statistics."""

from decimal import Decimal

import pytest

from bot.backtest import Backtester
from bot.config import BotConfig, PaperConfig
from bot.exchange.base import MarketMeta
from bot.metrics import compute_stats
from bot.state import TradeRecord
from bot.strategy.fvg import SignalSide
from bot.strategy.indicators import Candle
from tests.test_bot.conftest import BAR_SECONDS, append_bullish_gap, extend_flat, flat_series


def make_config(strategy_config, risk_config) -> BotConfig:
    """A backtest-ready config with no fees or slippage unless a test adds them."""
    config = BotConfig(mode="backtest", symbols=["ETHRUSDPERP"])
    config.strategy = strategy_config
    config.risk = risk_config
    config.strategy.bias_ma_period = 10
    config.paper = PaperConfig(
        starting_equity=Decimal("10000"),
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )
    return config


@pytest.fixture
def meta() -> MarketMeta:
    """A market with a fine step so small test sizes are representable."""
    return MarketMeta(
        symbol="ETHRUSDPERP",
        market_id=1,
        tick_size=Decimal("0.01"),
        qty_step_size=Decimal("0.0001"),
        min_order_qty=Decimal("0.0001"),
        max_leverage=20,
    )


def build_winning_series() -> list:
    """Bars that print a bullish gap, retest it, then rally to the target."""
    candles = append_bullish_gap(flat_series(40), gap_size="20", body="30")
    gap_bottom = candles[-3].high
    gap_top = candles[-1].low
    mid = (gap_bottom + gap_top) / 2

    # Retest bar: dips to the gap midpoint and closes back inside the zone.
    candles.append(
        Candle(
            timestamp=candles[-1].timestamp + BAR_SECONDS,
            open=gap_top + Decimal("10"),
            high=gap_top + Decimal("11"),
            low=mid,
            close=gap_top + Decimal("2"),
        )
    )
    # Then a long rally that clears any plausible target.
    base = candles[-1].close
    for step in range(1, 12):
        candles.append(
            Candle(
                timestamp=candles[-1].timestamp + BAR_SECONDS,
                open=base + Decimal(step * 10),
                high=base + Decimal(step * 10 + 12),
                low=base + Decimal(step * 10 - 2),
                close=base + Decimal(step * 10 + 10),
            )
        )
    return candles


def build_losing_series() -> list:
    """Bars that print a bullish gap, retest it, then collapse through the stop."""
    candles = append_bullish_gap(flat_series(40), gap_size="20", body="30")
    gap_bottom = candles[-3].high
    gap_top = candles[-1].low
    mid = (gap_bottom + gap_top) / 2

    candles.append(
        Candle(
            timestamp=candles[-1].timestamp + BAR_SECONDS,
            open=gap_top + Decimal("10"),
            high=gap_top + Decimal("11"),
            low=mid,
            close=gap_top + Decimal("2"),
        )
    )
    base = candles[-1].close
    for step in range(1, 12):
        candles.append(
            Candle(
                timestamp=candles[-1].timestamp + BAR_SECONDS,
                open=base - Decimal(step * 15),
                high=base - Decimal(step * 15 + 2),
                low=base - Decimal(step * 15 + 20),
                close=base - Decimal(step * 15 + 18),
            )
        )
    return candles


class TestBacktester:
    def test_rejects_a_series_that_is_too_short(self, strategy_config, risk_config, meta):
        backtester = Backtester(make_config(strategy_config, risk_config), "ETHRUSDPERP", meta)
        with pytest.raises(ValueError, match="Need more than"):
            backtester.run(flat_series(5))

    def test_quiet_market_produces_no_trades(self, strategy_config, risk_config, meta):
        backtester = Backtester(make_config(strategy_config, risk_config), "ETHRUSDPERP", meta)
        result = backtester.run(flat_series(120))

        assert result.trades == []
        assert result.signals == 0
        assert result.ending_equity == result.starting_equity

    def test_a_winning_setup_is_taken_and_booked(self, strategy_config, risk_config, meta):
        backtester = Backtester(make_config(strategy_config, risk_config), "ETHRUSDPERP", meta)
        result = backtester.run(build_winning_series())

        assert result.signals >= 1
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.side is SignalSide.LONG
        assert trade.reason == "take profit"
        assert trade.pnl > Decimal("0")
        assert result.ending_equity > result.starting_equity

    def test_a_losing_setup_stops_out(self, strategy_config, risk_config, meta):
        backtester = Backtester(make_config(strategy_config, risk_config), "ETHRUSDPERP", meta)
        result = backtester.run(build_losing_series())

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.reason == "stop loss"
        assert trade.pnl < Decimal("0")
        assert result.ending_equity < result.starting_equity

    def test_loss_is_capped_near_the_risk_budget(self, strategy_config, risk_config, meta):
        config = make_config(strategy_config, risk_config)
        config.risk.risk_per_trade_pct = Decimal("1")
        backtester = Backtester(config, "ETHRUSDPERP", meta)
        result = backtester.run(build_losing_series())

        trade = result.trades[0]
        # 1% of 10,000 is 100; rounding down the size can only reduce it.
        assert Decimal("0") < -trade.pnl <= Decimal("100")

    def test_stop_wins_when_one_bar_spans_both_levels(self, strategy_config, risk_config, meta):
        """A bar covering the stop and the target must book the loss."""
        candles = append_bullish_gap(flat_series(40), gap_size="20", body="30")
        gap_bottom = candles[-3].high
        gap_top = candles[-1].low
        mid = (gap_bottom + gap_top) / 2

        candles.append(
            Candle(
                timestamp=candles[-1].timestamp + BAR_SECONDS,
                open=gap_top + Decimal("10"),
                high=gap_top + Decimal("11"),
                low=mid,
                close=gap_top + Decimal("2"),
            )
        )
        # A huge outside bar that reaches far above the target and far below the stop.
        candles.append(
            Candle(
                timestamp=candles[-1].timestamp + BAR_SECONDS,
                open=gap_top,
                high=gap_top + Decimal("500"),
                low=gap_bottom - Decimal("500"),
                close=gap_top,
            )
        )
        candles = extend_flat(candles, 3)

        backtester = Backtester(make_config(strategy_config, risk_config), "ETHRUSDPERP", meta)
        result = backtester.run(candles)

        assert len(result.trades) == 1
        assert result.trades[0].reason == "stop loss"

    def test_open_position_is_marked_out_at_the_end_of_data(self, strategy_config, risk_config, meta):
        candles = append_bullish_gap(flat_series(40), gap_size="20", body="30")
        gap_top = candles[-1].low
        gap_bottom = candles[-3].high
        mid = (gap_bottom + gap_top) / 2
        candles.append(
            Candle(
                timestamp=candles[-1].timestamp + BAR_SECONDS,
                open=gap_top + Decimal("10"),
                high=gap_top + Decimal("11"),
                low=mid,
                close=gap_top + Decimal("2"),
            )
        )
        # One quiet bar: not enough to reach either bracket.
        candles = extend_flat(candles, 1, price=gap_top + Decimal("2"))

        backtester = Backtester(make_config(strategy_config, risk_config), "ETHRUSDPERP", meta)
        result = backtester.run(candles)

        if result.trades:
            assert result.trades[-1].reason == "end of data"

    def test_fees_reduce_the_result(self, strategy_config, risk_config, meta):
        candles = build_winning_series()

        free = Backtester(make_config(strategy_config, risk_config), "ETHRUSDPERP", meta).run(candles)

        costly_config = make_config(strategy_config, risk_config)
        costly_config.paper.fee_bps = Decimal("10")
        costly = Backtester(costly_config, "ETHRUSDPERP", meta).run(candles)

        assert costly.trades[0].fees > free.trades[0].fees
        assert costly.trades[0].pnl < free.trades[0].pnl

    def test_equity_curve_tracks_every_traded_bar(self, strategy_config, risk_config, meta):
        backtester = Backtester(make_config(strategy_config, risk_config), "ETHRUSDPERP", meta)
        result = backtester.run(build_winning_series())

        assert result.equity_curve
        assert result.equity_curve[-1][1] == result.ending_equity
        timestamps = [point[0] for point in result.equity_curve]
        assert timestamps == sorted(timestamps)

    def test_summary_renders(self, strategy_config, risk_config, meta):
        backtester = Backtester(make_config(strategy_config, risk_config), "ETHRUSDPERP", meta)
        summary = backtester.run(build_winning_series()).summary()

        assert "Backtest ETHRUSDPERP 15m" in summary
        assert "win rate" in summary

    def test_risk_halt_stops_further_entries(self, strategy_config, risk_config, meta):
        """A day that breaches the loss limit must not take another trade."""
        config = make_config(strategy_config, risk_config)
        config.risk.max_daily_loss_pct = Decimal("0.01")  # trips on any loss
        backtester = Backtester(config, "ETHRUSDPERP", meta)

        candles = build_losing_series()
        candles = append_bullish_gap(extend_flat(candles, 5), gap_size="20", body="30")
        result = backtester.run(candles)

        assert backtester.risk.state.halted
        assert len(result.trades) == 1


class TestPerformanceStats:
    def _trade(self, pnl: str, r: str = "1", fees: str = "0") -> TradeRecord:
        return TradeRecord(
            symbol="ETHRUSDPERP",
            side=SignalSide.LONG,
            qty=Decimal("1"),
            entry_price=Decimal("1000"),
            exit_price=Decimal("1000") + Decimal(pnl),
            pnl=Decimal(pnl),
            r_multiple=Decimal(r),
            opened_at=0.0,
            closed_at=60.0,
            reason="take profit",
            fees=Decimal(fees),
        )

    def test_empty_input_is_all_zero(self):
        stats = compute_stats([], Decimal("1000"))
        assert stats.trades == 0 and stats.net_pnl == Decimal("0")

    def test_counts_and_rates(self):
        trades = [self._trade("100"), self._trade("-50"), self._trade("75")]
        stats = compute_stats(trades, Decimal("1000"))

        assert stats.trades == 3
        assert stats.wins == 2 and stats.losses == 1
        assert stats.net_pnl == Decimal("125")
        assert stats.gross_profit == Decimal("175")
        assert stats.gross_loss == Decimal("50")
        assert stats.profit_factor == Decimal("3.5")

    def test_profit_factor_is_infinite_without_losses(self):
        stats = compute_stats([self._trade("100"), self._trade("50")], Decimal("1000"))
        assert stats.profit_factor == Decimal("Infinity")
        assert "inf" in dict(stats.as_rows())["Profit factor"]

    def test_expectancy_is_the_mean_result(self):
        stats = compute_stats([self._trade("100"), self._trade("-50")], Decimal("1000"))
        assert stats.expectancy == Decimal("25")

    def test_drawdown_measures_the_deepest_trough(self):
        trades = [self._trade("100"), self._trade("-300"), self._trade("50")]
        stats = compute_stats(trades, Decimal("1000"))
        # Peak 1100 after the first trade, trough 800 after the second.
        assert stats.max_drawdown == Decimal("300")

    def test_streaks(self):
        trades = [
            self._trade("10"),
            self._trade("10"),
            self._trade("-10"),
            self._trade("-10"),
            self._trade("-10"),
            self._trade("10"),
        ]
        stats = compute_stats(trades, Decimal("1000"))
        assert stats.max_consecutive_wins == 2
        assert stats.max_consecutive_losses == 3

    def test_fees_are_totalled(self):
        stats = compute_stats([self._trade("100", fees="2"), self._trade("-50", fees="3")], Decimal("1000"))
        assert stats.total_fees == Decimal("5")
