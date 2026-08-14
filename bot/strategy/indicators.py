"""Technical indicators used by the FVG strategy.

Everything here is pure and decimal-exact: given the same candles you get the
same numbers, which is what makes the backtester and the live engine agree.
"""

from typing import List, Optional, Sequence

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from bot.utils.numbers import ZERO, to_decimal


@dataclass(frozen=True)
class Candle:
    """A single OHLC bar.

    Attributes:
        timestamp: Bar open time in whole seconds (UTC), as returned by
            ``/v2/candleHistory``.
        open: Open price.
        high: High price.
        low: Low price.
        close: Close price.
    """

    timestamp: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @classmethod
    def create(cls, timestamp: int, open_: object, high: object, low: object, close: object) -> "Candle":
        """Build a candle from raw API strings."""
        return cls(
            timestamp=int(timestamp),
            open=to_decimal(open_),  # type: ignore[arg-type]
            high=to_decimal(high),  # type: ignore[arg-type]
            low=to_decimal(low),  # type: ignore[arg-type]
            close=to_decimal(close),  # type: ignore[arg-type]
        )

    @property
    def is_bullish(self) -> bool:
        """Whether the bar closed at or above its open."""
        return self.close >= self.open

    @property
    def is_bearish(self) -> bool:
        """Whether the bar closed below its open."""
        return self.close < self.open

    @property
    def body(self) -> Decimal:
        """Absolute size of the real body."""
        return abs(self.close - self.open)

    @property
    def range(self) -> Decimal:
        """High-to-low range."""
        return self.high - self.low

    @property
    def midpoint(self) -> Decimal:
        """Midpoint of the bar's range."""
        return (self.high + self.low) / Decimal(2)


class Bias(str, Enum):
    """Higher-level directional bias used to filter signals."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"

    def allows_long(self) -> bool:
        """Whether a long entry is permitted under this bias."""
        return self is not Bias.BEARISH

    def allows_short(self) -> bool:
        """Whether a short entry is permitted under this bias."""
        return self is not Bias.BULLISH


def true_range(current: Candle, previous: Optional[Candle]) -> Decimal:
    """Wilder's true range for one bar.

    Args:
        current: The bar being measured.
        previous: The bar before it, or ``None`` for the first bar.

    Returns:
        ``max(high-low, |high-prev_close|, |low-prev_close|)``.
    """
    if previous is None:
        return current.high - current.low
    return max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


def atr_series(candles: Sequence[Candle], period: int) -> List[Optional[Decimal]]:
    """Wilder-smoothed ATR aligned to ``candles``.

    The first ``period`` entries are ``None`` because there is not yet enough
    history; index ``period`` holds the simple average of the first ``period+1``
    true ranges' worth of data, and later entries use Wilder smoothing.

    Args:
        candles: Bars in chronological order.
        period: Averaging period (typically 14).

    Returns:
        A list the same length as ``candles``.
    """
    if period < 1:
        raise ValueError("ATR period must be >= 1")

    result: List[Optional[Decimal]] = [None] * len(candles)
    if len(candles) <= period:
        return result

    ranges = [true_range(candle, candles[i - 1] if i > 0 else None) for i, candle in enumerate(candles)]

    # Seed with the simple mean of the first `period` true ranges (index 0 is a
    # bare high-low, which is the standard Wilder seeding convention).
    seed = sum(ranges[1 : period + 1], ZERO) / Decimal(period)
    result[period] = seed
    previous = seed
    divisor = Decimal(period)
    for i in range(period + 1, len(candles)):
        previous = ((previous * (divisor - Decimal(1))) + ranges[i]) / divisor
        result[i] = previous
    return result


def latest_atr(candles: Sequence[Candle], period: int) -> Optional[Decimal]:
    """ATR of the most recent bar, or ``None`` when history is too short."""
    series = atr_series(candles, period)
    return series[-1] if series else None


def ema_series(values: Sequence[Decimal], period: int) -> List[Optional[Decimal]]:
    """Exponential moving average aligned to ``values``.

    Args:
        values: Input series (usually closes) in chronological order.
        period: EMA period.

    Returns:
        A list the same length as ``values``; entries before the seed are ``None``.
    """
    if period < 1:
        raise ValueError("EMA period must be >= 1")

    result: List[Optional[Decimal]] = [None] * len(values)
    if len(values) < period:
        return result

    multiplier = Decimal(2) / Decimal(period + 1)
    seed = sum(values[:period], ZERO) / Decimal(period)
    result[period - 1] = seed
    previous = seed
    for i in range(period, len(values)):
        previous = ((values[i] - previous) * multiplier) + previous
        result[i] = previous
    return result


def sma(values: Sequence[Decimal], period: int) -> Optional[Decimal]:
    """Simple moving average of the last ``period`` values."""
    if period < 1 or len(values) < period:
        return None
    return sum(values[-period:], ZERO) / Decimal(period)


def swing_high_indices(candles: Sequence[Candle], strength: int = 2) -> List[int]:
    """Indices of fractal swing highs.

    A swing high is a bar whose high is strictly greater than the highs of the
    ``strength`` bars on either side.
    """
    return [
        i
        for i in range(strength, len(candles) - strength)
        if all(candles[i].high > candles[j].high for j in range(i - strength, i + strength + 1) if j != i)
    ]


def swing_low_indices(candles: Sequence[Candle], strength: int = 2) -> List[int]:
    """Indices of fractal swing lows (mirror of :func:`swing_high_indices`)."""
    return [
        i
        for i in range(strength, len(candles) - strength)
        if all(candles[i].low < candles[j].low for j in range(i - strength, i + strength + 1) if j != i)
    ]


def structure_bias(candles: Sequence[Candle], lookback: int = 20, strength: int = 2) -> Bias:
    """Classify market structure from the last two swing highs and lows.

    Higher highs *and* higher lows read bullish, lower highs *and* lower lows
    read bearish, and anything else is neutral (i.e. no directional filter).

    Args:
        candles: Bars in chronological order.
        lookback: How many recent bars to consider.
        strength: Fractal strength for swing detection.

    Returns:
        The structural bias.
    """
    window = list(candles[-lookback:]) if lookback > 0 else list(candles)
    if len(window) < strength * 2 + 3:
        return Bias.NEUTRAL

    highs = swing_high_indices(window, strength)
    lows = swing_low_indices(window, strength)
    if len(highs) < 2 or len(lows) < 2:
        return Bias.NEUTRAL

    higher_high = window[highs[-1]].high > window[highs[-2]].high
    higher_low = window[lows[-1]].low > window[lows[-2]].low
    lower_high = window[highs[-1]].high < window[highs[-2]].high
    lower_low = window[lows[-1]].low < window[lows[-2]].low

    if higher_high and higher_low:
        return Bias.BULLISH
    if lower_high and lower_low:
        return Bias.BEARISH
    return Bias.NEUTRAL


def ma_bias(candles: Sequence[Candle], period: int = 50) -> Bias:
    """Classify bias from an EMA: price on the right side *and* the EMA rising.

    Requiring both conditions keeps the bot out of the chop where price
    oscillates around a flat average.
    """
    closes = [candle.close for candle in candles]
    series = ema_series(closes, period)
    current = series[-1] if series else None
    if current is None:
        return Bias.NEUTRAL

    # Compare against the EMA a few bars back to measure slope.
    slope_offset = max(1, period // 10)
    prior_index = len(series) - 1 - slope_offset
    prior = series[prior_index] if prior_index >= 0 else None
    if prior is None:
        return Bias.NEUTRAL

    price = closes[-1]
    if price > current and current > prior:
        return Bias.BULLISH
    if price < current and current < prior:
        return Bias.BEARISH
    return Bias.NEUTRAL


def compute_bias(candles: Sequence[Candle], mode: str, ma_period: int, structure_lookback: int) -> Bias:
    """Dispatch to the configured bias filter.

    Args:
        candles: Bars in chronological order.
        mode: ``none``, ``ma`` or ``structure``.
        ma_period: EMA period for ``ma`` mode.
        structure_lookback: Window for ``structure`` mode.

    Returns:
        The bias; ``NEUTRAL`` means "no filter", which permits both directions.
    """
    if mode == "none":
        return Bias.NEUTRAL
    if mode == "ma":
        return ma_bias(candles, ma_period)
    if mode == "structure":
        return structure_bias(candles, structure_lookback)
    raise ValueError(f"Unknown bias mode: {mode}")


def swept_liquidity_below(candles: Sequence[Candle], index: int, lookback: int) -> bool:
    """Whether bar ``index`` swept a recent low and closed back above it.

    This is the classic stop-run that often precedes a bullish displacement:
    price pokes beneath prior lows to collect resting sell stops, then reverses.

    Args:
        candles: Bars in chronological order.
        index: Bar to test.
        lookback: How many prior bars form the liquidity pool.

    Returns:
        ``True`` when the bar traded below the prior low and closed back above it.
    """
    start = index - lookback
    if start < 0 or index >= len(candles):
        return False
    prior_low = min(candle.low for candle in candles[start:index])
    bar = candles[index]
    return bar.low < prior_low <= bar.close


def swept_liquidity_above(candles: Sequence[Candle], index: int, lookback: int) -> bool:
    """Mirror of :func:`swept_liquidity_below` for bearish setups."""
    start = index - lookback
    if start < 0 or index >= len(candles):
        return False
    prior_high = max(candle.high for candle in candles[start:index])
    bar = candles[index]
    return bar.high > prior_high >= bar.close


__all__ = [
    "Bias",
    "Candle",
    "atr_series",
    "compute_bias",
    "ema_series",
    "latest_atr",
    "ma_bias",
    "sma",
    "structure_bias",
    "swept_liquidity_above",
    "swept_liquidity_below",
    "swing_high_indices",
    "swing_low_indices",
    "true_range",
]
