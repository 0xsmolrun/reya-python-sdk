"""Matrix palette and small Rich helpers shared by every panel.

Pure black ground, phosphor green foreground, amber for warnings and red for
losses and errors. Nothing else, so the screen reads as one system.
"""

from typing import Optional

from decimal import Decimal

from rich.style import Style
from rich.text import Text

from bot.utils.numbers import ZERO, format_signed

#: Core phosphor greens, brightest first.
NEON = "#00FF41"
BRIGHT = "#39FF14"
GREEN = "#0FBF3F"
DIM = "#0B6623"
DARK = "#063D14"
DARKEST = "#04220B"

BLACK = "#000000"
AMBER = "#FFD700"
RED = "#FF3131"
CYAN = "#00E5D0"
WHITE = "#D7FFD7"

#: Semantic aliases used across the panels.
UP = NEON
DOWN = RED
LABEL = DIM
VALUE = BRIGHT
MUTED = DARK

STYLE_LABEL = Style(color=LABEL)
STYLE_VALUE = Style(color=VALUE, bold=True)
STYLE_UP = Style(color=UP, bold=True)
STYLE_DOWN = Style(color=DOWN, bold=True)
STYLE_MUTED = Style(color=MUTED)


def pnl_style(value: Optional[Decimal]) -> str:
    """Colour for a PnL figure: green when up, red when down, dim at zero."""
    if value is None or value == ZERO:
        return MUTED
    return UP if value > ZERO else DOWN


def pnl_text(amount: Optional[Decimal], places: int = 2, suffix: str = "") -> Text:
    """Render a signed PnL number in its outcome colour."""
    return Text(f"{format_signed(amount, places)}{suffix}", style=pnl_style(amount))


def label(text: str) -> Text:
    """Render a dim field label."""
    return Text(text, style=STYLE_LABEL)


def bright(text: str, colour: Optional[str] = None) -> Text:
    """Render a bright field value."""
    return Text(text, style=Style(color=colour or VALUE, bold=True))


def side_style(is_long: bool) -> str:
    """Colour for a trade direction."""
    return UP if is_long else DOWN


def state_style(state: str) -> str:
    """Colour for an FVG lifecycle state."""
    return {
        "FRESH": NEON,
        "TESTED": AMBER,
        "MITIGATED": MUTED,
        "EXPIRED": MUTED,
    }.get(state, GREEN)


def status_style(status: str) -> str:
    """Colour for the run-state label in the status bar."""
    return {
        "ARMED": NEON,
        "PAUSED": AMBER,
        "IDLE": DIM,
        "STOPPING": RED,
    }.get(status, GREEN)


__all__ = [
    "AMBER",
    "BLACK",
    "BRIGHT",
    "CYAN",
    "DARK",
    "DARKEST",
    "DIM",
    "DOWN",
    "GREEN",
    "LABEL",
    "MUTED",
    "NEON",
    "RED",
    "STYLE_LABEL",
    "STYLE_MUTED",
    "STYLE_VALUE",
    "UP",
    "VALUE",
    "WHITE",
    "label",
    "pnl_style",
    "pnl_text",
    "side_style",
    "state_style",
    "status_style",
    "bright",
]
