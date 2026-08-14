"""Market data: 15m candles over REST and live streams over WebSocket.

Two feeds live here.

:class:`CandleFeed` pulls ``/v2/candleHistory/{symbol}/{resolution}``. The
endpoint returns at most 200 bars ending at ``endTime``, so warm-up walks
backwards page by page. Only *closed* bars are handed to the strategy — the
newest bar is still forming and its high and low keep moving.

:class:`MarketFeed` bridges the SDK's thread-based :class:`ReyaSocket` into
asyncio. Callbacks arrive on the WebSocket thread and are handed to the event
loop with ``call_soon_threadsafe``; a supervisor task watches for silence or a
dropped socket and rebuilds the connection with exponential backoff, then
re-subscribes. Because a reconnect can hide missed messages, the engine always
re-reads positions and orders over REST after one.
"""

from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal

from bot.exchange.reya_client import MarketDataClient
from bot.strategy.indicators import Candle
from sdk.async_api.error_message_payload import ErrorMessagePayload
from sdk.async_api.ping_message_payload import PingMessagePayload
from sdk.reya_websocket import ReyaSocket
from sdk.reya_websocket.config import WebSocketConfig

logger = logging.getLogger("bot.exchange.feed")

#: Candle resolutions understood by the bot, in seconds.
TIMEFRAME_SECONDS: Dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "1d": 86400,
}

#: Candles returned by one page of ``/v2/candleHistory``.
CANDLES_PER_PAGE = 200


def timeframe_seconds(timeframe: str) -> int:
    """Convert a resolution string such as ``15m`` to seconds.

    Raises:
        ValueError: If the resolution is not one the bot supports.
    """
    seconds = TIMEFRAME_SECONDS.get(timeframe.lower())
    if seconds is None:
        raise ValueError(f"Unsupported timeframe {timeframe!r}; expected one of {', '.join(TIMEFRAME_SECONDS)}")
    return seconds


def parse_candle_history(payload: Any, symbol: str = "") -> List[Candle]:
    """Convert the API's column-oriented candle payload into ``Candle`` rows.

    The endpoint returns parallel arrays (``t``, ``o``, ``h``, ``l``, ``c``)
    sorted newest first; the result here is chronological.
    """
    timestamps = list(getattr(payload, "t", []) or [])
    opens = list(getattr(payload, "o", []) or [])
    highs = list(getattr(payload, "h", []) or [])
    lows = list(getattr(payload, "l", []) or [])
    closes = list(getattr(payload, "c", []) or [])

    length = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))
    if length != len(timestamps):
        logger.warning("Ragged candle payload for %s; using the first %d complete rows", symbol, length)

    candles = [Candle.create(timestamps[i], opens[i], highs[i], lows[i], closes[i]) for i in range(length)]
    candles.sort(key=lambda candle: candle.timestamp)
    return candles


class CandleFeed:
    """Maintains a rolling window of closed candles for one symbol."""

    def __init__(
        self,
        client: MarketDataClient,
        symbol: str,
        timeframe: str = "15m",
        history_bars: int = 500,
    ) -> None:
        """Create a candle feed.

        Args:
            client: Market data client used for REST reads.
            symbol: Market symbol.
            timeframe: Candle resolution, e.g. ``15m``.
            history_bars: Target warm-up depth.
        """
        self.client = client
        self.symbol = symbol
        self.timeframe = timeframe
        self.history_bars = history_bars
        self.interval = timeframe_seconds(timeframe)
        self._candles: Dict[int, Candle] = {}
        self._last_closed_ts: Optional[int] = None
        self.last_poll_at: float = 0.0
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    def _sorted(self) -> List[Candle]:
        """All known candles in chronological order."""
        return [self._candles[key] for key in sorted(self._candles)]

    def is_closed(self, candle: Candle, now: Optional[float] = None) -> bool:
        """Whether ``candle``'s interval has elapsed."""
        moment = now if now is not None else time.time()
        return candle.timestamp + self.interval <= moment

    @property
    def closed_candles(self) -> List[Candle]:
        """Closed bars only, chronological. This is what the strategy sees."""
        now = time.time()
        return [candle for candle in self._sorted() if self.is_closed(candle, now)]

    @property
    def forming_candle(self) -> Optional[Candle]:
        """The bar currently being built, if any."""
        candles = self._sorted()
        if not candles:
            return None
        newest = candles[-1]
        return None if self.is_closed(newest) else newest

    @property
    def candle_count(self) -> int:
        """Number of bars held, closed and forming."""
        return len(self._candles)

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------
    async def _fetch_page(self, end_time: Optional[int]) -> List[Candle]:
        """Fetch one page of history, ending at ``end_time`` when given."""
        payload = await self.client.get_candles(
            symbol=self.symbol,
            resolution=self.timeframe,
            end_time=end_time,
        )
        return parse_candle_history(payload, self.symbol)

    def _absorb(self, candles: Iterable[Candle]) -> int:
        """Merge candles into the window, returning how many timestamps are new.

        Existing timestamps are overwritten so that a re-fetched forming bar
        updates in place rather than duplicating.
        """
        new = 0
        for candle in candles:
            if candle.timestamp not in self._candles:
                new += 1
            self._candles[candle.timestamp] = candle
        self._trim()
        return new

    def _trim(self) -> None:
        """Drop the oldest bars beyond the configured window."""
        # A little headroom above history_bars keeps indicator warm-up stable.
        limit = self.history_bars + CANDLES_PER_PAGE
        if len(self._candles) <= limit:
            return
        for key in sorted(self._candles)[: len(self._candles) - limit]:
            del self._candles[key]

    async def warmup(self) -> List[Candle]:
        """Page backwards until ``history_bars`` bars are loaded.

        Returns:
            The closed candles now held.

        Raises:
            RuntimeError: If the first page cannot be fetched at all.
        """
        try:
            first = await self._fetch_page(None)
        except Exception as exc:  # noqa: BLE001 - surfaced with context
            raise RuntimeError(f"Could not load {self.timeframe} candles for {self.symbol}: {exc}") from exc

        if not first:
            raise RuntimeError(f"No {self.timeframe} candle history returned for {self.symbol}")
        self._absorb(first)

        # Walk backwards a page at a time. Stop as soon as a page adds nothing,
        # which is how the endpoint signals the start of history.
        while len(self._candles) < self.history_bars:
            oldest = min(self._candles)
            try:
                page = await self._fetch_page(oldest)
            except Exception as exc:  # noqa: BLE001 - partial history is usable
                logger.warning("Candle pagination stopped for %s: %s", self.symbol, exc)
                break
            if not page or self._absorb(page) == 0:
                logger.info("Reached the start of available %s history for %s", self.timeframe, self.symbol)
                break

        closed = self.closed_candles
        if closed:
            self._last_closed_ts = closed[-1].timestamp
        logger.info(
            "Warmed up %s with %d %s candles (%d closed)",
            self.symbol,
            len(self._candles),
            self.timeframe,
            len(closed),
        )
        self.last_poll_at = time.time()
        return closed

    async def poll(self) -> Tuple[List[Candle], List[Candle]]:
        """Fetch the newest page and report any bars that have just closed.

        Returns:
            ``(closed_candles, newly_closed)``. ``newly_closed`` is empty on most
            polls and holds one bar every ``timeframe``.
        """
        try:
            page = await self._fetch_page(None)
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 - transient REST errors are normal
            self.last_error = str(exc)
            logger.warning("Candle poll failed for %s: %s", self.symbol, exc)
            return self.closed_candles, []

        self._absorb(page)
        self.last_poll_at = time.time()

        closed = self.closed_candles
        if not closed:
            return closed, []

        if self._last_closed_ts is None:
            self._last_closed_ts = closed[-1].timestamp
            return closed, []

        newly = [candle for candle in closed if candle.timestamp > self._last_closed_ts]
        if newly:
            self._last_closed_ts = closed[-1].timestamp
        return closed, newly


@dataclass
class FeedStatus:
    """Health of the WebSocket connection, rendered in the UI status bar."""

    connected: bool = False
    last_message_at: float = 0.0
    connected_at: float = 0.0
    reconnects: int = 0
    subscriptions: int = 0
    last_error: Optional[str] = None
    #: Set once after each reconnect so the engine knows to re-read REST state.
    needs_reconciliation: bool = False

    @property
    def silence_s(self) -> float:
        """Seconds since the last message, or ``inf`` before the first one."""
        if self.last_message_at <= 0:
            return float("inf")
        return time.time() - self.last_message_at

    @property
    def uptime_s(self) -> float:
        """Seconds the current connection has been up."""
        if not self.connected or self.connected_at <= 0:
            return 0.0
        return time.time() - self.connected_at


class MarketFeed:
    """Runs :class:`ReyaSocket` and delivers typed payloads into asyncio."""

    def __init__(
        self,
        ws_url: str,
        symbols: Sequence[str],
        wallet_address: str,
        queue_size: int = 2000,
        stale_timeout_s: float = 60.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        """Create a market feed.

        Args:
            ws_url: WebSocket endpoint.
            symbols: Symbols to subscribe market channels for.
            wallet_address: Wallet whose positions, orders and fills to stream.
            queue_size: Bounded queue depth; the oldest event is dropped when
                full so a slow consumer cannot exhaust memory.
            stale_timeout_s: Silence after which the connection is rebuilt.
            max_backoff_s: Ceiling on the reconnect backoff.
        """
        self.ws_url = ws_url
        self.symbols = list(symbols)
        self.wallet_address = wallet_address
        self.stale_timeout_s = stale_timeout_s
        self.max_backoff_s = max_backoff_s
        self.status = FeedStatus()

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._socket: Optional[ReyaSocket] = None
        self._supervisor: Optional[asyncio.Task] = None
        #: Set by stop(); an Event rather than a bool so the supervisor can
        #: re-check it after every await without the flag being stale.
        self._stopped = asyncio.Event()
        self._dropped = 0
        self._on_reconnect: List[Callable[[], None]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Connect and start the supervisor task."""
        self._loop = asyncio.get_running_loop()
        self._stopped.clear()
        self._open_socket()
        self._supervisor = asyncio.create_task(self._supervise(), name="market-feed-supervisor")

    async def stop(self) -> None:
        """Stop supervising and close the socket."""
        self._stopped.set()
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
            self._supervisor = None
        self._close_socket()

    def on_reconnect(self, callback: Callable[[], None]) -> None:
        """Register a callback fired (on the event loop) after each reconnect."""
        self._on_reconnect.append(callback)

    def _open_socket(self) -> None:
        """Build and connect a fresh :class:`ReyaSocket`."""
        config = WebSocketConfig(url=self.ws_url, ping_interval=20, ping_timeout=15)
        socket = ReyaSocket(
            config=config,
            on_open=self._handle_open,
            on_message=self._handle_message,
            on_error=self._handle_error,
            on_close=self._handle_close,
        )
        self._socket = socket
        logger.info("Connecting market feed to %s", self.ws_url)
        socket.connect(blocking=False)

    def _close_socket(self) -> None:
        """Close the current socket, ignoring teardown errors."""
        socket, self._socket = self._socket, None
        if socket is None:
            return
        try:
            socket.close()
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            logger.debug("Ignoring error while closing the market feed: %s", exc)

    # ------------------------------------------------------------------
    # WebSocket thread callbacks
    #
    # websocket-client hands these the WebSocketApp itself (our ReyaSocket), but
    # the SDK annotates the parameter as ``WebSocket``. The handlers therefore
    # take ``Any`` and work from ``self._socket``, which is the same object.
    # ------------------------------------------------------------------
    def _handle_open(self, _connection: Any) -> None:
        """Subscribe to every channel the bot needs. Runs on the WS thread."""
        socket = self._socket
        if socket is None:
            return

        for symbol in self.symbols:
            socket.prices.price(symbol).subscribe()
            socket.market.summary(symbol).subscribe()
            socket.market.depth(symbol).subscribe()
            socket.market.perp_executions(symbol).subscribe()

        if self.wallet_address:
            socket.wallet.positions(self.wallet_address).subscribe()
            socket.wallet.order_changes(self.wallet_address).subscribe()
            socket.wallet.perp_executions(self.wallet_address).subscribe()
            socket.wallet.balances(self.wallet_address).subscribe()

        self._post(lambda: self._mark_connected(len(socket.active_subscriptions)))

    def _handle_message(self, connection: Any, message: object) -> None:
        """Forward a typed payload to the event loop. Runs on the WS thread."""
        self.status.last_message_at = time.time()

        # Answer server pings inline; the round trip keeps the socket alive.
        if isinstance(message, PingMessagePayload):
            try:
                connection.send('{"type": "pong"}')
            except Exception as exc:  # noqa: BLE001 - a dead socket is handled by the supervisor
                logger.debug("Could not answer ping: %s", exc)
            return

        if isinstance(message, ErrorMessagePayload):
            self.status.last_error = str(message.message)
            logger.warning("Market feed error message: %s", message.message)

        self._post(lambda: self._enqueue(message))

    def _handle_error(self, _connection: Any, error: Exception) -> None:
        """Record a socket error. Runs on the WS thread."""
        self.status.last_error = str(error)
        logger.warning("Market feed error: %s", error)

    def _handle_close(self, _connection: Any, status_code: int, reason: str) -> None:
        """Mark the feed as down. Runs on the WS thread."""
        logger.warning("Market feed closed (%s): %s", status_code, reason)
        self._post(self._mark_disconnected)

    def _post(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` on the event loop from the WebSocket thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError:  # loop shutting down
            pass

    # ------------------------------------------------------------------
    # Event loop side
    # ------------------------------------------------------------------
    def _mark_connected(self, subscriptions: int) -> None:
        """Record a healthy connection and notify reconnect listeners."""
        first_connect = self.status.connected_at == 0.0
        self.status.connected = True
        self.status.connected_at = time.time()
        self.status.last_message_at = time.time()
        self.status.subscriptions = subscriptions
        self.status.last_error = None
        if not first_connect:
            self.status.needs_reconciliation = True
            for callback in self._on_reconnect:
                callback()
        logger.info("Market feed connected with %d subscription(s)", subscriptions)

    def _mark_disconnected(self) -> None:
        """Record that the connection is down."""
        self.status.connected = False

    def _enqueue(self, message: object) -> None:
        """Put a payload on the queue, dropping the oldest when saturated."""
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self._dropped += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(message)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            if self._dropped % 100 == 1:
                logger.warning("Market feed queue saturated; dropped %d message(s)", self._dropped)

    async def get(self, timeout: float = 0.25) -> Optional[object]:
        """Await the next payload, returning ``None`` on timeout."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def _supervise(self) -> None:
        """Rebuild the connection whenever it drops or goes quiet."""
        backoff = 1.0
        while not self._stopped.is_set():
            await asyncio.sleep(1.0)
            if self._stopped.is_set():
                break

            stale = self.status.silence_s > self.stale_timeout_s
            if self.status.connected and not stale:
                backoff = 1.0
                continue

            reason = "silent" if stale else "disconnected"
            logger.warning("Market feed %s; reconnecting in %.0fs", reason, backoff)
            await asyncio.sleep(backoff)
            if self._stopped.is_set():
                break

            self._close_socket()
            self.status.reconnects += 1
            try:
                self._open_socket()
            except Exception as exc:  # noqa: BLE001 - retry on the next pass
                self.status.last_error = str(exc)
                logger.error("Market feed reconnect failed: %s", exc)
            backoff = min(backoff * 2, self.max_backoff_s)


@dataclass
class DepthBook:
    """Latest L2 depth for one symbol."""

    symbol: str
    bids: List[Tuple[Decimal, Decimal]] = field(default_factory=list)
    asks: List[Tuple[Decimal, Decimal]] = field(default_factory=list)
    updated_at: int = 0

    @property
    def best_bid(self) -> Optional[Decimal]:
        """Highest bid price, if the book has one."""
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        """Lowest ask price, if the book has one."""
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[Decimal]:
        """Mid price, or ``None`` when either side is empty."""
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / Decimal(2)

    @property
    def spread_bps(self) -> Optional[Decimal]:
        """Spread in basis points of the mid, or ``None``."""
        mid = self.mid
        bid, ask = self.best_bid, self.best_ask
        if mid is None or mid <= 0 or bid is None or ask is None:
            return None
        return (ask - bid) / mid * Decimal(10000)


__all__ = [
    "CANDLES_PER_PAGE",
    "CandleFeed",
    "DepthBook",
    "FeedStatus",
    "MarketFeed",
    "TIMEFRAME_SECONDS",
    "parse_candle_history",
    "timeframe_seconds",
]
