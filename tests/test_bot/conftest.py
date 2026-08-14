"""Shared fixtures and candle builders for the FVG bot unit tests.

These tests are pure and offline: no network, no credentials, no event loop.
"""

from typing import List, Optional, Sequence

from decimal import Decimal

import pytest

from bot.config import RiskConfig, StrategyConfig
from bot.exchange.base import MarketMeta
from bot.strategy.indicators import Candle

#: 15m bars, so timestamps advance by 900 seconds.
BAR_SECONDS = 900
FIRST_TIMESTAMP = 1_700_000_000


def make_candle(
    index: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    first_timestamp: int = FIRST_TIMESTAMP,
) -> Candle:
    """Build one candle at bar ``index``."""
    return Candle(
        timestamp=first_timestamp + index * BAR_SECONDS,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


def make_series(rows: Sequence[Sequence[str]], first_timestamp: int = FIRST_TIMESTAMP) -> List[Candle]:
    """Build a candle series from ``(open, high, low, close)`` rows."""
    return [make_candle(i, *row, first_timestamp=first_timestamp) for i, row in enumerate(rows)]


def flat_series(
    count: int,
    price: str = "1000",
    spread: str = "2",
    first_timestamp: int = FIRST_TIMESTAMP,
) -> List[Candle]:
    """Build a quiet, gapless series used to warm up ATR without side effects.

    Each bar has the same body and a small symmetric range, so the ATR settles
    at ``2 * spread`` and no three-bar imbalance can form.
    """
    base = Decimal(price)
    width = Decimal(spread)
    return [
        Candle(
            timestamp=first_timestamp + i * BAR_SECONDS,
            open=base,
            high=base + width,
            low=base - width,
            close=base,
        )
        for i in range(count)
    ]


def append_bullish_gap(
    candles: List[Candle],
    gap_size: str = "20",
    body: str = "30",
) -> List[Candle]:
    """Append three rising bars that leave a bullish FVG.

    The gap sits between ``high`` of the first appended bar and ``low`` of the
    third, and every bar closes above its open so the three-candle-run filter
    is satisfied.
    """
    last = candles[-1]
    base = last.close
    size = Decimal(gap_size)
    impulse = Decimal(body)
    start = len(candles)

    first = Candle(
        timestamp=last.timestamp + BAR_SECONDS,
        open=base,
        high=base + Decimal(2),
        low=base - Decimal(1),
        close=base + Decimal(2),
    )
    # The displacement bar does the work: a large body that clears the gap.
    middle = Candle(
        timestamp=first.timestamp + BAR_SECONDS,
        open=base + Decimal(2),
        high=first.high + size + impulse,
        low=base + Decimal(1),
        close=base + Decimal(2) + impulse,
    )
    third = Candle(
        timestamp=middle.timestamp + BAR_SECONDS,
        open=middle.close,
        high=middle.close + Decimal(5),
        low=first.high + size,
        close=middle.close + Decimal(4),
    )
    assert third.low > first.high, "test fixture must actually leave a gap"
    assert start >= 0
    return candles + [first, middle, third]


def append_bearish_gap(
    candles: List[Candle],
    gap_size: str = "20",
    body: str = "30",
) -> List[Candle]:
    """Append three falling bars that leave a bearish FVG (mirror of the above)."""
    last = candles[-1]
    base = last.close
    size = Decimal(gap_size)
    impulse = Decimal(body)

    first = Candle(
        timestamp=last.timestamp + BAR_SECONDS,
        open=base,
        high=base + Decimal(1),
        low=base - Decimal(2),
        close=base - Decimal(2),
    )
    middle = Candle(
        timestamp=first.timestamp + BAR_SECONDS,
        open=base - Decimal(2),
        high=base - Decimal(1),
        low=first.low - size - impulse,
        close=base - Decimal(2) - impulse,
    )
    third = Candle(
        timestamp=middle.timestamp + BAR_SECONDS,
        open=middle.close,
        high=first.low - size,
        low=middle.close - Decimal(5),
        close=middle.close - Decimal(4),
    )
    assert third.high < first.low, "test fixture must actually leave a gap"
    return candles + [first, middle, third]


def extend_flat(candles: List[Candle], count: int, price: Optional[Decimal] = None) -> List[Candle]:
    """Append ``count`` quiet bars around ``price`` (defaults to the last close)."""
    result = list(candles)
    base = price if price is not None else result[-1].close
    for _ in range(count):
        last = result[-1]
        result.append(
            Candle(
                timestamp=last.timestamp + BAR_SECONDS,
                open=base,
                high=base + Decimal(1),
                low=base - Decimal(1),
                close=base,
            )
        )
    return result


@pytest.fixture
def strategy_config() -> StrategyConfig:
    """Permissive detection settings so tests exercise one filter at a time."""
    return StrategyConfig(
        atr_period=14,
        history_bars=500,
        min_fvg_atr_mult=Decimal("0.25"),
        min_fvg_price_pct=Decimal("0"),
        require_three_candle_run=False,
        require_displacement=False,
        require_liquidity_sweep=False,
        bias_mode="none",
        entry_zone_ratio=Decimal("0.5"),
        max_fvg_age_bars=60,
        mitigation_mode="full",
    )


@pytest.fixture
def risk_config() -> RiskConfig:
    """Risk settings with round numbers so expected sizes are exact."""
    return RiskConfig(
        risk_per_trade_pct=Decimal("1"),
        max_risk_per_trade_pct=Decimal("2"),
        sl_atr_buffer_mult=Decimal("0.5"),
        min_stop_atr_mult=Decimal("0"),
        tp_mode="rr",
        risk_reward=Decimal("2"),
        min_risk_reward=Decimal("1"),
        max_open_positions=2,
        max_daily_loss_pct=Decimal("3"),
        max_daily_trades=10,
        consecutive_loss_limit=3,
        cooldown_minutes=60,
        max_notional_pct=Decimal("1000"),
    )


@pytest.fixture
def market_meta() -> MarketMeta:
    """A market with a 0.01 tick and 0.001 quantity step."""
    return MarketMeta(
        symbol="ETHRUSDPERP",
        market_id=1,
        tick_size=Decimal("0.01"),
        qty_step_size=Decimal("0.001"),
        min_order_qty=Decimal("0.001"),
        max_leverage=20,
    )
