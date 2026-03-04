"""
Account tools for ibkr_mcp.

Provides account-level information: NAV, margin requirements, buying power,
excess liquidity, and P&L. These are the numbers you check first.
"""

import asyncio
import math
from decimal import Decimal, InvalidOperation
from typing import Optional

from ib_insync import Forex

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

# Import the mcp instance from server (registered via decorator)
from app import mcp
from core.connection import get_ib, resolve_account
from core.errors import handle_ib_error
from core.formatting import fmt_price, fmt_pct, fmt_pnl, fmt_shares


# --- Input Models ---

class AccountInput(BaseModel):
    """Standard input for account-scoped tools."""
    account: Optional[str] = Field(
        default=None,
        description="IBKR account ID. Leave blank to use primary account."
    )


class BriefingInput(BaseModel):
    """Input for morning briefing tool."""
    account: Optional[str] = Field(
        default=None,
        description="IBKR account ID. Leave blank to use primary account."
    )


# --- Tools ---

@mcp.tool(
    name="ibkr_get_account_summary",
    annotations={
        "title": "Get Account Summary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_account_summary(params: AccountInput, ctx: Context) -> str:
    """Get full account overview: NAV, margin, buying power, cushion, leverage.

    Returns the key account metrics that tell you the health of your account
    at a glance. This is the first tool to call for any portfolio question.

    Args:
        params (AccountInput): Optional account ID filter.

    Returns:
        str: Markdown-formatted account summary including NLV, margin
             requirements, buying power, excess liquidity, and leverage.
    """
    try:
        ib = get_ib(ctx)
        account = resolve_account(ctx, params.account)

        # accountSummary() returns a list of AccountValue objects
        # Each has: account, tag, value, currency, modelCode
        summary = ib.accountSummary(account)

        if not summary:
            return f"No account summary available for {account}."

        # Build a lookup dict: tag -> (value, currency)
        vals = {}
        for item in summary:
            vals[item.tag] = (item.value, item.currency)

        # Extract the key metrics
        nlv = _get_decimal(vals, "NetLiquidation")
        gpv = _get_decimal(vals, "GrossPositionValue")
        cash = _get_decimal(vals, "TotalCashValue")
        buying_power = _get_decimal(vals, "BuyingPower")
        init_margin = _get_decimal(vals, "InitMarginReq")
        maint_margin = _get_decimal(vals, "MaintMarginReq")
        excess_init = _get_decimal(vals, "ExcessLiquidity")
        excess_maint = _get_decimal(vals, "FullExcessLiquidity")
        cushion_raw = _get_decimal(vals, "Cushion")
        sma = _get_decimal(vals, "SMA")

        # Derived metrics
        cushion_pct = cushion_raw * 100 if cushion_raw else None
        leverage = gpv / nlv if nlv and nlv != 0 else None
        margin_util = init_margin / nlv * 100 if nlv and nlv != 0 else None

        currency = vals.get("NetLiquidation", (None, "USD"))[1]

        lines = [
            f"# Account Summary: {account}",
            "",
            f"**Net Liquidation Value**: {fmt_price(nlv, currency)}",
            f"**Gross Position Value**: {fmt_price(gpv, currency)}",
            f"**Total Cash**: {fmt_price(cash, currency)}",
            "",
            "## Margin",
            f"**Initial Margin Req**: {fmt_price(init_margin, currency)}",
            f"**Maintenance Margin Req**: {fmt_price(maint_margin, currency)}",
            f"**Excess Liquidity (Initial)**: {fmt_price(excess_init, currency)}",
            f"**Excess Liquidity (Maint)**: {fmt_price(excess_maint, currency)}",
            f"**Cushion**: {fmt_pct(cushion_pct) if cushion_pct else 'N/A'}",
            "",
            "## Buying Power & Leverage",
            f"**Buying Power**: {fmt_price(buying_power, currency)}",
            f"**SMA**: {fmt_price(sma, currency)}",
            f"**Leverage**: {f'{leverage:.2f}x' if leverage else 'N/A'}",
            f"**Margin Utilization**: {fmt_pct(margin_util) if margin_util else 'N/A'}",
        ]

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, "fetching account summary")


@mcp.tool(
    name="ibkr_get_margin_summary",
    annotations={
        "title": "Get Margin Summary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_margin_summary(params: AccountInput, ctx: Context) -> str:
    """Get focused margin view: distance to margin call and forced liquidation.

    This is the stress-check tool. Shows exactly how much room you have
    before bad things happen.

    Args:
        params (AccountInput): Optional account ID filter.

    Returns:
        str: Markdown with margin distances, excess liquidity, and cushion.
    """
    try:
        ib = get_ib(ctx)
        account = resolve_account(ctx, params.account)
        summary = ib.accountSummary(account)

        if not summary:
            return f"No margin data available for {account}."

        vals = {}
        for item in summary:
            vals[item.tag] = (item.value, item.currency)

        nlv = _get_decimal(vals, "NetLiquidation")
        equity = _get_decimal(vals, "EquityWithLoanValue")
        init_margin = _get_decimal(vals, "InitMarginReq")
        maint_margin = _get_decimal(vals, "MaintMarginReq")
        excess_init = _get_decimal(vals, "ExcessLiquidity")
        excess_maint = _get_decimal(vals, "FullExcessLiquidity")
        cushion_raw = _get_decimal(vals, "Cushion")
        gpv = _get_decimal(vals, "GrossPositionValue")

        currency = vals.get("NetLiquidation", (None, "USD"))[1]

        # Distance calculations
        dist_margin_call = equity - init_margin if equity and init_margin else None
        dist_liquidation = equity - maint_margin if equity and maint_margin else None

        # Max drawdown before trouble (as % of NLV)
        max_dd_call = (dist_margin_call / nlv * 100) if dist_margin_call and nlv else None
        max_dd_liq = (dist_liquidation / nlv * 100) if dist_liquidation and nlv else None

        cushion_pct = cushion_raw * 100 if cushion_raw else None

        lines = [
            f"# Margin Summary: {account}",
            "",
            f"**Equity**: {fmt_price(equity, currency)}",
            f"**Initial Margin Req**: {fmt_price(init_margin, currency)}",
            f"**Maintenance Margin Req**: {fmt_price(maint_margin, currency)}",
            "",
            "## Distances",
            f"**Above Initial Margin**: {fmt_price(dist_margin_call, currency)}",
            f"**Above Maintenance Margin**: {fmt_price(dist_liquidation, currency)}",
            f"**Cushion**: {fmt_pct(cushion_pct) if cushion_pct else 'N/A'}",
            "",
            "## Max Drawdown Before Trouble",
            f"**Before Margin Call (initial)**: {fmt_pct(max_dd_call) if max_dd_call else 'N/A'}",
            f"**Before Forced Liquidation (maint)**: {fmt_pct(max_dd_liq) if max_dd_liq else 'N/A'}",
            "",
            "## Excess Liquidity",
            f"**Excess (Initial)**: {fmt_price(excess_init, currency)}",
            f"**Excess (Maintenance)**: {fmt_price(excess_maint, currency)}",
        ]

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, "fetching margin summary")


@mcp.tool(
    name="ibkr_list_accounts",
    annotations={
        "title": "List Managed Accounts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_list_accounts(ctx: Context) -> str:
    """List all IBKR accounts connected to this session.

    Returns:
        str: Comma-separated list of account IDs with primary marked.
    """
    try:
        ib = get_ib(ctx)
        accounts = ib.managedAccounts()
        primary = resolve_account(ctx, None)

        lines = ["# Connected Accounts", ""]
        for acc in accounts:
            marker = " ← primary" if acc == primary else ""
            lines.append(f"- `{acc}`{marker}")

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, "listing accounts")


@mcp.tool(
    name="ibkr_get_account_pnl",
    annotations={
        "title": "Get Account P&L",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_account_pnl(params: AccountInput, ctx: Context) -> str:
    """Get real-time daily P&L for the account.

    Shows daily, unrealized, and realized P&L. This is what tells you
    how today is going.

    Args:
        params (AccountInput): Optional account ID filter.

    Returns:
        str: Markdown with daily, unrealized, and realized P&L.
    """
    try:
        ib = get_ib(ctx)
        account = resolve_account(ctx, params.account)

        # reqPnL returns a PnL object with dailyPnL, unrealizedPnL, realizedPnL.
        # try/finally ensures subscription cleanup even if formatting throws.
        pnl = ib.reqPnL(account)
        try:
            await asyncio.sleep(0.5)

            lines = [
                f"# Account P&L: {account}",
                "",
                f"**Daily P&L**: {fmt_pnl(pnl.dailyPnL)}",
                f"**Unrealized P&L**: {fmt_pnl(pnl.unrealizedPnL)}",
                f"**Realized P&L**: {fmt_pnl(pnl.realizedPnL)}",
            ]

            return "\n".join(lines)
        finally:
            ib.cancelPnL(account)

    except Exception as e:
        return handle_ib_error(e, "fetching account P&L")


@mcp.tool(
    name="ibkr_morning_briefing",
    annotations={
        "title": "Morning Briefing",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_morning_briefing(params: BriefingInput, ctx: Context) -> str:
    """Get everything you need to start the day in a single call.

    Aggregates: account health (NLV, leverage, cushion, margin util),
    daily P&L, top 3 gainers/losers by daily P&L, open orders summary,
    and USDCAD rate if multi-currency positions exist.

    Args:
        params (BriefingInput): Optional account ID filter.

    Returns:
        str: Markdown briefing covering account, P&L, movers, and orders.
    """
    try:
        ib = get_ib(ctx)
        account = resolve_account(ctx, params.account)

        # --- Account summary (sync, no subscription) ---
        summary = ib.accountSummary(account)
        vals = {}
        for item in summary:
            vals[item.tag] = (item.value, item.currency)

        nlv = _get_decimal(vals, "NetLiquidation")
        gpv = _get_decimal(vals, "GrossPositionValue")
        init_margin = _get_decimal(vals, "InitMarginReq")
        cushion_raw = _get_decimal(vals, "Cushion")
        excess_init = _get_decimal(vals, "ExcessLiquidity")
        currency = vals.get("NetLiquidation", (None, "USD"))[1]

        leverage = gpv / nlv if nlv and nlv != 0 else None
        cushion_pct = cushion_raw * 100 if cushion_raw else None
        margin_util = init_margin / nlv * 100 if nlv and nlv != 0 else None

        # --- Positions + per-position P&L (streaming subscriptions) ---
        items = list(ib.portfolio(account))
        pnl_account = ib.reqPnL(account)
        pnl_singles = []
        try:
            for p in items:
                pnl_s = ib.reqPnLSingle(account, "", p.contract.conId)
                pnl_singles.append((p, pnl_s))

            # Single sleep for all subscriptions to populate
            await asyncio.sleep(1.5)

            # Read account-level P&L
            daily_pnl = pnl_account.dailyPnL
            unreal_pnl = pnl_account.unrealizedPnL
            real_pnl = pnl_account.realizedPnL

            # Read per-position P&L, filter out NaN
            rows = []
            for p, pnl_s in pnl_singles:
                daily = pnl_s.dailyPnL if not math.isnan(pnl_s.dailyPnL) else None
                rows.append({
                    "symbol": p.contract.symbol,
                    "currency": p.contract.currency,
                    "shares": p.position,
                    "daily": daily,
                })
        finally:
            ib.cancelPnL(account)
            for p, pnl_s in pnl_singles:
                ib.cancelPnLSingle(account, "", p.contract.conId)

        # --- Top movers ---
        with_daily = [r for r in rows if r["daily"] is not None]
        with_daily.sort(key=lambda r: r["daily"], reverse=True)
        top_gainers = with_daily[:3]
        top_losers = with_daily[-3:][::-1] if len(with_daily) > 3 else []

        # --- Open orders (sync) ---
        trades = ib.openTrades()
        trades = [t for t in trades if t.order.account == account]

        # --- USDCAD (only if multi-currency positions) ---
        currencies = {p.contract.currency for p in items}
        usdcad_rate = None
        if "CAD" in currencies and "USD" in currencies:
            try:
                fx_contract = Forex("USDCAD")
                fx_qualified = await ib.qualifyContractsAsync(fx_contract)
                if fx_qualified:
                    ib.reqMktData(fx_qualified[0], "", True, False)
                    await asyncio.sleep(1)
                    fx_ticker = ib.ticker(fx_qualified[0])
                    if fx_ticker:
                        rate = fx_ticker.last
                        if rate is None or (isinstance(rate, float) and math.isnan(rate)):
                            rate = fx_ticker.bid
                        if rate and not (isinstance(rate, float) and math.isnan(rate)):
                            usdcad_rate = rate
            except Exception:
                pass  # FX is best-effort, don't fail the briefing

        # --- Format output ---
        lines = [
            "# Morning Briefing",
            "",
            "## Account Health",
            f"**NLV**: {fmt_price(nlv, currency)}",
            f"**Leverage**: {f'{leverage:.2f}x' if leverage else 'N/A'}",
            f"**Cushion**: {fmt_pct(cushion_pct) if cushion_pct else 'N/A'}",
            f"**Margin Utilization**: {fmt_pct(margin_util) if margin_util else 'N/A'}",
            f"**Excess Liquidity**: {fmt_price(excess_init, currency)}",
            "",
            "## Daily P&L",
            f"**Daily**: {fmt_pnl(daily_pnl)}",
            f"**Unrealized**: {fmt_pnl(unreal_pnl)}",
            f"**Realized**: {fmt_pnl(real_pnl)}",
        ]

        if top_gainers:
            lines.extend(["", "## Top Gainers"])
            for r in top_gainers:
                lines.append(
                    f"- **{r['symbol']}** {fmt_shares(r['shares'])} shares: "
                    f"{fmt_pnl(r['daily'], r['currency'])}"
                )

        if top_losers:
            lines.extend(["", "## Top Losers"])
            for r in top_losers:
                lines.append(
                    f"- **{r['symbol']}** {fmt_shares(r['shares'])} shares: "
                    f"{fmt_pnl(r['daily'], r['currency'])}"
                )

        # Orders summary
        lines.extend(["", "## Open Orders"])
        if trades:
            lines.append(f"**{len(trades)} open order(s)**")
            for t in trades[:5]:
                o = t.order
                c = t.contract
                lines.append(f"- {c.symbol} {o.action} {fmt_shares(o.totalQuantity)} @ {o.orderType}")
            if len(trades) > 5:
                lines.append(f"- ... and {len(trades) - 5} more")
        else:
            lines.append("No open orders.")

        if usdcad_rate:
            lines.extend([
                "",
                "## FX",
                f"**USDCAD**: {Decimal(str(usdcad_rate)).quantize(Decimal('0.0001'))}",
            ])

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, "generating morning briefing")


# --- Helpers ---

def _get_decimal(vals: dict, tag: str) -> Decimal | None:
    """Extract a Decimal value from the accountSummary lookup dict."""
    entry = vals.get(tag)
    if entry is None:
        return None
    try:
        return Decimal(str(entry[0]))
    except (ValueError, TypeError, InvalidOperation):
        return None
