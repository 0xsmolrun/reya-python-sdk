"""Tests for position sizing, bracket levels and the trading circuit breakers."""

from decimal import Decimal

import pytest

from bot.strategy.fvg import FairValueGap, FVGType, Signal, SignalSide
from bot.strategy.indicators import Bias
from bot.strategy.risk import RiskManager

# A fixed clock keeps the day-rollover and cooldown tests deterministic.
NOON_UTC = 1_700_000_000.0
ONE_DAY = 86_400.0


def make_signal(
    side: SignalSide = SignalSide.LONG,
    entry: str = "1000",
    far_edge: str = "990",
    atr: str = "10",
    opposing: str = None,
) -> Signal:
    """Build a signal whose gap has the requested far edge."""
    kind = FVGType.BULLISH if side is SignalSide.LONG else FVGType.BEARISH
    if side is SignalSide.LONG:
        bottom, top = Decimal(far_edge), Decimal(far_edge) + Decimal("20")
    else:
        bottom, top = Decimal(far_edge) - Decimal("20"), Decimal(far_edge)

    gap = FairValueGap(
        symbol="ETHRUSDPERP",
        kind=kind,
        index=10,
        timestamp=1,
        top=top,
        bottom=bottom,
        atr=Decimal(atr),
    )
    return Signal(
        symbol="ETHRUSDPERP",
        side=side,
        price=Decimal(entry),
        entry_price=Decimal(entry),
        fvg=gap,
        atr=Decimal(atr),
        bias=Bias.NEUTRAL,
        opposing_target=Decimal(opposing) if opposing else None,
    )


class TestStopPlacement:
    def test_long_stop_sits_below_the_far_edge_by_an_atr_buffer(self, risk_config):
        manager = RiskManager(risk_config)
        signal = make_signal(entry="1000", far_edge="990", atr="10")
        # far edge 990 - (0.5 * 10) = 985
        assert manager.stop_for(signal) == Decimal("985")

    def test_short_stop_sits_above_the_far_edge(self, risk_config):
        manager = RiskManager(risk_config)
        signal = make_signal(side=SignalSide.SHORT, entry="1000", far_edge="1010", atr="10")
        assert manager.stop_for(signal) == Decimal("1015")

    def test_minimum_stop_distance_is_enforced(self, risk_config):
        """A gap edge right next to the entry must not create a huge position."""
        risk_config.min_stop_atr_mult = Decimal("2")  # at least 20 points here
        manager = RiskManager(risk_config)
        signal = make_signal(entry="1000", far_edge="999.9", atr="10")

        stop = manager.stop_for(signal)
        assert stop == Decimal("980")  # entry - 2 * ATR, not the 999.85 edge

    def test_minimum_stop_distance_is_enforced_for_shorts(self, risk_config):
        risk_config.min_stop_atr_mult = Decimal("2")
        manager = RiskManager(risk_config)
        signal = make_signal(side=SignalSide.SHORT, entry="1000", far_edge="1000.1", atr="10")
        assert manager.stop_for(signal) == Decimal("1020")


class TestTargetPlacement:
    def test_fixed_reward_multiple(self, risk_config):
        manager = RiskManager(risk_config)
        signal = make_signal(entry="1000", far_edge="990", atr="10")
        # risk 15 points, reward multiple 2 -> 1030
        assert manager.target_for(signal, Decimal("1000"), Decimal("985")) == Decimal("1030")

    def test_short_target_is_below_the_entry(self, risk_config):
        manager = RiskManager(risk_config)
        signal = make_signal(side=SignalSide.SHORT, entry="1000", far_edge="1010", atr="10")
        assert manager.target_for(signal, Decimal("1000"), Decimal("1015")) == Decimal("970")

    def test_opposing_gap_target_is_used_when_it_pays_enough(self, risk_config):
        risk_config.tp_mode = "opposing_fvg"
        risk_config.min_risk_reward = Decimal("1.5")
        manager = RiskManager(risk_config)
        signal = make_signal(entry="1000", far_edge="990", atr="10", opposing="1050")
        # 50 points of reward on 15 of risk is 3.3R, comfortably above the floor.
        assert manager.target_for(signal, Decimal("1000"), Decimal("985")) == Decimal("1050")

    def test_opposing_gap_target_is_ignored_when_it_pays_too_little(self, risk_config):
        risk_config.tp_mode = "opposing_fvg"
        risk_config.min_risk_reward = Decimal("1.5")
        manager = RiskManager(risk_config)
        signal = make_signal(entry="1000", far_edge="990", atr="10", opposing="1005")
        # 5 points on 15 of risk is 0.33R, so the fixed multiple wins.
        assert manager.target_for(signal, Decimal("1000"), Decimal("985")) == Decimal("1030")


class TestSizing:
    def test_quantity_puts_exactly_the_risk_budget_at_stake(self, risk_config, market_meta):
        manager = RiskManager(risk_config)
        signal = make_signal(entry="1000", far_edge="990", atr="10")

        plan = manager.build_plan(signal, Decimal("10000"), market_meta)

        assert plan is not None
        # 1% of 10,000 = 100 risked over a 15-point stop -> 6.666 units.
        assert plan.stop_price == Decimal("985")
        assert plan.qty == Decimal("6.666")
        assert plan.risk_amount <= Decimal("100")
        assert plan.risk_reward == Decimal("2")

    def test_quantity_scales_with_equity(self, risk_config, market_meta):
        manager = RiskManager(risk_config)
        signal = make_signal(entry="1000", far_edge="990", atr="10")

        small = manager.build_plan(signal, Decimal("1000"), market_meta)
        large = manager.build_plan(signal, Decimal("100000"), market_meta)

        assert small is not None and large is not None
        assert large.qty > small.qty * Decimal("50")

    def test_wider_stop_means_smaller_size(self, risk_config, market_meta):
        manager = RiskManager(risk_config)
        tight = manager.build_plan(make_signal(entry="1000", far_edge="995", atr="2"), Decimal("10000"), market_meta)
        wide = manager.build_plan(make_signal(entry="1000", far_edge="950", atr="20"), Decimal("10000"), market_meta)

        assert tight is not None and wide is not None
        assert wide.qty < tight.qty

    def test_size_is_rounded_down_onto_the_step(self, risk_config, market_meta):
        manager = RiskManager(risk_config)
        plan = manager.build_plan(make_signal(entry="1000", far_edge="990", atr="10"), Decimal("10000"), market_meta)

        assert plan is not None
        # Rounding down keeps realised risk at or under the budget.
        assert plan.qty % market_meta.qty_step_size == Decimal("0")
        assert plan.risk_amount <= Decimal("10000") * Decimal("1") / Decimal("100")

    def test_notional_cap_limits_leverage(self, risk_config, market_meta):
        risk_config.max_notional_pct = Decimal("100")  # 1x equity
        manager = RiskManager(risk_config)
        # A very tight stop would otherwise buy an enormous position.
        plan = manager.build_plan(make_signal(entry="1000", far_edge="999.9", atr="0.2"), Decimal("10000"), market_meta)

        assert plan is not None
        assert plan.notional <= Decimal("10000")

    def test_exchange_leverage_cap_is_respected(self, risk_config, market_meta):
        risk_config.max_notional_pct = Decimal("100000")
        manager = RiskManager(risk_config)
        plan = manager.build_plan(make_signal(entry="1000", far_edge="999.9", atr="0.2"), Decimal("10000"), market_meta)

        assert plan is not None
        assert plan.notional <= Decimal("10000") * market_meta.max_leverage

    def test_equity_cap_limits_sizing(self, risk_config, market_meta):
        risk_config.equity_cap = Decimal("1000")
        manager = RiskManager(risk_config)
        plan = manager.build_plan(make_signal(entry="1000", far_edge="990", atr="10"), Decimal("100000"), market_meta)

        assert plan is not None
        # Sized off 1,000 rather than 100,000: 1% = 10 over a 15-point stop.
        assert plan.qty == Decimal("0.666")

    def test_zero_equity_is_rejected(self, risk_config, market_meta):
        manager = RiskManager(risk_config)
        assert manager.build_plan(make_signal(), Decimal("0"), market_meta) is None
        assert manager.last_rejection == "no equity"

    def test_size_below_the_market_minimum_is_rejected(self, risk_config, market_meta):
        risk_config.risk_per_trade_pct = Decimal("0.05")
        manager = RiskManager(risk_config)
        plan = manager.build_plan(make_signal(entry="1000", far_edge="990", atr="10"), Decimal("1"), market_meta)

        assert plan is None
        assert "below market minimum" in manager.last_rejection or "rounds to zero" in manager.last_rejection

    def test_reward_below_the_floor_is_rejected(self, risk_config, market_meta):
        risk_config.min_risk_reward = Decimal("5")
        manager = RiskManager(risk_config)
        plan = manager.build_plan(make_signal(entry="1000", far_edge="990", atr="10"), Decimal("10000"), market_meta)

        assert plan is None
        assert "below minimum" in manager.last_rejection

    def test_risk_percentage_is_clamped_to_the_ceiling(self, risk_config, market_meta):
        risk_config.risk_per_trade_pct = Decimal("50")
        risk_config.max_risk_per_trade_pct = Decimal("2")
        manager = RiskManager(risk_config)
        plan = manager.build_plan(make_signal(entry="1000", far_edge="990", atr="10"), Decimal("10000"), market_meta)

        assert plan is not None
        # Sized at 2%, not 50%.
        assert plan.risk_amount <= Decimal("200")

    def test_prices_are_snapped_to_the_tick(self, risk_config, market_meta):
        manager = RiskManager(risk_config)
        signal = make_signal(entry="1000.007", far_edge="990.003", atr="10")
        plan = manager.build_plan(signal, Decimal("10000"), market_meta)

        assert plan is not None
        for price in (plan.entry_price, plan.stop_price, plan.take_profit):
            assert price % market_meta.tick_size == Decimal("0")

    def test_short_plan_has_stop_above_and_target_below(self, risk_config, market_meta):
        manager = RiskManager(risk_config)
        signal = make_signal(side=SignalSide.SHORT, entry="1000", far_edge="1010", atr="10")
        plan = manager.build_plan(signal, Decimal("10000"), market_meta)

        assert plan is not None
        assert plan.stop_price > plan.entry_price > plan.take_profit
        assert not plan.is_buy


class TestGates:
    def test_clear_by_default(self, risk_config):
        manager = RiskManager(risk_config)
        manager.sync_equity(Decimal("10000"), now=NOON_UTC)
        assert manager.check_gates(open_positions=0, symbol_has_position=False, now=NOON_UTC) is None

    def test_existing_position_in_the_symbol_blocks(self, risk_config):
        manager = RiskManager(risk_config)
        blocked = manager.check_gates(open_positions=1, symbol_has_position=True, now=NOON_UTC)
        assert blocked is not None and "already in position" in blocked

    def test_max_open_positions_blocks(self, risk_config):
        risk_config.max_open_positions = 2
        manager = RiskManager(risk_config)
        blocked = manager.check_gates(open_positions=2, symbol_has_position=False, now=NOON_UTC)
        assert blocked is not None and "max open positions" in blocked

    def test_daily_trade_limit_blocks(self, risk_config):
        risk_config.max_daily_trades = 2
        manager = RiskManager(risk_config)
        manager.sync_equity(Decimal("10000"), now=NOON_UTC)
        manager.record_entry(now=NOON_UTC)
        manager.record_entry(now=NOON_UTC)

        blocked = manager.check_gates(open_positions=0, symbol_has_position=False, now=NOON_UTC)
        assert blocked is not None and "daily trade limit" in blocked

    def test_daily_loss_limit_halts_trading(self, risk_config):
        risk_config.max_daily_loss_pct = Decimal("3")
        manager = RiskManager(risk_config)
        manager.sync_equity(Decimal("10000"), now=NOON_UTC)
        manager.record_result(Decimal("-400"), now=NOON_UTC)  # 4% of equity

        assert manager.state.halted
        blocked = manager.check_gates(open_positions=0, symbol_has_position=False, now=NOON_UTC)
        assert blocked is not None and "halted" in blocked

    def test_a_loss_inside_the_limit_does_not_halt(self, risk_config):
        risk_config.max_daily_loss_pct = Decimal("3")
        manager = RiskManager(risk_config)
        manager.sync_equity(Decimal("10000"), now=NOON_UTC)
        manager.record_result(Decimal("-100"), now=NOON_UTC)  # 1%

        assert not manager.state.halted
        assert manager.check_gates(open_positions=0, symbol_has_position=False, now=NOON_UTC) is None

    def test_loss_streak_starts_a_cooldown(self, risk_config):
        risk_config.consecutive_loss_limit = 3
        risk_config.cooldown_minutes = 60
        risk_config.max_daily_loss_pct = Decimal("100")  # isolate the streak rule
        manager = RiskManager(risk_config)
        manager.sync_equity(Decimal("100000"), now=NOON_UTC)

        for _ in range(3):
            manager.record_result(Decimal("-10"), now=NOON_UTC)

        assert manager.state.consecutive_losses == 3
        assert manager.state.cooldown_until == NOON_UTC + 3600
        blocked = manager.check_gates(open_positions=0, symbol_has_position=False, now=NOON_UTC + 60)
        assert blocked is not None and "cooldown" in blocked

    def test_a_win_resets_the_loss_streak(self, risk_config):
        risk_config.max_daily_loss_pct = Decimal("100")
        manager = RiskManager(risk_config)
        manager.sync_equity(Decimal("100000"), now=NOON_UTC)

        manager.record_result(Decimal("-10"), now=NOON_UTC)
        manager.record_result(Decimal("-10"), now=NOON_UTC)
        manager.record_result(Decimal("50"), now=NOON_UTC)

        assert manager.state.consecutive_losses == 0
        assert manager.state.wins == 1 and manager.state.losses == 2

    def test_cooldown_expires(self, risk_config):
        risk_config.consecutive_loss_limit = 1
        risk_config.cooldown_minutes = 1
        risk_config.max_daily_loss_pct = Decimal("100")
        manager = RiskManager(risk_config)
        manager.sync_equity(Decimal("100000"), now=NOON_UTC)
        manager.record_result(Decimal("-10"), now=NOON_UTC)

        # check_gates reads the wall clock for the cooldown, so wind it back.
        manager.state.cooldown_until = 0.0
        assert manager.check_gates(open_positions=0, symbol_has_position=False, now=NOON_UTC) is None

    def test_manual_halt_and_resume(self, risk_config):
        manager = RiskManager(risk_config)
        manager.halt("operator stopped the bot")
        assert manager.check_gates(0, False, now=NOON_UTC) is not None

        manager.resume()
        manager.sync_equity(Decimal("10000"), now=NOON_UTC)
        assert manager.check_gates(0, False, now=NOON_UTC) is None


class TestDayRollover:
    def test_new_day_resets_counters_and_halts(self, risk_config):
        manager = RiskManager(risk_config)
        manager.sync_equity(Decimal("10000"), now=NOON_UTC)
        manager.record_entry(now=NOON_UTC)
        manager.record_result(Decimal("-500"), now=NOON_UTC)
        assert manager.state.halted

        manager.sync_equity(Decimal("9500"), now=NOON_UTC + ONE_DAY)

        assert not manager.state.halted
        assert manager.state.daily_trades == 0
        assert manager.state.daily_pnl == Decimal("0")
        assert manager.state.day_start_equity == Decimal("9500")

    def test_same_day_does_not_reset(self, risk_config):
        manager = RiskManager(risk_config)
        manager.sync_equity(Decimal("10000"), now=NOON_UTC)
        manager.record_entry(now=NOON_UTC)
        manager.sync_equity(Decimal("9900"), now=NOON_UTC + 600)

        assert manager.state.daily_trades == 1
        assert manager.state.day_start_equity == Decimal("10000")

    def test_daily_loss_percentage_is_measured_against_the_day_open(self, risk_config):
        manager = RiskManager(risk_config)
        manager.sync_equity(Decimal("10000"), now=NOON_UTC)
        risk_config.max_daily_loss_pct = Decimal("100")
        manager.record_result(Decimal("-250"), now=NOON_UTC)

        assert manager.state.daily_loss_pct == Decimal("2.5")

    def test_profit_leaves_the_loss_percentage_at_zero(self, risk_config):
        manager = RiskManager(risk_config)
        manager.sync_equity(Decimal("10000"), now=NOON_UTC)
        manager.record_result(Decimal("250"), now=NOON_UTC)
        assert manager.state.daily_loss_pct == Decimal("0")


@pytest.mark.parametrize("side", [SignalSide.LONG, SignalSide.SHORT])
def test_plan_stop_is_always_on_the_losing_side(risk_config, market_meta, side):
    """Whatever the inputs, the stop must sit where a loss would occur."""
    manager = RiskManager(risk_config)
    far_edge = "990" if side is SignalSide.LONG else "1010"
    plan = manager.build_plan(make_signal(side=side, far_edge=far_edge), Decimal("10000"), market_meta)

    assert plan is not None
    if side is SignalSide.LONG:
        assert plan.stop_price < plan.entry_price < plan.take_profit
    else:
        assert plan.stop_price > plan.entry_price > plan.take_profit
