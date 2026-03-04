"""
Orders tools for ibkr_mcp.

Today's executions and open orders. Read-only — no order placement yet.
"""

import math
from decimal import Decimal
from typing import Optional

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from app import mcp
from core.connection import get_ib, resolve_account
from core.errors import handle_ib_error
from core.formatting import fmt_price, fmt_pnl, fmt_shares


# --- Input Models ---

class TradesInput(BaseModel):
    account: Optional[str] = Field(
        default=None,
        description="IBKR account ID. Leave blank for primary."
    )
    symbol_filter: Optional[str] = Field(
        default=None,
        description="Filter by symbol substring (e.g., 'AAPL')."
    )


class OrdersInput(BaseModel):
    account: Optional[str] = Field(
        default=None,
        description="IBKR account ID. Leave blank for primary."
    )


# IB uses a sentinel for unset prices (max float value)
_UNSET_DOUBLE = 1.7976931348623157e+308


def _price_or_none(val: float) -> float | None:
    """Return None for IB's UNSET_DOUBLE sentinel and NaN."""
    if val is None or val >= _UNSET_DOUBLE or (isinstance(val, float) and math.isnan(val)):
        return None
    return val


# --- Tools ---

@mcp.tool(
    name="ibkr_get_trades",
    annotations={
        "title": "Get Today's Executions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_trades(params: TradesInput, ctx: Context) -> str:
    """Get today's trade executions: what filled, at what price, and commissions.

    Shows all fills from this IB Gateway session — symbol, side, quantity,
    fill price, time, and commission paid.

    Args:
        params (TradesInput): Optional account and symbol filter.

    Returns:
        str: Markdown table of today's executions.
    """
    try:
        ib = get_ib(ctx)
        account = resolve_account(ctx, params.account)

        fills = ib.fills()

        # Filter by account
        fills = [f for f in fills if f.execution.acctNumber == account]

        # Filter by symbol if requested
        if params.symbol_filter:
            filt = params.symbol_filter.upper()
            fills = [f for f in fills if filt in f.contract.symbol.upper()]

        if not fills:
            msg = f"No executions for {account}"
            if params.symbol_filter:
                msg += f" matching '{params.symbol_filter}'"
            msg += " this session."
            return msg

        # Sort by time descending (most recent first)
        fills.sort(key=lambda f: f.time, reverse=True)

        total_commission = Decimal("0")

        lines = [
            f"# Executions: {account}",
            "",
            "| Time | Symbol | Side | Qty | Price | Commission |",
            "|------|--------|------|-----|-------|------------|",
        ]

        for f in fills:
            ex = f.execution
            cr = f.commissionReport
            symbol = f.contract.symbol
            side = ex.side
            qty = fmt_shares(ex.shares)
            price = fmt_price(ex.price, "")
            time_str = f.time.strftime("%H:%M:%S") if f.time else "N/A"

            comm = Decimal("0")
            comm_str = "N/A"
            if cr and cr.commission and not math.isnan(cr.commission):
                comm = Decimal(str(cr.commission))
                total_commission += comm
                comm_str = fmt_price(comm, cr.currency or "")

            lines.append(
                f"| {time_str} | {symbol} | {side} | {qty} | "
                f"{price} | {comm_str} |"
            )

        lines.extend([
            "",
            f"**Total Executions**: {len(fills)}",
            f"**Total Commissions**: {fmt_price(total_commission)}",
        ])

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, "fetching executions")


@mcp.tool(
    name="ibkr_get_orders",
    annotations={
        "title": "Get Open Orders",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_orders(params: OrdersInput, ctx: Context) -> str:
    """Get all open/pending orders: what's waiting to fill.

    Shows order type, side, quantity, limit price, status, and time-in-force
    for all active orders.

    Args:
        params (OrdersInput): Optional account filter.

    Returns:
        str: Markdown table of open orders, or a message if none exist.
    """
    try:
        ib = get_ib(ctx)
        account = resolve_account(ctx, params.account)

        trades = ib.openTrades()

        # Filter by account
        trades = [t for t in trades if t.order.account == account]

        if not trades:
            return f"No open orders for {account}."

        lines = [
            f"# Open Orders: {account}",
            "",
            "| Symbol | Side | Qty | Type | Limit | Stop | TIF | Status |",
            "|--------|------|-----|------|-------|------|-----|--------|",
        ]

        for t in trades:
            o = t.order
            c = t.contract
            status = t.orderStatus.status if t.orderStatus else "Unknown"
            filled = t.filled()
            remaining = t.remaining()

            lmt = _price_or_none(o.lmtPrice)
            stop = _price_or_none(o.auxPrice)

            lmt_str = fmt_price(lmt, "") if lmt else "—"
            stop_str = fmt_price(stop, "") if stop else "—"

            qty_str = fmt_shares(o.totalQuantity)
            if filled > 0:
                qty_str += f" ({fmt_shares(filled)} filled)"

            lines.append(
                f"| {c.symbol} | {o.action} | {qty_str} | "
                f"{o.orderType} | {lmt_str} | {stop_str} | "
                f"{o.tif} | {status} |"
            )

        lines.append(f"\n**Open Orders**: {len(trades)}")

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, "fetching open orders")
