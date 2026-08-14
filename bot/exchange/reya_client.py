"""Live broker: a thin, opinionated wrapper around :class:`ReyaTradingClient`.

Order entry on Reya is a REST call (``POST /v2/createOrder``) that carries an
EIP-712 signature; the SDK's ``create_limit_order`` / ``create_trigger_order``
helpers handle the signing, nonce and deadline, so this module never touches the
private key directly.

Two exchange behaviours shape the code here:

* **Entries are marketable IOC limits.** Reya has no "market" order type for
  perps at the API level; an IOC limit priced a few basis points through the
  book is the equivalent, and the offset doubles as an explicit slippage cap.
* **Trigger orders are position-level.** ``create_trigger_order`` takes no
  quantity — an SL or TP applies to the whole position. So a bracket is exactly
  one SL plus one TP, and the bot cancels the survivor itself when one fires
  (the exchange does not link them as OCO).
"""

from typing import Dict, List, Mapping, Optional

import asyncio
import logging
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from sdk._version import SDK_VERSION
from sdk.open_api.api.market_data_api import MarketDataApi
from sdk.open_api.api.reference_data_api import ReferenceDataApi
from sdk.open_api.api_client import ApiClient
from sdk.open_api.configuration import Configuration
from sdk.open_api.exceptions import ApiException
from sdk.open_api.models.candle_history_data import CandleHistoryData
from sdk.open_api.models.market_summary import MarketSummary
from sdk.open_api.models.order_type import OrderType
from sdk.open_api.models.price import Price
from sdk.open_api.models.time_in_force import TimeInForce
from sdk.reya_rest_api import ReyaTradingClient
from sdk.reya_rest_api.models.orders import LimitOrderParameters, TriggerOrderParameters

from bot.config import ExchangeConfig, ExecutionConfig
from bot.exchange.base import (
    AccountSnapshot,
    BracketResult,
    Broker,
    BrokerOrder,
    BrokerPosition,
    ExchangeError,
    MarketMeta,
    OrderResult,
    order_from_api,
    position_from_api,
)
from bot.strategy.fvg import SignalSide
from bot.strategy.risk import TradePlan
from bot.utils.numbers import ZERO, apply_bps, dec_to_str, round_price, to_decimal

logger = logging.getLogger("bot.exchange.reya")

#: Collateral asset whose balance counts as account equity.
COLLATERAL_ASSET = "RUSD"


def _describe_api_error(exc: Exception) -> str:
    """Render an SDK exception compactly enough for a log line and the UI."""
    if isinstance(exc, ApiException):
        body = str(exc.body or "").strip().replace("\n", " ")
        if len(body) > 240:
            body = body[:237] + "..."
        return f"HTTP {exc.status} {exc.reason}: {body}" if body else f"HTTP {exc.status} {exc.reason}"
    return f"{type(exc).__name__}: {exc}"


class MarketDataClient:
    """Unauthenticated reader for Reya's public market data endpoints.

    Candles, market definitions and prices need no signature, so this client is
    built straight on the generated API classes. That keeps paper and backtest
    modes runnable with no private key in the environment at all.
    """

    def __init__(self, api_url: str) -> None:
        """Create a market data client against ``api_url``."""
        self.api_url = api_url
        configuration = Configuration(host=api_url)
        self._api_client = ApiClient(configuration)
        self._api_client.set_default_header("X-SDK-Version", f"reya-python-sdk/{SDK_VERSION}")
        self._api_client.set_default_header("User-Agent", f"reya-fvg-bot/{SDK_VERSION}")
        self.markets = MarketDataApi(self._api_client)
        self.reference = ReferenceDataApi(self._api_client)
        self._market_meta: Dict[str, MarketMeta] = {}

    async def load_markets(self) -> Dict[str, MarketMeta]:
        """Fetch and cache perp market definitions.

        Raises:
            ExchangeError: If the definitions cannot be read.
        """
        try:
            definitions = await self.reference.get_market_definitions()
        except Exception as exc:  # noqa: BLE001 - surfaced with context
            raise ExchangeError(f"Could not load market definitions: {_describe_api_error(exc)}") from exc

        self._market_meta = {
            definition.symbol: MarketMeta(
                symbol=definition.symbol,
                market_id=definition.market_id,
                tick_size=to_decimal(definition.tick_size),
                qty_step_size=to_decimal(definition.qty_step_size),
                min_order_qty=to_decimal(definition.min_order_qty),
                max_leverage=int(definition.max_leverage),
                initial_margin_parameter=to_decimal(definition.initial_margin_parameter),
            )
            for definition in definitions
        }
        return self._market_meta

    @property
    def market_meta_map(self) -> Dict[str, MarketMeta]:
        """Cached market definitions keyed by symbol."""
        return self._market_meta

    def market_meta(self, symbol: str) -> MarketMeta:
        """Constraints for ``symbol``, or a conservative fallback."""
        return self._market_meta.get(symbol) or MarketMeta.fallback(symbol)

    async def get_candles(self, symbol: str, resolution: str, end_time: Optional[int] = None) -> CandleHistoryData:
        """Fetch one page of candle history."""
        return await self.markets.get_candles(symbol=symbol, resolution=resolution, end_time=end_time)

    async def get_price(self, symbol: str) -> Price:
        """Fetch the latest oracle and pool price for ``symbol``."""
        return await self.markets.get_price(symbol=symbol)

    async def get_market_summary(self, symbol: str) -> MarketSummary:
        """Fetch funding, open interest and 24h stats for ``symbol``."""
        return await self.markets.get_market_summary(symbol=symbol)

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if getattr(self._api_client, "rest_client", None):
            await self._api_client.rest_client.close()


class LiveBroker(Broker):
    """Signs and routes orders to Reya, and reads account state back."""

    name = "reya-live"

    def __init__(self, exchange: ExchangeConfig, execution: ExecutionConfig) -> None:
        """Create a live broker.

        Args:
            exchange: Endpoints and credentials.
            execution: Slippage, expiry and dry-run settings.
        """
        self.exchange = exchange
        self.execution = execution
        self.client: Optional[ReyaTradingClient] = None
        self._markets: Dict[str, MarketMeta] = {}
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """Create the SDK client and load market definitions."""
        config = self.exchange.to_trading_config()
        client = ReyaTradingClient(config=config)
        await client.start()
        self.client = client
        await self.load_markets()
        logger.info(
            "Connected to %s (%s) as %s, account %s",
            self.exchange.network_name,
            self.exchange.api_url,
            self.exchange.owner_wallet_address,
            self.exchange.account_id,
        )

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self.client is not None:
            await self.client.close()
            self.client = None

    def _require_client(self) -> ReyaTradingClient:
        """Return the client, or raise if :meth:`connect` has not run."""
        if self.client is None:
            raise ExchangeError("Broker is not connected; call connect() first")
        return self.client

    async def load_markets(self) -> Dict[str, MarketMeta]:
        """Fetch and cache perp market definitions (tick, step, limits)."""
        client = self._require_client()
        definitions = await client.reference.get_market_definitions()
        self._markets = {
            definition.symbol: MarketMeta(
                symbol=definition.symbol,
                market_id=definition.market_id,
                tick_size=to_decimal(definition.tick_size),
                qty_step_size=to_decimal(definition.qty_step_size),
                min_order_qty=to_decimal(definition.min_order_qty),
                max_leverage=int(definition.max_leverage),
                initial_margin_parameter=to_decimal(definition.initial_margin_parameter),
            )
            for definition in definitions
        }
        logger.info("Loaded %d perp market definitions", len(self._markets))
        return self._markets

    def market_meta(self, symbol: str) -> MarketMeta:
        """Return cached constraints for ``symbol``, or a safe fallback."""
        meta = self._markets.get(symbol)
        if meta is None:
            logger.warning("No market definition for %s; using conservative defaults", symbol)
            return MarketMeta.fallback(symbol)
        return meta

    @property
    def known_symbols(self) -> List[str]:
        """Symbols the venue exposes for perp trading."""
        return sorted(self._markets)

    @property
    def last_error(self) -> Optional[str]:
        """Most recent exchange error, for the UI status bar."""
        return self._last_error

    # ------------------------------------------------------------------
    # Account state
    # ------------------------------------------------------------------
    async def fetch_account(self, marks: Optional[Mapping[str, Decimal]] = None) -> AccountSnapshot:
        """Read balance, positions and resting orders in one pass.

        Args:
            marks: Latest prices per symbol, used to fold unrealised PnL into
                equity. Without them, equity equals the collateral balance.

        Returns:
            A snapshot of the account.

        Raises:
            ExchangeError: If the venue rejects any of the three reads.
        """
        client = self._require_client()
        try:
            balances, positions, orders = await asyncio.gather(
                client.get_account_balances(),
                client.get_positions(),
                client.get_open_orders(),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the engine
            self._last_error = _describe_api_error(exc)
            raise ExchangeError(f"Failed to read account state: {self._last_error}") from exc

        balance = ZERO
        for entry in balances:
            if self.exchange.account_id is not None and entry.account_id != self.exchange.account_id:
                continue
            if entry.asset.upper() != COLLATERAL_ASSET:
                continue
            balance += to_decimal(entry.real_balance)

        position_map: Dict[str, BrokerPosition] = {}
        for raw in positions:
            position = position_from_api(raw)
            if position.is_flat:
                continue
            if self.exchange.account_id is not None and position.account_id != self.exchange.account_id:
                continue
            position_map[position.symbol] = position

        order_list = [order_from_api(raw) for raw in orders]

        unrealized = ZERO
        if marks:
            for symbol, position in position_map.items():
                unrealized += position.unrealized_pnl(marks.get(symbol))

        self._last_error = None
        return AccountSnapshot(
            balance=balance,
            equity=balance + unrealized,
            positions=position_map,
            orders=order_list,
            updated_at=time.time(),
        )

    # ------------------------------------------------------------------
    # Order entry
    # ------------------------------------------------------------------
    def marketable_limit(self, symbol: str, is_buy: bool, reference_price: Decimal, slippage_bps: Decimal) -> Decimal:
        """Price an IOC limit so that it crosses the spread.

        The limit is pushed ``slippage_bps`` through the reference price in the
        aggressive direction, then rounded *away* from the mid so that tick
        rounding can never make it non-marketable.
        """
        raw = apply_bps(reference_price, slippage_bps, is_buy)
        tick = self.market_meta(symbol).tick_size
        return round_price(raw, tick, rounding=ROUND_UP if is_buy else ROUND_DOWN)

    async def submit_entry(self, plan: TradePlan, reference_price: Decimal) -> OrderResult:
        """Send the entry as a marketable IOC limit order.

        Args:
            plan: Risk-approved trade carrying side and quantity.
            reference_price: Live price to build the marketable limit from.

        Returns:
            The submission result; ``filled_qty`` is zero when the IOC did not
            trade, which the engine treats as a missed entry rather than an error.
        """
        limit_px = self.marketable_limit(plan.symbol, plan.is_buy, reference_price, self.execution.entry_slippage_bps)

        if self.execution.dry_run:
            logger.warning(
                "[DRY RUN] entry %s %s %s @ %s (not sent)",
                plan.side.value,
                plan.qty,
                plan.symbol,
                limit_px,
            )
            return OrderResult(ok=True, status="DRY_RUN", filled_qty=plan.qty, avg_price=limit_px)

        params = LimitOrderParameters(
            symbol=plan.symbol,
            is_buy=plan.is_buy,
            limit_px=dec_to_str(limit_px),
            qty=dec_to_str(plan.qty),
            time_in_force=TimeInForce.IOC,
            reduce_only=False,
            expires_after=int(time.time()) + self.execution.entry_expiry_s,
        )
        return await self._send_limit_order(params, fallback_price=limit_px)

    async def close_position(self, position: BrokerPosition, reference_price: Decimal) -> OrderResult:
        """Flatten a position with a reduce-only marketable IOC order."""
        is_buy = not position.side.is_buy  # closing trades the other way
        limit_px = self.marketable_limit(position.symbol, is_buy, reference_price, self.execution.exit_slippage_bps)

        if self.execution.dry_run:
            logger.warning("[DRY RUN] close %s %s (not sent)", position.qty, position.symbol)
            return OrderResult(ok=True, status="DRY_RUN", filled_qty=position.qty, avg_price=limit_px)

        params = LimitOrderParameters(
            symbol=position.symbol,
            is_buy=is_buy,
            limit_px=dec_to_str(limit_px),
            qty=dec_to_str(position.qty),
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
            expires_after=int(time.time()) + self.execution.entry_expiry_s,
        )
        return await self._send_limit_order(params, fallback_price=limit_px)

    async def _send_limit_order(self, params: LimitOrderParameters, fallback_price: Decimal) -> OrderResult:
        """Submit a limit order and normalise the response."""
        client = self._require_client()
        try:
            response = await client.create_limit_order(params)
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            error = _describe_api_error(exc)
            self._last_error = error
            logger.error("Order rejected (%s %s %s): %s", params.symbol, params.qty, params.limit_px, error)
            return OrderResult.failure(error)

        status = str(getattr(response.status, "value", response.status))
        filled = to_decimal(response.exec_qty or response.cum_qty or "0", default=ZERO)
        self._last_error = None
        logger.info(
            "Order %s %s %s @ %s -> %s (filled %s)",
            "BUY" if params.is_buy else "SELL",
            params.qty,
            params.symbol,
            params.limit_px,
            status,
            filled,
        )
        return OrderResult(
            ok=status not in ("REJECTED",),
            order_id=response.order_id,
            status=status,
            filled_qty=filled,
            avg_price=fallback_price if filled > ZERO else None,
        )

    # ------------------------------------------------------------------
    # Brackets
    # ------------------------------------------------------------------
    async def attach_brackets(
        self,
        symbol: str,
        position_side: SignalSide,
        stop_price: Decimal,
        take_profit_price: Decimal,
    ) -> BracketResult:
        """Attach a stop-loss and a take-profit to an open position.

        Both legs trade opposite the position, so a long is protected by sell-side
        triggers. They are position-level orders on Reya: no quantity is sent and
        each one closes the position when it fires.
        """
        closing_is_buy = not position_side.is_buy
        tick = self.market_meta(symbol).tick_size
        # Round each trigger away from the entry so rounding never tightens the
        # stop or shortens the target.
        if position_side is SignalSide.LONG:
            stop = round_price(stop_price, tick, rounding=ROUND_DOWN)
            target = round_price(take_profit_price, tick, rounding=ROUND_UP)
        else:
            stop = round_price(stop_price, tick, rounding=ROUND_UP)
            target = round_price(take_profit_price, tick, rounding=ROUND_DOWN)

        stop_result = await self._send_trigger_order(symbol, closing_is_buy, stop, OrderType.SL)
        tp_result = await self._send_trigger_order(symbol, closing_is_buy, target, OrderType.TP)
        return BracketResult(stop=stop_result, take_profit=tp_result)

    async def _send_trigger_order(
        self,
        symbol: str,
        is_buy: bool,
        trigger_px: Decimal,
        trigger_type: OrderType,
    ) -> OrderResult:
        """Submit one SL or TP trigger order and normalise the response."""
        if self.execution.dry_run:
            logger.warning("[DRY RUN] %s trigger %s @ %s (not sent)", trigger_type.value, symbol, trigger_px)
            return OrderResult(ok=True, status="DRY_RUN")

        client = self._require_client()
        try:
            response = await client.create_trigger_order(
                TriggerOrderParameters(
                    symbol=symbol,
                    is_buy=is_buy,
                    trigger_px=dec_to_str(trigger_px),
                    trigger_type=trigger_type,
                )
            )
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            error = _describe_api_error(exc)
            self._last_error = error
            logger.error("%s trigger rejected for %s @ %s: %s", trigger_type.value, symbol, trigger_px, error)
            return OrderResult.failure(error)

        status = str(getattr(response.status, "value", response.status))
        logger.info("%s trigger placed for %s @ %s (%s)", trigger_type.value, symbol, trigger_px, status)
        return OrderResult(ok=True, order_id=response.order_id, status=status)

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------
    async def cancel_order(self, order: BrokerOrder) -> bool:
        """Cancel a single resting order."""
        if self.execution.dry_run:
            logger.warning("[DRY RUN] cancel %s (not sent)", order.order_id)
            return True

        client = self._require_client()
        try:
            await client.cancel_order(order_id=order.order_id)
        except Exception as exc:  # noqa: BLE001 - cancellation is best-effort
            error = _describe_api_error(exc)
            self._last_error = error
            logger.warning("Cancel failed for %s: %s", order.order_id, error)
            return False
        logger.info("Cancelled order %s (%s %s)", order.order_id, order.order_type, order.symbol)
        return True

    async def cancel_symbol_orders(self, symbol: str) -> int:
        """Cancel every resting order for ``symbol``.

        ``mass_cancel`` is a spot-only endpoint, so perps are cancelled one order
        at a time from the current open-orders snapshot.
        """
        client = self._require_client()
        try:
            orders = await client.get_open_orders()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            self._last_error = _describe_api_error(exc)
            logger.warning("Could not list open orders for %s: %s", symbol, self._last_error)
            return 0

        targets = [order_from_api(raw) for raw in orders]
        results = await asyncio.gather(
            *(self.cancel_order(order) for order in targets if order.symbol == symbol),
            return_exceptions=True,
        )
        return sum(1 for result in results if result is True)


__all__ = ["COLLATERAL_ASSET", "LiveBroker", "MarketDataClient"]
