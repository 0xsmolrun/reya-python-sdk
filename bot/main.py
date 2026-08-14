"""Command-line entry point for the Reya 15m FVG bot.

Examples::

    # Paper trade ETHRUSDPERP against live prices, with the Matrix dashboard
    python -m bot.main --mode paper

    # Live trade on testnet, headless (logs only)
    python -m bot.main --mode live --no-ui

    # Backtest the last 2000 15m candles
    python -m bot.main --mode backtest --bars 2000

    # Validate configuration and connectivity without trading
    python -m bot.main --check
"""

from typing import List, Optional, Sequence

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from pathlib import Path

from bot.backtest import Backtester
from bot.config import BotConfig, ConfigError, load_config
from bot.engine import TradingEngine
from bot.exchange.base import ExchangeError
from bot.exchange.data_feed import CandleFeed
from bot.exchange.reya_client import MarketDataClient
from bot.strategy.indicators import Candle
from bot.utils.logging import setup_logging

logger = logging.getLogger("bot.main")

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_RUNTIME_ERROR = 3
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="python -m bot.main",
        description="Reya Network 15m Fair Value Gap trading bot with a Matrix terminal UI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=("live", "paper", "backtest"),
        help="live signs and sends orders; paper simulates fills against live prices; "
        "backtest replays historical candles. Defaults to the value in config.yaml.",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Symbol to trade, repeatable (e.g. --symbol ETHRUSDPERP --symbol BTCRUSDPERP).",
    )
    parser.add_argument("--config", help="Path to config.yaml (default: ./config.yaml if present).")
    parser.add_argument("--no-ui", action="store_true", help="Run headless, logging to the console instead.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="In live mode, log orders instead of sending them.",
    )
    parser.add_argument(
        "--autostart",
        action="store_true",
        help="Arm the strategy immediately instead of waiting for the 's' key.",
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING or ERROR.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration, load markets and candles, then exit.",
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=None,
        help="Backtest only: number of historical candles to load (default: strategy.history_bars).",
    )
    parser.add_argument(
        "--candles",
        help="Backtest only: JSON file of candles to replay instead of fetching them.",
    )
    parser.add_argument(
        "--save-candles",
        help="Backtest only: write the fetched candles to this JSON file for reuse.",
    )
    return parser


# ----------------------------------------------------------------------
# Candle IO for the backtester
# ----------------------------------------------------------------------
def load_candles_file(path: str) -> List[Candle]:
    """Read candles from a JSON file.

    The file may hold either a list of ``{t, o, h, l, c}`` objects or the raw
    column-oriented shape returned by ``/v2/candleHistory``.

    Raises:
        ConfigError: If the file is missing or its shape is not recognised.
    """
    target = Path(path)
    if not target.exists():
        raise ConfigError(f"Candle file not found: {target}")

    with target.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if isinstance(payload, dict) and "t" in payload:
        rows = zip(payload["t"], payload["o"], payload["h"], payload["l"], payload["c"])
        candles = [Candle.create(t, o, h, low, c) for t, o, h, low, c in rows]
    elif isinstance(payload, list):
        candles = [Candle.create(row["t"], row["o"], row["h"], row["l"], row["c"]) for row in payload]
    else:
        raise ConfigError(f"Unrecognised candle file format in {target}")

    candles.sort(key=lambda candle: candle.timestamp)
    logger.info("Loaded %d candles from %s", len(candles), target)
    return candles


def save_candles_file(path: str, candles: Sequence[Candle]) -> None:
    """Write candles to a JSON file so a backtest can be replayed offline."""
    rows = [
        {
            "t": candle.timestamp,
            "o": str(candle.open),
            "h": str(candle.high),
            "l": str(candle.low),
            "c": str(candle.close),
        }
        for candle in candles
    ]
    Path(path).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    logger.info("Wrote %d candles to %s", len(rows), path)


async def fetch_candles(config: BotConfig, symbol: str, bars: int) -> List[Candle]:
    """Download ``bars`` candles for ``symbol`` from the public API."""
    client = MarketDataClient(config.exchange.api_url)
    try:
        feed = CandleFeed(client, symbol, timeframe=config.strategy.timeframe, history_bars=bars)
        return await feed.warmup()
    finally:
        await client.close()


# ----------------------------------------------------------------------
# Modes
# ----------------------------------------------------------------------
async def run_backtest(config: BotConfig, args: argparse.Namespace) -> int:
    """Replay historical candles and print the performance report."""
    symbol = config.primary_symbol
    bars = args.bars or config.strategy.history_bars

    if args.candles:
        candles = load_candles_file(args.candles)
    else:
        logger.info("Fetching %d %s candles for %s", bars, config.strategy.timeframe, symbol)
        candles = await fetch_candles(config, symbol, bars)
        if args.save_candles:
            save_candles_file(args.save_candles, candles)

    meta = None
    if not args.candles:
        # Real tick and step sizes make the sizing realistic.
        client = MarketDataClient(config.exchange.api_url)
        try:
            markets = await client.load_markets()
            meta = markets.get(symbol)
        except ExchangeError as exc:
            logger.warning("Could not load market definitions (%s); using defaults", exc)
        finally:
            await client.close()

    result = Backtester(config, symbol, meta).run(candles)
    print()  # noqa: T201 - the report is the point of this mode
    print(result.summary())  # noqa: T201
    print()  # noqa: T201
    return EXIT_OK


async def run_check(config: BotConfig) -> int:
    """Validate configuration and connectivity without placing any orders."""
    logger.info("Configuration:")
    for line in json.dumps(config.to_dict(), indent=2).splitlines():
        logger.info("  %s", line)

    client = MarketDataClient(config.exchange.api_url)
    try:
        markets = await client.load_markets()
        logger.info("Market definitions loaded: %d symbols", len(markets))
        for symbol in config.symbols:
            meta = markets.get(symbol)
            if meta is None:
                logger.error("Symbol %s is not listed on this network", symbol)
                return EXIT_CONFIG_ERROR
            logger.info(
                "  %s: tick %s, step %s, min qty %s, max leverage %sx",
                symbol,
                meta.tick_size,
                meta.qty_step_size,
                meta.min_order_qty,
                meta.max_leverage,
            )

        feed = CandleFeed(client, config.primary_symbol, config.strategy.timeframe, history_bars=200)
        candles = await feed.warmup()
        logger.info(
            "Candle history OK: %d closed %s bars, latest close %s",
            len(candles),
            config.strategy.timeframe,
            candles[-1].close if candles else "n/a",
        )
    finally:
        await client.close()

    logger.info("Check passed")
    return EXIT_OK


async def run_trading(config: BotConfig, use_ui: bool) -> int:
    """Start the engine and either show the dashboard or run headless."""
    engine = TradingEngine(config)
    try:
        await engine.start()
    except (ExchangeError, ConfigError) as exc:
        logger.error("Startup failed: %s", exc)
        with contextlib.suppress(Exception):
            await engine.stop()
        return EXIT_RUNTIME_ERROR

    try:
        if use_ui:
            # Imported here so headless runs never need Textual installed.
            from bot.ui.matrix_app import MatrixApp  # pylint: disable=import-outside-toplevel

            await MatrixApp(engine).run_async()
        else:
            await run_headless(engine)
    finally:
        await engine.stop()
    return EXIT_OK


async def run_headless(engine: TradingEngine) -> None:
    """Run until SIGINT or SIGTERM, logging a heartbeat as it goes."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows lacks add_signal_handler
            loop.add_signal_handler(sig, stop.set)

    logger.info("Running headless. Press Ctrl+C to stop.")
    while not stop.is_set():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=60)
        if stop.is_set():
            break
        state = engine.state
        logger.info(
            "Heartbeat | %s | equity %s | session PnL %s | %d position(s) | %d trade(s)",
            state.status_label,
            state.equity,
            state.session_pnl,
            len(state.open_positions),
            len(state.trades),
        )
    logger.info("Shutdown signal received")


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
async def async_main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments, load configuration and dispatch to the chosen mode."""
    args = build_parser().parse_args(argv)

    try:
        config = load_config(
            args.config,
            mode=args.mode,
            symbols=args.symbols,
            log_level=args.log_level,
            dry_run=True if args.dry_run else None,
        )
    except ConfigError as exc:
        # Logging is not configured yet, so this goes straight to stderr.
        print(f"Configuration error: {exc}", file=sys.stderr)  # noqa: T201
        return EXIT_CONFIG_ERROR

    if args.autostart:
        config.autostart = True

    use_ui = not args.no_ui and config.mode in ("live", "paper") and not args.check
    setup_logging(level=config.log_level, log_file=config.log_file, console=not use_ui)

    logger.info(
        "Reya 15m FVG bot | mode=%s network=%s symbols=%s",
        config.mode,
        config.exchange.network_name,
        ", ".join(config.symbols),
    )
    if config.mode == "live" and not config.execution.dry_run:
        logger.warning("LIVE MODE: orders will be signed and sent with real funds")

    try:
        if args.check:
            return await run_check(config)
        if config.mode == "backtest":
            return await run_backtest(config, args)
        return await run_trading(config, use_ui)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return EXIT_CONFIG_ERROR
    except ExchangeError as exc:
        logger.error("Exchange error: %s", exc)
        return EXIT_RUNTIME_ERROR
    except Exception as exc:  # noqa: BLE001 - top-level guard
        logger.exception("Fatal error: %s", exc)
        return EXIT_RUNTIME_ERROR


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Synchronous wrapper around :func:`async_main`."""
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)  # noqa: T201
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
