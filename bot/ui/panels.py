"""Rich renderables for each panel of the Matrix dashboard.

These are pure functions of :class:`~bot.state.BotState`: give them the same
state and they produce the same picture. Keeping them free of widget plumbing
makes them straightforward to test headlessly.
"""

from typing import List, Optional, Sequence

import time
from decimal import Decimal

from rich.align import Align
from rich.box import HEAVY, SIMPLE
from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from bot.metrics import PerformanceStats
from bot.state import BotState
from bot.strategy.fvg import FairValueGap
from bot.ui import theme
from bot.ui.chart import CandleChart
from bot.utils.logging import LogRecordView
from bot.utils.numbers import ZERO, format_price, format_qty, format_signed

PANEL_BORDER = Style(color=theme.DIM)

#: Short labels so the history panel's exit column fits a narrow column.
EXIT_LABELS = {
    "take profit": "TP",
    "stop loss": "SL",
    "manual close": "manual",
    "reconciled": "recon",
    "end of data": "eod",
}
TITLE_STYLE = Style(color=theme.NEON, bold=True)


def framed(renderable: RenderableType, title: str, subtitle: Optional[str] = None) -> Panel:
    """Wrap a renderable in the standard glowing green frame."""
    return Panel(
        renderable,
        title=Text(f" {title} ", style=TITLE_STYLE),
        subtitle=Text(f" {subtitle} ", style=Style(color=theme.DARK)) if subtitle else None,
        border_style=PANEL_BORDER,
        box=HEAVY,
        padding=(0, 1),
    )


def _table(*columns: str, widths: Sequence[Optional[int]] = ()) -> Table:
    """Build a compact, borderless table with dim headers."""
    table = Table(
        box=SIMPLE,
        show_edge=False,
        pad_edge=False,
        expand=True,
        header_style=Style(color=theme.DARK),
    )
    for index, column in enumerate(columns):
        width = widths[index] if index < len(widths) else None
        table.add_column(column, width=width, no_wrap=True)
    return table


def _empty(message: str) -> Text:
    """Placeholder shown when a panel has nothing to display."""
    return Text(message, style=Style(color=theme.DARK, italic=True))


# ----------------------------------------------------------------------
# Header
# ----------------------------------------------------------------------
def render_header(state: BotState) -> RenderableType:
    """Top strip: symbol, price, PnL, equity and funding."""
    symbol = state.focus
    direction = symbol.price_direction
    price_colour = theme.UP if direction > 0 else theme.DOWN if direction < 0 else theme.VALUE
    arrow = "▲" if direction > 0 else "▼" if direction < 0 else "•"
    places = symbol.meta.price_places if symbol.meta else 2

    def cell(title: str, body: Text) -> Table:
        block = Table.grid(padding=(0, 0))
        block.add_column(justify="left")
        block.add_row(Text(title, style=Style(color=theme.DARK)))
        block.add_row(body)
        return block

    change = symbol.price_change_24h
    change_text = Text(f"{format_signed(change, 2)}%", style=Style(color=theme.pnl_style(change)))

    funding = symbol.funding_rate
    funding_text = Text(
        "--" if funding is None else f"{funding * Decimal(100):+.4f}%",
        style=Style(color=theme.pnl_style(-funding if funding is not None else None)),
    )

    session = state.session_pnl
    open_pnl = state.total_unrealized

    cells = [
        cell(
            "MARKET",
            Text(symbol.symbol, style=Style(color=theme.NEON, bold=True)),
        ),
        cell(
            "PRICE",
            Text(f"{arrow} {format_price(symbol.price, places)}", style=Style(color=price_colour, bold=True)),
        ),
        cell("24H", change_text),
        cell(
            "SESSION PnL",
            Text(format_signed(session), style=Style(color=theme.pnl_style(session), bold=True)),
        ),
        cell(
            "OPEN PnL",
            Text(format_signed(open_pnl), style=Style(color=theme.pnl_style(open_pnl))),
        ),
        cell(
            "EQUITY",
            Text(format_price(state.equity), style=Style(color=theme.VALUE, bold=True)),
        ),
        cell("FUNDING", funding_text),
        cell(
            "MODE",
            Text(
                state.mode.upper(),
                style=Style(color=theme.AMBER if state.mode == "live" else theme.CYAN, bold=True),
            ),
        ),
    ]
    return Columns(cells, expand=True, equal=True, padding=(0, 1))


# ----------------------------------------------------------------------
# Left column: chart and gaps
# ----------------------------------------------------------------------
def render_chart(state: BotState, width: int, height: int) -> RenderableType:
    """15m candles with Fair Value Gap zones and any active bracket levels."""
    symbol = state.focus
    if not symbol.candles:
        return framed(_empty("waiting for candle history..."), f"{symbol.symbol} 15m")

    levels = []
    trade = symbol.open_trade
    if trade is not None:
        levels = [
            (trade.entry_price, "─", theme.CYAN),
            (trade.stop_price, "─", theme.RED),
            (trade.take_profit, "─", theme.NEON),
        ]

    candles = list(symbol.candles)
    if symbol.forming_candle is not None:
        candles.append(symbol.forming_candle)

    chart = CandleChart(width=max(20, width - 14), height=max(6, height - 2))
    body = chart.render(candles, symbol.tradeable_gaps, symbol.price, levels)

    atr = f"ATR {format_price(symbol.atr, 2)}" if symbol.atr is not None else "ATR --"
    subtitle = f"{symbol.bias.value.lower()} bias · {atr} · {len(symbol.candles)} bars"
    return framed(body, f"{symbol.symbol} 15m", subtitle)


def render_gaps(state: BotState, limit: int = 8) -> RenderableType:
    """Tradeable Fair Value Gaps, newest first, with their entry levels."""
    symbol = state.focus
    gaps: List[FairValueGap] = symbol.tradeable_gaps[:limit]
    if not gaps:
        return framed(_empty("no active fair value gaps"), "FAIR VALUE GAPS")

    places = symbol.meta.price_places if symbol.meta else 2
    table = _table("DIR", "ZONE", "ENTRY", "SIZE", "AGE", "STATE", "Q")
    for gap in gaps:
        direction = "LONG" if gap.kind.is_bullish else "SHORT"
        table.add_row(
            Text(direction, style=Style(color=theme.side_style(gap.kind.is_bullish), bold=True)),
            Text(
                f"{format_price(gap.bottom, places)}-{format_price(gap.top, places)}",
                style=Style(color=theme.GREEN),
            ),
            Text(format_price(gap.entry_price(Decimal("0.5")), places), style=Style(color=theme.VALUE)),
            Text(format_price(gap.height, places), style=Style(color=theme.DIM)),
            Text(f"{gap.age_bars}b", style=Style(color=theme.DIM)),
            Text(gap.state.value, style=Style(color=theme.state_style(gap.state.value))),
            Text("★" * gap.quality, style=Style(color=theme.AMBER)),
        )
    return framed(table, "FAIR VALUE GAPS", f"{len(symbol.gaps)} tracked")


# ----------------------------------------------------------------------
# Centre column: order book and tape
# ----------------------------------------------------------------------
def render_depth(state: BotState, levels: int = 8) -> RenderableType:
    """L2 order book, asks descending above bids descending."""
    symbol = state.focus
    book = symbol.depth
    if not book.bids and not book.asks:
        return framed(_empty("waiting for order book..."), "DEPTH")

    places = symbol.meta.price_places if symbol.meta else 2
    max_qty = max([qty for _, qty in book.bids[:levels]] + [qty for _, qty in book.asks[:levels]] + [Decimal(1)])

    def rows(side: Sequence, colour: str, reverse: bool) -> List:
        window = list(side[:levels])
        if reverse:
            window.reverse()
        result = []
        for price, qty in window:
            # A proportional bar makes the resting size readable at a glance.
            bar_width = int((qty / max_qty) * Decimal(14)) if max_qty > ZERO else 0
            result.append(
                (
                    Text(format_price(price, places), style=Style(color=colour)),
                    Text(format_qty(qty, 3), style=Style(color=theme.DIM)),
                    Text("▇" * max(0, bar_width), style=Style(color=colour)),
                )
            )
        return result

    table = _table("PRICE", "SIZE", "")
    for row in rows(book.asks, theme.DOWN, reverse=True):
        table.add_row(*row)

    spread = book.spread_bps
    mid = book.mid
    table.add_row(
        Text(format_price(mid, places), style=Style(color=theme.NEON, bold=True)),
        Text(f"{spread:.1f}bp" if spread is not None else "--", style=Style(color=theme.DARK)),
        Text("─" * 14, style=Style(color=theme.DARK)),
    )
    for row in rows(book.bids, theme.UP, reverse=False):
        table.add_row(*row)

    return framed(table, "DEPTH", f"{len(book.bids)}x{len(book.asks)} levels")


def render_tape(state: BotState, rows: int = 12) -> RenderableType:
    """Recent public prints, newest first."""
    symbol = state.focus
    trades = list(symbol.tape)[-rows:][::-1]
    if not trades:
        return framed(_empty("waiting for trades..."), "TAPE")

    places = symbol.meta.price_places if symbol.meta else 2
    table = _table("TIME", "SIDE", "PRICE", "SIZE")
    for trade in trades:
        clock = time.strftime("%H:%M:%S", time.localtime(trade.timestamp / 1000)) if trade.timestamp else "--:--:--"
        table.add_row(
            Text(clock, style=Style(color=theme.DARK)),
            Text(
                "BUY" if trade.side.is_buy else "SELL",
                style=Style(color=theme.side_style(trade.side.is_buy), bold=True),
            ),
            Text(format_price(trade.price, places), style=Style(color=theme.GREEN)),
            Text(format_qty(trade.qty, 4), style=Style(color=theme.DIM)),
        )
    return framed(table, "TAPE")


# ----------------------------------------------------------------------
# Right column: positions, orders, history, metrics, logs
# ----------------------------------------------------------------------
def render_positions(state: BotState) -> RenderableType:
    """Open positions across every traded symbol."""
    positions = state.open_positions
    if not positions:
        return framed(_empty("flat"), "POSITIONS")

    table = _table("SYMBOL", "SIDE", "SIZE", "ENTRY", "PnL")
    for symbol, position in positions.items():
        symbol_state = state.symbol_state(symbol)
        places = symbol_state.meta.price_places if symbol_state.meta else 2
        pnl = position.unrealized_pnl(symbol_state.price)
        table.add_row(
            Text(symbol.replace("RUSDPERP", ""), style=Style(color=theme.VALUE, bold=True)),
            Text(
                position.side.value,
                style=Style(color=theme.side_style(position.side.is_buy), bold=True),
            ),
            Text(format_qty(position.qty, 4), style=Style(color=theme.GREEN)),
            Text(format_price(position.avg_entry_price, places), style=Style(color=theme.DIM)),
            Text(format_signed(pnl), style=Style(color=theme.pnl_style(pnl), bold=True)),
        )
    return framed(table, "POSITIONS", f"{len(positions)} open")


def render_orders(state: BotState) -> RenderableType:
    """Resting orders for the focused symbol, brackets included."""
    symbol = state.focus
    if not symbol.orders:
        return framed(_empty("no resting orders"), "ORDERS")

    places = symbol.meta.price_places if symbol.meta else 2
    table = _table("TYPE", "SIDE", "PRICE", "SIZE", "STATUS")
    for order in symbol.orders:
        price = order.trigger_price if order.is_bracket else order.limit_price
        colour = theme.RED if order.order_type == "SL" else theme.NEON if order.order_type == "TP" else theme.CYAN
        table.add_row(
            Text(order.order_type, style=Style(color=colour, bold=True)),
            Text(
                "BUY" if order.side.is_buy else "SELL",
                style=Style(color=theme.side_style(order.side.is_buy)),
            ),
            Text(format_price(price, places), style=Style(color=theme.GREEN)),
            Text(format_qty(order.qty, 4), style=Style(color=theme.DIM)),
            Text(order.status, style=Style(color=theme.DARK)),
        )
    return framed(table, "ORDERS", f"{len(symbol.orders)} resting")


def render_history(state: BotState, rows: int = 8) -> RenderableType:
    """Closed trades, newest first."""
    trades = state.trades[-rows:][::-1]
    if not trades:
        return framed(_empty("no closed trades yet"), "TRADE HISTORY")

    table = _table("TIME", "SYMBOL", "SIDE", "PnL", "R", "WHY")
    for trade in trades:
        table.add_row(
            Text(time.strftime("%H:%M", time.localtime(trade.closed_at)), style=Style(color=theme.DARK)),
            Text(trade.symbol.replace("RUSDPERP", ""), style=Style(color=theme.GREEN)),
            Text(
                trade.side.value,
                style=Style(color=theme.side_style(trade.side.is_buy)),
            ),
            Text(format_signed(trade.pnl), style=Style(color=theme.pnl_style(trade.pnl), bold=True)),
            Text(f"{trade.r_multiple:+.2f}", style=Style(color=theme.pnl_style(trade.r_multiple))),
            Text(EXIT_LABELS.get(trade.reason, trade.reason), style=Style(color=theme.DARK)),
        )
    return framed(table, "TRADE HISTORY", f"{len(state.trades)} total")


def render_metrics(state: BotState, stats: PerformanceStats) -> RenderableType:
    """Session performance and the current state of the risk limits."""
    left = Table.grid(padding=(0, 1))
    left.add_column(justify="left", style=Style(color=theme.DARK))
    left.add_column(justify="right")

    profit_factor = "inf" if stats.profit_factor == Decimal("Infinity") else f"{stats.profit_factor:.2f}"
    for name, body in (
        ("trades", str(stats.trades)),
        ("win rate", f"{stats.win_rate_pct:.0f}%"),
        ("profit factor", profit_factor),
        ("expectancy", f"{stats.expectancy_r:+.2f}R"),
        ("max drawdown", f"{stats.max_drawdown:.2f}"),
    ):
        left.add_row(Text(name), Text(body, style=Style(color=theme.VALUE)))

    right = Table.grid(padding=(0, 1))
    right.add_column(justify="left", style=Style(color=theme.DARK))
    right.add_column(justify="right")

    risk = state.risk
    if risk is not None:
        cooldown = f"{risk.cooldown_remaining_s}s" if risk.in_cooldown else "-"
        day_loss = f"{risk.daily_loss_pct:.2f}%"
        right.add_row(
            Text("day PnL"), Text(format_signed(risk.daily_pnl), style=Style(color=theme.pnl_style(risk.daily_pnl)))
        )
        right.add_row(
            Text("day loss"),
            Text(day_loss, style=Style(color=theme.AMBER if risk.daily_loss_pct > ZERO else theme.VALUE)),
        )
        right.add_row(Text("day trades"), Text(str(risk.daily_trades), style=Style(color=theme.VALUE)))
        right.add_row(
            Text("loss streak"),
            Text(
                str(risk.consecutive_losses), style=Style(color=theme.RED if risk.consecutive_losses else theme.VALUE)
            ),
        )
        right.add_row(
            Text("cooldown"), Text(cooldown, style=Style(color=theme.AMBER if risk.in_cooldown else theme.DARK))
        )

    return framed(Columns([left, right], expand=True, equal=True), "SESSION")


def render_logs(records: Sequence[LogRecordView], rows: int = 12) -> RenderableType:
    """Strategy log tail, newest at the bottom."""
    if not records:
        return framed(_empty("no log output yet"), "STRATEGY LOG")

    body = Text()
    for index, record in enumerate(list(records)[-rows:]):
        if index:
            body.append("\n")
        body.append(f"{record.clock} ", style=Style(color=theme.DARK))
        body.append(f"{record.message}", style=Style(color=record.style))
    return framed(body, "STRATEGY LOG")


# ----------------------------------------------------------------------
# Footer
# ----------------------------------------------------------------------
def render_status(state: BotState, risk_pct: Decimal) -> RenderableType:
    """Bottom bar: run state, connection health, risk budget and hotkeys."""
    feed = state.feed
    connected = bool(feed and feed.connected)
    silence = feed.silence_s if feed else float("inf")

    if not connected:
        link = Text("● LINK DOWN", style=Style(color=theme.RED, bold=True))
    elif silence > 30:
        link = Text(f"● LINK {silence:.0f}s", style=Style(color=theme.AMBER, bold=True))
    else:
        link = Text("● LINK UP", style=Style(color=theme.NEON, bold=True))

    status = Text(state.status_label, style=Style(color=theme.status_style(state.status_label), bold=True))
    uptime = time.strftime("%H:%M:%S", time.gmtime(state.uptime_s))

    parts = [
        status,
        link,
        Text(f"{state.broker_name}@{state.network}", style=Style(color=theme.DIM)),
        Text(f"risk {risk_pct}%", style=Style(color=theme.VALUE)),
        Text(f"up {uptime}", style=Style(color=theme.DARK)),
        Text(f"reconnects {feed.reconnects if feed else 0}", style=Style(color=theme.DARK)),
    ]

    message = state.notice or state.last_error or state.focus.status_reason
    if message:
        colour = theme.RED if state.last_error and not state.notice else theme.DARK
        parts.append(Text(f"› {message}", style=Style(color=colour)))

    line = Text(" │ ", style=Style(color=theme.DARKEST)).join(parts)
    return Align.left(line)


def render_controls() -> RenderableType:
    """Hotkey legend."""
    keys = [
        ("s", "arm"),
        ("x", "disarm"),
        ("space", "pause"),
        ("f", "flatten"),
        ("F", "flatten all"),
        ("[ ]", "risk"),
        ("n p", "symbol"),
        ("r", "reset limits"),
        ("q", "quit"),
    ]
    line = Text()
    for index, (key, action) in enumerate(keys):
        if index:
            line.append("  ", style=Style(color=theme.DARKEST))
        line.append(f" {key} ", style=Style(color=theme.BLACK, bgcolor=theme.DIM, bold=True))
        line.append(f" {action}", style=Style(color=theme.DARK))
    return Align.left(line)


def render_symbol_tabs(state: BotState) -> RenderableType:
    """Row of configured symbols with the focused one highlighted."""
    line = Text()
    for index, symbol in enumerate(state.symbols):
        if index:
            line.append("  ")
        focused = symbol == state.focus_symbol
        has_position = state.symbol_state(symbol).position is not None
        marker = "◆" if has_position else "◇"
        style = Style(color=theme.BLACK, bgcolor=theme.NEON, bold=True) if focused else Style(color=theme.DIM)
        line.append(f" {marker} {symbol} ", style=style)
    return Align.left(line)


def render_startup_banner() -> RenderableType:
    """Splash shown while the engine warms up."""
    art = Text(
        "\n".join(
            [
                "██████╗ ███████╗██╗   ██╗ █████╗ ",
                "██╔══██╗██╔════╝╚██╗ ██╔╝██╔══██╗",
                "██████╔╝█████╗   ╚████╔╝ ███████║",
                "██╔══██╗██╔══╝    ╚██╔╝  ██╔══██║",
                "██║  ██║███████╗   ██║   ██║  ██║",
                "╚═╝  ╚═╝╚══════╝   ╚═╝   ╚═╝  ╚═╝",
                "",
                "15m FAIR VALUE GAP ENGINE",
            ]
        ),
        style=Style(color=theme.NEON, bold=True),
    )
    return Align.center(Group(art, Text("\n  initialising...", style=Style(color=theme.DIM))), vertical="middle")


__all__ = [
    "framed",
    "render_chart",
    "render_controls",
    "render_depth",
    "render_gaps",
    "render_header",
    "render_history",
    "render_logs",
    "render_metrics",
    "render_orders",
    "render_positions",
    "render_startup_banner",
    "render_status",
    "render_symbol_tabs",
    "render_tape",
]
