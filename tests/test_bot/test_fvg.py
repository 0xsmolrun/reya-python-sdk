"""Tests for Fair Value Gap detection, lifecycle and signal generation."""

from decimal import Decimal

import pytest

from bot.strategy.fvg import (
    FairValueGap,
    FVGState,
    FVGStrategy,
    FVGType,
    SignalSide,
    detect_fvgs,
    update_gap_states,
)
from bot.strategy.indicators import Bias, Candle
from tests.test_bot.conftest import (
    BAR_SECONDS,
    append_bearish_gap,
    append_bullish_gap,
    extend_flat,
    flat_series,
    make_series,
)


def _gap(kind: FVGType, bottom: str, top: str, timestamp: int) -> FairValueGap:
    """Build a bare gap for tests that only care about its levels."""
    return FairValueGap(
        symbol="ETHRUSDPERP",
        kind=kind,
        index=0,
        timestamp=timestamp,
        top=Decimal(top),
        bottom=Decimal(bottom),
        atr=Decimal("4"),
    )


def _bullish_gap(bottom: str, top: str, timestamp: int) -> FairValueGap:
    """A bullish gap at the given levels."""
    return _gap(FVGType.BULLISH, bottom, top, timestamp)


def _bearish_gap(bottom: str, top: str, timestamp: int) -> FairValueGap:
    """A bearish gap at the given levels."""
    return _gap(FVGType.BEARISH, bottom, top, timestamp)


class TestDetection:
    def test_finds_a_bullish_gap_between_bar_one_and_three(self, strategy_config):
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        gaps = detect_fvgs(candles, strategy_config, "ETHRUSDPERP")

        assert len(gaps) == 1
        gap = gaps[0]
        assert gap.kind is FVGType.BULLISH
        assert gap.side is SignalSide.LONG
        # The zone spans high[i-2] .. low[i].
        assert gap.bottom == candles[-3].high
        assert gap.top == candles[-1].low
        assert gap.height == Decimal("20")
        assert gap.timestamp == candles[-1].timestamp

    def test_finds_a_bearish_gap(self, strategy_config):
        candles = append_bearish_gap(flat_series(30), gap_size="20")
        gaps = detect_fvgs(candles, strategy_config, "ETHRUSDPERP")

        assert len(gaps) == 1
        gap = gaps[0]
        assert gap.kind is FVGType.BEARISH
        assert gap.side is SignalSide.SHORT
        assert gap.top == candles[-3].low
        assert gap.bottom == candles[-1].high
        assert gap.height == Decimal("20")

    def test_overlapping_bars_leave_no_gap(self, strategy_config):
        # A steady drift where every bar overlaps its neighbour two back.
        candles = make_series([(str(1000 + i), str(1005 + i), str(995 + i), str(1002 + i)) for i in range(40)])
        assert detect_fvgs(candles, strategy_config, "ETHRUSDPERP") == []

    def test_needs_at_least_three_candles(self, strategy_config):
        assert detect_fvgs(flat_series(2), strategy_config, "X") == []

    def test_no_gaps_before_atr_is_available(self, strategy_config):
        # ATR is None for the first `atr_period` bars, so nothing can qualify.
        candles = append_bullish_gap(flat_series(5), gap_size="50")
        assert detect_fvgs(candles, strategy_config, "X") == []


class TestSignificanceFilters:
    def test_gap_below_the_atr_multiple_is_rejected(self, strategy_config):
        # ATR settles at 4, so a 0.25 multiple demands at least a 1-point gap.
        strategy_config.min_fvg_atr_mult = Decimal("10")
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        assert detect_fvgs(candles, strategy_config, "X") == []

    def test_gap_at_the_atr_threshold_is_accepted(self, strategy_config):
        strategy_config.min_fvg_atr_mult = Decimal("1")
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        assert len(detect_fvgs(candles, strategy_config, "X")) == 1

    def test_price_percentage_filter_rejects_dust_gaps(self, strategy_config):
        strategy_config.min_fvg_atr_mult = Decimal("0")
        strategy_config.min_fvg_price_pct = Decimal("0.5")  # 50% of price
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        assert detect_fvgs(candles, strategy_config, "X") == []

    def test_three_candle_run_filter(self, strategy_config):
        strategy_config.require_three_candle_run = True
        # The fixture builds three rising, bullish-closing bars.
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        assert len(detect_fvgs(candles, strategy_config, "X")) == 1

    def test_three_candle_run_filter_rejects_a_mixed_run(self, strategy_config):
        strategy_config.require_three_candle_run = True
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        # Turn the first bar of the pattern into a down-close.
        first = candles[-3]
        candles[-3] = Candle(first.timestamp, first.high, first.high, first.low, first.low)
        assert detect_fvgs(candles, strategy_config, "X") == []

    def test_displacement_filter_rejects_a_small_middle_body(self, strategy_config):
        strategy_config.require_displacement = True
        strategy_config.displacement_atr_mult = Decimal("50")
        candles = append_bullish_gap(flat_series(30), gap_size="20", body="30")
        assert detect_fvgs(candles, strategy_config, "X") == []

    def test_displacement_filter_accepts_a_large_middle_body(self, strategy_config):
        strategy_config.require_displacement = True
        strategy_config.displacement_atr_mult = Decimal("1")
        candles = append_bullish_gap(flat_series(30), gap_size="20", body="30")
        gaps = detect_fvgs(candles, strategy_config, "X")
        assert len(gaps) == 1 and gaps[0].displacement

    def test_liquidity_sweep_filter_rejects_when_absent(self, strategy_config):
        strategy_config.require_liquidity_sweep = True
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        assert detect_fvgs(candles, strategy_config, "X") == []

    def test_tracked_gap_count_is_capped(self, strategy_config):
        strategy_config.max_tracked_fvgs = 2
        candles = flat_series(30)
        for _ in range(5):
            candles = append_bullish_gap(extend_flat(candles, 3), gap_size="20")
        assert len(detect_fvgs(candles, strategy_config, "X")) == 2


class TestZoneGeometry:
    def test_bullish_entry_ratio_walks_from_far_to_near_edge(self, strategy_config):
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        gap = detect_fvgs(candles, strategy_config, "X")[0]

        assert gap.entry_price(Decimal("0")) == gap.bottom  # far edge
        assert gap.entry_price(Decimal("1")) == gap.top  # near edge
        assert gap.entry_price(Decimal("0.5")) == gap.mid
        assert gap.near_edge == gap.top
        assert gap.far_edge == gap.bottom

    def test_bearish_entry_ratio_is_mirrored(self, strategy_config):
        candles = append_bearish_gap(flat_series(30), gap_size="20")
        gap = detect_fvgs(candles, strategy_config, "X")[0]

        assert gap.entry_price(Decimal("0")) == gap.top  # far edge
        assert gap.entry_price(Decimal("1")) == gap.bottom  # near edge
        assert gap.near_edge == gap.bottom
        assert gap.far_edge == gap.top

    def test_contains_is_inclusive_of_both_edges(self, strategy_config):
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        gap = detect_fvgs(candles, strategy_config, "X")[0]

        assert gap.contains(gap.bottom)
        assert gap.contains(gap.top)
        assert gap.contains(gap.mid)
        assert not gap.contains(gap.top + Decimal("1"))
        assert not gap.contains(gap.bottom - Decimal("1"))

    def test_beyond_far_edge_detects_invalidation(self, strategy_config):
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        gap = detect_fvgs(candles, strategy_config, "X")[0]

        assert gap.beyond_far_edge(gap.bottom - Decimal("1"))
        assert not gap.beyond_far_edge(gap.mid)

    def test_quality_counts_satisfied_filters(self, strategy_config):
        strategy_config.displacement_atr_mult = Decimal("1")
        candles = append_bullish_gap(flat_series(30), gap_size="20", body="30")
        gap = detect_fvgs(candles, strategy_config, "X")[0]
        assert gap.quality == sum((gap.three_candle_run, gap.displacement, gap.liquidity_sweep))


class TestLifecycle:
    def test_untouched_gap_stays_fresh(self, strategy_config):
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        # Price drifts higher, well clear of the zone.
        candles = extend_flat(candles, 5, price=candles[-1].close + Decimal("50"))

        gaps = detect_fvgs(candles, strategy_config, "X")
        update_gap_states(gaps, candles, strategy_config)
        assert gaps[0].state is FVGState.FRESH

    def test_touching_the_zone_marks_it_tested(self, strategy_config):
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        gap_top = candles[-1].low
        # A bar that dips into the zone but does not fill it.
        candles.append(
            Candle(
                timestamp=candles[-1].timestamp + BAR_SECONDS,
                open=gap_top + Decimal("5"),
                high=gap_top + Decimal("6"),
                low=gap_top - Decimal("2"),
                close=gap_top + Decimal("4"),
            )
        )
        gaps = detect_fvgs(candles, strategy_config, "X")
        update_gap_states(gaps, candles, strategy_config)
        assert gaps[0].state is FVGState.TESTED
        assert gaps[0].tested_at == candles[-1].timestamp

    def test_trading_through_the_far_edge_mitigates(self, strategy_config):
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        gap_bottom = candles[-3].high
        candles.append(
            Candle(
                timestamp=candles[-1].timestamp + BAR_SECONDS,
                open=gap_bottom + Decimal("10"),
                high=gap_bottom + Decimal("11"),
                low=gap_bottom - Decimal("5"),
                close=gap_bottom - Decimal("3"),
            )
        )
        gaps = detect_fvgs(candles, strategy_config, "X")
        update_gap_states(gaps, candles, strategy_config)
        assert gaps[0].state is FVGState.MITIGATED
        assert not gaps[0].state.is_tradeable

    def test_touch_mode_mitigates_on_first_tag(self, strategy_config):
        strategy_config.mitigation_mode = "touch"
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        gap_top = candles[-1].low
        candles.append(
            Candle(
                timestamp=candles[-1].timestamp + BAR_SECONDS,
                open=gap_top + Decimal("5"),
                high=gap_top + Decimal("6"),
                low=gap_top - Decimal("1"),
                close=gap_top + Decimal("4"),
            )
        )
        gaps = detect_fvgs(candles, strategy_config, "X")
        update_gap_states(gaps, candles, strategy_config)
        assert gaps[0].state is FVGState.MITIGATED

    def test_old_untouched_gaps_expire(self, strategy_config):
        strategy_config.max_fvg_age_bars = 3
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        candles = extend_flat(candles, 10, price=candles[-1].close + Decimal("50"))

        gaps = detect_fvgs(candles, strategy_config, "X")
        update_gap_states(gaps, candles, strategy_config)
        assert gaps[0].state is FVGState.EXPIRED
        assert not gaps[0].state.is_tradeable

    def test_state_is_recomputed_deterministically(self, strategy_config):
        """Replaying the same candles must reproduce the same states.

        This is what lets the bot recover its view after a restart or a
        WebSocket gap without persisting anything.
        """
        candles = append_bullish_gap(flat_series(30), gap_size="20")
        candles = extend_flat(candles, 4)

        first = detect_fvgs(candles, strategy_config, "X")
        update_gap_states(first, candles, strategy_config)
        second = detect_fvgs(candles, strategy_config, "X")
        update_gap_states(second, candles, strategy_config)

        assert [gap.state for gap in first] == [gap.state for gap in second]
        assert [gap.key for gap in first] == [gap.key for gap in second]


class TestStrategySignals:
    def _strategy_with_bullish_gap(self, config):
        candles = append_bullish_gap(flat_series(40), gap_size="20")
        strategy = FVGStrategy("ETHRUSDPERP", config)
        strategy.update_candles(candles)
        return strategy, strategy.gaps[0]

    def test_no_signal_before_warm_up(self, strategy_config):
        strategy = FVGStrategy("ETHRUSDPERP", strategy_config)
        strategy.update_candles(flat_series(5))
        assert strategy.evaluate(Decimal("1000")) is None
        assert strategy.last_block_reason == "warming up"

    def test_price_outside_the_zone_produces_no_signal(self, strategy_config):
        strategy, gap = self._strategy_with_bullish_gap(strategy_config)
        assert strategy.evaluate(gap.top + Decimal("10")) is None
        assert strategy.last_block_reason == "no gap retest"

    def test_retest_inside_the_zone_fires_a_long(self, strategy_config):
        strategy, gap = self._strategy_with_bullish_gap(strategy_config)
        signal = strategy.evaluate(gap.mid)

        assert signal is not None
        assert signal.side is SignalSide.LONG
        assert signal.entry_price == gap.entry_price(strategy_config.entry_zone_ratio)
        assert signal.atr == strategy.atr
        assert "BULLISH FVG retest" in signal.reasons

    def test_bearish_gap_retest_fires_a_short(self, strategy_config):
        candles = append_bearish_gap(flat_series(40), gap_size="20")
        strategy = FVGStrategy("ETHRUSDPERP", strategy_config)
        strategy.update_candles(candles)
        gap = strategy.gaps[0]

        signal = strategy.evaluate(gap.mid)
        assert signal is not None and signal.side is SignalSide.SHORT

    def test_each_gap_signals_only_once(self, strategy_config):
        strategy, gap = self._strategy_with_bullish_gap(strategy_config)
        assert strategy.evaluate(gap.mid) is not None
        assert strategy.evaluate(gap.mid) is None

    def test_forget_signal_re_arms_a_gap(self, strategy_config):
        strategy, gap = self._strategy_with_bullish_gap(strategy_config)
        signal = strategy.evaluate(gap.mid)
        assert signal is not None

        strategy.forget_signal(signal.fvg.key)
        assert strategy.evaluate(gap.mid) is not None

    def test_bearish_bias_blocks_a_long(self, strategy_config):
        strategy, gap = self._strategy_with_bullish_gap(strategy_config)
        strategy.bias = Bias.BEARISH
        assert strategy.evaluate(gap.mid) is None
        assert "blocks longs" in strategy.last_block_reason

    def test_price_beyond_the_far_edge_is_rejected(self, strategy_config):
        strategy, gap = self._strategy_with_bullish_gap(strategy_config)
        # Inside `contains` only at the exact bottom; push just below it.
        assert strategy.evaluate(gap.bottom - Decimal("0.01")) is None

    def test_mitigated_gaps_stop_signalling(self, strategy_config):
        candles = append_bullish_gap(flat_series(40), gap_size="20")
        gap_bottom = candles[-3].high
        candles.append(
            Candle(
                timestamp=candles[-1].timestamp + BAR_SECONDS,
                open=gap_bottom + Decimal("10"),
                high=gap_bottom + Decimal("11"),
                low=gap_bottom - Decimal("5"),
                close=gap_bottom - Decimal("3"),
            )
        )
        strategy = FVGStrategy("ETHRUSDPERP", strategy_config)
        strategy.update_candles(candles)

        assert strategy.gaps[0].state is FVGState.MITIGATED
        assert strategy.evaluate(strategy.gaps[0].mid) is None

    def test_opposing_target_picks_the_nearest_unfilled_gap_above(self, strategy_config):
        strategy, _ = self._strategy_with_bullish_gap(strategy_config)
        # Two unfilled bearish gaps overhead; the nearer one is the target.
        strategy.gaps = [
            _bearish_gap(bottom="1100", top="1120", timestamp=1),
            _bearish_gap(bottom="1200", top="1220", timestamp=2),
        ]
        assert strategy.opposing_target(SignalSide.LONG, Decimal("1000")) == Decimal("1100")

    def test_opposing_target_for_a_short_picks_the_nearest_gap_below(self, strategy_config):
        strategy, _ = self._strategy_with_bullish_gap(strategy_config)
        strategy.gaps = [
            _bullish_gap(bottom="900", top="920", timestamp=1),
            _bullish_gap(bottom="800", top="820", timestamp=2),
        ]
        # For a short the near edge of a bullish gap is its top.
        assert strategy.opposing_target(SignalSide.SHORT, Decimal("1000")) == Decimal("920")

    def test_opposing_target_ignores_mitigated_gaps(self, strategy_config):
        strategy, _ = self._strategy_with_bullish_gap(strategy_config)
        filled = _bearish_gap(bottom="1100", top="1120", timestamp=1)
        filled.state = FVGState.MITIGATED
        strategy.gaps = [filled]
        assert strategy.opposing_target(SignalSide.LONG, Decimal("1000")) is None

    def test_no_opposing_target_when_none_exists(self, strategy_config):
        strategy, gap = self._strategy_with_bullish_gap(strategy_config)
        assert strategy.opposing_target(SignalSide.LONG, gap.mid) is None

    def test_snapshot_reports_strategy_state(self, strategy_config):
        strategy, _ = self._strategy_with_bullish_gap(strategy_config)
        snapshot = strategy.snapshot()
        assert snapshot["symbol"] == "ETHRUSDPERP"
        assert snapshot["gaps"] == len(strategy.gaps)
        assert snapshot["atr"] == strategy.atr

    def test_signalled_markers_do_not_grow_without_bound(self, strategy_config):
        """Markers for gaps that scrolled out of the window are dropped."""
        strategy, gap = self._strategy_with_bullish_gap(strategy_config)
        assert strategy.evaluate(gap.mid) is not None
        assert len(strategy._signalled) == 1

        # Replace the history entirely; the old gap no longer exists.
        strategy.update_candles(flat_series(60, price="5000"))
        assert strategy._signalled == set()


@pytest.mark.parametrize("zone_ratio", ["0", "0.25", "0.5", "0.75", "1"])
def test_entry_price_always_lands_inside_the_zone(strategy_config, zone_ratio):
    candles = append_bullish_gap(flat_series(30), gap_size="20")
    gap = detect_fvgs(candles, strategy_config, "X")[0]
    entry = gap.entry_price(Decimal(zone_ratio))
    assert gap.bottom <= entry <= gap.top
