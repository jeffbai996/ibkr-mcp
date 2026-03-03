"""
Response formatting utilities for ibkr_mcp.

All tools return either Markdown (default, human-readable) or JSON (machine-readable).
This module provides helpers to format financial data consistently.
"""

import json
import math
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def _is_nan(value: float | Decimal) -> bool:
    """Check for NaN in both float and Decimal. IB uses NaN for unset fields."""
    if isinstance(value, Decimal):
        return value.is_nan()
    return isinstance(value, float) and math.isnan(value)


def fmt_price(value: float | Decimal | None, currency: str = "USD") -> str:
    """Format a price value: $1,234.56 USD"""
    if value is None or _is_nan(value):
        return "N/A"
    d = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    formatted = f"${d:,.2f}"
    return f"{formatted} {currency}" if currency else formatted


def fmt_pct(value: float | Decimal | None) -> str:
    """Format a percentage: 12.34%"""
    if value is None or _is_nan(value):
        return "N/A"
    d = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{d:+.2f}%" if d != 0 else "0.00%"


def fmt_shares(value: float | Decimal | None) -> str:
    """Format share count: 1,234"""
    if value is None or _is_nan(value):
        return "N/A"
    return f"{int(value):,}" if value == int(value) else f"{value:,.2f}"


def fmt_pnl(value: float | Decimal | None, currency: str = "USD") -> str:
    """Format P&L with sign: +$1,234.56 or -$567.89"""
    if value is None or _is_nan(value):
        return "N/A"
    d = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "+" if d > 0 else ""
    return f"{sign}${d:,.2f} {currency}"


def to_json(data: Any) -> str:
    """Serialize data to JSON, handling Decimal types."""
    return json.dumps(data, indent=2, default=_json_default)


def _json_default(obj: Any) -> Any:
    """JSON serializer for types not handled by default."""
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
