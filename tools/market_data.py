"""
Market data tools for ibkr_mcp.

Quotes, historical bars, and contract details. The data layer.
"""

import asyncio
from decimal import Decimal
from typing import Optional

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from ib_insync import Stock, Contract

from app import mcp
from core.connection import get_ib
from core.errors import handle_ib_error
from core.formatting import fmt_price, fmt_pct, fmt_pnl, fmt_shares


# --- Input Models ---

class QuoteInput(BaseModel):
    symbol: str = Field(..., description="Stock symbol (e.g., 'MSFT', 'AAPL')")
    sec_type: str = Field(default="STK", description="Security type: STK, OPT, FUT, CASH")
    exchange: str = Field(default="SMART", description="Exchange. SMART for best routing.")
    currency: str = Field(default="USD", description="Currency (USD, CAD, HKD, etc.)")


class HistoricalInput(BaseModel):
    symbol: str = Field(..., description="Stock symbol")
    duration: str = Field(default="30 D", description="Lookback: '30 D', '6 M', '1 Y', etc.")
    bar_size: str = Field(default="1 day", description="Bar size: '1 day', '1 hour', '5 mins'")
    what_to_show: str = Field(default="TRADES", description="TRADES, MIDPOINT, BID, ASK")
    use_rth: bool = Field(default=True, description="Regular trading hours only")
    sec_type: str = Field(default="STK", description="Security type")
    exchange: str = Field(default="SMART", description="Exchange")
    currency: str = Field(default="USD", description="Currency")


class ContractInput(BaseModel):
    symbol: str = Field(..., description="Stock symbol")
    sec_type: str = Field(default="STK", description="Security type")
    exchange: str = Field(default="SMART", description="Exchange")
    currency: str = Field(default="USD", description="Currency")


# --- Tools ---

@mcp.tool(
    name="ibkr_get_quote",
    annotations={
        "title": "Get Market Quote",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_quote(params: QuoteInput, ctx: Context) -> str:
    """Get current market data for a symbol: last price, bid/ask, volume, change.

    Requests a snapshot of market data from IBKR. Works during market hours
    for real-time data, or returns delayed/frozen data outside hours.

    Args:
        params (QuoteInput): Symbol and contract specification.

    Returns:
        str: Markdown with last price, bid, ask, spread, volume, and daily change.
    """
    try:
        ib = get_ib(ctx)

        contract = Stock(params.symbol, params.exchange, params.currency)
        if params.sec_type != "STK":
            contract = Contract(
                symbol=params.symbol,
                secType=params.sec_type,
                exchange=params.exchange,
                currency=params.currency,
            )

        # Qualify the contract (resolve to specific conId)
        contracts = await ib.qualifyContractsAsync(contract)
        if not contracts:
            return f"Could not find contract for {params.symbol}."

        qualified = contracts[0]

        # Request snapshot market data
        ib.reqMktData(qualified, "", True, False)
        await asyncio.sleep(2)  # Give IB time to send data

        ticker = ib.ticker(qualified)

        if ticker is None:
            return f"No market data available for {params.symbol}."

        last = ticker.last if ticker.last == ticker.last else ticker.close
        bid = ticker.bid if ticker.bid == ticker.bid else None
        ask = ticker.ask if ticker.ask == ticker.ask else None
        spread = Decimal(str(ask)) - Decimal(str(bid)) if bid and ask else None
        volume = ticker.volume if ticker.volume == ticker.volume else None
        close = ticker.close if ticker.close == ticker.close else None
        change = Decimal(str(last)) - Decimal(str(close)) if last and close else None
        change_pct = change / Decimal(str(close)) * 100 if change and close else None

        ccy = params.currency

        lines = [
            f"# {params.symbol} Quote",
            "",
            f"**Last**: {fmt_price(last, ccy)}",
            f"**Bid**: {fmt_price(bid, ccy)}",
            f"**Ask**: {fmt_price(ask, ccy)}",
            f"**Spread**: {fmt_price(spread, '')}",
            f"**Volume**: {f'{int(volume):,}' if volume else 'N/A'}",
            "",
            f"**Close**: {fmt_price(close, ccy)}",
            f"**Change**: {fmt_pnl(change, ccy) if change else 'N/A'}",
            f"**Change %**: {fmt_pct(change_pct) if change_pct else 'N/A'}",
        ]

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, f"fetching quote for {params.symbol}")


@mcp.tool(
    name="ibkr_get_historical_bars",
    annotations={
        "title": "Get Historical Bars",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_historical_bars(params: HistoricalInput, ctx: Context) -> str:
    """Get historical OHLCV data for a symbol.

    Returns bars for the specified duration and size. Useful for
    trend analysis, support/resistance levels, and charting.

    Args:
        params (HistoricalInput): Symbol, duration, bar size, and filters.

    Returns:
        str: Markdown with bar data and summary statistics.
    """
    try:
        ib = get_ib(ctx)

        contract = Stock(params.symbol, params.exchange, params.currency)
        contracts = await ib.qualifyContractsAsync(contract)
        if not contracts:
            return f"Could not find contract for {params.symbol}."

        bars = await ib.reqHistoricalDataAsync(
            contracts[0],
            endDateTime="",
            durationStr=params.duration,
            barSizeSetting=params.bar_size,
            whatToShow=params.what_to_show,
            useRTH=params.use_rth,
        )

        if not bars:
            return f"No historical data for {params.symbol} ({params.duration})."

        # Summary stats
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        volumes = [b.volume for b in bars if b.volume > 0]
        closes = [b.close for b in bars]

        period_high = max(highs) if highs else None
        period_low = min(lows) if lows else None
        avg_volume = sum(volumes) / len(volumes) if volumes else None
        first_close = closes[0] if closes else None
        last_close = closes[-1] if closes else None
        period_return = ((last_close - first_close) / first_close * 100
                         if first_close and last_close else None)

        lines = [
            f"# {params.symbol} Historical Data",
            f"**Period**: {params.duration} | **Bar Size**: {params.bar_size}",
            "",
            "## Summary",
            f"**Period High**: {fmt_price(period_high, params.currency)}",
            f"**Period Low**: {fmt_price(period_low, params.currency)}",
            f"**Period Return**: {fmt_pct(period_return) if period_return else 'N/A'}",
            f"**Avg Volume**: {f'{int(avg_volume):,}' if avg_volume else 'N/A'}",
            f"**Bars**: {len(bars)}",
            "",
            "## Recent Bars (last 10)",
            "| Date | Open | High | Low | Close | Volume |",
            "|------|------|------|-----|-------|--------|",
        ]

        for b in bars[-10:]:
            lines.append(
                f"| {b.date} | {fmt_price(b.open, '')} | {fmt_price(b.high, '')} | "
                f"{fmt_price(b.low, '')} | {fmt_price(b.close, '')} | "
                f"{f'{int(b.volume):,}' if b.volume > 0 else 'N/A'} |"
            )

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, f"fetching historical data for {params.symbol}")


@mcp.tool(
    name="ibkr_get_contract_details",
    annotations={
        "title": "Get Contract Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_contract_details(params: ContractInput, ctx: Context) -> str:
    """Get full contract specification: long name, industry, exchanges, tick size.

    Useful for verifying you have the right contract and understanding
    trading parameters.

    Args:
        params (ContractInput): Symbol and contract specification.

    Returns:
        str: Markdown with contract details including industry and trading info.
    """
    try:
        ib = get_ib(ctx)

        contract = Stock(params.symbol, params.exchange, params.currency)
        details_list = await ib.reqContractDetailsAsync(contract)

        if not details_list:
            return f"No contract details found for {params.symbol}."

        # Usually one result for stocks
        d = details_list[0]
        c = d.contract

        lines = [
            f"# {c.symbol} Contract Details",
            "",
            f"**Long Name**: {d.longName}",
            f"**ConId**: {c.conId}",
            f"**SecType**: {c.secType}",
            f"**Exchange**: {c.exchange}",
            f"**Primary Exchange**: {c.primaryExchange}",
            f"**Currency**: {c.currency}",
            "",
            "## Classification",
            f"**Industry**: {d.industry}",
            f"**Category**: {d.category}",
            f"**Subcategory**: {d.subcategory}",
            "",
            "## Trading Info",
            f"**Min Tick**: {d.minTick}",
            f"**Price Magnifier**: {d.priceMagnifier}",
            f"**Valid Exchanges**: {d.validExchanges}",
        ]

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, f"fetching contract details for {params.symbol}")
