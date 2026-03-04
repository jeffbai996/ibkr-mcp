"""
Market data tools for ibkr_mcp.

Quotes, historical bars, and contract details. The data layer.
"""

import asyncio
import math
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


class DividendInput(BaseModel):
    symbol: str = Field(..., description="Stock symbol (e.g., 'AAPL', 'MSFT')")
    exchange: str = Field(default="SMART", description="Exchange")
    currency: str = Field(default="USD", description="Currency")


class SearchInput(BaseModel):
    query: str = Field(
        ...,
        description="Search term: ticker, partial name, or company name "
                    "(e.g., 'AAPL', 'Taiwan Semi', 'ARM')"
    )


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
    for real-time data. Outside RTH, automatically falls back to historical
    bars to get extended hours / pre-market prices.

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

        contracts = await ib.qualifyContractsAsync(contract)
        if not contracts:
            return f"Could not find contract for {params.symbol}."

        qualified = contracts[0]

        # Try snapshot first — works during RTH
        ib.reqMktData(qualified, "", True, False)
        await asyncio.sleep(2)

        ticker = ib.ticker(qualified)

        def _val(x):
            return x if x == x else None

        # Check if snapshot returned usable data
        has_snapshot = ticker is not None and (
            _val(ticker.last) is not None or _val(ticker.bid) is not None
        )

        if has_snapshot:
            close = _val(ticker.close)
            last = _val(ticker.last) or close
            bid = _val(ticker.bid)
            ask = _val(ticker.ask)
            spread = Decimal(str(ask)) - Decimal(str(bid)) if bid and ask else None
            volume = _val(ticker.volume)
            change = Decimal(str(last)) - Decimal(str(close)) if last and close else None
            change_pct = (
                change / Decimal(str(close)) * 100 if change and close else None
            )

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

        # Snapshot returned nothing — fall back to historical bars for
        # extended hours / pre-market / overnight data
        ext_bars, rth_bars = await asyncio.gather(
            ib.reqHistoricalDataAsync(
                qualified,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=False,
            ),
            ib.reqHistoricalDataAsync(
                qualified,
                endDateTime="",
                durationStr="2 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
            ),
        )

        if not ext_bars:
            return f"No market data available for {params.symbol}."

        last_bar = ext_bars[-1]
        last = last_bar.close
        ext_high = max(b.high for b in ext_bars)
        ext_low = min(b.low for b in ext_bars)
        ext_volume = sum(b.volume for b in ext_bars if b.volume > 0)

        # Previous RTH close for change calculation
        prev_close = rth_bars[-1].close if rth_bars else None
        change = (
            Decimal(str(last)) - Decimal(str(prev_close))
            if last and prev_close
            else None
        )
        change_pct = (
            change / Decimal(str(prev_close)) * 100
            if change and prev_close
            else None
        )

        ccy = params.currency
        bar_time = str(last_bar.date)

        lines = [
            f"# {params.symbol} Quote (Extended Hours)",
            f"*Last trade: {bar_time}*",
            "",
            f"**Last**: {fmt_price(last, ccy)}",
            f"**Session High**: {fmt_price(ext_high, ccy)}",
            f"**Session Low**: {fmt_price(ext_low, ccy)}",
            f"**Volume**: {f'{int(ext_volume):,}' if ext_volume else 'N/A'}",
            "",
            f"**Prev Close**: {fmt_price(prev_close, ccy)}",
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


@mcp.tool(
    name="ibkr_get_dividends",
    annotations={
        "title": "Get Dividend Info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_dividends(params: DividendInput, ctx: Context) -> str:
    """Get dividend information for a stock: next date, amount, and trailing yield.

    Uses IB's generic tick 456 to fetch dividend data including past 12 months,
    next 12 months projected, next ex-date, and next amount per share.

    Args:
        params (DividendInput): Symbol to look up.

    Returns:
        str: Markdown with dividend details and yield estimates.
    """
    try:
        ib = get_ib(ctx)

        contract = Stock(params.symbol, params.exchange, params.currency)
        contracts = await ib.qualifyContractsAsync(contract)
        if not contracts:
            return f"Could not find contract for {params.symbol}."

        qualified = contracts[0]

        # Generic tick 456 = dividend data.
        # Streaming mode (not snapshot) because tick 456 may not
        # populate in snapshot mode. try/finally guarantees cleanup.
        ib.reqMktData(qualified, "456", False, False)
        try:
            await asyncio.sleep(2)

            ticker = ib.ticker(qualified)

            if not ticker or not ticker.dividends:
                return f"No dividend data available for {params.symbol}."

            div = ticker.dividends
            last_price = ticker.last if ticker.last and not math.isnan(ticker.last) else None
            if not last_price and ticker.close and not math.isnan(ticker.close):
                last_price = ticker.close

            lines = [
                f"# {params.symbol} Dividends",
                "",
            ]

            if div.nextDate:
                lines.append(f"**Next Ex-Date**: {div.nextDate}")
            if div.nextAmount:
                lines.append(f"**Next Amount**: ${div.nextAmount:.4f} per share")

            if div.past12Months is not None:
                lines.append(f"**Past 12 Months**: ${div.past12Months:.4f} per share")
                if last_price:
                    trailing_yield = div.past12Months / last_price * 100
                    lines.append(f"**Trailing Yield**: {trailing_yield:.2f}%")

            if div.next12Months is not None:
                lines.append(f"**Next 12 Months (est)**: ${div.next12Months:.4f} per share")
                if last_price:
                    fwd_yield = div.next12Months / last_price * 100
                    lines.append(f"**Forward Yield (est)**: {fwd_yield:.2f}%")

            if last_price:
                lines.extend(["", f"*Based on last price: {fmt_price(last_price, params.currency)}*"])

            return "\n".join(lines)
        finally:
            ib.cancelMktData(qualified)

    except Exception as e:
        return handle_ib_error(e, f"fetching dividend data for {params.symbol}")


@mcp.tool(
    name="ibkr_search_contracts",
    annotations={
        "title": "Search Contracts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_search_contracts(params: SearchInput, ctx: Context) -> str:
    """Search for contracts by symbol or company name.

    Finds matching contracts on IBKR — useful for discovering tickers,
    verifying symbols, or finding the right contract for an ETF/ADR.

    Args:
        params (SearchInput): Search query (ticker or company name).

    Returns:
        str: Markdown table of matching contracts with symbol, name, type,
             exchange, and available derivatives.
    """
    try:
        ib = get_ib(ctx)

        results = await ib.reqMatchingSymbolsAsync(params.query)

        if not results:
            return f"No contracts found matching '{params.query}'."

        lines = [
            f"# Search Results: '{params.query}'",
            "",
            "| Symbol | Name | Type | Exchange | Currency | Derivatives |",
            "|--------|------|------|----------|----------|-------------|",
        ]

        for desc in results:
            c = desc.contract
            if not c:
                continue

            name = c.symbol
            derivs = ", ".join(desc.derivativeSecTypes) if desc.derivativeSecTypes else "—"

            lines.append(
                f"| {c.symbol} | {name} | {c.secType} | "
                f"{c.primaryExchange or c.exchange} | {c.currency} | {derivs} |"
            )

        lines.append(f"\n**Results**: {len(results)}")

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, f"searching contracts for '{params.query}'")
