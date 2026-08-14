"""The Matrix terminal dashboard.

A Textual app that renders the engine's state a few times a second and gives the
operator direct control: arm, disarm, pause, flatten, retune risk and switch
symbols without leaving the terminal.

The UI never blocks the trading loop. It only *reads* :class:`~bot.state.BotState`
and calls engine methods that are safe to invoke from the event loop; anything
that touches the exchange is dispatched as a Textual worker so a slow REST call
cannot freeze a repaint.
"""

from typing import Optional

import logging
from decimal import Decimal

from rich.console import RenderableType
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Static

from bot.engine import TradingEngine
from bot.ui import panels
from bot.ui.rain import MatrixRain
from bot.utils.logging import RingBufferHandler, get_ring_buffer

logger = logging.getLogger("bot.ui")

#: Risk adjustment step for the `[` and `]` keys, in percentage points.
RISK_STEP = Decimal("0.1")


class Panel(Static):
    """A Static that repaints from a render function each frame."""

    def show(self, renderable: RenderableType) -> None:
        """Replace the panel's content."""
        self.update(renderable)


class MatrixApp(App):
    """Terminal dashboard for the Reya 15m FVG bot."""

    CSS_PATH = "matrix.tcss"
    TITLE = "REYA · 15m FVG ENGINE"

    BINDINGS = [
        Binding("s", "arm", "Arm", show=True),
        Binding("x", "disarm", "Disarm", show=True),
        Binding("space", "toggle_pause", "Pause", show=True),
        Binding("f", "flatten", "Flatten", show=True),
        Binding("F", "flatten_all", "Flatten all", show=True),
        Binding("left_square_bracket", "risk_down", "Risk -", show=True),
        Binding("right_square_bracket", "risk_up", "Risk +", show=True),
        Binding("n", "next_symbol", "Next symbol", show=True),
        Binding("p", "previous_symbol", "Prev symbol", show=False),
        # Tab would otherwise be swallowed by Textual's focus handling.
        Binding("tab", "next_symbol", "Next symbol", show=False, priority=True),
        Binding("r", "reset_limits", "Reset limits", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, engine: TradingEngine, log_buffer: Optional[RingBufferHandler] = None) -> None:
        """Create the dashboard.

        Args:
            engine: The running engine to display and control.
            log_buffer: Ring buffer to read the strategy log from.
        """
        super().__init__()
        self.engine = engine
        self.log_buffer = log_buffer or get_ring_buffer()
        self.config = engine.config
        self._rain_left: Optional[MatrixRain] = None
        self._rain_right: Optional[MatrixRain] = None
        self._last_fill_seen = 0.0

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        """Build the three-column dashboard flanked by rain."""
        yield Panel(panels.render_startup_banner(), id="header")
        with Horizontal(id="body"):
            yield MatrixRain(density=self.config.ui.rain_density, id="rain-left")
            with Vertical(id="left"):
                yield Panel(id="chart")
                yield Panel(id="gaps")
            with Vertical(id="centre"):
                yield Panel(id="depth")
                yield Panel(id="tape")
            with Vertical(id="right"):
                yield Panel(id="positions")
                yield Panel(id="orders")
                yield Panel(id="history")
                yield Panel(id="metrics")
                yield Panel(id="logs")
            yield MatrixRain(density=self.config.ui.rain_density, id="rain-right")
        yield Panel(id="tabs")
        yield Panel(id="status")
        yield Panel(panels.render_controls(), id="controls")

    def on_mount(self) -> None:
        """Start the repaint and animation timers."""
        self.theme = "textual-dark"
        self._rain_left = self.query_one("#rain-left", MatrixRain)
        self._rain_right = self.query_one("#rain-right", MatrixRain)

        if not self.config.ui.rain_enabled:
            self._rain_left.display = False
            self._rain_right.display = False
        else:
            self.set_interval(self.config.ui.rain_ms / 1000, self._tick_rain)

        self.set_interval(self.config.ui.refresh_ms / 1000, self._refresh_panels)
        self._refresh_panels()

    # ------------------------------------------------------------------
    # Frames
    # ------------------------------------------------------------------
    def _tick_rain(self) -> None:
        """Advance the rain, intensifying it briefly after each fill."""
        state = self.engine.state
        if state.last_fill_at > self._last_fill_seen:
            self._last_fill_seen = state.last_fill_at
            for rain in (self._rain_left, self._rain_right):
                if rain is not None:
                    rain.pulse(1.0)

        for rain in (self._rain_left, self._rain_right):
            if rain is not None and rain.display:
                rain.tick()

    def _refresh_panels(self) -> None:
        """Repaint every data panel from the current engine state.

        Children mount asynchronously, so a frame can land before the layout
        exists; that case is skipped quietly rather than logged as an error.
        """
        state = self.engine.state
        ui = self.config.ui

        try:
            chart_widget = self.query_one("#chart", Panel)
            chart_size = chart_widget.size

            self.query_one("#header", Panel).show(panels.render_header(state))
            chart_widget.show(
                panels.render_chart(state, width=max(30, chart_size.width), height=max(8, chart_size.height))
            )
            self.query_one("#gaps", Panel).show(panels.render_gaps(state))
            self.query_one("#depth", Panel).show(panels.render_depth(state, ui.depth_levels))
            self.query_one("#tape", Panel).show(panels.render_tape(state, ui.trade_rows))
            self.query_one("#positions", Panel).show(panels.render_positions(state))
            self.query_one("#orders", Panel).show(panels.render_orders(state))
            self.query_one("#history", Panel).show(panels.render_history(state))
            self.query_one("#metrics", Panel).show(panels.render_metrics(state, self.engine.stats()))
            self.query_one("#logs", Panel).show(panels.render_logs(self.log_buffer.tail(ui.log_lines)))
            self.query_one("#tabs", Panel).show(panels.render_symbol_tabs(state))
            self.query_one("#status", Panel).show(panels.render_status(state, self.config.risk.risk_per_trade_pct))
        except NoMatches:
            return  # layout not ready yet; the next frame will paint it
        except Exception:  # noqa: BLE001 - a render bug must not kill the bot
            logger.exception("Dashboard repaint failed")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_arm(self) -> None:
        """Allow the strategy to take new entries."""
        self.engine.arm()
        self.notify("Strategy armed", severity="information")

    def action_disarm(self) -> None:
        """Stop new entries; open positions keep their brackets."""
        self.engine.disarm()
        self.notify("Strategy disarmed", severity="warning")

    def action_toggle_pause(self) -> None:
        """Pause or resume entries."""
        paused = self.engine.toggle_pause()
        self.notify("Entries paused" if paused else "Entries resumed", severity="warning" if paused else "information")

    def action_risk_up(self) -> None:
        """Increase the per-trade risk budget."""
        applied = self.engine.set_risk_pct(self.config.risk.risk_per_trade_pct + RISK_STEP)
        self.notify(f"Risk per trade: {applied}%", severity="information")

    def action_risk_down(self) -> None:
        """Decrease the per-trade risk budget."""
        applied = self.engine.set_risk_pct(self.config.risk.risk_per_trade_pct - RISK_STEP)
        self.notify(f"Risk per trade: {applied}%", severity="information")

    def action_next_symbol(self) -> None:
        """Move focus to the next configured symbol."""
        symbol = self.engine.cycle_focus(1)
        self.notify(f"Focus: {symbol}", severity="information")
        self._refresh_panels()

    def action_previous_symbol(self) -> None:
        """Move focus to the previous configured symbol."""
        symbol = self.engine.cycle_focus(-1)
        self.notify(f"Focus: {symbol}", severity="information")
        self._refresh_panels()

    def action_reset_limits(self) -> None:
        """Clear a halt or cooldown after an operator review."""
        self.engine.risk.resume()
        self.engine.state.notice = ""
        self.notify("Risk limits reset", severity="warning")

    async def action_flatten(self) -> None:
        """Close the focused symbol's position."""
        symbol = self.engine.state.focus_symbol
        self.notify(f"Flattening {symbol}...", severity="warning")
        closed = await self.engine.flatten(symbol, reason="manual close")
        self.notify(f"{symbol} closed" if closed else f"{symbol} was already flat")

    async def action_flatten_all(self) -> None:
        """Close every open position."""
        self.notify("Flattening all positions...", severity="warning")
        count = await self.engine.flatten_all(reason="manual close")
        self.notify(f"Closed {count} position(s)")

    async def action_quit(self) -> None:
        """Leave the dashboard. The caller stops the engine."""
        self.exit()


__all__ = ["MatrixApp"]
