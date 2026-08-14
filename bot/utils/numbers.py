"""Decimal helpers shared by the strategy, risk and execution layers.

Every price and quantity in this bot is a :class:`~decimal.Decimal`. The Reya API
speaks in decimal strings, and binary floats silently break exchange constraints
(tick size, quantity step), so the conversion to ``float`` only ever happens at
the presentation or statistics boundary.
"""

from typing import Iterable, Optional, Union

from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation

Number = Union[str, int, float, Decimal]

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10000)


def to_decimal(value: Number, default: Optional[Decimal] = None) -> Decimal:
    """Convert an API value to :class:`Decimal`.

    Floats are routed through ``repr`` so that ``0.1`` becomes ``Decimal("0.1")``
    rather than the exact binary expansion.

    Args:
        value: Value to convert.
        default: Returned when the value cannot be parsed. If ``None`` a parse
            failure raises.

    Returns:
        The parsed decimal.

    Raises:
        ValueError: If the value is unparseable and no default was supplied.
    """
    if isinstance(value, Decimal):
        return value
    try:
        if isinstance(value, float):
            return Decimal(repr(value))
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        if default is None:
            raise ValueError(f"Cannot parse {value!r} as Decimal")
        return default


def optional_decimal(value: Optional[Number]) -> Optional[Decimal]:
    """Convert an optional API value to :class:`Decimal`, preserving ``None``."""
    if value is None:
        return None
    return to_decimal(value, default=ZERO)


def dec_to_str(value: Decimal) -> str:
    """Render a decimal for the API without scientific notation.

    ``str(Decimal("1E-3"))`` yields ``"0.001"`` only after normalisation, and
    ``Decimal("1000").normalize()`` yields ``"1E+3"`` which the API rejects.
    Fixed-point formatting avoids both traps.
    """
    return f"{value:f}"


def quantize_to_step(value: Decimal, step: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    """Snap ``value`` onto a multiple of ``step``.

    Args:
        value: Raw value.
        step: Exchange tick size or quantity step. Non-positive steps are treated
            as "no constraint" and the value is returned untouched.
        rounding: A :mod:`decimal` rounding mode.

    Returns:
        The largest/nearest multiple of ``step`` per the rounding mode.
    """
    if step <= ZERO:
        return value
    steps = (value / step).to_integral_value(rounding=rounding)
    return (steps * step).quantize(step)


def round_price(value: Decimal, tick: Decimal, rounding: str = ROUND_HALF_UP) -> Decimal:
    """Round a price onto the market tick size."""
    return quantize_to_step(value, tick, rounding=rounding)


def round_qty_down(value: Decimal, step: Decimal) -> Decimal:
    """Round a quantity down onto the market step size.

    Rounding down is deliberate: it keeps the realised risk at or below the
    configured budget rather than above it.
    """
    return quantize_to_step(value, step, rounding=ROUND_DOWN)


def apply_bps(price: Decimal, bps: Decimal, is_buy: bool) -> Decimal:
    """Offset a price by ``bps`` basis points in the aggressive direction.

    Buys are pushed up and sells are pushed down, which is what makes an IOC
    limit order marketable.
    """
    factor = ONE + (bps / BPS) if is_buy else ONE - (bps / BPS)
    return price * factor


def safe_div(numerator: Decimal, denominator: Decimal, default: Decimal = ZERO) -> Decimal:
    """Divide, returning ``default`` instead of raising on a zero denominator."""
    if denominator == ZERO:
        return default
    return numerator / denominator


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(high, value))


def decimal_sum(values: Iterable[Decimal]) -> Decimal:
    """Sum decimals starting from an exact zero."""
    total = ZERO
    for value in values:
        total += value
    return total


def format_price(value: Optional[Decimal], places: int = 2) -> str:
    """Format a price for display, with ``--`` for missing values."""
    if value is None:
        return "--"
    quant = Decimal(1).scaleb(-places)
    return f"{value.quantize(quant, rounding=ROUND_HALF_UP):,f}"


def format_qty(value: Optional[Decimal], places: int = 4) -> str:
    """Format a quantity for display, with ``--`` for missing values."""
    if value is None:
        return "--"
    quant = Decimal(1).scaleb(-places)
    return f"{value.quantize(quant, rounding=ROUND_DOWN):f}"


def format_signed(value: Optional[Decimal], places: int = 2) -> str:
    """Format a PnL-style number with an explicit sign."""
    if value is None:
        return "--"
    quant = Decimal(1).scaleb(-places)
    rounded = value.quantize(quant, rounding=ROUND_HALF_UP)
    sign = "+" if rounded > ZERO else ""
    return f"{sign}{rounded:,f}"


__all__ = [
    "BPS",
    "ONE",
    "ROUND_DOWN",
    "ROUND_HALF_UP",
    "ROUND_UP",
    "ZERO",
    "Number",
    "apply_bps",
    "clamp",
    "dec_to_str",
    "decimal_sum",
    "format_price",
    "format_qty",
    "format_signed",
    "optional_decimal",
    "quantize_to_step",
    "round_price",
    "round_qty_down",
    "safe_div",
    "to_decimal",
]
