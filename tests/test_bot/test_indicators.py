"""Tests for the indicator primitives the FVG strategy depends on."""

from decimal import Decimal

import pytest

from bot.strategy.indicators import (
    Bias,
    Candle,
    atr_series,
    compute_bias,
    ema_series,
    latest_atr,
    sma,
    swept_liquidity_above,
    swept_liquidity_below,
    swing_high_indices,
    swing_low_indices,
    true_range,
)
from tests.test_bot.conftest import BAR_SECONDS, FIRST_TIMESTAMP, flat_series, make_series


class TestTrueRange:
    def test_first_bar_uses_high_low(self):
        candle = Candle(0, Decimal("100"), Decimal("110"), Decimal("95"), Decimal("105"))
        assert true_range(candle, None) == Decimal("15")

    def test_gap_up_uses_previous_close(self):
        previous = Candle(0, Decimal("100"), Decimal("102"), Decimal("98"), Decimal("100"))
        current = Candle(1, Decimal("120"), Decimal("125"), Decimal("118"), Decimal("124"))
        # high - prev_close = 25 beats the 7-point high-low range.
        assert true_range(current, previous) == Decimal("25")

    def test_gap_down_uses_previous_close(self):
        previous = Candle(0, Decimal("100"), Decimal("102"), Decimal("98"), Decimal("100"))
        current = Candle(1, Decimal("80"), Decimal("82"), Decimal("78"), Decimal("79"))
        assert true_range(current, previous) == Decimal("22")


class TestAtr:
    def test_returns_none_until_enough_history(
        self,
    ):
        candles = flat_series(10)
        series = atr_series(candles, 14)
        assert len(series) == len(candles)
        assert all(value is None for value in series)

    def test_seeds_at_period_index(self):
        candles = flat_series(30, price="1000", spread="2")
        series = atr_series(candles, 14)
        assert series[13] is None
        assert series[14] is not None
        # Every bar has a 4-point range, so the ATR settles at exactly 4.
        assert series[14] == Decimal("4")
        assert series[-1] == Decimal("4")

    def test_wilder_smoothing_moves_toward_new_range(self):
        candles = flat_series(30, price="1000", spread="2")
        candles.append(
            Candle(
                timestamp=candles[-1].timestamp + BAR_SECONDS,
                open=Decimal("1000"),
                high=Decimal("1020"),
                low=Decimal("1000"),
                close=Decimal("1015"),
            )
        )
        series = atr_series(candles, 14)
        previous, current = series[-2], series[-1]
        assert previous == Decimal("4")
        # (4 * 13 + 20) / 14 = 5.142857...
        assert current is not None and Decimal("5.1") < current < Decimal("5.2")

    def test_latest_atr_matches_series_tail(self):
        candles = flat_series(40)
        assert latest_atr(candles, 14) == atr_series(candles, 14)[-1]

    def test_rejects_invalid_period(self):
        with pytest.raises(ValueError):
            atr_series(flat_series(20), 0)


class TestMovingAverages:
    def test_ema_seeds_with_simple_average(self):
        values = [Decimal(x) for x in range(1, 11)]
        series = ema_series(values, 5)
        assert series[3] is None
        assert series[4] == Decimal(3)  # mean of 1..5

    def test_ema_tracks_a_rising_series(self):
        values = [Decimal(x) for x in range(1, 30)]
        series = ema_series(values, 10)
        assert series[-1] is not None and series[-2] is not None
        assert series[-1] > series[-2]

    def test_sma_needs_enough_values(self):
        assert sma([Decimal(1), Decimal(2)], 5) is None
        assert sma([Decimal(2), Decimal(4), Decimal(6)], 3) == Decimal(4)


class TestSwings:
    def test_detects_a_swing_high_and_low(self):
        candles = make_series(
            [
                ("100", "101", "99", "100"),
                ("100", "102", "99", "101"),
                ("101", "110", "100", "109"),  # swing high at index 2
                ("109", "105", "99", "100"),
                ("100", "103", "98", "101"),
                ("101", "102", "90", "95"),  # swing low at index 5
                ("95", "104", "94", "103"),
                ("103", "106", "100", "105"),
            ]
        )
        assert 2 in swing_high_indices(candles, strength=2)
        assert 5 in swing_low_indices(candles, strength=2)


class TestBias:
    def test_none_mode_is_always_neutral(self):
        candles = flat_series(60)
        assert compute_bias(candles, "none", 50, 20) is Bias.NEUTRAL

    def test_ma_mode_reads_a_steady_uptrend_as_bullish(self):
        candles = [
            Candle(
                timestamp=FIRST_TIMESTAMP + i * BAR_SECONDS,
                open=Decimal(1000 + i * 5),
                high=Decimal(1000 + i * 5 + 3),
                low=Decimal(1000 + i * 5 - 3),
                close=Decimal(1000 + i * 5 + 2),
            )
            for i in range(80)
        ]
        assert compute_bias(candles, "ma", 20, 20) is Bias.BULLISH

    def test_ma_mode_reads_a_steady_downtrend_as_bearish(self):
        candles = [
            Candle(
                timestamp=FIRST_TIMESTAMP + i * BAR_SECONDS,
                open=Decimal(2000 - i * 5),
                high=Decimal(2000 - i * 5 + 3),
                low=Decimal(2000 - i * 5 - 3),
                close=Decimal(2000 - i * 5 - 2),
            )
            for i in range(80)
        ]
        assert compute_bias(candles, "ma", 20, 20) is Bias.BEARISH

    def test_flat_market_is_not_directional(self):
        assert compute_bias(flat_series(80), "ma", 20, 20) is Bias.NEUTRAL

    def test_neutral_bias_permits_both_directions(self):
        assert Bias.NEUTRAL.allows_long() and Bias.NEUTRAL.allows_short()
        assert Bias.BULLISH.allows_long() and not Bias.BULLISH.allows_short()
        assert Bias.BEARISH.allows_short() and not Bias.BEARISH.allows_long()

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError):
            compute_bias(flat_series(30), "wat", 20, 20)


class TestLiquiditySweeps:
    def test_sweep_below_needs_a_close_back_above(self):
        candles = flat_series(10, price="1000", spread="2")
        # Pokes below the 998 prior low and closes back above it.
        candles.append(
            Candle(
                timestamp=candles[-1].timestamp + BAR_SECONDS,
                open=Decimal("999"),
                high=Decimal("1002"),
                low=Decimal("990"),
                close=Decimal("1001"),
            )
        )
        assert swept_liquidity_below(candles, len(candles) - 1, lookback=5)

    def test_close_below_the_pool_is_not_a_sweep(self):
        candles = flat_series(10, price="1000", spread="2")
        candles.append(
            Candle(
                timestamp=candles[-1].timestamp + BAR_SECONDS,
                open=Decimal("999"),
                high=Decimal("1000"),
                low=Decimal("990"),
                close=Decimal("991"),
            )
        )
        assert not swept_liquidity_below(candles, len(candles) - 1, lookback=5)

    def test_sweep_above_needs_a_close_back_below(self):
        candles = flat_series(10, price="1000", spread="2")
        candles.append(
            Candle(
                timestamp=candles[-1].timestamp + BAR_SECONDS,
                open=Decimal("1001"),
                high=Decimal("1010"),
                low=Decimal("998"),
                close=Decimal("999"),
            )
        )
        assert swept_liquidity_above(candles, len(candles) - 1, lookback=5)

    def test_out_of_range_index_is_false(self):
        candles = flat_series(5)
        assert not swept_liquidity_below(candles, 1, lookback=5)
        assert not swept_liquidity_above(candles, 99, lookback=5)
