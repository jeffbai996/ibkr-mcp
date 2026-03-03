"""
Analytics tools for ibkr_mcp.

What-if margin scenarios, concentration analysis, and stress tests.
These automate the margin math Jeff and Claude were doing manually
during the Iran crisis — one sentence instead of 20 minutes of math.
"""

import asyncio
from decimal import Decimal, InvalidOperation
from typing import Optional

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from ib_insync import Stock, MarketOrder

from app import mcp
from core.connection import get_ib, resolve_account
from core.errors import handle_ib_error
from core.formatting import fmt_price, fmt_pct, fmt_pnl


# --- Input Models ---

class WhatIfInput(BaseModel):
    symbol: str = Field(..., description="Stock symbol to simulate trading")
    quantity: int = Field(..., description="Number of shares to simulate", gt=0)
    account: Optional[str] = Field(
        default=None, description="IBKR account ID. Leave blank for primary."
    )


class StressTestInput(BaseModel):
    drawdown_pct: float = Field(
        ..., description="Simulated portfolio drawdown percentage (e.g., 5.0 for 5%)",
        gt=0, le=100,
    )
    account: Optional[str] = Field(
        default=None, description="IBKR account ID. Leave blank for primary."
    )


class ConcentrationInput(BaseModel):
    account: Optional[str] = Field(
        default=None, description="IBKR account ID. Leave blank for primary."
    )


# --- Tools ---

@mcp.tool(
    name="ibkr_what_if_sell",
    annotations={
        "title": "What-If Sell Simulation",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_what_if_sell(params: WhatIfInput, ctx: Context) -> str:
    """Simulate the margin impact of selling shares WITHOUT placing an order.

    Uses IBKR's whatIfOrder API to calculate how your margin requirements,
    excess liquidity, and buying power would change if you sold X shares.
    No order is placed — this is purely a simulation.

    This is the tool that replaces the manual margin math from the Iran weekend.

    Args:
        params (WhatIfInput): Symbol, quantity, and optional account.

    Returns:
        str: Markdown showing current vs post-trade margin state, with deltas.
    """
    try:
        ib = get_ib(ctx)
        account = resolve_account(ctx, params.account)

        # Build the contract
        contract = Stock(params.symbol, "SMART", "USD")
        contracts = await ib.qualifyContractsAsync(contract)
        if not contracts:
            return f"Could not find contract for {params.symbol}."

        # Create a SELL market order for the what-if simulation
        order = MarketOrder("SELL", params.quantity)
        order.account = account

        # whatIfOrder returns an OrderState with margin impact
        what_if = ib.whatIfOrder(contracts[0], order)
        await asyncio.sleep(1)

        if not what_if or what_if.initMarginChange is None:
            return (f"What-if simulation failed for selling {params.quantity} "
                    f"{params.symbol}. IB may not support what-if for this contract.")

        # Get current account state for comparison
        summary = ib.accountSummary(account)
        vals = {}
        for item in summary:
            vals[item.tag] = (item.value, item.currency)

        curr_equity = _get_dec(vals, "EquityWithLoanValue")
        curr_init = _get_dec(vals, "InitMarginReq")
        curr_maint = _get_dec(vals, "MaintMarginReq")
        curr_excess = _get_dec(vals, "ExcessLiquidity")
        currency = vals.get("NetLiquidation", (None, "USD"))[1]

        # What-if results
        init_change = _safe_dec(what_if.initMarginChange)
        maint_change = _safe_dec(what_if.maintMarginChange)
        equity_change = _safe_dec(what_if.equityWithLoanChange)

        # Post-trade estimates
        post_init = curr_init + init_change if curr_init and init_change else None
        post_maint = curr_maint + maint_change if curr_maint and maint_change else None
        post_equity = curr_equity + equity_change if curr_equity and equity_change else None
        post_excess = post_equity - post_init if post_equity and post_init else None

        init_relief = -init_change if init_change else None  # Negative change = relief
        maint_relief = -maint_change if maint_change else None

        lines = [
            f"# What-If: SELL {params.quantity:,} {params.symbol}",
            f"Account: {account}",
            "",
            "## Current State",
            f"**Equity**: {fmt_price(curr_equity, currency)}",
            f"**Initial Margin**: {fmt_price(curr_init, currency)}",
            f"**Maintenance Margin**: {fmt_price(curr_maint, currency)}",
            f"**Excess Liquidity**: {fmt_price(curr_excess, currency)}",
            "",
            "## Post-Trade Estimate",
            f"**Equity**: {fmt_price(post_equity, currency)}",
            f"**Initial Margin**: {fmt_price(post_init, currency)}",
            f"**Maintenance Margin**: {fmt_price(post_maint, currency)}",
            f"**Excess Liquidity**: {fmt_price(post_excess, currency)}",
            "",
            "## Margin Impact",
            f"**Initial Margin Relief**: {fmt_pnl(init_relief, currency)}",
            f"**Maintenance Margin Relief**: {fmt_pnl(maint_relief, currency)}",
            f"**Equity Change**: {fmt_pnl(equity_change, currency)}",
        ]

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, f"simulating sell of {params.quantity} {params.symbol}")


@mcp.tool(
    name="ibkr_what_if_buy",
    annotations={
        "title": "What-If Buy Simulation",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_what_if_buy(params: WhatIfInput, ctx: Context) -> str:
    """Simulate the margin impact of buying shares WITHOUT placing an order.

    Same as what_if_sell but for adding to positions. Shows how much
    margin would be consumed by the purchase.

    Args:
        params (WhatIfInput): Symbol, quantity, and optional account.

    Returns:
        str: Markdown showing current vs post-trade margin state.
    """
    try:
        ib = get_ib(ctx)
        account = resolve_account(ctx, params.account)

        contract = Stock(params.symbol, "SMART", "USD")
        contracts = await ib.qualifyContractsAsync(contract)
        if not contracts:
            return f"Could not find contract for {params.symbol}."

        order = MarketOrder("BUY", params.quantity)
        order.account = account

        what_if = ib.whatIfOrder(contracts[0], order)
        await asyncio.sleep(1)

        if not what_if or what_if.initMarginChange is None:
            return (f"What-if simulation failed for buying {params.quantity} "
                    f"{params.symbol}.")

        summary = ib.accountSummary(account)
        vals = {}
        for item in summary:
            vals[item.tag] = (item.value, item.currency)

        curr_equity = _get_dec(vals, "EquityWithLoanValue")
        curr_init = _get_dec(vals, "InitMarginReq")
        curr_excess = _get_dec(vals, "ExcessLiquidity")
        currency = vals.get("NetLiquidation", (None, "USD"))[1]

        init_change = _safe_dec(what_if.initMarginChange)
        maint_change = _safe_dec(what_if.maintMarginChange)

        post_init = curr_init + init_change if curr_init and init_change else None
        post_excess = curr_equity - post_init if curr_equity and post_init else None

        lines = [
            f"# What-If: BUY {params.quantity:,} {params.symbol}",
            f"Account: {account}",
            "",
            f"**Current Excess Liquidity**: {fmt_price(curr_excess, currency)}",
            f"**Additional Initial Margin**: {fmt_price(init_change, currency)}",
            f"**Additional Maint Margin**: {fmt_price(maint_change, currency)}",
            f"**Post-Trade Excess Liquidity**: {fmt_price(post_excess, currency)}",
            "",
        ]

        if post_excess and post_excess < 0:
            lines.append("⚠️ **WARNING: This trade would put you in margin deficit.**")

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, f"simulating buy of {params.quantity} {params.symbol}")


@mcp.tool(
    name="ibkr_portfolio_concentration",
    annotations={
        "title": "Portfolio Concentration Analysis",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_portfolio_concentration(params: ConcentrationInput, ctx: Context) -> str:
    """Analyze portfolio concentration and single-name risk.

    Returns position weights, HHI, top-N concentration, and flags
    any position over 20% weight.

    Args:
        params (ConcentrationInput): Optional account ID.

    Returns:
        str: Markdown with concentration metrics and risk flags.
    """
    try:
        ib = get_ib(ctx)
        account = resolve_account(ctx, params.account)

        summary = ib.accountSummary(account)
        vals = {}
        for item in summary:
            vals[item.tag] = (item.value, item.currency)
        nlv = _get_dec(vals, "NetLiquidation")
        currency = vals.get("NetLiquidation", (None, "USD"))[1]

        items = list(ib.portfolio(account))
        items.sort(key=lambda p: abs(p.marketValue), reverse=True)

        if not items or not nlv:
            return f"No positions or NAV data for {account}."

        weights = []
        lines = [
            f"# Concentration Analysis: {account}",
            f"**NAV**: {fmt_price(nlv, currency)}",
            "",
            "## Position Weights",
            "| Symbol | Market Value | Weight |",
            "|--------|-------------|--------|",
        ]

        for p in items:
            weight = abs(Decimal(str(p.marketValue))) / nlv * 100
            weights.append(weight)
            flag = " ⚠️" if weight > 20 else ""
            lines.append(
                f"| {p.contract.symbol}{flag} | "
                f"{fmt_price(p.marketValue, p.contract.currency)} | "
                f"{fmt_pct(weight)} |"
            )

        # Concentration metrics
        hhi = sum(w ** 2 for w in weights)
        top1 = weights[0] if weights else Decimal("0")
        top3 = sum(weights[:3])
        top5 = sum(weights[:5])
        over_20 = [items[i].contract.symbol for i, w in enumerate(weights) if w > 20]

        lines.extend([
            "",
            "## Concentration Metrics",
            f"**HHI**: {hhi:.0f} (10000=single position, <1500=diversified)",
            f"**Top 1**: {fmt_pct(top1)}",
            f"**Top 3**: {fmt_pct(top3)}",
            f"**Top 5**: {fmt_pct(top5)}",
            f"**Positions >20%**: {', '.join(over_20) if over_20 else 'None'}",
            f"**Total Positions**: {len(items)}",
        ])

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, "analyzing concentration")


@mcp.tool(
    name="ibkr_margin_stress_test",
    annotations={
        "title": "Margin Stress Test",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_margin_stress_test(params: StressTestInput, ctx: Context) -> str:
    """Simulate a portfolio drawdown and show margin impact.

    Answers: 'If the market drops X%, do I get margin called?'
    Calculates approximate post-stress NLV, margin requirements, and
    distance to both initial margin call and forced liquidation.

    NOTE: This is an approximation. Actual margin requirements may change
    non-linearly during drawdowns as volatility increases.

    Args:
        params (StressTestInput): Drawdown percentage and optional account.

    Returns:
        str: Markdown comparing current state vs stressed state.
    """
    try:
        ib = get_ib(ctx)
        account = resolve_account(ctx, params.account)

        summary = ib.accountSummary(account)
        vals = {}
        for item in summary:
            vals[item.tag] = (item.value, item.currency)

        nlv = _get_dec(vals, "NetLiquidation")
        equity = _get_dec(vals, "EquityWithLoanValue")
        gpv = _get_dec(vals, "GrossPositionValue")
        init_margin = _get_dec(vals, "InitMarginReq")
        maint_margin = _get_dec(vals, "MaintMarginReq")
        cash = _get_dec(vals, "TotalCashValue")
        cushion = _get_dec(vals, "Cushion")
        currency = vals.get("NetLiquidation", (None, "USD"))[1]

        if not all([nlv, equity, gpv, init_margin, maint_margin]):
            return f"Insufficient account data for stress test on {account}."

        dd = Decimal(str(params.drawdown_pct)) / 100

        # Approximate stressed values
        # Position value drops by dd%, cash is unchanged
        position_value = gpv  # Approximate: gross positions
        loss = position_value * dd
        stressed_nlv = nlv - loss
        stressed_equity = equity - loss

        # Margin requirements scale roughly linearly with position value
        # (This is an approximation — real margin is more complex)
        stressed_gpv = gpv * (1 - dd)
        margin_scale = (1 - dd)
        stressed_init = init_margin * margin_scale
        stressed_maint = maint_margin * margin_scale

        stressed_excess_init = stressed_equity - stressed_init
        stressed_excess_maint = stressed_equity - stressed_maint

        # Max drawdown calculations
        curr_excess_init = equity - init_margin
        curr_excess_maint = equity - maint_margin

        # How much can equity drop before hitting margin?
        # equity - loss = init_margin * (1 - loss/gpv)
        # This is approximate; solving for the crossover point
        max_dd_call_pct = None
        max_dd_liq_pct = None
        if gpv and gpv > 0:
            # Iterative approximation (simple enough)
            for test_dd in range(1, 100):
                test_frac = Decimal(str(test_dd)) / 100
                test_equity = equity - gpv * test_frac
                test_init = init_margin * (1 - test_frac)
                if test_equity <= test_init and max_dd_call_pct is None:
                    max_dd_call_pct = Decimal(str(test_dd))
                test_maint = maint_margin * (1 - test_frac)
                if test_equity <= test_maint and max_dd_liq_pct is None:
                    max_dd_liq_pct = Decimal(str(test_dd))
                if max_dd_call_pct and max_dd_liq_pct:
                    break

        lines = [
            f"# Stress Test: {fmt_pct(Decimal(str(params.drawdown_pct)))} Drawdown",
            f"Account: {account}",
            "",
            "## Current State",
            f"**NLV**: {fmt_price(nlv, currency)}",
            f"**Equity**: {fmt_price(equity, currency)}",
            f"**Init Margin**: {fmt_price(init_margin, currency)}",
            f"**Maint Margin**: {fmt_price(maint_margin, currency)}",
            f"**Excess (Init)**: {fmt_price(curr_excess_init, currency)}",
            f"**Excess (Maint)**: {fmt_price(curr_excess_maint, currency)}",
            f"**Cushion**: {fmt_pct(cushion * 100) if cushion else 'N/A'}",
            "",
            f"## After {fmt_pct(Decimal(str(params.drawdown_pct)))} Drawdown (estimated)",
            f"**Estimated Loss**: {fmt_pnl(-loss, currency)}",
            f"**Stressed NLV**: {fmt_price(stressed_nlv, currency)}",
            f"**Stressed Equity**: {fmt_price(stressed_equity, currency)}",
            f"**Stressed Init Margin**: {fmt_price(stressed_init, currency)}",
            f"**Stressed Maint Margin**: {fmt_price(stressed_maint, currency)}",
            f"**Stressed Excess (Init)**: {fmt_price(stressed_excess_init, currency)}",
            f"**Stressed Excess (Maint)**: {fmt_price(stressed_excess_maint, currency)}",
            "",
            "## Survivability",
            f"**Max DD before margin call**: ~{fmt_pct(max_dd_call_pct) if max_dd_call_pct else '>99%'}",
            f"**Max DD before forced liq**: ~{fmt_pct(max_dd_liq_pct) if max_dd_liq_pct else '>99%'}",
        ]

        # Warnings
        if stressed_excess_init and stressed_excess_init < 0:
            lines.append("")
            lines.append(f"⚠️ **MARGIN CALL**: A {params.drawdown_pct}% drawdown "
                         f"would breach initial margin by {fmt_price(-stressed_excess_init, currency)}.")
        if stressed_excess_maint and stressed_excess_maint < 0:
            lines.append(f"🚨 **FORCED LIQUIDATION**: A {params.drawdown_pct}% drawdown "
                         f"would breach maintenance margin by {fmt_price(-stressed_excess_maint, currency)}.")

        lines.extend([
            "",
            "_Note: This is an approximation. Real margin requirements may increase "
            "during drawdowns as volatility rises._",
        ])

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, "running stress test")


# --- Helpers ---

def _get_dec(vals: dict, tag: str) -> Decimal | None:
    entry = vals.get(tag)
    if entry is None:
        return None
    try:
        return Decimal(str(entry[0]))
    except (ValueError, TypeError, InvalidOperation):
        return None


def _safe_dec(value) -> Decimal | None:
    """Safely convert an IB API value to Decimal."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        return None
