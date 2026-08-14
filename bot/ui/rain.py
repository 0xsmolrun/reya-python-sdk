"""The digital rain: falling katakana columns that intensify on fills.

Implemented as a Textual widget that paints one line at a time via
``render_line``. Only cells actually covered by a drop are computed each frame,
so a full-height column costs a handful of segments rather than a screen
repaint.

The rain reacts to the trading: :meth:`MatrixRain.pulse` speeds it up and
brightens it briefly when an order fills or price moves sharply, then it decays
back to its resting cadence.
"""

from typing import Dict, List, Optional, Tuple

import random
from dataclasses import dataclass, field

from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget

from bot.ui import theme

#: Half-width katakana plus digits, the canonical rain alphabet.
GLYPHS = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜｦﾝ0123456789"

#: Trail colours from the bright head backwards into the dark.
TRAIL = [theme.WHITE, theme.BRIGHT, theme.NEON, theme.GREEN, theme.DIM, theme.DARK, theme.DARKEST]


@dataclass
class Drop:
    """One falling column of glyphs."""

    #: Fractional row of the leading glyph; fractional so speeds can differ.
    head: float
    speed: float
    length: int
    glyphs: List[str] = field(default_factory=list)

    def advance(self, multiplier: float) -> None:
        """Move the drop down the screen and churn its glyphs."""
        self.head += self.speed * multiplier
        # Occasionally mutate a glyph so the trail shimmers rather than scrolls.
        if self.glyphs and random.random() < 0.3:  # noqa: S311 - decorative only
            self.glyphs[random.randrange(len(self.glyphs))] = random.choice(GLYPHS)  # noqa: S311


class MatrixRain(Widget):
    """A column of animated digital rain."""

    DEFAULT_CSS = """
    MatrixRain {
        background: #000000;
    }
    """

    def __init__(
        self,
        density: float = 0.6,
        base_speed: float = 0.5,
        name: Optional[str] = None,
        id: Optional[str] = None,  # noqa: A002 - Textual's parameter name
        classes: Optional[str] = None,
    ) -> None:
        """Create a rain widget.

        Args:
            density: Fraction of columns carrying a drop at rest, 0..1.
            base_speed: Rows advanced per frame at rest.
            name: Textual widget name.
            id: Textual widget id.
            classes: Textual CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self.density = max(0.0, min(1.0, density))
        self.base_speed = base_speed
        self._drops: Dict[int, Drop] = {}
        self._cells: Dict[Tuple[int, int], Tuple[str, Style]] = {}
        #: 0 at rest, decaying towards 0 after a pulse.
        self.intensity = 0.0

    def pulse(self, amount: float = 1.0) -> None:
        """Intensify the rain briefly, e.g. when an order fills."""
        self.intensity = min(1.0, self.intensity + amount)

    def tick(self) -> None:
        """Advance one animation frame and request a repaint."""
        width, height = self.size.width, self.size.height
        if width <= 0 or height <= 0:
            return

        multiplier = 1.0 + self.intensity * 2.0
        self.intensity = max(0.0, self.intensity - 0.05)

        self._spawn(width, height, multiplier)
        for column in list(self._drops):
            drop = self._drops[column]
            drop.advance(multiplier)
            # Retire the drop once its whole trail is below the screen.
            if drop.head - drop.length > height:
                del self._drops[column]

        self._rasterise(width, height)
        self.refresh()

    def _spawn(self, width: int, height: int, multiplier: float) -> None:
        """Start new drops in empty columns, in proportion to the density."""
        target = max(1, int(width * self.density))
        chance = 0.08 * multiplier
        for column in range(width):
            if len(self._drops) >= target and self.intensity <= 0:
                return
            if column in self._drops:
                continue
            if random.random() > chance:  # noqa: S311 - decorative only
                continue
            length = random.randint(max(3, height // 4), max(4, height))  # noqa: S311
            self._drops[column] = Drop(
                head=float(-random.randint(0, height // 2)),  # noqa: S311
                speed=self.base_speed * random.uniform(0.6, 1.8),  # noqa: S311
                length=length,
                glyphs=[random.choice(GLYPHS) for _ in range(length)],  # noqa: S311
            )

    def _rasterise(self, width: int, height: int) -> None:
        """Flatten the drops into a sparse cell map for :meth:`render_line`."""
        cells: Dict[Tuple[int, int], Tuple[str, Style]] = {}
        for column, drop in self._drops.items():
            head_row = int(drop.head)
            for offset in range(drop.length):
                row = head_row - offset
                if not 0 <= row < height or not 0 <= column < width:
                    continue
                # Map the position in the trail onto the fading palette.
                shade = min(len(TRAIL) - 1, int(offset / max(1, drop.length) * len(TRAIL)))
                if offset == 0:
                    shade = 0
                glyph = drop.glyphs[offset] if offset < len(drop.glyphs) else "0"
                cells[(row, column)] = (glyph, Style(color=TRAIL[shade], bold=offset <= 1))
        self._cells = cells

    def render_line(self, y: int) -> Strip:
        """Render one row of the rain."""
        width = self.size.width
        if width <= 0:
            return Strip.blank(0)

        segments: List[Segment] = []
        blank = Style(color=theme.DARKEST)
        for column in range(width):
            cell = self._cells.get((y, column))
            if cell is None:
                segments.append(Segment(" ", blank))
            else:
                segments.append(Segment(cell[0], cell[1]))
        return Strip(segments, width)


__all__ = ["GLYPHS", "MatrixRain"]
