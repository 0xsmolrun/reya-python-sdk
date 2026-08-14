"""Rendering tests for the Matrix dashboard.

These drive the real Textual app headlessly, which is what catches broken CSS
selectors, bad layout ids and renderables that raise only when painted.
"""

from decimal import Decimal

import pytest

from bot.config import BotConfig, UIConfig
from bot.engine import TradingEngine
from bot.exchange.base import BrokerOrder, BrokerPosition, MarketMeta
from bot.exchange.data_feed import DepthBook, FeedStatus
from bot.state import MarketTrade, TradeRecord
from bot.strategy.fvg import FVGStrategy, SignalSide
from bot.strategy.risk import RiskManager
from bot.ui import panels
from bot.ui.chart import CandleChart
from bot.ui.rain import GLYPHS, MatrixRain
from tests.test_bot.conftest import append_bullish_gap, flat_series


@pytest.fixture
def engine(strategy_config, risk_config) -> TradingEngine:
    """A populated engine that has never touched the network."""
    config = BotConfig(mode="paper", symbols=["ETHRUSDPERP", "BTCRUSDPERP"])
    config.strategy = strategy_config
    config.risk = risk_config
    config.ui = UIConfig(refresh_ms=50, rain_ms=50, rain_density=0.5)

    engine = TradingEngine(config)
    engine.state.equity = Decimal("10000")
    engine.state.balance = Decimal("10000")
    engine.state.session_start_equity = Decimal("10000")
    engine.state.feed = FeedStatus(connected=True, last_message_at=9e9, subscriptions=8)

    candles = append_bullish_gap(flat_series(60), gap_size="20", body="30")
    for symbol in config.symbols:
        strategy = FVGStrategy(symbol, strategy_config)
        strategy.update_candles(candles)
        engine.strategies[symbol] = strategy

        state = engine.state.symbol_state(symbol)
        state.meta = MarketMeta(
            symbol=symbol,
            market_id=1,
            tick_size=Decimal("0.01"),
            qty_step_size=Decimal("0.001"),
            min_order_qty=Decimal("0.001"),
            max_leverage=20,
        )
        state.candles = strategy.candles
        state.gaps = strategy.gaps
        state.bias = strategy.bias
        state.atr = strategy.atr
        state.price = candles[-1].close
        state.previous_price = candles[-2].close
        state.funding_rate = Decimal("0.00001")
        state.price_change_24h = Decimal("1.5")
        state.depth = DepthBook(
            symbol=symbol,
            bids=[(Decimal("999.5"), Decimal("2")), (Decimal("999.0"), Decimal("5"))],
            asks=[(Decimal("1000.5"), Decimal("3")), (Decimal("1001.0"), Decimal("1"))],
        )
        state.tape.append(
            MarketTrade(symbol=symbol, side=SignalSide.LONG, qty=Decimal("1"), price=Decimal("1000"), timestamp=0)
        )

    engine.risk = RiskManager(config.risk)
    engine.state.risk = engine.risk.state
    return engine


def add_position(engine: TradingEngine) -> None:
    """Give the focused symbol an open position with brackets and history."""
    symbol = engine.state.focus_symbol
    state = engine.state.symbol_state(symbol)
    state.position = BrokerPosition(
        symbol=symbol,
        side=SignalSide.LONG,
        qty=Decimal("1.5"),
        avg_entry_price=Decimal("990"),
    )
    state.orders = [
        BrokerOrder(
            order_id="1",
            symbol=symbol,
            side=SignalSide.SHORT,
            qty=Decimal("1.5"),
            limit_price=None,
            order_type="SL",
            trigger_price=Decimal("975"),
        ),
        BrokerOrder(
            order_id="2",
            symbol=symbol,
            side=SignalSide.SHORT,
            qty=Decimal("1.5"),
            limit_price=None,
            order_type="TP",
            trigger_price=Decimal("1020"),
        ),
    ]
    engine.state.trades.append(
        TradeRecord(
            symbol=symbol,
            side=SignalSide.LONG,
            qty=Decimal("1"),
            entry_price=Decimal("980"),
            exit_price=Decimal("1000"),
            pnl=Decimal("20"),
            r_multiple=Decimal("2"),
            opened_at=0.0,
            closed_at=60.0,
            reason="take profit",
        )
    )


class TestCandleChart:
    def test_renders_the_requested_height(self):
        chart = CandleChart(width=40, height=12)
        text = chart.render(flat_series(60))
        assert len(text.plain.splitlines()) == 12

    def test_handles_an_empty_series(self):
        assert "no candles" in CandleChart(width=40, height=10).render([]).plain

    def test_draws_gap_zones_and_levels_without_error(self, strategy_config):
        from bot.strategy.fvg import detect_fvgs, update_gap_states

        candles = append_bullish_gap(flat_series(40), gap_size="20", body="30")
        gaps = detect_fvgs(candles, strategy_config, "ETHRUSDPERP")
        update_gap_states(gaps, candles, strategy_config)

        text = CandleChart(width=50, height=14).render(
            candles,
            gaps,
            price=candles[-1].close,
            levels=[(candles[-1].close, "─", "#00FF41")],
        )
        assert len(text.plain.splitlines()) == 14

    def test_survives_a_single_candle(self):
        text = CandleChart(width=30, height=8).render(flat_series(1))
        assert len(text.plain.splitlines()) == 8

    def test_survives_a_zero_range_series(self):
        candles = flat_series(30, price="1000", spread="0")
        assert CandleChart(width=30, height=8).render(candles).plain


class TestPanels:
    def test_every_panel_renders(self, engine):
        add_position(engine)
        state = engine.state
        renderables = [
            panels.render_header(state),
            panels.render_chart(state, 80, 20),
            panels.render_gaps(state),
            panels.render_depth(state),
            panels.render_tape(state),
            panels.render_positions(state),
            panels.render_orders(state),
            panels.render_history(state),
            panels.render_metrics(state, engine.stats()),
            panels.render_logs([]),
            panels.render_status(state, Decimal("0.5")),
            panels.render_controls(),
            panels.render_symbol_tabs(state),
            panels.render_startup_banner(),
        ]
        assert all(renderable is not None for renderable in renderables)

    def test_panels_render_with_empty_state(self):
        state = TradingEngine(BotConfig(mode="paper", symbols=["ETHRUSDPERP"])).state
        assert panels.render_positions(state) is not None
        assert panels.render_orders(state) is not None
        assert panels.render_depth(state) is not None
        assert panels.render_chart(state, 60, 15) is not None

    def test_panels_print_to_a_console(self, engine):
        """Rendering to a real console catches style and markup errors."""
        from rich.console import Console

        add_position(engine)
        console = Console(width=120, force_terminal=False, record=True)
        console.print(panels.render_header(engine.state))
        console.print(panels.render_gaps(engine.state))
        console.print(panels.render_positions(engine.state))
        console.print(panels.render_metrics(engine.state, engine.stats()))

        output = console.export_text()
        assert "ETHRUSDPERP" in output
        assert "POSITIONS" in output


class TestRain:
    def test_glyph_alphabet_is_non_empty(self):
        assert len(GLYPHS) > 20

    def test_pulse_raises_and_clamps_intensity(self):
        rain = MatrixRain()
        rain.pulse(0.5)
        assert rain.intensity == pytest.approx(0.5)
        rain.pulse(5.0)
        assert rain.intensity == 1.0

    def test_tick_is_a_no_op_before_layout(self):
        # size is 0x0 until the widget is mounted; ticking must not raise.
        MatrixRain().tick()


class TestApp:
    async def test_dashboard_mounts_and_repaints(self, engine):
        from bot.ui.matrix_app import MatrixApp

        add_position(engine)
        app = MatrixApp(engine)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            # A second frame exercises the timer-driven repaint path.
            app._refresh_panels()
            await pilot.pause()
            assert app.query_one("#chart") is not None
            assert app.query_one("#status") is not None

    async def test_controls_drive_the_engine(self, engine):
        from bot.ui.matrix_app import MatrixApp

        app = MatrixApp(engine)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("s")
            assert engine.state.running is True

            await pilot.press("space")
            assert engine.state.paused is True
            await pilot.press("space")
            assert engine.state.paused is False

            await pilot.press("x")
            assert engine.state.running is False

            before = engine.config.risk.risk_per_trade_pct
            await pilot.press("]")
            assert engine.config.risk.risk_per_trade_pct > before
            await pilot.press("[")
            assert engine.config.risk.risk_per_trade_pct == before

    async def test_tab_cycles_the_focused_symbol(self, engine):
        from bot.ui.matrix_app import MatrixApp

        app = MatrixApp(engine)
        async with app.run_test(size=(160, 50)) as pilot:
            first = engine.state.focus_symbol
            await pilot.press("tab")
            assert engine.state.focus_symbol != first
            await pilot.press("tab")
            assert engine.state.focus_symbol == first

    async def test_rain_can_be_disabled(self, engine):
        from bot.ui.matrix_app import MatrixApp

        engine.config.ui.rain_enabled = False
        app = MatrixApp(engine)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            assert app.query_one("#rain-left").display is False
