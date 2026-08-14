"""The trading engine: wiring, event loop and position lifecycle.

Responsibilities, in the order they happen:

1. **Start up** — load market definitions, warm up 15m candles, connect the
   WebSocket, reconcile account state over REST.
2. **React to prices** — feed each tick to the strategy, and when a Fair Value
   Gap retest fires, size it through the risk manager and send the entry.
3. **Manage positions** — attach SL/TP brackets, emulate OCO by cancelling the
   surviving leg, and book the round trip when a position goes flat.
4. **Stay honest** — re-read positions and orders over REST on a timer and after
   every reconnect, because a WebSocket that missed a message must never be the
   bot's only source of truth.

Every background task is supervised: if one raises, it is logged and restarted
with backoff rather than silently dying and leaving positions unmanaged.
"""

from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import asyncio
import contextlib
import logging
import time
from decimal import Decimal

from bot.config import BotConfig
from bot.exchange.base import (
    Broker,
    BrokerPosition,
    ExchangeError,
    Fill,
    MarketMeta,
    order_from_api,
    position_from_api,
)
from bot.exchange.data_feed import CandleFeed, MarketFeed
from bot.exchange.paper import PaperBroker
from bot.exchange.reya_client import COLLATERAL_ASSET, LiveBroker, MarketDataClient
from bot.metrics import PerformanceStats, compute_stats
from bot.notifier import Notifier
from bot.state import BotState, MarketTrade, OpenTrade, SymbolState, TradeRecord
from bot.strategy.fvg import FVGStrategy, Signal, SignalSide
from bot.strategy.risk import RiskManager, TradePlan
from bot.utils.numbers import ZERO, optional_decimal, to_decimal
from sdk.async_api.account_balance_update_payload import AccountBalanceUpdatePayload
from sdk.async_api.market_depth_update_payload import MarketDepthUpdatePayload
from sdk.async_api.market_perp_execution_update_payload import (
    MarketPerpExecutionUpdatePayload,
)
from sdk.async_api.market_summary_update_payload import MarketSummaryUpdatePayload
from sdk.async_api.order_change_update_payload import OrderChangeUpdatePayload
from sdk.async_api.position_update_payload import PositionUpdatePayload
from sdk.async_api.price_update_payload import PriceUpdatePayload
from sdk.async_api.prices_update_payload import PricesUpdatePayload
from sdk.async_api.wallet_perp_execution_update_payload import (
    WalletPerpExecutionUpdatePayload,
)

logger = logging.getLogger("bot.engine")

#: Seconds of WebSocket silence after which new entries are suspended.
FEED_SAFETY_TIMEOUT_S = 90.0
#: Backoff ceiling when a supervised task keeps failing.
TASK_RESTART_MAX_BACKOFF_S = 30.0


class TradingEngine:
    """Owns the brokers, feeds, strategies and the shared :class:`BotState`."""

    def __init__(self, config: BotConfig) -> None:
        """Create an engine. Nothing connects until :meth:`start` is awaited."""
        self.config = config
        self.state = BotState(
            mode=config.mode,
            network=config.exchange.network_name,
            focus_symbol=config.primary_symbol,
            account_label=str(config.exchange.account_id or "-"),
        )
        self.risk = RiskManager(config.risk)
        self.state.risk = self.risk.state
        self.notifier = Notifier(config.notifier)

        self.market_data = MarketDataClient(config.exchange.api_url)
        self.broker: Optional[Broker] = None
        self.live_broker: Optional[LiveBroker] = None
        self.feed: Optional[MarketFeed] = None

        self.candle_feeds: Dict[str, CandleFeed] = {}
        self.strategies: Dict[str, FVGStrategy] = {}

        self._tasks: List[asyncio.Task] = []
        self._entry_lock = asyncio.Lock()
        self._entering: set = set()
        self._exit_fills: Dict[str, List[Fill]] = {}
        #: Reason to record on the next close-out, set by an explicit flatten.
        self._close_reasons: Dict[str, str] = {}
        self._stopping = False
        self._auto_paused = False
        self._reconcile_requested = asyncio.Event()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Connect everything and begin trading (or waiting, if not armed).

        Raises:
            ExchangeError: If market definitions or candle history cannot load,
                which leaves the bot with nothing to trade on.
        """
        logger.info("Starting engine in %s mode on %s", self.config.mode, self.config.exchange.network_name)
        await self.notifier.start()

        markets = await self.market_data.load_markets()
        self._validate_symbols(markets)

        await self._build_broker(markets)
        self._build_strategies()
        await self._warm_up_candles()
        await self._start_feed()

        try:
            await self.reconcile()
        except ExchangeError as exc:
            # Paper mode has nothing to reconcile against; live mode should not
            # start blind, but a transient failure here is recoverable.
            logger.warning("Initial reconciliation failed: %s", exc)

        self.state.session_start_equity = self.state.equity
        self.state.running = self.config.autostart
        self._spawn_tasks()
        logger.info(
            "Engine ready | broker=%s equity=%s symbols=%s armed=%s",
            self.state.broker_name,
            self.state.equity,
            ", ".join(self.config.symbols),
            self.state.running,
        )

    def _validate_symbols(self, markets: Dict[str, MarketMeta]) -> None:
        """Fail fast on a symbol the venue does not list."""
        unknown = [symbol for symbol in self.config.symbols if symbol not in markets]
        if unknown:
            available = ", ".join(sorted(markets)[:20])
            raise ExchangeError(f"Unknown symbol(s): {', '.join(unknown)}. Available markets include: {available}")

    async def _build_broker(self, markets: Dict[str, MarketMeta]) -> None:
        """Instantiate the live or paper broker and connect it."""
        if self.config.is_paper:
            broker: Broker = PaperBroker(self.config.paper, self.config.execution, markets)
        else:
            live = LiveBroker(self.config.exchange, self.config.execution)
            await live.connect()
            self.live_broker = live
            broker = live
        if self.config.is_paper:
            await broker.connect()
        self.broker = broker
        self.state.broker_name = broker.name

        for symbol in self.config.symbols:
            self.state.symbol_state(symbol).meta = broker.market_meta(symbol)

    def _build_strategies(self) -> None:
        """Create one strategy and one candle feed per configured symbol."""
        for symbol in self.config.symbols:
            self.strategies[symbol] = FVGStrategy(symbol, self.config.strategy)
            self.candle_feeds[symbol] = CandleFeed(
                self.market_data,
                symbol,
                timeframe=self.config.strategy.timeframe,
                history_bars=self.config.strategy.history_bars,
            )

    async def _warm_up_candles(self) -> None:
        """Load history for every symbol and seed the strategies."""
        results = await asyncio.gather(
            *(feed.warmup() for feed in self.candle_feeds.values()),
            return_exceptions=True,
        )
        for symbol, result in zip(self.candle_feeds, results):
            if isinstance(result, BaseException):
                raise ExchangeError(f"Candle warm-up failed for {symbol}: {result}")
            self._apply_candles(symbol, result)

    def _apply_candles(self, symbol: str, closed) -> None:
        """Push closed candles into the strategy and mirror them into state."""
        strategy = self.strategies[symbol]
        strategy.update_candles(closed)

        state = self.state.symbol_state(symbol)
        state.candles = strategy.candles
        state.gaps = strategy.gaps
        state.bias = strategy.bias
        state.atr = strategy.atr
        state.forming_candle = self.candle_feeds[symbol].forming_candle
        state.status_reason = strategy.last_block_reason
        state.last_candle_at = time.time()

    async def _start_feed(self) -> None:
        """Connect the WebSocket feed and register the reconnect hook."""
        feed = MarketFeed(
            ws_url=self.config.exchange.ws_url,
            symbols=self.config.symbols,
            wallet_address=self.config.exchange.owner_wallet_address,
        )
        feed.on_reconnect(self.request_reconciliation)
        await feed.start()
        self.feed = feed
        self.state.feed = feed.status

    def _spawn_tasks(self) -> None:
        """Start the supervised background tasks."""
        self._tasks = [
            asyncio.create_task(self._supervise("events", self._event_loop), name="engine-events"),
            asyncio.create_task(self._supervise("candles", self._candle_loop), name="engine-candles"),
            asyncio.create_task(self._supervise("reconcile", self._reconcile_loop), name="engine-reconcile"),
            asyncio.create_task(self._supervise("health", self._health_loop), name="engine-health"),
        ]

    async def _supervise(self, name: str, coro_factory: Callable[[], Awaitable[None]]) -> None:
        """Run a loop forever, restarting it with backoff if it raises.

        A crashed task would otherwise leave positions unmanaged, which is the
        worst failure mode a trading bot has.
        """
        backoff = 1.0
        while not self._stopping:
            try:
                await coro_factory()
                return
            # Cancellation is shutdown, not failure: it must reach the caller
            # rather than be swallowed by the restart handler below.
            except asyncio.CancelledError:  # pylint: disable=try-except-raise
                raise
            except Exception as exc:  # noqa: BLE001 - restart rather than die
                self.state.last_error = f"{name}: {exc}"
                logger.exception("Task %r failed; restarting in %.0fs", name, backoff)
                self.notifier.notify("error", f"Task {name} crashed: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, TASK_RESTART_MAX_BACKOFF_S)

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------
    async def _event_loop(self) -> None:
        """Consume WebSocket payloads and drive the strategy from prices."""
        assert self.feed is not None
        while not self._stopping:
            message = await self.feed.get(timeout=0.25)
            if message is not None:
                await self._handle_message(message)

    async def _candle_loop(self) -> None:
        """Poll candles and re-run detection whenever a bar closes."""
        interval = max(5, self.config.execution.candle_poll_interval_s)
        while not self._stopping:
            await asyncio.sleep(interval)
            for symbol, feed in self.candle_feeds.items():
                closed, newly = await feed.poll()
                if not closed:
                    continue
                self._apply_candles(symbol, closed)
                for candle in newly:
                    logger.info(
                        "%s %s candle closed at %s (O %s H %s L %s C %s)",
                        symbol,
                        self.config.strategy.timeframe,
                        candle.timestamp,
                        candle.open,
                        candle.high,
                        candle.low,
                        candle.close,
                    )

    async def _reconcile_loop(self) -> None:
        """Re-read account state on a timer, or immediately when asked."""
        interval = max(5, self.config.execution.reconcile_interval_s)
        while not self._stopping:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._reconcile_requested.wait(), timeout=interval)
            self._reconcile_requested.clear()
            try:
                await self.reconcile()
            except ExchangeError as exc:
                self.state.last_error = str(exc)
                logger.warning("Reconciliation failed: %s", exc)

    async def _health_loop(self) -> None:
        """Watch feed health and suspend entries when data goes stale."""
        while not self._stopping:
            await asyncio.sleep(5)
            feed = self.feed
            if feed is None or feed.status.last_message_at <= 0:
                continue  # nothing has arrived yet; startup is not staleness

            stale = feed.status.silence_s > FEED_SAFETY_TIMEOUT_S
            if stale and not self._auto_paused:
                self._auto_paused = True
                self.state.paused = True
                self.state.notice = f"entries paused: no market data for {feed.status.silence_s:.0f}s"
                logger.error(self.state.notice)
                self.notifier.notify("halt", self.state.notice)
            elif not stale and self._auto_paused:
                # Only lift the pause the health loop applied; a manual pause stands.
                self._auto_paused = False
                self.state.paused = False
                self.state.notice = ""
                logger.info("Market data recovered; entries resumed")

    # ------------------------------------------------------------------
    # WebSocket message handling
    # ------------------------------------------------------------------
    async def _handle_message(self, message: object) -> None:
        """Route one typed payload to its handler."""
        if isinstance(message, PriceUpdatePayload):
            await self._handle_price(message.data)
        elif isinstance(message, PricesUpdatePayload):
            for price in message.data:
                await self._handle_price(price)
        elif isinstance(message, MarketSummaryUpdatePayload):
            self._handle_summary(message.data)
        elif isinstance(message, MarketDepthUpdatePayload):
            self._handle_depth(message.data)
        elif isinstance(message, MarketPerpExecutionUpdatePayload):
            self._handle_tape(message.data)
        elif isinstance(message, PositionUpdatePayload):
            await self._handle_positions(message.data)
        elif isinstance(message, OrderChangeUpdatePayload):
            self._handle_orders(message.data)
        elif isinstance(message, WalletPerpExecutionUpdatePayload):
            self._handle_wallet_fills(message.data)
        elif isinstance(message, AccountBalanceUpdatePayload):
            self._handle_balances(message.data)

    async def _handle_price(self, price: object) -> None:
        """Apply a price tick: update state, mark the book, look for an entry."""
        symbol = str(getattr(price, "symbol", ""))
        if symbol not in self.strategies:
            return

        oracle = optional_decimal(getattr(price, "oracle_price", None))
        pool = optional_decimal(getattr(price, "pool_price", None))
        # The pool price is what a taker actually trades against; the oracle is
        # the mark. Prefer the pool price and fall back to the oracle.
        current = pool if pool and pool > ZERO else oracle
        if current is None or current <= ZERO:
            return

        state = self.state.symbol_state(symbol)
        state.oracle_price = oracle
        state.pool_price = pool
        state.update_price(current)

        if self.broker is not None:
            # The paper broker settles stops and targets against this tick, so
            # drain its fills and refresh its position before looking for a new
            # entry — otherwise the bot could size a trade against stale state.
            self.broker.on_price(symbol, current)
            if self.config.is_paper:
                self._drain_broker_fills()
                await self._sync_paper_position(symbol)

        await self._evaluate_entry(symbol, current)

    def _handle_summary(self, summary: object) -> None:
        """Store funding, open interest and 24h statistics."""
        symbol = str(getattr(summary, "symbol", ""))
        if symbol not in self.strategies:
            return
        state = self.state.symbol_state(symbol)
        state.funding_rate = optional_decimal(getattr(summary, "funding_rate", None))
        state.open_interest = optional_decimal(getattr(summary, "oi_qty", None))
        state.volume_24h = optional_decimal(getattr(summary, "volume24h", None))
        state.price_change_24h = optional_decimal(getattr(summary, "px_change24h", None))

    def _handle_depth(self, depth: object) -> None:
        """Apply a depth snapshot or incremental level update."""
        symbol = str(getattr(depth, "symbol", ""))
        if symbol not in self.strategies:
            return
        book = self.state.symbol_state(symbol).depth
        book.symbol = symbol
        book.updated_at = int(getattr(depth, "updated_at", 0) or 0)

        kind = str(getattr(getattr(depth, "type", None), "value", "SNAPSHOT"))
        bids = [(to_decimal(level.px), to_decimal(level.qty)) for level in getattr(depth, "bids", []) or []]
        asks = [(to_decimal(level.px), to_decimal(level.qty)) for level in getattr(depth, "asks", []) or []]

        if kind == "SNAPSHOT":
            book.bids = sorted(bids, key=lambda level: level[0], reverse=True)
            book.asks = sorted(asks, key=lambda level: level[0])
            return

        book.bids = self._merge_levels(book.bids, bids, descending=True)
        book.asks = self._merge_levels(book.asks, asks, descending=False)

    @staticmethod
    def _merge_levels(
        existing: List[Tuple[Decimal, Decimal]],
        updates: List[Tuple[Decimal, Decimal]],
        descending: bool,
    ) -> List[Tuple[Decimal, Decimal]]:
        """Apply incremental level updates; a zero quantity removes the level."""
        merged = dict(existing)
        for price, qty in updates:
            if qty <= ZERO:
                merged.pop(price, None)
            else:
                merged[price] = qty
        return sorted(merged.items(), key=lambda level: level[0], reverse=descending)

    def _handle_tape(self, executions: List) -> None:
        """Append public prints to the per-symbol tape."""
        for execution in executions:
            symbol = str(getattr(execution, "symbol", ""))
            if symbol not in self.strategies:
                continue
            side = SignalSide.LONG if str(getattr(execution.side, "value", execution.side)) == "B" else SignalSide.SHORT
            self.state.symbol_state(symbol).tape.append(
                MarketTrade(
                    symbol=symbol,
                    side=side,
                    qty=abs(to_decimal(execution.qty)),
                    price=to_decimal(execution.price),
                    timestamp=int(getattr(execution, "timestamp", 0) or 0),
                )
            )

    async def _handle_positions(self, positions: List) -> None:
        """Apply a wallet position update.

        The channel may carry a delta rather than a full snapshot, so only the
        symbols actually present are touched; a symbol's absence is *not* read
        as flat. REST reconciliation, which is requested here, is the
        authoritative check for positions that disappeared.
        """
        if self.config.is_paper:
            return  # paper positions are authoritative in the simulator

        account_id = self.config.exchange.account_id
        touched = False
        for raw in positions:
            position = position_from_api(raw)
            if account_id is not None and position.account_id != account_id:
                continue
            if position.symbol not in self.strategies:
                continue
            touched = True
            await self._apply_position(position.symbol, None if position.is_flat else position)

        if touched:
            self.request_reconciliation()

    def _handle_orders(self, orders: List) -> None:
        """Merge order changes into the cached open orders.

        Merging rather than replacing keeps this correct whether the channel
        sends snapshots or deltas.
        """
        for raw in orders:
            order = order_from_api(raw)
            if order.symbol not in self.strategies:
                continue
            state = self.state.symbol_state(order.symbol)
            remaining = [existing for existing in state.orders if existing.order_id != order.order_id]
            if order.status == "OPEN":
                remaining.append(order)
            state.orders = remaining

    def _handle_wallet_fills(self, executions: List) -> None:
        """Record our own fills so exits can be priced accurately."""
        for execution in executions:
            symbol = str(getattr(execution, "symbol", ""))
            if symbol not in self.strategies:
                continue
            side = SignalSide.LONG if str(getattr(execution.side, "value", execution.side)) == "B" else SignalSide.SHORT
            fill = Fill(
                symbol=symbol,
                side=side,
                qty=abs(to_decimal(execution.qty)),
                price=to_decimal(execution.price),
                fee=to_decimal(getattr(execution, "fee", "0"), default=ZERO),
                timestamp=int(getattr(execution, "timestamp", 0) or 0),
                execution_type=str(getattr(execution.type, "value", execution.type)),
                sequence_number=int(getattr(execution, "sequence_number", 0) or 0),
            )
            self._record_fill(fill)

    def _handle_balances(self, balances: List) -> None:
        """Update the cached collateral balance from a wallet balance update."""
        if self.config.is_paper:
            return
        account_id = self.config.exchange.account_id
        total = ZERO
        matched = False
        for balance in balances:
            if account_id is not None and getattr(balance, "account_id", None) != account_id:
                continue
            if str(getattr(balance, "asset", "")).upper() != COLLATERAL_ASSET:
                continue
            total += to_decimal(getattr(balance, "real_balance", "0"))
            matched = True
        if matched:
            self.state.balance = total
            self.state.equity = total + self.state.total_unrealized

    def _record_fill(self, fill: Fill) -> None:
        """Note a fill against the open trade for its symbol."""
        self.state.last_fill_at = time.time()
        state = self.state.symbol_state(fill.symbol)
        open_trade = state.open_trade
        if open_trade is None:
            return
        if fill.side is open_trade.plan.side:
            open_trade.fees += fill.fee
        else:
            # Opposite side: this is (part of) the exit.
            self._exit_fills.setdefault(fill.symbol, []).append(fill)
        if fill.execution_type in ("LIQUIDATION", "ADL"):
            logger.error("%s on %s at %s", fill.execution_type, fill.symbol, fill.price)
            self.notifier.notify("error", f"{fill.execution_type} on {fill.symbol} at {fill.price}")

    def _drain_broker_fills(self) -> None:
        """Fold locally generated fills (paper mode) into the trade ledger."""
        if self.broker is None:
            return
        for fill in self.broker.drain_fills():
            self._record_fill(fill)

    async def _sync_paper_position(self, symbol: str) -> None:
        """Mirror the simulator's in-memory account into shared state."""
        if self.broker is None:
            return
        snapshot = await self.broker.fetch_account()
        self.state.balance = snapshot.balance
        self.state.equity = snapshot.equity
        position = snapshot.positions.get(symbol)
        if position is not None and position.is_flat:
            position = None
        await self._apply_position(symbol, position)
        self.state.symbol_state(symbol).orders = [order for order in snapshot.orders if order.symbol == symbol]

    # ------------------------------------------------------------------
    # Position lifecycle
    # ------------------------------------------------------------------
    async def _apply_position(self, symbol: str, position: Optional[BrokerPosition]) -> None:
        """Store the current position and detect the transition to flat."""
        state = self.state.symbol_state(symbol)
        previous = state.position
        state.position = position

        if position is not None or previous is None:
            return
        if state.open_trade is None:
            return
        await self._close_out(symbol)

    async def _close_out(self, symbol: str) -> None:
        """Book a finished round trip and clean up its bracket orders."""
        state = self.state.symbol_state(symbol)
        open_trade = state.open_trade
        if open_trade is None:
            return
        state.open_trade = None

        fills = self._exit_fills.pop(symbol, [])
        exit_price, fees, reason = self._resolve_exit(open_trade, fills, state)
        reason = self._close_reasons.pop(symbol, reason)
        gross = (exit_price - open_trade.entry_price) * open_trade.qty * open_trade.plan.side.sign
        total_fees = open_trade.fees + fees
        pnl = gross - total_fees

        record = TradeRecord(
            symbol=symbol,
            side=open_trade.plan.side,
            qty=open_trade.qty,
            entry_price=open_trade.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            r_multiple=open_trade.r_multiple(exit_price),
            opened_at=open_trade.opened_at,
            closed_at=time.time(),
            reason=reason,
            fees=total_fees,
        )
        self.state.trades.append(record)
        self.state.session_realized_pnl += pnl
        self.risk.record_result(pnl)

        logger.info(
            "Closed %s %s %s: entry %s exit %s -> PnL %s (%.2fR, %s)",
            record.side.value,
            record.qty,
            symbol,
            record.entry_price,
            record.exit_price,
            record.pnl,
            record.r_multiple,
            reason,
        )
        self.notifier.notify(
            "exit",
            f"{record.side.value} {symbol} closed at {record.exit_price} "
            f"for {record.pnl:.2f} ({record.r_multiple:.2f}R, {reason})",
        )

        # Emulate OCO: whichever bracket leg did not fire is still resting.
        if self.broker is not None:
            cancelled = await self.broker.cancel_symbol_orders(symbol)
            if cancelled:
                logger.info("Cancelled %d leftover bracket order(s) for %s", cancelled, symbol)
        state.orders = []

    def _resolve_exit(
        self,
        open_trade: OpenTrade,
        fills: List[Fill],
        state: SymbolState,
    ) -> Tuple[Decimal, Decimal, str]:
        """Work out the exit price, fees and a human-readable reason.

        Exit fills are the truth when they arrived; otherwise the current mark
        is used and the trade is flagged as ``reconciled`` so the log is honest
        about the estimate.
        """
        if fills:
            quantity = sum((fill.qty for fill in fills), ZERO)
            notional = sum((fill.qty * fill.price for fill in fills), ZERO)
            fees = sum((fill.fee for fill in fills), ZERO)
            price = notional / quantity if quantity > ZERO else open_trade.entry_price
            return price, fees, self._classify_exit(open_trade, price)

        mark = state.price or open_trade.entry_price
        return mark, ZERO, "reconciled"

    @staticmethod
    def _classify_exit(open_trade: OpenTrade, price: Decimal) -> str:
        """Label an exit as a stop, a target or a manual close."""
        stop_distance = abs(price - open_trade.stop_price)
        target_distance = abs(price - open_trade.take_profit)
        if stop_distance <= target_distance:
            return "stop loss"
        return "take profit"

    # ------------------------------------------------------------------
    # Entry path
    # ------------------------------------------------------------------
    async def _evaluate_entry(self, symbol: str, price: Decimal) -> None:
        """Run the strategy against a tick and take the trade if it qualifies."""
        state = self.state.symbol_state(symbol)
        strategy = self.strategies[symbol]

        signal = strategy.evaluate(price)
        state.status_reason = strategy.last_block_reason
        if signal is None:
            return

        state.last_signal = signal
        state.last_signal_at = time.time()
        logger.info("Signal: %s", signal.describe())

        if not self.state.running or self.state.paused or self._stopping:
            logger.info("Signal ignored: bot is %s", self.state.status_label)
            # Let the gap fire again once the operator arms the strategy.
            strategy.forget_signal(signal.fvg.key)
            return

        async with self._entry_lock:
            if symbol in self._entering:
                return
            self._entering.add(symbol)
        try:
            await self._execute_signal(signal, price)
        finally:
            self._entering.discard(symbol)

    async def _execute_signal(self, signal: Signal, price: Decimal) -> None:
        """Apply the risk gates, size the trade and send the entry."""
        symbol = signal.symbol
        state = self.state.symbol_state(symbol)
        strategy = self.strategies[symbol]

        self.risk.sync_equity(self.state.equity)

        elapsed = time.time() - self.risk.state.last_entry_at
        if self.risk.state.last_entry_at > 0 and elapsed < self.config.execution.min_seconds_between_entries:
            state.status_reason = f"entry spacing ({elapsed:.0f}s)"
            strategy.forget_signal(signal.fvg.key)
            return

        blocked = self.risk.check_gates(
            open_positions=len(self.state.open_positions),
            symbol_has_position=state.position is not None,
        )
        if blocked:
            state.status_reason = f"blocked: {blocked}"
            logger.info("Entry blocked for %s: %s", symbol, blocked)
            strategy.forget_signal(signal.fvg.key)
            return

        meta = self.broker.market_meta(symbol) if self.broker else MarketMeta.fallback(symbol)
        plan = self.risk.build_plan(signal, self.state.equity, meta)
        if plan is None:
            state.status_reason = f"rejected: {self.risk.last_rejection}"
            logger.info("Entry rejected for %s: %s", symbol, self.risk.last_rejection)
            strategy.forget_signal(signal.fvg.key)
            return

        await self._submit(plan, price)

    async def _submit(self, plan: TradePlan, price: Decimal) -> None:
        """Send the entry order and attach brackets when it fills."""
        assert self.broker is not None
        symbol = plan.symbol
        state = self.state.symbol_state(symbol)

        logger.info("Submitting %s", plan.describe())
        result = await self.broker.submit_entry(plan, price)

        if not result.ok:
            state.status_reason = f"order failed: {result.error}"
            self.state.last_error = result.error
            self.notifier.notify("error", f"Entry failed for {symbol}: {result.error}")
            self.strategies[symbol].forget_signal(plan.signal.fvg.key)
            return

        if not result.filled:
            # An IOC that did not trade is a miss, not a failure: the gap may
            # still be valid, so allow it to signal again on a later tick.
            state.status_reason = f"entry not filled ({result.status})"
            logger.info("Entry for %s did not fill (%s)", symbol, result.status)
            self.strategies[symbol].forget_signal(plan.signal.fvg.key)
            return

        entry_price = result.avg_price or plan.entry_price
        self.risk.record_entry()
        self.state.last_fill_at = time.time()
        state.open_trade = OpenTrade(
            plan=plan,
            entry_price=entry_price,
            qty=result.filled_qty,
            opened_at=time.time(),
            stop_price=plan.stop_price,
            take_profit=plan.take_profit,
        )
        state.status_reason = "in position"
        self.notifier.notify(
            "entry",
            f"{plan.side.value} {result.filled_qty} {symbol} @ {entry_price} "
            f"SL {plan.stop_price} TP {plan.take_profit} ({plan.risk_reward:.2f}R)",
        )

        if self.config.execution.attach_brackets:
            await self._attach_brackets(plan, state)

        self.request_reconciliation()

    async def _attach_brackets(self, plan: TradePlan, state: SymbolState) -> None:
        """Attach SL and TP, and escalate loudly if the stop does not stick."""
        assert self.broker is not None
        brackets = await self.broker.attach_brackets(
            plan.symbol,
            plan.side,
            plan.stop_price,
            plan.take_profit,
        )
        if brackets.ok:
            if state.open_trade is not None:
                state.open_trade.bracket_order_ids = [
                    order_id for order_id in (brackets.stop.order_id, brackets.take_profit.order_id) if order_id
                ]
            return

        errors = "; ".join(brackets.errors)
        logger.error("Bracket placement incomplete for %s: %s", plan.symbol, errors)
        self.notifier.notify("error", f"Bracket placement failed for {plan.symbol}: {errors}")

        # An unprotected position is the one thing worth closing immediately.
        if not brackets.stop.ok:
            logger.error("Stop loss missing for %s; flattening the position", plan.symbol)
            await self.flatten(plan.symbol, reason="missing stop loss")

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------
    def request_reconciliation(self) -> None:
        """Ask the reconcile loop to run as soon as possible."""
        self._reconcile_requested.set()

    async def reconcile(self) -> None:
        """Re-read balance, positions and orders, and repair any drift.

        Raises:
            ExchangeError: If the venue could not be read.
        """
        if self.broker is None:
            return

        marks = {symbol: state.price for symbol, state in self.state.symbols.items() if state.price is not None}
        snapshot = await self.broker.fetch_account(marks)

        self.state.balance = snapshot.balance
        self.state.equity = snapshot.equity
        if self.state.session_start_equity <= ZERO:
            self.state.session_start_equity = snapshot.equity
        self.risk.sync_equity(snapshot.equity)

        for symbol in self.strategies:
            state = self.state.symbol_state(symbol)
            state.orders = [order for order in snapshot.orders if order.symbol == symbol]
            position = snapshot.positions.get(symbol)
            if position is not None and position.is_flat:
                position = None
            await self._apply_position(symbol, position)
            await self._repair_brackets(symbol, state)

        if self.feed is not None:
            self.feed.status.needs_reconciliation = False

    async def _repair_brackets(self, symbol: str, state: SymbolState) -> None:
        """Re-attach missing SL/TP for a position the bot is managing.

        A dropped WebSocket or a rejected trigger order can leave a position
        without protection; this is the safety net that notices.
        """
        open_trade = state.open_trade
        if open_trade is None or state.position is None or not self.config.execution.attach_brackets:
            return
        if self.broker is None or self._stopping:
            return

        resting = {order.order_type for order in state.orders if order.is_bracket}
        if {"SL", "TP"}.issubset(resting):
            return

        logger.warning(
            "%s position is missing bracket leg(s) %s; re-attaching",
            symbol,
            ", ".join(sorted({"SL", "TP"} - resting)),
        )
        await self.broker.attach_brackets(
            symbol,
            open_trade.plan.side,
            open_trade.stop_price,
            open_trade.take_profit,
        )

    # ------------------------------------------------------------------
    # Operator controls
    # ------------------------------------------------------------------
    def arm(self) -> None:
        """Allow new entries."""
        self.state.running = True
        self.state.paused = False
        self.state.notice = ""
        logger.info("Strategy armed: new entries allowed")

    def disarm(self) -> None:
        """Stop taking new entries. Open positions keep their brackets."""
        self.state.running = False
        logger.info("Strategy disarmed: no new entries (existing positions still managed)")

    def toggle_pause(self) -> bool:
        """Flip the pause flag and return the new value."""
        self.state.paused = not self.state.paused
        logger.info("Entries %s", "paused" if self.state.paused else "resumed")
        return self.state.paused

    def set_risk_pct(self, value: Decimal) -> Decimal:
        """Change the per-trade risk budget, clamped to the configured ceiling.

        Args:
            value: Desired risk percentage of equity.

        Returns:
            The value actually applied.
        """
        ceiling = self.config.risk.max_risk_per_trade_pct
        applied = max(Decimal("0.05"), min(value, ceiling))
        self.config.risk.risk_per_trade_pct = applied
        logger.info("Risk per trade set to %s%% of equity", applied)
        return applied

    def cycle_focus(self, step: int = 1) -> str:
        """Move the UI focus to the next configured symbol."""
        symbols = self.config.symbols
        if not symbols:
            return self.state.focus_symbol
        try:
            index = symbols.index(self.state.focus_symbol)
        except ValueError:
            index = 0
        self.state.focus_symbol = symbols[(index + step) % len(symbols)]
        return self.state.focus_symbol

    async def flatten(self, symbol: str, reason: str = "manual close") -> bool:
        """Cancel resting orders and close the position for one symbol.

        Args:
            symbol: Market to flatten.
            reason: Recorded on the resulting trade.

        Returns:
            Whether a position was closed.
        """
        if self.broker is None:
            return False
        state = self.state.symbol_state(symbol)
        position = state.position
        await self.broker.cancel_symbol_orders(symbol)

        if position is None or position.is_flat:
            return False

        price = state.price
        if price is None:
            logger.error("Cannot flatten %s: no live price", symbol)
            return False

        logger.warning("Flattening %s (%s)", symbol, reason)
        self._close_reasons[symbol] = reason
        result = await self.broker.close_position(position, price)
        if not result.ok:
            self._close_reasons.pop(symbol, None)
            self.state.last_error = result.error
            logger.error("Failed to flatten %s: %s", symbol, result.error)
            return False

        # Collect the closing fill so the round trip is priced from the real
        # execution. The paper broker reports it through drain_fills; the live
        # broker returns it inline (the WebSocket copy may arrive later).
        self._drain_broker_fills()
        if not self._exit_fills.get(symbol) and result.avg_price is not None:
            self._exit_fills.setdefault(symbol, []).append(
                Fill(
                    symbol=symbol,
                    side=position.side.opposite,
                    qty=result.filled_qty or position.qty,
                    price=result.avg_price,
                    timestamp=int(time.time() * 1000),
                )
            )
        await self._apply_position(symbol, None)
        self.request_reconciliation()
        return True

    async def flatten_all(self, reason: str = "manual close") -> int:
        """Flatten every open position. Returns how many were closed."""
        results = await asyncio.gather(
            *(self.flatten(symbol, reason) for symbol in list(self.strategies)),
            return_exceptions=True,
        )
        return sum(1 for result in results if result is True)

    def stats(self) -> PerformanceStats:
        """Performance statistics for the trades closed this session."""
        return compute_stats(self.state.trades, self.state.session_start_equity)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    async def stop(self) -> None:
        """Shut down cleanly, honouring the configured shutdown policy."""
        if self._stopping:
            return
        self._stopping = True
        self.state.shutting_down = True
        self.state.running = False
        logger.info("Shutting down the engine")

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []

        if self.broker is not None:
            if self.config.execution.close_positions_on_shutdown:
                closed = await self.flatten_all(reason="shutdown")
                logger.info("Closed %d position(s) on shutdown", closed)
            if self.config.execution.cancel_orders_on_shutdown:
                for symbol in self.strategies:
                    with contextlib.suppress(Exception):
                        cancelled = await self.broker.cancel_symbol_orders(symbol)
                        if cancelled:
                            logger.info("Cancelled %d order(s) for %s", cancelled, symbol)

        if self.feed is not None:
            await self.feed.stop()
        if self.broker is not None:
            await self.broker.close()
        await self.market_data.close()
        await self.notifier.close()

        stats = self.stats()
        if stats.trades:
            logger.info(
                "Session summary: %d trade(s), %.1f%% win rate, net PnL %s",
                stats.trades,
                stats.win_rate_pct,
                stats.net_pnl,
            )
        logger.info("Engine stopped")


__all__ = ["TradingEngine"]
