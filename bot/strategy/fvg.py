"""Fair Value Gap detection and signal generation (ICT / SMC style).

What a Fair Value Gap is
------------------------
A Fair Value Gap is a three-bar imbalance: price moved so quickly in one
direction that bar ``i`` never traded in the same range as bar ``i-2``. The
untraded band between them is the gap.

For bars ``i-2``, ``i-1``, ``i`` (``i`` being the most recent *closed* bar)::

    Bullish FVG   low[i] > high[i-2]      zone = [high[i-2] .. low[i]]
    Bearish FVG   high[i] < low[i-2]      zone = [high[i] .. low[i-2]]

The middle bar ``i-1`` is the displacement candle that did the work; it is not
part of the zone itself.

How the bot trades it
---------------------
1. Detect gaps on **closed candles only** — an in-progress bar can un-print a
   gap, so trading it is trading noise.
2. Filter for significance: the gap must be at least ``min_fvg_atr_mult`` ATRs
   tall and a minimum fraction of price, optionally backed by a three-candle
   run, a displacement body and/or a liquidity sweep.
3. Wait for price to **retrace back into** the zone (a "retest"). A bullish gap
   is entered from above as price falls into it; a bearish gap from below.
4. Enter only in the direction of the higher-level bias.

Zone geometry, once, so the rest of the module can stop thinking about it:
``top`` is always the higher price and ``bottom`` the lower. The **near edge**
is the one price reaches first when retracing (top for bullish, bottom for
bearish); the **far edge** is the deep side, and is where the stop lives.
"""

from typing import Dict, List, Optional, Sequence, Set, Tuple

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from bot.config import StrategyConfig
from bot.strategy.indicators import (
    Bias,
    Candle,
    atr_series,
    compute_bias,
    swept_liquidity_above,
    swept_liquidity_below,
)
from bot.utils.numbers import ZERO

logger = logging.getLogger("bot.strategy.fvg")


class FVGType(str, Enum):
    """Direction of the imbalance."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"

    @property
    def is_bullish(self) -> bool:
        """Whether this gap is traded from the long side."""
        return self is FVGType.BULLISH


class FVGState(str, Enum):
    """Lifecycle of a tracked gap.

    ``FRESH``      never touched since it printed — tradeable.
    ``TESTED``     price has entered the zone at least once.
    ``MITIGATED``  the imbalance has been filled; no longer tradeable.
    ``EXPIRED``    too old to be relevant.
    """

    FRESH = "FRESH"
    TESTED = "TESTED"
    MITIGATED = "MITIGATED"
    EXPIRED = "EXPIRED"

    @property
    def is_tradeable(self) -> bool:
        """Whether a signal may still be generated from this gap."""
        return self in (FVGState.FRESH, FVGState.TESTED)


class SignalSide(str, Enum):
    """Direction of a trade signal."""

    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def is_buy(self) -> bool:
        """Whether entering this side means buying."""
        return self is SignalSide.LONG

    @property
    def sign(self) -> Decimal:
        """``+1`` for long, ``-1`` for short — handy for PnL arithmetic."""
        return Decimal(1) if self is SignalSide.LONG else Decimal(-1)

    @property
    def opposite(self) -> "SignalSide":
        """The other side."""
        return SignalSide.SHORT if self is SignalSide.LONG else SignalSide.LONG


@dataclass
class FairValueGap:
    """A detected imbalance and everything the bot knows about it."""

    symbol: str
    kind: FVGType
    #: Bar index of the third candle, within the candle window it was found in.
    index: int
    #: Open time (seconds) of the third candle — stable across recomputation.
    timestamp: int
    #: Upper edge of the untraded band.
    top: Decimal
    #: Lower edge of the untraded band.
    bottom: Decimal
    #: ATR at formation, used for the gap-size filter and stop buffer.
    atr: Decimal
    #: Quality flags recorded at detection time.
    three_candle_run: bool = False
    displacement: bool = False
    liquidity_sweep: bool = False
    state: FVGState = FVGState.FRESH
    #: Bar timestamp at which price first entered the zone.
    tested_at: Optional[int] = None
    #: Bar timestamp at which the gap was filled.
    mitigated_at: Optional[int] = None
    #: Bars elapsed since formation.
    age_bars: int = 0

    @property
    def key(self) -> Tuple[str, int, str]:
        """Stable identity across recomputation of the gap list."""
        return (self.symbol, self.timestamp, self.kind.value)

    @property
    def height(self) -> Decimal:
        """Vertical size of the gap."""
        return self.top - self.bottom

    @property
    def mid(self) -> Decimal:
        """Midpoint of the zone."""
        return (self.top + self.bottom) / Decimal(2)

    @property
    def near_edge(self) -> Decimal:
        """Edge price reaches first when retracing into the zone."""
        return self.top if self.kind.is_bullish else self.bottom

    @property
    def far_edge(self) -> Decimal:
        """Deep edge of the zone; the stop sits beyond this."""
        return self.bottom if self.kind.is_bullish else self.top

    @property
    def side(self) -> SignalSide:
        """Trade direction implied by this gap."""
        return SignalSide.LONG if self.kind.is_bullish else SignalSide.SHORT

    @property
    def quality(self) -> int:
        """Count of satisfied quality filters (0-3), for ranking candidates."""
        return sum((self.three_candle_run, self.displacement, self.liquidity_sweep))

    def contains(self, price: Decimal) -> bool:
        """Whether ``price`` is inside the zone, inclusive of both edges."""
        return self.bottom <= price <= self.top

    def entry_price(self, zone_ratio: Decimal) -> Decimal:
        """Limit price inside the zone.

        Args:
            zone_ratio: ``0`` sits at the far edge (deepest fill, best price,
                least likely to be reached), ``1`` at the near edge (shallowest,
                most likely to fill), ``0.5`` at the midpoint.

        Returns:
            The price to work the entry at.
        """
        span = self.height * zone_ratio
        if self.kind.is_bullish:
            return self.bottom + span
        return self.top - span

    def beyond_far_edge(self, price: Decimal) -> bool:
        """Whether price has run past the deep side, invalidating the setup."""
        if self.kind.is_bullish:
            return price < self.bottom
        return price > self.top


@dataclass
class Signal:
    """An entry candidate emitted by the strategy.

    The strategy decides *where* and *which way*; :mod:`bot.strategy.risk` turns
    this into a sized order with a stop and a target.
    """

    symbol: str
    side: SignalSide
    #: Live price at the moment the signal fired.
    price: Decimal
    #: Limit price the entry should be worked at (inside the gap).
    entry_price: Decimal
    #: The gap that produced this signal.
    fvg: FairValueGap
    #: ATR of the most recent closed bar.
    atr: Decimal
    #: Bias in force when the signal fired.
    bias: Bias
    #: Nearest opposing gap edge, used by ``tp_mode: opposing_fvg``.
    opposing_target: Optional[Decimal] = None
    #: Human-readable justification, shown in the UI and the log.
    reasons: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def is_buy(self) -> bool:
        """Whether entering this signal means buying."""
        return self.side.is_buy

    def describe(self) -> str:
        """One-line summary for logs and alerts."""
        return (
            f"{self.side.value} {self.symbol} @ {self.entry_price} "
            f"[{self.fvg.kind.value} FVG {self.fvg.bottom}-{self.fvg.top}] "
            f"({', '.join(self.reasons) or 'no filters'})"
        )


def detect_fvgs(
    candles: Sequence[Candle],
    config: StrategyConfig,
    symbol: str = "",
) -> List[FairValueGap]:
    """Scan closed candles for Fair Value Gaps.

    Only bars that are already closed should be passed in: an in-progress bar's
    high and low still move, so a gap found against it is not real yet.

    Args:
        candles: Closed bars in chronological order.
        config: Detection thresholds.
        symbol: Symbol recorded on each gap.

    Returns:
        Gaps in chronological order, newest last, capped at
        ``config.max_tracked_fvgs``.
    """
    gaps: List[FairValueGap] = []
    if len(candles) < 3:
        return gaps

    atr = atr_series(candles, config.atr_period)

    # i is the third candle of the pattern; the gap spans bars i-2 and i.
    for i in range(2, len(candles)):
        first, middle, last = candles[i - 2], candles[i - 1], candles[i]
        current_atr = atr[i]
        if current_atr is None or current_atr <= ZERO:
            continue

        if last.low > first.high:
            kind = FVGType.BULLISH
            bottom, top = first.high, last.low
        elif last.high < first.low:
            kind = FVGType.BEARISH
            bottom, top = last.high, first.low
        else:
            continue

        height = top - bottom

        # Significance filter 1: the gap must be meaningful versus volatility.
        if height < config.min_fvg_atr_mult * current_atr:
            continue
        # Significance filter 2: and versus the price level itself, so that a
        # quiet-tape ATR cannot let a dust gap through.
        if last.close > ZERO and height < last.close * config.min_fvg_price_pct:
            continue

        # A run of three same-direction candles marks a genuine impulse rather
        # than a gap left by a single spike.
        if kind.is_bullish:
            three_run = first.is_bullish and middle.is_bullish and last.is_bullish
            sweep = swept_liquidity_below(candles, i - 2, config.sweep_lookback) or swept_liquidity_below(
                candles, i - 1, config.sweep_lookback
            )
        else:
            three_run = first.is_bearish and middle.is_bearish and last.is_bearish
            sweep = swept_liquidity_above(candles, i - 2, config.sweep_lookback) or swept_liquidity_above(
                candles, i - 1, config.sweep_lookback
            )

        displacement = middle.body >= config.displacement_atr_mult * current_atr

        if config.require_three_candle_run and not three_run:
            continue
        if config.require_displacement and not displacement:
            continue
        if config.require_liquidity_sweep and not sweep:
            continue

        gaps.append(
            FairValueGap(
                symbol=symbol,
                kind=kind,
                index=i,
                timestamp=last.timestamp,
                top=top,
                bottom=bottom,
                atr=current_atr,
                three_candle_run=three_run,
                displacement=displacement,
                liquidity_sweep=sweep,
            )
        )

    if config.max_tracked_fvgs > 0:
        gaps = gaps[-config.max_tracked_fvgs :]
    return gaps


def update_gap_states(
    gaps: Sequence[FairValueGap],
    candles: Sequence[Candle],
    config: StrategyConfig,
) -> None:
    """Replay candles after each gap to set its lifecycle state, in place.

    State is derived purely from the candle history, so it survives restarts and
    WebSocket gaps: recomputing from the same candles always gives the same
    answer.

    A gap is *tested* the first time a later bar trades into the zone, and
    *mitigated* once the imbalance is filled — on first touch when
    ``mitigation_mode`` is ``touch``, or only after price trades clean through
    the far edge when it is ``full``.
    """
    last_index = len(candles) - 1
    for gap in gaps:
        gap.state = FVGState.FRESH
        gap.tested_at = None
        gap.mitigated_at = None
        gap.age_bars = max(0, last_index - gap.index)

        for candle in candles[gap.index + 1 :]:
            entered = candle.low <= gap.top if gap.kind.is_bullish else candle.high >= gap.bottom
            if entered and gap.tested_at is None:
                gap.tested_at = candle.timestamp
                gap.state = FVGState.TESTED

            filled = candle.low <= gap.bottom if gap.kind.is_bullish else candle.high >= gap.top
            if config.mitigation_mode == "touch":
                filled = entered
            if filled:
                gap.mitigated_at = candle.timestamp
                gap.state = FVGState.MITIGATED
                break

        if gap.state is not FVGState.MITIGATED and gap.age_bars > config.max_fvg_age_bars:
            gap.state = FVGState.EXPIRED


class FVGStrategy:
    """Per-symbol 15m FVG strategy: tracks gaps and emits entry signals.

    Usage from the engine::

        strategy.update_candles(closed_candles)   # on every newly closed bar
        signal = strategy.evaluate(live_price)    # on every price tick

    ``update_candles`` is the expensive, bar-cadence half; ``evaluate`` is cheap
    enough to run on every tick.
    """

    def __init__(self, symbol: str, config: StrategyConfig) -> None:
        """Create a strategy instance bound to one symbol.

        Args:
            symbol: Market symbol, e.g. ``ETHRUSDPERP``.
            config: Detection and filter settings.
        """
        self.symbol = symbol
        self.config = config
        self.candles: List[Candle] = []
        self.gaps: List[FairValueGap] = []
        self.bias: Bias = Bias.NEUTRAL
        self.atr: Optional[Decimal] = None
        #: Gaps that already produced a signal, so each fires at most once.
        self._signalled: Set[Tuple[str, int, str]] = set()
        #: Last computed reason a tick produced no signal (surfaced in the UI).
        self.last_block_reason: str = "warming up"

    @property
    def is_ready(self) -> bool:
        """Whether enough history has loaded for ATR and bias to be valid."""
        return self.atr is not None and len(self.candles) > self.config.atr_period + 2

    @property
    def tradeable_gaps(self) -> List[FairValueGap]:
        """Gaps that could still produce a signal, newest first."""
        return [gap for gap in reversed(self.gaps) if gap.state.is_tradeable]

    def update_candles(self, candles: Sequence[Candle]) -> None:
        """Recompute gaps, states, ATR and bias from closed candles.

        Args:
            candles: Closed bars in chronological order. Only the most recent
                ``history_bars`` are retained.
        """
        window = list(candles[-self.config.history_bars :])
        if len(window) < 3:
            self.candles = window
            self.last_block_reason = "warming up"
            return

        self.candles = window
        atr = atr_series(window, self.config.atr_period)
        self.atr = atr[-1]
        self.bias = compute_bias(
            window,
            self.config.bias_mode,
            self.config.bias_ma_period,
            self.config.structure_lookback,
        )

        self.gaps = detect_fvgs(window, self.config, self.symbol)
        update_gap_states(self.gaps, window, self.config)

        # Forget signalled markers for gaps that have aged out of the window, so
        # the set cannot grow without bound over a long session.
        live_keys = {gap.key for gap in self.gaps}
        self._signalled &= live_keys

        logger.debug(
            "%s: %d candles, ATR=%s, bias=%s, %d tradeable gap(s)",
            self.symbol,
            len(window),
            self.atr,
            self.bias.value,
            len(self.tradeable_gaps),
        )

    def evaluate(self, price: Decimal) -> Optional[Signal]:
        """Test the live price against tracked gaps and emit a signal if one fires.

        Args:
            price: Current market price.

        Returns:
            The best qualifying signal, or ``None``. The reason for a ``None``
            is recorded on :attr:`last_block_reason` for the UI.
        """
        if not self.is_ready or self.atr is None:
            self.last_block_reason = "warming up"
            return None
        if price <= ZERO:
            self.last_block_reason = "no price"
            return None

        candidates: List[Signal] = []
        blocked: Optional[str] = None

        for gap in self.tradeable_gaps:
            if gap.key in self._signalled:
                continue
            if not gap.contains(price):
                continue

            # Price has already run through the deep side: the imbalance is
            # being filled rather than respected, so stand aside.
            if self.config.reject_if_beyond_far_edge and gap.beyond_far_edge(price):
                blocked = "price beyond far edge"
                continue

            side = gap.side
            if side is SignalSide.LONG and not self.bias.allows_long():
                blocked = f"bias {self.bias.value} blocks longs"
                continue
            if side is SignalSide.SHORT and not self.bias.allows_short():
                blocked = f"bias {self.bias.value} blocks shorts"
                continue

            reasons = [f"{gap.kind.value} FVG retest"]
            if gap.three_candle_run:
                reasons.append("3-candle run")
            if gap.displacement:
                reasons.append("displacement")
            if gap.liquidity_sweep:
                reasons.append("liquidity sweep")
            if self.bias is not Bias.NEUTRAL:
                reasons.append(f"{self.bias.value.lower()} bias")

            candidates.append(
                Signal(
                    symbol=self.symbol,
                    side=side,
                    price=price,
                    entry_price=gap.entry_price(self.config.entry_zone_ratio),
                    fvg=gap,
                    atr=self.atr,
                    bias=self.bias,
                    opposing_target=self.opposing_target(side, price),
                    reasons=reasons,
                )
            )

        if not candidates:
            self.last_block_reason = blocked or "no gap retest"
            return None

        # Prefer the highest-quality setup; break ties on the freshest gap.
        candidates.sort(key=lambda signal: (signal.fvg.quality, signal.fvg.timestamp), reverse=True)
        best = candidates[0]
        self._signalled.add(best.fvg.key)
        self.last_block_reason = "signal fired"
        return best

    def opposing_target(self, side: SignalSide, price: Decimal) -> Optional[Decimal]:
        """Nearest opposing gap edge in the trade's favour, if any.

        Used by ``risk.tp_mode: opposing_fvg`` — an unfilled gap in the other
        direction is where price is likely to be drawn next.

        Args:
            side: Direction of the intended trade.
            price: Current price.

        Returns:
            The near edge of the closest opposing gap beyond ``price``, or
            ``None`` when there is none.
        """
        wanted = FVGType.BEARISH if side is SignalSide.LONG else FVGType.BULLISH
        levels = [
            gap.near_edge
            for gap in self.gaps
            if gap.kind is wanted
            and gap.state.is_tradeable
            and (gap.near_edge > price if side is SignalSide.LONG else gap.near_edge < price)
        ]
        if not levels:
            return None
        return min(levels) if side is SignalSide.LONG else max(levels)

    def forget_signal(self, gap_key: Tuple[str, int, str]) -> None:
        """Allow a gap to signal again (used when an entry is aborted)."""
        self._signalled.discard(gap_key)

    def snapshot(self) -> Dict[str, object]:
        """Small dictionary of strategy state for the UI header."""
        return {
            "symbol": self.symbol,
            "bias": self.bias.value,
            "atr": self.atr,
            "candles": len(self.candles),
            "gaps": len(self.gaps),
            "tradeable": len(self.tradeable_gaps),
            "reason": self.last_block_reason,
        }


__all__ = [
    "FVGState",
    "FVGStrategy",
    "FVGType",
    "FairValueGap",
    "Signal",
    "SignalSide",
    "detect_fvgs",
    "update_gap_states",
]
