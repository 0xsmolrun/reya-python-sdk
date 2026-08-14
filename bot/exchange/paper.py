"""Paper broker: simulates fills against live prices.

This is a full stand-in for :class:`~bot.exchange.reya_client.LiveBroker`, so the
strategy, risk and engine code paths exercised in paper mode are identical to
the live ones. Only the fills are invented.

Simulation rules, stated plainly so results are not read as more than they are:

* Entries fill immediately and completely at the current price plus a fixed
  slippage, which is optimistic — a real IOC can miss entirely.
* Stops and targets fill at their exact trigger price when a price tick crosses
  them, which ignores gap risk. Real stops slip; these do not.
* Fees are a flat taker rate on both sides. Funding is not simulated.
"""

from typing import Dict, List, Mapping, Optional

import logging
import time
from decimal import Decimal

from bot.config import ExecutionConfig, PaperConfig
from bot.exchange.base import (
    AccountSnapshot,
    BracketResult,
    Broker,
    BrokerOrder,
    BrokerPosition,
    Fill,
    MarketMeta,
    OrderResult,
)
from bot.strategy.fvg import SignalSide
from bot.strategy.risk import TradePlan
from bot.utils.numbers import BPS, ZERO, apply_bps

logger = logging.getLogger("bot.exchange.paper")


class PaperBroker(Broker):
    """In-memory exchange simulator driven by live price ticks."""

    name = "paper"

    def __init__(
        self,
        paper: PaperConfig,
        execution: ExecutionConfig,
        markets: Optional[Dict[str, MarketMeta]] = None,
    ) -> None:
        """Create a paper broker.

        Args:
            paper: Starting equity, fee and slippage assumptions.
            execution: Shared execution settings (used for parity with live).
            markets: Market constraints, normally copied from the live broker so
                that tick and step rounding match the real venue.
        """
        self.paper = paper
        self.execution = execution
        self._markets: Dict[str, MarketMeta] = dict(markets or {})
        self._balance: Decimal = paper.starting_equity
        self._positions: Dict[str, BrokerPosition] = {}
        self._brackets: Dict[str, Dict[str, Decimal]] = {}
        self._marks: Dict[str, Decimal] = {}
        self._fills: List[Fill] = []
        self._sequence = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """No connection to make; logs the simulated starting equity."""
        logger.info("Paper broker ready with simulated equity %s", self.paper.starting_equity)

    async def close(self) -> None:
        """Nothing to release."""

    def set_markets(self, markets: Mapping[str, MarketMeta]) -> None:
        """Adopt real market definitions so rounding matches the live venue."""
        self._markets = dict(markets)

    def market_meta(self, symbol: str) -> MarketMeta:
        """Return market constraints for ``symbol``."""
        return self._markets.get(symbol) or MarketMeta.fallback(symbol)

    # ------------------------------------------------------------------
    # Account state
    # ------------------------------------------------------------------
    async def fetch_account(self, marks: Optional[Mapping[str, Decimal]] = None) -> AccountSnapshot:
        """Return the simulated account, marked at the latest prices."""
        price_map = dict(self._marks)
        price_map.update(marks or {})
        unrealized = sum(
            (position.unrealized_pnl(price_map.get(symbol)) for symbol, position in self._positions.items()),
            ZERO,
        )
        return AccountSnapshot(
            balance=self._balance,
            equity=self._balance + unrealized,
            positions=dict(self._positions),
            orders=self.open_orders(),
            updated_at=time.time(),
        )

    def open_orders(self) -> List[BrokerOrder]:
        """Synthesise :class:`BrokerOrder` rows for the simulated brackets."""
        orders: List[BrokerOrder] = []
        for symbol, bracket in self._brackets.items():
            position = self._positions.get(symbol)
            if position is None:
                continue
            closing_side = position.side.opposite
            for kind in ("SL", "TP"):
                price = bracket.get(kind)
                if price is None:
                    continue
                orders.append(
                    BrokerOrder(
                        order_id=f"paper-{symbol}-{kind}",
                        symbol=symbol,
                        side=closing_side,
                        qty=position.qty,
                        limit_price=None,
                        order_type=kind,
                        trigger_price=price,
                        reduce_only=True,
                    )
                )
        return orders

    # ------------------------------------------------------------------
    # Price-driven simulation
    # ------------------------------------------------------------------
    def on_price(self, symbol: str, price: Decimal) -> None:
        """Mark the book and fire any bracket the tick crossed.

        Stops are checked before targets: when a single tick would satisfy both
        (which a real tape resolves intrabar), assuming the loss is the
        conservative reading.
        """
        if price <= ZERO:
            return
        self._marks[symbol] = price

        position = self._positions.get(symbol)
        bracket = self._brackets.get(symbol)
        if position is None or not bracket:
            return

        stop = bracket.get("SL")
        target = bracket.get("TP")
        if position.side is SignalSide.LONG:
            if stop is not None and price <= stop:
                self._settle(symbol, stop, "stop loss")
                return
            if target is not None and price >= target:
                self._settle(symbol, target, "take profit")
        else:
            if stop is not None and price >= stop:
                self._settle(symbol, stop, "stop loss")
                return
            if target is not None and price <= target:
                self._settle(symbol, target, "take profit")

    def _settle(self, symbol: str, price: Decimal, reason: str) -> None:
        """Close a simulated position at ``price`` and book the PnL."""
        position = self._positions.pop(symbol, None)
        self._brackets.pop(symbol, None)
        if position is None:
            return

        gross = (price - position.avg_entry_price) * position.qty * position.side.sign
        fee = price * position.qty * self.paper.fee_bps / BPS
        self._balance += gross - fee
        self._record_fill(symbol, position.side.opposite, position.qty, price, fee)
        logger.info(
            "PAPER %s on %s @ %s -> PnL %s (balance %s)",
            reason,
            symbol,
            price,
            gross - fee,
            self._balance,
        )

    def _record_fill(self, symbol: str, side: SignalSide, qty: Decimal, price: Decimal, fee: Decimal) -> None:
        """Append a simulated fill for the engine to consume."""
        self._sequence += 1
        self._fills.append(
            Fill(
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                fee=fee,
                timestamp=int(time.time() * 1000),
                sequence_number=self._sequence,
            )
        )

    def drain_fills(self) -> List[Fill]:
        """Return and clear simulated fills since the last call."""
        fills, self._fills = self._fills, []
        return fills

    # ------------------------------------------------------------------
    # Order entry
    # ------------------------------------------------------------------
    async def submit_entry(self, plan: TradePlan, reference_price: Decimal) -> OrderResult:
        """Fill the entry immediately at the reference price plus slippage."""
        fill_price = apply_bps(reference_price, self.paper.slippage_bps, plan.is_buy)
        fee = fill_price * plan.qty * self.paper.fee_bps / BPS
        self._balance -= fee

        existing = self._positions.get(plan.symbol)
        if existing is not None and existing.side is plan.side:
            total_qty = existing.qty + plan.qty
            blended = ((existing.avg_entry_price * existing.qty) + (fill_price * plan.qty)) / total_qty
            existing.qty = total_qty
            existing.avg_entry_price = blended
        else:
            self._positions[plan.symbol] = BrokerPosition(
                symbol=plan.symbol,
                side=plan.side,
                qty=plan.qty,
                avg_entry_price=fill_price,
            )

        self._marks[plan.symbol] = reference_price
        self._record_fill(plan.symbol, plan.side, plan.qty, fill_price, fee)
        logger.info("PAPER entry %s %s %s @ %s (fee %s)", plan.side.value, plan.qty, plan.symbol, fill_price, fee)
        return OrderResult(
            ok=True,
            order_id=f"paper-entry-{self._sequence}",
            status="FILLED",
            filled_qty=plan.qty,
            avg_price=fill_price,
        )

    async def attach_brackets(
        self,
        symbol: str,
        position_side: SignalSide,
        stop_price: Decimal,
        take_profit_price: Decimal,
    ) -> BracketResult:
        """Record the stop and target that :meth:`on_price` will watch."""
        self._brackets[symbol] = {"SL": stop_price, "TP": take_profit_price}
        logger.info("PAPER brackets for %s: SL %s / TP %s", symbol, stop_price, take_profit_price)
        return BracketResult(
            stop=OrderResult(ok=True, order_id=f"paper-{symbol}-SL", status="OPEN"),
            take_profit=OrderResult(ok=True, order_id=f"paper-{symbol}-TP", status="OPEN"),
        )

    async def cancel_order(self, order: BrokerOrder) -> bool:
        """Drop one simulated bracket leg."""
        bracket = self._brackets.get(order.symbol)
        if bracket and order.order_type in bracket:
            bracket.pop(order.order_type, None)
            return True
        return False

    async def cancel_symbol_orders(self, symbol: str) -> int:
        """Drop both simulated bracket legs for ``symbol``."""
        bracket = self._brackets.pop(symbol, None)
        return len(bracket or {})

    async def close_position(self, position: BrokerPosition, reference_price: Decimal) -> OrderResult:
        """Flatten a simulated position at the reference price plus slippage."""
        fill_price = apply_bps(reference_price, self.paper.slippage_bps, not position.side.is_buy)
        self._settle(position.symbol, fill_price, "manual close")
        return OrderResult(
            ok=True,
            order_id=f"paper-close-{self._sequence}",
            status="FILLED",
            filled_qty=position.qty,
            avg_price=fill_price,
        )


__all__ = ["PaperBroker"]
