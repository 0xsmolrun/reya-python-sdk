"""ASCII candle chart with Fair Value Gap zones drawn behind the price.

Rendering is a two-pass affair: gaps are painted into the background first so
that the candles, the live price line and the active bracket levels all sit on
top of them and stay readable.
"""

from typing import List, Optional, Sequence, Tuple

from decimal import Decimal

from rich.style import Style
from rich.text import Text

from bot.strategy.fvg import FairValueGap, FVGType
from bot.strategy.indicators import Candle
from bot.ui import theme
from bot.utils.numbers import ZERO

#: Glyphs for the body, the wick and the forming bar.
BODY_UP = "█"
BODY_DOWN = "█"
WICK = "│"
DOJI = "─"

#: Background tints for gap zones, dark enough to read text over.
BULLISH_ZONE_BG = "#04220B"
BEARISH_ZONE_BG = "#2A0707"


class CandleChart:
    """Renders candles, gaps and levels into a grid of styled cells."""

    def __init__(self, width: int, height: int) -> None:
        """Create a chart canvas.

        Args:
            width: Columns available, one candle per column.
            height: Rows available for the price axis.
        """
        self.width = max(1, width)
        self.height = max(1, height)
        self._cells: List[List[Tuple[str, Style]]] = []

    def render(
        self,
        candles: Sequence[Candle],
        gaps: Sequence[FairValueGap] = (),
        price: Optional[Decimal] = None,
        levels: Sequence[Tuple[Decimal, str, str]] = (),
    ) -> Text:
        """Draw the chart and return it as a Rich renderable.

        Args:
            candles: Bars in chronological order; the last ``width`` are shown.
            gaps: Gaps to shade behind the price action.
            price: Live price, drawn as a dashed line across the chart.
            levels: ``(price, glyph, colour)`` markers such as entry/SL/TP.

        Returns:
            A :class:`~rich.text.Text` of exactly ``height`` lines.
        """
        window = list(candles[-self.width :])
        if not window:
            return Text("no candles yet", style=Style(color=theme.MUTED))

        low, high = self._bounds(window, gaps, price, levels)
        if high <= low:
            high = low + Decimal(1)

        self._reset()
        # gap.index refers to the strategy's full candle window, so translate it
        # into a chart column via the index of the leftmost visible candle.
        self._paint_gaps(gaps, low, high, len(window), len(candles) - len(window))
        self._paint_levels(levels, low, high)
        if price is not None:
            self._paint_price_line(price, low, high)
        self._paint_candles(window, low, high)

        return self._to_text(low, high)

    # ------------------------------------------------------------------
    # Scaling
    # ------------------------------------------------------------------
    def _bounds(
        self,
        candles: Sequence[Candle],
        gaps: Sequence[FairValueGap],
        price: Optional[Decimal],
        levels: Sequence[Tuple[Decimal, str, str]],
    ) -> Tuple[Decimal, Decimal]:
        """Price range to fit on screen, padded slightly for breathing room."""
        lows = [candle.low for candle in candles]
        highs = [candle.high for candle in candles]

        # Only include gaps that are near the visible action; a stale gap far
        # away would otherwise squash the candles into a couple of rows.
        span = max(highs) - min(lows)
        margin = span * Decimal(2) if span > ZERO else max(highs)
        for gap in gaps:
            if gap.bottom >= min(lows) - margin and gap.top <= max(highs) + margin:
                lows.append(gap.bottom)
                highs.append(gap.top)

        for level, _glyph, _colour in levels:
            if min(lows) - margin <= level <= max(highs) + margin:
                lows.append(level)
                highs.append(level)

        if price is not None:
            lows.append(price)
            highs.append(price)

        low, high = min(lows), max(highs)
        pad = (high - low) * Decimal("0.04")
        return low - pad, high + pad

    def _row_for(self, price: Decimal, low: Decimal, high: Decimal) -> int:
        """Screen row for ``price``; row 0 is the top of the chart."""
        if high <= low:
            return self.height - 1
        ratio = (price - low) / (high - low)
        row = self.height - 1 - int(ratio * Decimal(self.height - 1))
        return max(0, min(self.height - 1, row))

    def _reset(self) -> None:
        """Clear the canvas to blank cells."""
        blank = (" ", Style())
        self._cells = [[blank for _ in range(self.width)] for _ in range(self.height)]

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def _paint_gaps(
        self,
        gaps: Sequence[FairValueGap],
        low: Decimal,
        high: Decimal,
        candle_count: int,
        first_visible_index: int,
    ) -> None:
        """Shade each tradeable gap's rows from its bar to the right edge."""
        offset = self.width - candle_count
        for gap in gaps:
            if not gap.state.is_tradeable:
                continue
            top_row = self._row_for(gap.top, low, high)
            bottom_row = self._row_for(gap.bottom, low, high)
            background = BULLISH_ZONE_BG if gap.kind is FVGType.BULLISH else BEARISH_ZONE_BG
            style = Style(bgcolor=background)

            # The zone extends from the bar that printed it to "now"; gaps older
            # than the visible window are shaded from the left edge.
            start = max(0, min(self.width - 1, offset + gap.index - first_visible_index))
            for row in range(min(top_row, bottom_row), max(top_row, bottom_row) + 1):
                for column in range(start, self.width):
                    self._cells[row][column] = (" ", style)

    def _paint_levels(
        self,
        levels: Sequence[Tuple[Decimal, str, str]],
        low: Decimal,
        high: Decimal,
    ) -> None:
        """Draw horizontal marker lines for entry, stop and target."""
        for level, glyph, colour in levels:
            if not low <= level <= high:
                continue
            row = self._row_for(level, low, high)
            style = Style(color=colour)
            for column in range(self.width):
                background = self._cells[row][column][1].bgcolor
                self._cells[row][column] = (
                    glyph if column % 2 == 0 else " ",
                    style + Style(bgcolor=background) if background else style,
                )

    def _paint_price_line(self, price: Decimal, low: Decimal, high: Decimal) -> None:
        """Draw the live price as a faint dashed line."""
        row = self._row_for(price, low, high)
        style = Style(color=theme.DIM)
        for column in range(self.width):
            char, existing = self._cells[row][column]
            if char == " ":
                self._cells[row][column] = (
                    "·",
                    style + Style(bgcolor=existing.bgcolor) if existing.bgcolor else style,
                )

    def _paint_candles(self, candles: Sequence[Candle], low: Decimal, high: Decimal) -> None:
        """Draw wicks and bodies, one candle per column, right-aligned."""
        offset = self.width - len(candles)
        for index, candle in enumerate(candles):
            column = offset + index
            if column < 0 or column >= self.width:
                continue

            colour = theme.UP if candle.is_bullish else theme.DOWN
            high_row = self._row_for(candle.high, low, high)
            low_row = self._row_for(candle.low, low, high)
            open_row = self._row_for(candle.open, low, high)
            close_row = self._row_for(candle.close, low, high)
            body_top = min(open_row, close_row)
            body_bottom = max(open_row, close_row)

            for row in range(high_row, low_row + 1):
                background = self._cells[row][column][1].bgcolor
                style = Style(color=colour, bgcolor=background) if background else Style(color=colour)
                if body_top <= row <= body_bottom:
                    glyph = BODY_UP if candle.is_bullish else BODY_DOWN
                    if body_top == body_bottom and candle.body == ZERO:
                        glyph = DOJI
                    self._cells[row][column] = (glyph, style)
                else:
                    self._cells[row][column] = (WICK, style)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def _to_text(self, low: Decimal, high: Decimal) -> Text:
        """Flatten the canvas into a Rich ``Text`` with a price axis."""
        axis_width = 10
        result = Text()
        for row, cells in enumerate(self._cells):
            axis_price = high - (high - low) * Decimal(row) / Decimal(max(1, self.height - 1))
            # Label only the top, middle and bottom rows to avoid visual noise.
            if row in (0, self.height // 2, self.height - 1):
                axis = f"{axis_price:>{axis_width - 1}.2f} "
                result.append(axis, style=Style(color=theme.DARK))
            else:
                result.append(" " * axis_width)

            for char, style in cells:
                result.append(char, style=style)
            if row < self.height - 1:
                result.append("\n")
        return result


__all__ = ["CandleChart"]
