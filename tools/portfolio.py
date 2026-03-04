"""
Portfolio tools for ibkr_mcp.

Position-level data: what you own, what it's worth, how it's performing.
"""

import asyncio
import math
from decimal import Decimal, InvalidOperation
from typing import Optional

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from app import mcp
from core.connection import get_ib, resolve_account
from core.errors import handle_ib_error
from core.formatting import fmt_price, fmt_pct, fmt_pnl, fmt_shares


# --- Input Models ---

class PortfolioInput(BaseModel):
    account: Optional[str] = Field(
        default=None,
        description="IBKR account ID. Leave blank for primary."
    )
    symbol_filter: Optional[str] = Field(
        default=None,
        description="Filter positions by symbol substring (e.g., 'MSFT', 'NVD')."
    )


class PositionDetailInput(BaseModel):
    symbol: str = Field(
        ..., description="Stock symbol to look up (e.g., 'MSFT', 'AAPL')."
    )
    account: Optional[str] = Field(
        default=None,
        description="IBKR account ID. Leave blank for primary."
    )


# --- Tools ---

@mcp.tool(
    name="ibkr_get_positions",
    annotations={
        "title": "Get Portfolio Positions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_positions(params: PortfolioInput, ctx: Context) -> str:
    """Get all current positions with market value and P&L.

    Returns every position in the account with live pricing, unrealized P&L,
    and percentage weight. Optionally filter by symbol.

    Args:
        params (PortfolioInput): Account ID and optional symbol filter.

    Returns:
        str: Markdown table of positions with price, value, P&L, and weight.
    """
    try:
        ib = get_ib(ctx)
        account = resolve_account(ctx, params.account)

        # ib.portfolio() returns PortfolioItem objects with live data
        # Each has: contract, position, marketPrice, marketValue,
        #           averageCost, unrealizedPNL, realizedPNL, account
        items = [p for p in ib.portfolio(account)]

        if params.symbol_filter:
            filt = params.symbol_filter.upper()
            items = [p for p in items if filt in p.contract.symbol.upper()]

        if not items:
            msg = f"No positions found for {account}"
            if params.symbol_filter:
                msg += f" matching '{params.symbol_filter}'"
            return msg

        # Calculate total market value for weight calculation
        total_value = sum(abs(Decimal(str(p.marketValue))) for p in items)

        # Sort by absolute market value descending
        items.sort(key=lambda p: abs(p.marketValue), reverse=True)

        lines = [f"# Positions: {account}", ""]
        lines.append("| Symbol | Shares | Avg Cost | Mkt Price | Mkt Value | Unreal P&L | Weight |")
        lines.append("|--------|--------|----------|-----------|-----------|------------|--------|")

        for p in items:
            symbol = p.contract.symbol
            currency = p.contract.currency
            shares = fmt_shares(p.position)
            avg_cost = fmt_price(p.averageCost, "")
            mkt_price = fmt_price(p.marketPrice, "")
            mkt_value = fmt_price(p.marketValue, currency)
            unreal = fmt_pnl(p.unrealizedPNL, currency)

            weight = Decimal(str(abs(p.marketValue))) / total_value * 100 if total_value else Decimal("0")

            lines.append(
                f"| {symbol} | {shares} | {avg_cost} | {mkt_price} | "
                f"{mkt_value} | {unreal} | {fmt_pct(weight)} |"
            )

        # Totals — skip NaN values to prevent one bad position from poisoning the sum
        total_unreal = sum(
            Decimal(str(p.unrealizedPNL)) for p in items
            if not math.isnan(p.unrealizedPNL)
        )
        lines.append("")
        lines.append(f"**Total Market Value**: {fmt_price(total_value)}")
        lines.append(f"**Total Unrealized P&L**: {fmt_pnl(total_unreal)}")
        lines.append(f"**Positions**: {len(items)}")

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, "fetching positions")


@mcp.tool(
    name="ibkr_get_portfolio_snapshot",
    annotations={
        "title": "Get Portfolio Snapshot",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_portfolio_snapshot(params: PortfolioInput, ctx: Context) -> str:
    """Get high-level portfolio summary — the 'at a glance' view.

    Combines account data with position data to give you NAV, total P&L,
    margin utilization, top holdings, and concentration metrics.

    Args:
        params (PortfolioInput): Account ID filter.

    Returns:
        str: Markdown with portfolio overview, top positions, and risk metrics.
    """
    try:
        ib = get_ib(ctx)
        account = resolve_account(ctx, params.account)

        # Get account metrics
        summary = ib.accountSummary(account)
        vals = {}
        for item in summary:
            vals[item.tag] = (item.value, item.currency)

        nlv = _get_dec(vals, "NetLiquidation")
        gpv = _get_dec(vals, "GrossPositionValue")
        init_margin = _get_dec(vals, "InitMarginReq")
        cushion = _get_dec(vals, "Cushion")
        currency = vals.get("NetLiquidation", (None, "USD"))[1]

        # Get positions
        items = list(ib.portfolio(account))
        items.sort(key=lambda p: abs(p.marketValue), reverse=True)

        total_unreal = sum(
            Decimal(str(p.unrealizedPNL)) for p in items
            if not math.isnan(p.unrealizedPNL)
        )
        total_mkt = sum(abs(Decimal(str(p.marketValue))) for p in items)

        margin_util = init_margin / nlv * 100 if nlv and nlv != 0 else None
        leverage = gpv / nlv if nlv and nlv != 0 else None
        cushion_pct = cushion * 100 if cushion else None

        lines = [
            f"# Portfolio Snapshot: {account}",
            "",
            f"**NAV**: {fmt_price(nlv, currency)}",
            f"**Gross Position Value**: {fmt_price(gpv, currency)}",
            f"**Leverage**: {f'{leverage:.2f}x' if leverage else 'N/A'}",
            f"**Margin Utilization**: {fmt_pct(margin_util) if margin_util else 'N/A'}",
            f"**Cushion**: {fmt_pct(cushion_pct) if cushion_pct else 'N/A'}",
            f"**Total Unrealized P&L**: {fmt_pnl(total_unreal, currency)}",
            "",
            "## Top Holdings",
        ]

        # Top 5 positions
        for p in items[:5]:
            weight = abs(Decimal(str(p.marketValue))) / nlv * 100 if nlv else Decimal("0")
            lines.append(
                f"- **{p.contract.symbol}**: {fmt_price(p.marketValue, p.contract.currency)} "
                f"({fmt_pct(weight)}) — P&L: {fmt_pnl(p.unrealizedPNL, p.contract.currency)}"
            )

        # Concentration metrics
        if items and nlv:
            weights = [abs(Decimal(str(p.marketValue))) / nlv * 100 for p in items]
            top1 = weights[0] if weights else Decimal("0")
            top3 = sum(weights[:3])
            top5 = sum(weights[:5])
            hhi = sum(w ** 2 for w in weights)

            lines.extend([
                "",
                "## Concentration",
                f"**Top 1 weight**: {fmt_pct(top1)}",
                f"**Top 3 weight**: {fmt_pct(top3)}",
                f"**Top 5 weight**: {fmt_pct(top5)}",
                f"**HHI**: {hhi:.0f} (10000 = single position)",
                f"**Positions**: {len(items)}",
            ])

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, "building portfolio snapshot")


@mcp.tool(
    name="ibkr_get_portfolio_by_currency",
    annotations={
        "title": "Get Portfolio by Currency",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_portfolio_by_currency(params: PortfolioInput, ctx: Context) -> str:
    """Get positions grouped by currency — shows CAD vs USD exposure breakdown.

    Critical for understanding cross-currency risk and planning FX conversions.

    Args:
        params (PortfolioInput): Account ID filter.

    Returns:
        str: Markdown with positions grouped by currency, totals, and percentages.
    """
    try:
        ib = get_ib(ctx)
        account = resolve_account(ctx, params.account)
        items = list(ib.portfolio(account))

        if not items:
            return f"No positions found for {account}."

        # Group by currency
        by_currency: dict[str, list] = {}
        for p in items:
            ccy = p.contract.currency
            if ccy not in by_currency:
                by_currency[ccy] = []
            by_currency[ccy].append(p)

        # Grand total in all currencies (approximate)
        grand_total = sum(abs(Decimal(str(p.marketValue))) for p in items)

        lines = [f"# Portfolio by Currency: {account}", ""]

        for ccy, positions in sorted(by_currency.items()):
            positions.sort(key=lambda p: abs(p.marketValue), reverse=True)
            ccy_total = sum(abs(Decimal(str(p.marketValue))) for p in positions)
            ccy_pct = ccy_total / grand_total * 100 if grand_total else Decimal("0")

            lines.append(f"## {ccy} ({fmt_pct(ccy_pct)} of portfolio)")
            lines.append(f"**Total**: {fmt_price(ccy_total, ccy)}")
            lines.append("")

            for p in positions:
                lines.append(
                    f"- **{p.contract.symbol}**: {fmt_shares(p.position)} shares, "
                    f"{fmt_price(p.marketValue, ccy)}"
                )
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, "grouping portfolio by currency")


@mcp.tool(
    name="ibkr_get_pnl_by_position",
    annotations={
        "title": "Get P&L by Position",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_pnl_by_position(params: PortfolioInput, ctx: Context) -> str:
    """Get daily P&L broken down by position — what's driving today's returns.

    Subscribes to per-position P&L from IB, showing daily, unrealized, and
    realized P&L for each holding. Sorted by daily P&L so your biggest
    movers are at the top.

    Args:
        params (PortfolioInput): Optional account and symbol filter.

    Returns:
        str: Markdown table of per-position P&L sorted by daily P&L.
    """
    try:
        ib = get_ib(ctx)
        account = resolve_account(ctx, params.account)

        # Get positions to know which conIds to subscribe to
        items = list(ib.portfolio(account))
        if params.symbol_filter:
            filt = params.symbol_filter.upper()
            items = [p for p in items if filt in p.contract.symbol.upper()]

        if not items:
            msg = f"No positions for {account}"
            if params.symbol_filter:
                msg += f" matching '{params.symbol_filter}'"
            return msg

        # Subscribe to P&L for each position.
        # try/finally ensures subscriptions are always cancelled,
        # even if processing throws an exception (resource leak prevention).
        pnl_singles = []
        try:
            for p in items:
                pnl = ib.reqPnLSingle(account, "", p.contract.conId)
                pnl_singles.append((p, pnl))

            # Wait for IB to populate the data
            await asyncio.sleep(1.5)

            # Build results
            rows = []
            for p, pnl in pnl_singles:
                daily = pnl.dailyPnL if not math.isnan(pnl.dailyPnL) else None
                unreal = pnl.unrealizedPnL if not math.isnan(pnl.unrealizedPnL) else None
                real = pnl.realizedPnL if not math.isnan(pnl.realizedPnL) else None
                value = pnl.value if not math.isnan(pnl.value) else None

                rows.append({
                    "symbol": p.contract.symbol,
                    "currency": p.contract.currency,
                    "shares": p.position,
                    "daily": daily,
                    "unrealized": unreal,
                    "realized": real,
                    "value": value,
                })
        finally:
            for p, pnl in pnl_singles:
                ib.cancelPnLSingle(account, "", p.contract.conId)

        # Sort by daily P&L descending (best performers first, None last)
        rows.sort(key=lambda r: r["daily"] if r["daily"] is not None else float("-inf"), reverse=True)

        total_daily = sum(Decimal(str(r["daily"])) for r in rows if r["daily"] is not None)

        lines = [
            f"# P&L by Position: {account}",
            "",
            "| Symbol | Shares | Daily P&L | Unrealized | Realized | Value |",
            "|--------|--------|-----------|------------|----------|-------|",
        ]

        for r in rows:
            lines.append(
                f"| {r['symbol']} | {fmt_shares(r['shares'])} | "
                f"{fmt_pnl(r['daily'], r['currency'])} | "
                f"{fmt_pnl(r['unrealized'], r['currency'])} | "
                f"{fmt_pnl(r['realized'], r['currency'])} | "
                f"{fmt_price(r['value'], r['currency']) if r['value'] else 'N/A'} |"
            )

        lines.extend([
            "",
            f"**Total Daily P&L**: {fmt_pnl(total_daily)}",
            f"**Positions**: {len(rows)}",
        ])

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, "fetching per-position P&L")


# --- Helpers ---

def _get_dec(vals: dict, tag: str) -> Decimal | None:
    """Extract Decimal from accountSummary dict."""
    entry = vals.get(tag)
    if entry is None:
        return None
    try:
        return Decimal(str(entry[0]))
    except (ValueError, TypeError, InvalidOperation):
        return None
