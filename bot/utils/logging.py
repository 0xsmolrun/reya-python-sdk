"""Logging setup: rotating file logs, colourised console, and a UI ring buffer.

The Matrix TUI owns the terminal, so when it is running the console handler is
suppressed and the UI reads from :class:`RingBufferHandler` instead.
"""

from typing import Deque, Iterable, List, Optional

import logging
import logging.handlers
import os
import time
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# Matrix palette reused by both the console formatter and the TUI.
LEVEL_COLOURS = {
    "DEBUG": "\033[38;5;28m",  # deep green
    "INFO": "\033[38;5;46m",  # neon green
    "WARNING": "\033[38;5;226m",  # amber
    "ERROR": "\033[38;5;196m",  # red
    "CRITICAL": "\033[1;38;5;196m",
}
RESET = "\033[0m"
DIM = "\033[38;5;22m"

# Textual/Rich markup colours keyed by level, used by the log panel.
LEVEL_STYLES = {
    "DEBUG": "#1f6f2f",
    "INFO": "#00ff41",
    "WARNING": "#ffd700",
    "ERROR": "#ff3131",
    "CRITICAL": "bold #ff3131",
}


@dataclass(frozen=True)
class LogRecordView:
    """A flattened log record for rendering in the UI."""

    timestamp: float
    level: str
    name: str
    message: str

    @property
    def clock(self) -> str:
        """``HH:MM:SS`` rendering of the record time."""
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))

    @property
    def style(self) -> str:
        """Rich style for this record's level."""
        return LEVEL_STYLES.get(self.level, "#00ff41")


class RingBufferHandler(logging.Handler):
    """Keeps the most recent log records in memory for the TUI log panel."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._records: Deque[LogRecordView] = deque(maxlen=capacity)
        self._revision = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message} | {logging.Formatter().formatException(record.exc_info)}"
            self._records.append(
                LogRecordView(
                    timestamp=record.created,
                    level=record.levelname,
                    name=record.name,
                    message=message,
                )
            )
            self._revision += 1
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)

    @property
    def revision(self) -> int:
        """Monotonic counter so the UI can skip redraws when nothing changed."""
        return self._revision

    def resize(self, capacity: int) -> None:
        """Change how many records are retained, keeping the most recent ones."""
        self._records = deque(self._records, maxlen=capacity)

    def tail(self, count: int) -> List[LogRecordView]:
        """Return the last ``count`` records, oldest first."""
        if count <= 0:
            return []
        records = list(self._records)
        return records[-count:]

    def all(self) -> Iterable[LogRecordView]:
        """Iterate over every buffered record."""
        return tuple(self._records)


class MatrixConsoleFormatter(logging.Formatter):
    """ANSI formatter that keeps console output on-theme when the TUI is off."""

    def format(self, record: logging.LogRecord) -> str:
        colour = LEVEL_COLOURS.get(record.levelname, LEVEL_COLOURS["INFO"])
        clock = time.strftime("%H:%M:%S", time.localtime(record.created))
        name = record.name.replace("bot.", "")
        message = super().format(record)
        return f"{DIM}{clock}{RESET} {colour}{record.levelname[:4]:<4}{RESET} {DIM}{name:<18}{RESET} {colour}{message}{RESET}"


@lru_cache(maxsize=1)
def get_ring_buffer() -> RingBufferHandler:
    """Return the process-wide ring buffer handler, creating it on first use."""
    return RingBufferHandler()


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = "logs/reya_fvg_bot.log",
    console: bool = True,
    capacity: int = 500,
) -> RingBufferHandler:
    """Configure root logging for the bot.

    Args:
        level: Log level name for the bot's own loggers.
        log_file: Path of the rotating log file, or ``None`` to disable it.
        console: Whether to attach the colourised stream handler. The TUI passes
            ``False`` because it repaints the whole screen.
        capacity: Number of records the UI ring buffer retains.

    Returns:
        The ring buffer handler the UI reads from.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    ring = get_ring_buffer()
    ring.resize(capacity)
    ring.setLevel(numeric_level)
    root.addHandler(ring)

    if console:
        stream = logging.StreamHandler()
        stream.setLevel(numeric_level)
        stream.setFormatter(MatrixConsoleFormatter("%(message)s"))
        root.addHandler(stream)

    if log_file:
        path = Path(log_file)
        if path.parent and str(path.parent) not in ("", "."):
            os.makedirs(path.parent, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)-28s %(message)s"),
        )
        root.addHandler(file_handler)

    # Third-party chatter would otherwise drown the strategy log.
    for noisy in ("websocket", "urllib3", "asyncio", "aiohttp", "markdown_it"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("reya_trading.client").setLevel(logging.WARNING)
    logging.getLogger("reya.websocket").setLevel(logging.WARNING)

    return ring


__all__ = [
    "LEVEL_STYLES",
    "LogRecordView",
    "MatrixConsoleFormatter",
    "RingBufferHandler",
    "get_ring_buffer",
    "setup_logging",
]
