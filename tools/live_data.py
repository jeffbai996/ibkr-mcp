"""
Live data tools for ibkr_mcp.

Real-time market feeds that give Claude access to data it cannot get from
any other source: FX rates, intraday price action, multi-symbol comparison,
options chains, and relative performance analysis.
"""

import asyncio
import math
from decimal import Decimal
from typing import Optional

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from ib_insync import Stock, Forex, Option

from app import mcp
from core.connection import get_ib
from core.errors import handle_ib_error
from core.formatting import fmt_price, fmt_pct, fmt_pnl


# --- Input Models ---

class FxInput(BaseModel):
    pair: str = Field(
        ...,
        description="Currency pair (e.g., 'USDCAD', 'EURUSD'). "
                    "First 3 chars = base, last 3 = quote. "
                    "USDCAD = how many CAD per 1 USD."
    )


class IntradayInput(BaseModel):
    symbol: str = Field(..., description="Stock symbol (e.g., 'AAPL')")
    minutes: int = Field(
        default=30,
        description="Number of minutes of 1-min bars to show (1-120).",
        ge=1, le=120,
    )
    exchange: str = Field(default="SMART", description="Exchange")
    currency: str = Field(default="USD", description="Currency")


class CompareInput(BaseModel):
    symbols: str = Field(
        ...,
        description="Comma-separated symbols to compare (e.g., 'AAPL,AMD,TSLA,SMH'). Max 8."
    )
    currency: str = Field(default="USD", description="Currency")


class OptionChainInput(BaseModel):
    symbol: str = Field(..., description="Underlying stock symbol (e.g., 'AAPL')")
    expiration: Optional[str] = Field(
        default=None,
        description="Expiration date (YYYYMMDD, e.g., '20250321'). "
                    "If omitted, shows available expirations."
    )
    strikes_around_atm: int = Field(
        default=5,
        description="Number of strikes above and below ATM to show (1-10).",
        ge=1, le=10,
    )
    exchange: str = Field(default="SMART", description="Exchange")
    currency: str = Field(default="USD", description="Currency")


class PerformanceInput(BaseModel):
    symbols: str = Field(
        ...,
        description="Comma-separated symbols to compare (e.g., 'AAPL,SMH,SPY'). Max 8."
    )
    duration: str = Field(
        default="1 M",
        description="Lookback period: '5 D', '1 M', '3 M', '6 M', '1 Y'"
    )
    currency: str = Field(default="USD", description="Currency")


# --- Helpers ---

def _val(x):
    """Return None for NaN values from IB."""
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    return x


async def _qualify_stock(ib, symbol: str, exchange: str = "SMART",
                         currency: str = "USD") -> Stock | None:
    """Qualify a stock contract, return None on failure."""
    contract = Stock(symbol, exchange, currency)
    contracts = await ib.qualifyContractsAsync(contract)
    return contracts[0] if contracts else None


# --- Tools ---

@mcp.tool(
    name="ibkr_get_fx_rate",
    annotations={
        "title": "Get FX Rate",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_fx_rate(params: FxInput, ctx: Context) -> str:
    """Get live FX rate for a currency pair.

    Returns bid, ask, last, and daily change for the pair. Critical for
    cross-currency portfolios — converts between CAD and USD positions.

    Args:
        params (FxInput): Currency pair like 'USDCAD'.

    Returns:
        str: Markdown with live FX rate and spread.
    """
    try:
        ib = get_ib(ctx)

        pair = params.pair.upper().strip()
        if len(pair) != 6:
            return f"Invalid pair '{pair}'. Must be 6 chars (e.g., 'USDCAD')."

        base = pair[:3]
        quote = pair[3:]

        contract = Forex(pair)
        contracts = await ib.qualifyContractsAsync(contract)
        if not contracts:
            return f"Could not find FX pair {pair}. Try common pairs like USDCAD, EURUSD."

        qualified = contracts[0]

        # Request snapshot
        ib.reqMktData(qualified, "", True, False)
        await asyncio.sleep(2)

        ticker = ib.ticker(qualified)

        bid = _val(ticker.bid) if ticker else None
        ask = _val(ticker.ask) if ticker else None
        last = _val(ticker.last) if ticker else None
        close = _val(ticker.close) if ticker else None
        high = _val(ticker.high) if ticker else None
        low = _val(ticker.low) if ticker else None

        # Use midpoint as rate if last not available
        rate = last
        if rate is None and bid and ask:
            rate = (bid + ask) / 2

        if rate is None:
            return f"No FX data available for {pair}."

        spread = Decimal(str(ask)) - Decimal(str(bid)) if bid and ask else None
        change = Decimal(str(rate)) - Decimal(str(close)) if rate and close else None
        change_pct = change / Decimal(str(close)) * 100 if change and close else None

        # Show inverse rate too
        inverse = 1 / Decimal(str(rate)) if rate else None

        lines = [
            f"# {pair} Exchange Rate",
            "",
            f"**Rate**: {Decimal(str(rate)).quantize(Decimal('0.00001'))} "
            f"(1 {base} = {Decimal(str(rate)).quantize(Decimal('0.0001'))} {quote})",
            f"**Inverse**: {inverse.quantize(Decimal('0.00001')) if inverse else 'N/A'} "
            f"(1 {quote} = {inverse.quantize(Decimal('0.0001')) if inverse else 'N/A'} {base})",
            "",
            f"**Bid**: {Decimal(str(bid)).quantize(Decimal('0.00001')) if bid else 'N/A'}",
            f"**Ask**: {Decimal(str(ask)).quantize(Decimal('0.00001')) if ask else 'N/A'}",
            f"**Spread**: {spread.quantize(Decimal('0.00001')) if spread else 'N/A'}",
        ]

        if high or low:
            lines.extend([
                "",
                f"**Day High**: {Decimal(str(high)).quantize(Decimal('0.00001')) if high else 'N/A'}",
                f"**Day Low**: {Decimal(str(low)).quantize(Decimal('0.00001')) if low else 'N/A'}",
            ])

        if change is not None:
            lines.extend([
                "",
                f"**Close**: {Decimal(str(close)).quantize(Decimal('0.00001'))}",
                f"**Change**: {'+' if change > 0 else ''}{change.quantize(Decimal('0.00001'))}",
                f"**Change %**: {fmt_pct(change_pct)}",
            ])

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, f"fetching FX rate for {params.pair}")


@mcp.tool(
    name="ibkr_get_intraday_snapshot",
    annotations={
        "title": "Get Intraday Snapshot",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_intraday_snapshot(params: IntradayInput, ctx: Context) -> str:
    """Get recent intraday price action as 1-minute bars.

    Shows the last N minutes of 1-min OHLCV data including extended hours.
    Useful for tracking real-time moves, identifying intraday levels,
    and monitoring pre/post-market activity.

    Args:
        params (IntradayInput): Symbol and number of minutes.

    Returns:
        str: Markdown with 1-min bars, current price, and session range.
    """
    try:
        ib = get_ib(ctx)

        qualified = await _qualify_stock(ib, params.symbol, params.exchange, params.currency)
        if not qualified:
            return f"Could not find contract for {params.symbol}."

        # Request enough bars to cover the requested window
        # IB returns bars in the duration window, so "3600 S" = 1 hour
        duration_secs = params.minutes * 60
        # IB duration format: use seconds for < 1 day
        if duration_secs <= 86400:
            duration_str = f"{duration_secs} S"
        else:
            duration_str = "1 D"

        bars = await ib.reqHistoricalDataAsync(
            qualified,
            endDateTime="",
            durationStr=duration_str,
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=False,
        )

        if not bars:
            return f"No intraday data available for {params.symbol}."

        # Trim to requested minutes
        bars = bars[-params.minutes:]

        ccy = params.currency
        current = bars[-1].close
        session_high = max(b.high for b in bars)
        session_low = min(b.low for b in bars)
        total_vol = sum(b.volume for b in bars if b.volume > 0)
        first_price = bars[0].open
        move = Decimal(str(current)) - Decimal(str(first_price))
        move_pct = move / Decimal(str(first_price)) * 100 if first_price else None

        lines = [
            f"# {params.symbol} Intraday ({params.minutes} min)",
            f"*{bars[0].date} to {bars[-1].date}*",
            "",
            f"**Current**: {fmt_price(current, ccy)}",
            f"**Session High**: {fmt_price(session_high, ccy)}",
            f"**Session Low**: {fmt_price(session_low, ccy)}",
            f"**Volume**: {f'{int(total_vol):,}' if total_vol else 'N/A'}",
            f"**Move**: {fmt_pnl(move, ccy)} ({fmt_pct(move_pct)})",
            "",
        ]

        # Show bars — if >20, show first 5 and last 10 with gap
        if len(bars) <= 20:
            show_bars = bars
            lines.append("| Time | Open | High | Low | Close | Vol |")
            lines.append("|------|------|------|-----|-------|-----|")
            for b in show_bars:
                time_str = str(b.date).split(" ")[-1] if " " in str(b.date) else str(b.date)
                vol_str = f"{int(b.volume):,}" if b.volume > 0 else "—"
                lines.append(
                    f"| {time_str} | {fmt_price(b.open, '')} | "
                    f"{fmt_price(b.high, '')} | {fmt_price(b.low, '')} | "
                    f"{fmt_price(b.close, '')} | {vol_str} |"
                )
        else:
            lines.append("| Time | Open | High | Low | Close | Vol |")
            lines.append("|------|------|------|-----|-------|-----|")
            for b in bars[:5]:
                time_str = str(b.date).split(" ")[-1] if " " in str(b.date) else str(b.date)
                vol_str = f"{int(b.volume):,}" if b.volume > 0 else "—"
                lines.append(
                    f"| {time_str} | {fmt_price(b.open, '')} | "
                    f"{fmt_price(b.high, '')} | {fmt_price(b.low, '')} | "
                    f"{fmt_price(b.close, '')} | {vol_str} |"
                )
            lines.append(f"| ... | *{len(bars) - 15} bars omitted* | | | | |")
            for b in bars[-10:]:
                time_str = str(b.date).split(" ")[-1] if " " in str(b.date) else str(b.date)
                vol_str = f"{int(b.volume):,}" if b.volume > 0 else "—"
                lines.append(
                    f"| {time_str} | {fmt_price(b.open, '')} | "
                    f"{fmt_price(b.high, '')} | {fmt_price(b.low, '')} | "
                    f"{fmt_price(b.close, '')} | {vol_str} |"
                )

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, f"fetching intraday data for {params.symbol}")


@mcp.tool(
    name="ibkr_compare_symbols",
    annotations={
        "title": "Compare Symbol Quotes",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_compare_symbols(params: CompareInput, ctx: Context) -> str:
    """Compare live quotes for multiple symbols side by side.

    Batch-fetches snapshots for up to 8 symbols and shows them in a
    comparison table. Faster than calling ibkr_get_quote repeatedly.

    Args:
        params (CompareInput): Comma-separated symbols.

    Returns:
        str: Markdown comparison table with price, change, volume for each symbol.
    """
    try:
        ib = get_ib(ctx)

        raw_symbols = [s.strip().upper() for s in params.symbols.split(",") if s.strip()]
        if not raw_symbols:
            return "No symbols provided."
        if len(raw_symbols) > 8:
            raw_symbols = raw_symbols[:8]

        # Qualify all contracts in parallel
        contracts = []
        for sym in raw_symbols:
            contracts.append(Stock(sym, "SMART", params.currency))

        qualified = await ib.qualifyContractsAsync(*contracts)
        if not qualified:
            return "Could not qualify any of the provided symbols."

        # Map qualified contracts back to symbols
        qual_map = {}
        for c in qualified:
            if c.conId:  # Successfully qualified
                qual_map[c.symbol] = c

        failed = [s for s in raw_symbols if s not in qual_map]

        # Request snapshots for all qualified contracts
        for c in qual_map.values():
            ib.reqMktData(c, "", True, False)

        await asyncio.sleep(2.5)

        lines = [
            f"# Symbol Comparison",
            "",
            "| Symbol | Last | Change | Change % | Bid | Ask | Volume |",
            "|--------|------|--------|----------|-----|-----|--------|",
        ]

        for sym in raw_symbols:
            if sym not in qual_map:
                lines.append(f"| {sym} | *not found* | | | | | |")
                continue

            ticker = ib.ticker(qual_map[sym])
            if not ticker:
                lines.append(f"| {sym} | *no data* | | | | | |")
                continue

            last = _val(ticker.last)
            close = _val(ticker.close)
            bid = _val(ticker.bid)
            ask = _val(ticker.ask)
            volume = _val(ticker.volume)

            # Use close as fallback for last (outside RTH)
            if last is None:
                last = close

            change = None
            change_pct = None
            if last and close:
                change = Decimal(str(last)) - Decimal(str(close))
                change_pct = change / Decimal(str(close)) * 100

            lines.append(
                f"| {sym} | {fmt_price(last, '')} | "
                f"{fmt_pnl(change, '') if change is not None else 'N/A'} | "
                f"{fmt_pct(change_pct) if change_pct is not None else 'N/A'} | "
                f"{fmt_price(bid, '')} | {fmt_price(ask, '')} | "
                f"{f'{int(volume):,}' if volume else 'N/A'} |"
            )

        if failed:
            lines.append(f"\n*Could not find: {', '.join(failed)}*")

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, "comparing symbols")


@mcp.tool(
    name="ibkr_get_option_chain",
    annotations={
        "title": "Get Option Chain",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_get_option_chain(params: OptionChainInput, ctx: Context) -> str:
    """Get options chain for a stock — expirations, strikes, and live pricing.

    Without an expiration: shows available expirations and ATM strike.
    With an expiration: shows calls and puts near ATM with bid/ask and IV.

    Useful for evaluating hedges, covered calls, or reading market-implied
    probability distributions.

    Args:
        params (OptionChainInput): Symbol, optional expiry, strike range.

    Returns:
        str: Markdown with option chain data.
    """
    try:
        ib = get_ib(ctx)

        # Qualify the underlying
        underlying = await _qualify_stock(ib, params.symbol, params.exchange, params.currency)
        if not underlying:
            return f"Could not find underlying contract for {params.symbol}."

        # Get underlying price for ATM reference
        ib.reqMktData(underlying, "", True, False)
        await asyncio.sleep(1.5)
        und_ticker = ib.ticker(underlying)
        und_price = _val(und_ticker.last) if und_ticker else None
        if und_price is None and und_ticker:
            und_price = _val(und_ticker.close)
        if und_price is None:
            return f"Cannot determine {params.symbol} price for ATM reference."

        # Get available expirations and strikes
        chains = await ib.reqSecDefOptParamsAsync(
            params.symbol, "", "STK", underlying.conId
        )

        if not chains:
            return f"No options available for {params.symbol}."

        # Use SMART exchange chain if available, otherwise first
        chain = None
        for c in chains:
            if c.exchange == "SMART":
                chain = c
                break
        if chain is None:
            chain = chains[0]

        expirations = sorted(chain.expirations)
        strikes = sorted(chain.strikes)

        # If no expiration specified, show available expirations
        if not params.expiration:
            lines = [
                f"# {params.symbol} Options — Available Expirations",
                f"**Underlying Price**: {fmt_price(und_price, params.currency)}",
                f"**Exchange**: {chain.exchange}",
                f"**Multiplier**: {chain.multiplier}",
                "",
                f"**Total Expirations**: {len(expirations)}",
                f"**Total Strikes**: {len(strikes)}",
                "",
                "## Nearest Expirations",
            ]

            for exp in expirations[:12]:
                # Format YYYYMMDD as YYYY-MM-DD
                formatted = f"{exp[:4]}-{exp[4:6]}-{exp[6:]}"
                lines.append(f"- `{exp}` ({formatted})")

            if len(expirations) > 12:
                lines.append(f"- *...and {len(expirations) - 12} more*")

            lines.extend([
                "",
                f"*Call again with expiration parameter (e.g., '{expirations[0]}') "
                f"to see strikes with live pricing.*"
            ])

            return "\n".join(lines)

        # Expiration specified — show chain near ATM
        if params.expiration not in chain.expirations:
            closest = min(expirations, key=lambda e: abs(int(e) - int(params.expiration)))
            return (f"Expiration '{params.expiration}' not available. "
                    f"Closest: '{closest}'. Available: {', '.join(expirations[:10])}")

        # Find strikes near ATM
        atm_strike = min(strikes, key=lambda s: abs(s - und_price))
        n = params.strikes_around_atm
        atm_idx = strikes.index(atm_strike)
        start = max(0, atm_idx - n)
        end = min(len(strikes), atm_idx + n + 1)
        selected_strikes = strikes[start:end]

        # Build option contracts for calls and puts
        option_contracts = []
        for strike in selected_strikes:
            for right in ("C", "P"):
                opt = Option(params.symbol, params.expiration, strike, right, params.exchange)
                option_contracts.append(opt)

        # Qualify all at once
        qualified_opts = await ib.qualifyContractsAsync(*option_contracts)
        valid_opts = [o for o in qualified_opts if o.conId]

        if not valid_opts:
            return (f"Could not qualify options for {params.symbol} "
                    f"expiry {params.expiration}.")

        # Request snapshots for all valid options
        for opt in valid_opts:
            ib.reqMktData(opt, "", True, False)

        await asyncio.sleep(3)

        # Collect data by strike
        call_data = {}
        put_data = {}
        for opt in valid_opts:
            ticker = ib.ticker(opt)
            if not ticker:
                continue

            bid = _val(ticker.bid)
            ask = _val(ticker.ask)
            last = _val(ticker.last)
            volume = _val(ticker.volume)

            # Try to get IV from model greeks
            iv = None
            if ticker.modelGreeks:
                iv = _val(ticker.modelGreeks.impliedVol)
            delta = None
            if ticker.modelGreeks:
                delta = _val(ticker.modelGreeks.delta)

            data = {
                "bid": bid, "ask": ask, "last": last,
                "volume": volume, "iv": iv, "delta": delta,
            }

            if opt.right == "C":
                call_data[opt.strike] = data
            else:
                put_data[opt.strike] = data

        # Format output
        exp_fmt = f"{params.expiration[:4]}-{params.expiration[4:6]}-{params.expiration[6:]}"
        lines = [
            f"# {params.symbol} Options — {exp_fmt}",
            f"**Underlying**: {fmt_price(und_price, params.currency)} | "
            f"**ATM Strike**: {atm_strike}",
            "",
            "## Calls",
            "| Strike | Bid | Ask | Last | IV | Delta | Vol |",
            "|--------|-----|-----|------|----|-------|-----|",
        ]

        for strike in selected_strikes:
            d = call_data.get(strike, {})
            iv_str = f"{d.get('iv', 0) * 100:.1f}%" if d.get("iv") else "—"
            delta_str = f"{d.get('delta', 0):.3f}" if d.get("delta") else "—"
            vol_str = f"{int(d['volume']):,}" if d.get("volume") else "—"
            atm_marker = " *" if strike == atm_strike else ""
            lines.append(
                f"| {strike}{atm_marker} | "
                f"{fmt_price(d.get('bid'), '')} | "
                f"{fmt_price(d.get('ask'), '')} | "
                f"{fmt_price(d.get('last'), '')} | "
                f"{iv_str} | {delta_str} | {vol_str} |"
            )

        lines.extend([
            "",
            "## Puts",
            "| Strike | Bid | Ask | Last | IV | Delta | Vol |",
            "|--------|-----|-----|------|----|-------|-----|",
        ])

        for strike in selected_strikes:
            d = put_data.get(strike, {})
            iv_str = f"{d.get('iv', 0) * 100:.1f}%" if d.get("iv") else "—"
            delta_str = f"{d.get('delta', 0):.3f}" if d.get("delta") else "—"
            vol_str = f"{int(d['volume']):,}" if d.get("volume") else "—"
            atm_marker = " *" if strike == atm_strike else ""
            lines.append(
                f"| {strike}{atm_marker} | "
                f"{fmt_price(d.get('bid'), '')} | "
                f"{fmt_price(d.get('ask'), '')} | "
                f"{fmt_price(d.get('last'), '')} | "
                f"{iv_str} | {delta_str} | {vol_str} |"
            )

        lines.append(f"\n*ATM strike marked with \\**")

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, f"fetching option chain for {params.symbol}")


@mcp.tool(
    name="ibkr_compare_performance",
    annotations={
        "title": "Compare Performance",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ibkr_compare_performance(params: PerformanceInput, ctx: Context) -> str:
    """Compare historical returns across multiple symbols.

    Fetches daily bars for each symbol over the given period and calculates
    total return, sorted best to worst. Useful for relative strength analysis
    and comparing your holdings against benchmarks (SPY, SMH, etc.).

    Args:
        params (PerformanceInput): Comma-separated symbols and lookback.

    Returns:
        str: Markdown comparison table with returns and high/low for each symbol.
    """
    try:
        ib = get_ib(ctx)

        raw_symbols = [s.strip().upper() for s in params.symbols.split(",") if s.strip()]
        if not raw_symbols:
            return "No symbols provided."
        if len(raw_symbols) > 8:
            raw_symbols = raw_symbols[:8]

        # Qualify all contracts
        contracts = [Stock(sym, "SMART", params.currency) for sym in raw_symbols]
        qualified = await ib.qualifyContractsAsync(*contracts)
        qual_map = {c.symbol: c for c in qualified if c.conId}
        failed = [s for s in raw_symbols if s not in qual_map]

        if not qual_map:
            return "Could not qualify any of the provided symbols."

        # Request historical data for all qualified symbols in parallel
        bar_tasks = []
        bar_symbols = []
        for sym, contract in qual_map.items():
            task = ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=params.duration,
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
            )
            bar_tasks.append(task)
            bar_symbols.append(sym)

        all_bars = await asyncio.gather(*bar_tasks)

        # Calculate returns for each symbol
        results = []
        for sym, bars in zip(bar_symbols, all_bars):
            if not bars or len(bars) < 2:
                results.append({
                    "symbol": sym, "return_pct": None,
                    "start": None, "end": None,
                    "high": None, "low": None, "bars": 0,
                })
                continue

            start_price = bars[0].open
            end_price = bars[-1].close
            period_high = max(b.high for b in bars)
            period_low = min(b.low for b in bars)

            ret = ((end_price - start_price) / start_price * 100
                   if start_price else None)

            # Max drawdown from peak
            peak = bars[0].high
            max_dd = 0
            for b in bars:
                if b.high > peak:
                    peak = b.high
                dd = (b.low - peak) / peak * 100 if peak else 0
                if dd < max_dd:
                    max_dd = dd

            results.append({
                "symbol": sym,
                "return_pct": ret,
                "start": start_price,
                "end": end_price,
                "high": period_high,
                "low": period_low,
                "max_dd": max_dd,
                "bars": len(bars),
            })

        # Sort by return descending
        results.sort(
            key=lambda r: r["return_pct"] if r["return_pct"] is not None else float("-inf"),
            reverse=True,
        )

        lines = [
            f"# Performance Comparison — {params.duration}",
            "",
            "| Symbol | Return | Start | End | High | Low | Max DD |",
            "|--------|--------|-------|-----|------|-----|--------|",
        ]

        for r in results:
            if r["return_pct"] is None:
                lines.append(f"| {r['symbol']} | *no data* | | | | | |")
                continue

            lines.append(
                f"| {r['symbol']} | {fmt_pct(r['return_pct'])} | "
                f"{fmt_price(r['start'], '')} | {fmt_price(r['end'], '')} | "
                f"{fmt_price(r['high'], '')} | {fmt_price(r['low'], '')} | "
                f"{fmt_pct(r['max_dd'])} |"
            )

        if failed:
            lines.append(f"\n*Could not find: {', '.join(failed)}*")

        # Spread between best and worst
        valid = [r for r in results if r["return_pct"] is not None]
        if len(valid) >= 2:
            spread = valid[0]["return_pct"] - valid[-1]["return_pct"]
            lines.append(
                f"\n**Spread** (best - worst): {fmt_pct(spread)}"
            )

        return "\n".join(lines)

    except Exception as e:
        return handle_ib_error(e, "comparing performance")
