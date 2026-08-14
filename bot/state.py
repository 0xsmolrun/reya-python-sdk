"""Shared, read-only-for-the-UI view of what the bot is doing.

The engine owns every field here and mutates it from the event loop; the Matrix
UI polls it a few times a second. Keeping one plain-data structure between them
means the UI can never block or reorder trading work.
"""

from typing import Deque, Dict, List, Optional

import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from bot.exchange.base import BrokerOrder, BrokerPosition, MarketMeta
from bot.exchange.data_feed import DepthBook, FeedStatus
from bot.strategy.fvg import FairValueGap, Signal, SignalSide
from bot.strategy.indicators import Bias, Candle
from bot.strategy.risk import RiskState, TradePlan
from bot.utils.numbers import ZERO


@dataclass
class MarketTrade:
    """A print on the public tape."""

    symbol: str
    side: SignalSide
    qty: Decimal
    price: Decimal
    timestamp: int


@dataclass
class TradeRecord:
    """A completed round trip, used for the history panel and the statistics."""

    symbol: str
    side: SignalSide
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    #: Realised PnL net of the fees the bot knows about.
    pnl: Decimal
    #: PnL expressed in units of the trade's initial risk.
    r_multiple: Decimal
    opened_at: float
    closed_at: float
    #: ``take profit``, ``stop loss``, ``manual close`` or ``reconciled``.
    reason: str
    fees: Decimal = ZERO

    @property
    def is_win(self) -> bool:
        """Whether the round trip finished in profit."""
        return self.pnl > ZERO

    @property
    def duration_s(self) -> float:
        """How long the position was open, in seconds."""
        return max(0.0, self.closed_at - self.opened_at)


@dataclass
class OpenTrade:
    """A live position the bot itself opened, with the plan that created it."""

    plan: TradePlan
    entry_price: Decimal
    qty: Decimal
    opened_at: float
    stop_price: Decimal
    take_profit: Decimal
    #: Bracket orders currently believed to be resting.
    bracket_order_ids: List[str] = field(default_factory=list)
    fees: Decimal = ZERO

    @property
    def risk_amount(self) -> Decimal:
        """Currency at risk between the entry and the stop."""
        return abs(self.entry_price - self.stop_price) * self.qty

    def r_multiple(self, exit_price: Decimal) -> Decimal:
        """Result at ``exit_price`` expressed in units of initial risk."""
        risk = abs(self.entry_price - self.stop_price)
        if risk <= ZERO:
            return ZERO
        return (exit_price - self.entry_price) * self.plan.side.sign / risk


@dataclass
class SymbolState:
    """Everything known about one traded market."""

    symbol: str
    meta: Optional[MarketMeta] = None
    price: Optional[Decimal] = None
    previous_price: Optional[Decimal] = None
    oracle_price: Optional[Decimal] = None
    pool_price: Optional[Decimal] = None
    funding_rate: Optional[Decimal] = None
    open_interest: Optional[Decimal] = None
    volume_24h: Optional[Decimal] = None
    price_change_24h: Optional[Decimal] = None

    candles: List[Candle] = field(default_factory=list)
    forming_candle: Optional[Candle] = None
    gaps: List[FairValueGap] = field(default_factory=list)
    bias: Bias = Bias.NEUTRAL
    atr: Optional[Decimal] = None

    position: Optional[BrokerPosition] = None
    open_trade: Optional[OpenTrade] = None
    orders: List[BrokerOrder] = field(default_factory=list)

    depth: DepthBook = field(default_factory=lambda: DepthBook(symbol=""))
    tape: Deque[MarketTrade] = field(default_factory=lambda: deque(maxlen=60))

    last_signal: Optional[Signal] = None
    last_signal_at: float = 0.0
    status_reason: str = "warming up"
    last_candle_at: float = 0.0

    @property
    def tradeable_gaps(self) -> List[FairValueGap]:
        """Gaps that can still produce a signal, newest first."""
        return [gap for gap in self.gaps if gap.state.is_tradeable][::-1]

    @property
    def unrealized_pnl(self) -> Decimal:
        """Mark-to-market PnL on the open position."""
        if self.position is None:
            return ZERO
        return self.position.unrealized_pnl(self.price)

    @property
    def price_direction(self) -> int:
        """``1`` if the last tick was up, ``-1`` if down, ``0`` if unchanged."""
        if self.price is None or self.previous_price is None:
            return 0
        if self.price > self.previous_price:
            return 1
        if self.price < self.previous_price:
            return -1
        return 0

    def update_price(self, price: Decimal) -> None:
        """Record a new price, keeping the previous one for tick colouring."""
        if price <= ZERO:
            return
        self.previous_price = self.price
        self.price = price


@dataclass
class BotState:
    """Top-level state shared between the engine and the UI."""

    mode: str = "paper"
    broker_name: str = "paper"
    network: str = ""
    account_label: str = ""
    #: The strategy is actively looking for entries.
    running: bool = False
    #: Existing positions are still managed, but no new entries are taken.
    paused: bool = False
    shutting_down: bool = False

    symbols: Dict[str, SymbolState] = field(default_factory=dict)
    focus_symbol: str = ""

    balance: Decimal = ZERO
    equity: Decimal = ZERO
    session_start_equity: Decimal = ZERO
    session_realized_pnl: Decimal = ZERO

    trades: List[TradeRecord] = field(default_factory=list)
    risk: Optional[RiskState] = None
    feed: Optional[FeedStatus] = None

    started_at: float = field(default_factory=time.time)
    last_error: Optional[str] = None
    #: Timestamp of the most recent fill; the rain animation reacts to it.
    last_fill_at: float = 0.0
    #: Free-form banner shown in the status bar.
    notice: str = ""

    def symbol_state(self, symbol: str) -> SymbolState:
        """Return (creating if needed) the state for ``symbol``."""
        state = self.symbols.get(symbol)
        if state is None:
            state = SymbolState(symbol=symbol, depth=DepthBook(symbol=symbol))
            self.symbols[symbol] = state
        return state

    @property
    def focus(self) -> SymbolState:
        """State for the symbol the UI is currently showing."""
        if self.focus_symbol not in self.symbols and self.symbols:
            self.focus_symbol = next(iter(self.symbols))
        return self.symbol_state(self.focus_symbol or "UNKNOWN")

    @property
    def open_positions(self) -> Dict[str, BrokerPosition]:
        """Every symbol currently holding a position."""
        return {
            symbol: state.position
            for symbol, state in self.symbols.items()
            if state.position is not None and not state.position.is_flat
        }

    @property
    def total_unrealized(self) -> Decimal:
        """Sum of unrealised PnL across open positions."""
        return sum((state.unrealized_pnl for state in self.symbols.values()), ZERO)

    @property
    def session_pnl(self) -> Decimal:
        """Realised plus unrealised PnL since the bot started."""
        return self.session_realized_pnl + self.total_unrealized

    @property
    def session_return_pct(self) -> Decimal:
        """Session PnL as a percentage of starting equity."""
        if self.session_start_equity <= ZERO:
            return ZERO
        return self.session_pnl / self.session_start_equity * Decimal(100)

    @property
    def uptime_s(self) -> float:
        """Seconds since the engine started."""
        return time.time() - self.started_at

    @property
    def status_label(self) -> str:
        """Short run-state label for the status bar."""
        if self.shutting_down:
            return "STOPPING"
        if not self.running:
            return "IDLE"
        if self.paused:
            return "PAUSED"
        return "ARMED"


__all__ = ["BotState", "MarketTrade", "OpenTrade", "SymbolState", "TradeRecord"]
