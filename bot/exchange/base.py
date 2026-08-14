"""Broker abstraction shared by live and paper trading.

The engine only ever talks to a :class:`Broker`. ``LiveBroker`` signs and sends
orders through the Reya REST API; ``PaperBroker`` simulates fills against the
same live prices. Because both satisfy the same interface, the strategy and risk
code that runs in paper mode is byte-for-byte the code that runs with real
capital.
"""

from typing import TYPE_CHECKING, Dict, List, Mapping, Optional

import abc
from dataclasses import dataclass, field
from decimal import Decimal

from bot.strategy.fvg import SignalSide
from bot.utils.numbers import ZERO, to_decimal

if TYPE_CHECKING:  # avoids a cycle: risk imports MarketMeta from this module
    from bot.strategy.risk import TradePlan


class ExchangeError(RuntimeError):
    """Raised when an exchange call fails in a way the engine must handle."""


@dataclass(frozen=True)
class MarketMeta:
    """Trading constraints for one market, from ``/v2/marketDefinitions``."""

    symbol: str
    market_id: int
    tick_size: Decimal
    qty_step_size: Decimal
    min_order_qty: Decimal
    max_leverage: int
    initial_margin_parameter: Decimal = ZERO

    @property
    def price_places(self) -> int:
        """Decimal places implied by the tick size, for display."""
        exponent = self.tick_size.as_tuple().exponent
        return max(0, -int(exponent)) if isinstance(exponent, int) else 2

    @property
    def qty_places(self) -> int:
        """Decimal places implied by the quantity step, for display."""
        exponent = self.qty_step_size.as_tuple().exponent
        return max(0, -int(exponent)) if isinstance(exponent, int) else 4

    @classmethod
    def fallback(cls, symbol: str) -> "MarketMeta":
        """Conservative placeholder used before definitions have loaded."""
        return cls(
            symbol=symbol,
            market_id=-1,
            tick_size=Decimal("0.01"),
            qty_step_size=Decimal("0.001"),
            min_order_qty=Decimal("0.001"),
            max_leverage=10,
        )


@dataclass
class BrokerPosition:
    """An open position, normalised across live and paper brokers."""

    symbol: str
    side: SignalSide
    #: Absolute size; direction lives in :attr:`side`.
    qty: Decimal
    avg_entry_price: Decimal
    account_id: Optional[int] = None

    @property
    def is_flat(self) -> bool:
        """Whether the position carries no size."""
        return self.qty <= ZERO

    @property
    def signed_qty(self) -> Decimal:
        """Size with a sign: positive for long, negative for short."""
        return self.qty * self.side.sign

    def unrealized_pnl(self, mark_price: Optional[Decimal]) -> Decimal:
        """Mark-to-market PnL at ``mark_price`` (excluding funding and fees)."""
        if mark_price is None or self.is_flat:
            return ZERO
        return (mark_price - self.avg_entry_price) * self.qty * self.side.sign

    def notional(self, mark_price: Optional[Decimal]) -> Decimal:
        """Position value at ``mark_price``, falling back to entry price."""
        price = mark_price if mark_price is not None else self.avg_entry_price
        return self.qty * price


@dataclass
class BrokerOrder:
    """A resting order, normalised across live and paper brokers."""

    order_id: str
    symbol: str
    side: SignalSide
    qty: Optional[Decimal]
    limit_price: Optional[Decimal]
    #: ``LIMIT``, ``SL`` or ``TP``.
    order_type: str
    trigger_price: Optional[Decimal] = None
    status: str = "OPEN"
    reduce_only: bool = False
    created_at: int = 0

    @property
    def is_bracket(self) -> bool:
        """Whether this is a stop-loss or take-profit trigger order."""
        return self.order_type in ("SL", "TP")


@dataclass
class OrderResult:
    """Outcome of an order submission."""

    ok: bool
    order_id: Optional[str] = None
    status: str = ""
    filled_qty: Decimal = ZERO
    avg_price: Optional[Decimal] = None
    error: Optional[str] = None

    @property
    def filled(self) -> bool:
        """Whether any quantity was executed."""
        return self.ok and self.filled_qty > ZERO

    @classmethod
    def failure(cls, error: str) -> "OrderResult":
        """Build a failed result carrying ``error``."""
        return cls(ok=False, error=error)


@dataclass
class BracketResult:
    """Outcome of attaching stop-loss and take-profit orders."""

    stop: OrderResult
    take_profit: OrderResult

    @property
    def ok(self) -> bool:
        """Whether both legs were accepted."""
        return self.stop.ok and self.take_profit.ok

    @property
    def errors(self) -> List[str]:
        """Any errors reported by either leg."""
        return [result.error for result in (self.stop, self.take_profit) if result.error]


@dataclass
class Fill:
    """A single execution reported by the exchange or the simulator."""

    symbol: str
    side: SignalSide
    qty: Decimal
    price: Decimal
    fee: Decimal = ZERO
    timestamp: int = 0
    execution_type: str = "ORDER_MATCH"
    sequence_number: int = 0


@dataclass
class AccountSnapshot:
    """Point-in-time account state used for sizing and the UI header."""

    #: Free + used collateral, excluding unrealised PnL.
    balance: Decimal = ZERO
    #: ``balance`` plus unrealised PnL on open positions.
    equity: Decimal = ZERO
    positions: Dict[str, BrokerPosition] = field(default_factory=dict)
    orders: List[BrokerOrder] = field(default_factory=list)
    updated_at: float = 0.0


class Broker(abc.ABC):
    """Everything the trading engine needs from an execution venue."""

    #: Short label shown in the UI status bar.
    name: str = "broker"

    @abc.abstractmethod
    async def connect(self) -> None:
        """Open connections and load market definitions."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release connections."""

    @abc.abstractmethod
    def market_meta(self, symbol: str) -> MarketMeta:
        """Return trading constraints for ``symbol``."""

    @abc.abstractmethod
    async def fetch_account(self, marks: Optional[Mapping[str, Decimal]] = None) -> AccountSnapshot:
        """Read balance, positions and resting orders from the venue.

        Args:
            marks: Latest price per symbol, used to fold unrealised PnL into the
                reported equity.
        """

    @abc.abstractmethod
    async def submit_entry(self, plan: "TradePlan", reference_price: Decimal) -> OrderResult:
        """Submit the entry order for ``plan``.

        Args:
            plan: The risk-approved trade.
            reference_price: Live price used to price a marketable IOC limit.
        """

    @abc.abstractmethod
    async def attach_brackets(
        self,
        symbol: str,
        position_side: SignalSide,
        stop_price: Decimal,
        take_profit_price: Decimal,
    ) -> BracketResult:
        """Attach stop-loss and take-profit trigger orders to a position."""

    @abc.abstractmethod
    async def cancel_order(self, order: BrokerOrder) -> bool:
        """Cancel one resting order. Returns whether the venue accepted it."""

    @abc.abstractmethod
    async def cancel_symbol_orders(self, symbol: str) -> int:
        """Cancel every resting order for ``symbol``. Returns the count cancelled."""

    @abc.abstractmethod
    async def close_position(
        self,
        position: BrokerPosition,
        reference_price: Decimal,
    ) -> OrderResult:
        """Flatten ``position`` with a reduce-only marketable order."""

    def on_price(self, symbol: str, price: Decimal) -> None:
        """Feed a live price to the broker.

        Live brokers ignore this; the paper broker uses it to mark positions and
        to trigger simulated stop and target fills.
        """

    def drain_fills(self) -> List[Fill]:
        """Return and clear any fills the broker generated locally.

        Only the paper broker produces these; live fills arrive over WebSocket.
        """
        return []


def side_from_api(value: object) -> SignalSide:
    """Map the API's ``Side`` enum (``B``/``A``) onto :class:`SignalSide`."""
    raw = getattr(value, "value", value)
    return SignalSide.LONG if str(raw).upper() in ("B", "BUY", "LONG") else SignalSide.SHORT


def position_from_api(position: object) -> BrokerPosition:
    """Build a :class:`BrokerPosition` from the SDK's ``Position`` model."""
    qty = abs(to_decimal(getattr(position, "qty", "0")))
    return BrokerPosition(
        symbol=str(getattr(position, "symbol", "")),
        side=side_from_api(getattr(position, "side", "B")),
        qty=qty,
        avg_entry_price=to_decimal(getattr(position, "avg_entry_price", "0")),
        account_id=getattr(position, "account_id", None),
    )


def order_from_api(order: object) -> BrokerOrder:
    """Build a :class:`BrokerOrder` from the SDK's ``Order`` model."""
    qty_raw = getattr(order, "qty", None)
    trigger_raw = getattr(order, "trigger_px", None)
    order_type = getattr(order, "order_type", None)
    status = getattr(order, "status", None)
    return BrokerOrder(
        order_id=str(getattr(order, "order_id", "")),
        symbol=str(getattr(order, "symbol", "")),
        side=side_from_api(getattr(order, "side", "B")),
        qty=to_decimal(qty_raw) if qty_raw is not None else None,
        limit_price=to_decimal(getattr(order, "limit_px", "0")),
        order_type=str(getattr(order_type, "value", order_type or "LIMIT")),
        trigger_price=to_decimal(trigger_raw) if trigger_raw is not None else None,
        status=str(getattr(status, "value", status or "OPEN")),
        reduce_only=bool(getattr(order, "reduce_only", False)),
        created_at=int(getattr(order, "created_at", 0) or 0),
    )


__all__ = [
    "AccountSnapshot",
    "BracketResult",
    "Broker",
    "BrokerOrder",
    "BrokerPosition",
    "ExchangeError",
    "Fill",
    "MarketMeta",
    "OrderResult",
    "order_from_api",
    "position_from_api",
    "side_from_api",
]
