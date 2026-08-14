"""Optional Telegram / Discord alerting.

Alerts are strictly best-effort: a failed webhook must never interrupt trading,
so every send is wrapped and errors are logged at warning level and dropped.
Delivery happens on a background task so a slow endpoint cannot stall the engine.
"""

from typing import List, Optional

import asyncio
import logging

from bot.config import NotifierConfig

logger = logging.getLogger("bot.notifier")

try:  # aiohttp arrives transitively via aiohttp-retry, but degrade gracefully.
    import aiohttp
except ImportError:  # pragma: no cover - only hit in a stripped environment
    aiohttp = None  # type: ignore[assignment]

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT_S = 10


class Notifier:
    """Fire-and-forget alert dispatcher."""

    def __init__(self, config: NotifierConfig) -> None:
        """Create a notifier.

        Args:
            config: Channel credentials and per-event toggles. When disabled or
                unconfigured, every send becomes a no-op.
        """
        self.config = config
        self._session: Optional["aiohttp.ClientSession"] = None
        self._tasks: set = set()

    @property
    def enabled(self) -> bool:
        """Whether alerts will actually be sent."""
        return bool(self.config.enabled and self.config.has_target and aiohttp is not None)

    async def start(self) -> None:
        """Open the HTTP session used for deliveries."""
        if self.enabled and self._session is None:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
            self._session = aiohttp.ClientSession(timeout=timeout)
            logger.info("Alerts enabled (%s)", ", ".join(self._channels()))

    async def close(self) -> None:
        """Wait briefly for in-flight sends, then close the session."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _channels(self) -> List[str]:
        """Names of the configured delivery channels."""
        channels = []
        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            channels.append("telegram")
        if self.config.discord_webhook_url:
            channels.append("discord")
        return channels

    def _wants(self, event: str) -> bool:
        """Whether this event kind is enabled."""
        return {
            "entry": self.config.notify_entries,
            "exit": self.config.notify_exits,
            "error": self.config.notify_errors,
            "halt": self.config.notify_halts,
        }.get(event, True)

    def notify(self, event: str, message: str) -> None:
        """Queue an alert without awaiting it.

        Args:
            event: One of ``entry``, ``exit``, ``error``, ``halt``.
            message: Body text.
        """
        if not self.enabled or not self._wants(event):
            return
        task = asyncio.create_task(self.send(event, message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send(self, event: str, message: str) -> None:
        """Deliver an alert to every configured channel, swallowing failures."""
        if not self.enabled or self._session is None:
            return

        prefix = {"entry": "🟢", "exit": "🔵", "error": "🔴", "halt": "⛔"}.get(event, "▫️")
        text = f"{prefix} [REYA FVG] {message}"

        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            await self._post_json(
                TELEGRAM_API.format(token=self.config.telegram_bot_token),
                {"chat_id": self.config.telegram_chat_id, "text": text},
                "telegram",
            )
        if self.config.discord_webhook_url:
            await self._post_json(self.config.discord_webhook_url, {"content": text}, "discord")

    async def _post_json(self, url: str, payload: dict, channel: str) -> None:
        """POST a JSON body, logging rather than raising on failure."""
        if self._session is None:
            return
        try:
            async with self._session.post(url, json=payload) as response:
                if response.status >= 400:
                    body = (await response.text())[:200]
                    logger.warning("%s alert failed (HTTP %s): %s", channel, response.status, body)
        except Exception as exc:  # noqa: BLE001 - alerts must never break trading
            logger.warning("%s alert failed: %s", channel, exc)


__all__ = ["Notifier"]
