"""
Dashboard REST API — custom routes added to the MCP server.

Registers /api/* endpoints via @mcp.custom_route() so they're served
from the same process/port as the MCP protocol. The frontend calls
these directly instead of routing through the Anthropic API.

Must be imported AFTER server_http.py swaps app.mcp and registers tools.
"""

import asyncio
import logging
import math
import threading
import time
from decimal import Decimal
from typing import Optional

import yfinance as yf
from google import genai
from google.genai import types
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from starlette.requests import Request
from starlette.responses import JSONResponse

import functools as _functools


def _param_errors_to_400(handler):
    """Turn malformed/missing query-param errors into a clean 400 (F-04).

    The /api/* routes cast query params (int/float) and index required ones
    (qp["symbol"]). A bad value or missing key otherwise raises straight out
    as an uncaught 500. Catch the parse-level exceptions here and return a
    structured 400; genuine downstream errors still propagate.
    """
    @_functools.wraps(handler)
    async def _wrapped(request):
        try:
            return await handler(request)
        except (KeyError, ValueError, TypeError) as exc:
            return JSONResponse(
                {"error": f"bad or missing query parameter: {exc}"},
                status_code=400,
            )
    return _wrapped

from app import mcp
from core.formatting import get_decimal
from core import SNAPSHOT_SETTLE_SECS
from core.fx import apply_fx_fallbacks, build_fx_cache

# Tool functions and input models
from tools.account import (
    ibkr_margin, ibkr_get_account_summary, ibkr_get_account_pnl,
    MarginInput, AccountInput,
)
from tools.intelligence import ibkr_currency, CurrencyInput
from tools.risk import ibkr_stress_test, ibkr_what_if, StressTestInput, WhatIfInput
from tools.orders import ibkr_get_orders, ibkr_trades, OrdersInput, TradesInput
from tools.market_data import ibkr_dividends, ibkr_technicals, DividendInput, TechnicalsInput
from tools.monitoring import (
    ibkr_connection_status, ibkr_margin_history, MarginHistoryInput,
)
from tools.risk import ibkr_margin_ladder, MarginLadderInput
from tools.news import ibkr_news, NewsInput
from tools.fundamentals import ibkr_fundamentals, FundamentalsInput
from tools.scanner import ibkr_scanner, ScannerInput
from tools.shortable import ibkr_shortable, ShortableInput
from tools.volatility import ibkr_volatility, VolatilityInput
from tools.risk import ibkr_correlation_matrix, ibkr_var_estimate, CorrelationInput, VarInput
from tools.monitoring import ibkr_drawdown_tracker, DrawdownInput
from tools.intelligence import (
    ibkr_sector_exposure, ibkr_portfolio_beta, ibkr_rebalance_planner,
    SectorInput, BetaInput, RebalanceInput,
)
from tools.briefing import (
    ibkr_geopolitical_risk, ibkr_thesis_check, GeopoliticalInput, ThesisCheckInput,
)
from tools.live_data import (
    ibkr_compare_performance,
    ibkr_get_option_chain,
    OptionChainInput,
    PerformanceInput,
)

logger = logging.getLogger("ibkr_mcp.dashboard")

# Dashboard-specific config (from env)
import os
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MCP_URL = os.environ.get("MCP_URL", "")
YAHOO_CACHE_TTL = int(os.environ.get("YAHOO_CACHE_TTL", "30"))

# Yahoo Finance price cache: symbol -> (data, timestamp)
# Guarded by _cache_lock: /api/prices fans out across worker threads, so reads,
# the eviction sweep, and writes would otherwise race on the same dict.
_price_cache: dict[str, tuple[dict, float]] = {}
_cache_lock = threading.Lock()


# ── Helpers ──────────────────────────────────────────────────────────

class _FakeRequestContext:
    def __init__(self, lc: dict):
        self.lifespan_context = lc


class _FakeContext:
    """Shim that satisfies ctx.request_context.lifespan_context access."""
    def __init__(self, lc: dict):
        self.request_context = _FakeRequestContext(lc)


async def _ensure_connected_and_ctx() -> _FakeContext:
    """
    Get a fake MCP context wrapping the current IB connections.

    Triggers a connection if the server hasn't connected yet (REST route
    hit before the first MCP session). Uses the same lock and globals
    as server_http.py's http_lifespan.
    """
    sh = _sh()  # Use __main__ module, not a re-import

    async with sh._ib_lock:
        needs_connect = (
            sh._ib is None
            or not sh._ib.isConnected()
            or not sh._health.connected
        )
        if needs_connect:
            sh._ib, sh._primary_account = await sh._connect_ib()
            sh._account_map = {}
            sh._health_map = {}
            for acc in sh._ib.managedAccounts():
                sh._account_map[acc] = sh._ib
                sh._health_map[acc] = sh._health

            # Secondary
            if sh.IB_PORT_2:
                sh._ib2, sh._secondary_account = await sh._connect_ib2()
                if sh._ib2:
                    for acc in sh._ib2.managedAccounts():
                        sh._account_map[acc] = sh._ib2
                        sh._health_map[acc] = sh._health2

    return _FakeContext({
        "ib": sh._ib,
        "ib2": sh._ib2,
        "primary_account": sh._primary_account,
        "secondary_account": sh._secondary_account,
        "account_map": sh._account_map,
        "health": sh._health,
        "health_map": sh._health_map,
    })


def _sh():
    """Return the live server_http module (__main__ when run as a script).

    `import server_http` re-imports the file as a fresh module with all
    globals reset — it does NOT return the already-running __main__ module.
    Using sys.modules['__main__'] gives us the actual running instance.
    """
    import sys
    return sys.modules["__main__"]


def _get_ib(account: Optional[str] = None):
    """Get IB instance from module globals (non-async, for data routes)."""
    sh = _sh()
    if account and account in sh._account_map:
        return sh._account_map[account]
    return sh._ib


def _get_accounts() -> list[str]:
    sh = _sh()
    accounts: list[str] = []
    if sh._ib is not None and sh._ib.isConnected():
        accounts.extend(sh._ib.managedAccounts())
    if sh._ib2 is not None and sh._ib2.isConnected():
        for acc in sh._ib2.managedAccounts():
            if acc not in accounts:
                accounts.append(acc)
    return accounts


def _to_float(d: Optional[Decimal]) -> Optional[float]:
    return float(d) if d is not None else None


# ── Structured Data Endpoints ────────────────────────────────────────

def _account_summary_json(ib, account: str) -> dict:
    """Extract key account metrics as JSON-friendly dict."""
    try:
        summary = ib.accountSummary(account)
    except Exception as e:
        logger.warning(f"accountSummary failed for {account}: {e}")
        return {"account": account, "error": str(e)}
    if not summary:
        return {"account": account, "error": "No summary available"}

    vals = {item.tag: (item.value, item.currency) for item in summary}

    nlv = get_decimal(vals, "NetLiquidation")
    gpv = get_decimal(vals, "GrossPositionValue")
    init_margin = get_decimal(vals, "InitMarginReq")
    maint_margin = get_decimal(vals, "MaintMarginReq")
    excess_init = get_decimal(vals, "ExcessLiquidity")
    cushion_raw = get_decimal(vals, "Cushion")
    currency = vals.get("NetLiquidation", (None, "USD"))[1]

    return {
        "account": account,
        "currency": currency,
        "nlv": _to_float(nlv),
        "gpv": _to_float(gpv),
        "cash": _to_float(get_decimal(vals, "TotalCashValue")),
        "buying_power": _to_float(get_decimal(vals, "BuyingPower")),
        "init_margin": _to_float(init_margin),
        "maint_margin": _to_float(maint_margin),
        "excess_liquidity": _to_float(excess_init),
        "full_excess_liquidity": _to_float(get_decimal(vals, "FullExcessLiquidity")),
        "cushion_pct": float(cushion_raw * 100) if cushion_raw is not None else None,
        "leverage": float(gpv / nlv) if nlv and nlv != 0 else None,
        "margin_util_pct": float(init_margin / nlv * 100) if nlv and nlv != 0 else None,
    }


async def _positions_json(ib, account: str) -> list[dict]:
    """Extract positions as JSON-friendly list.

    weight_pct is a fraction of the account's TOTAL portfolio value in the base
    currency. A mixed-currency book (e.g. USD + CAD legs) must have each foreign
    leg's native marketValue FX-converted to base before summing/weighting —
    otherwise the total mixes unlike units and every weight is wrong. This
    mirrors the tools-layer fix (risk.py, intelligence.py portfolio_beta) that
    already converts via core.fx.build_fx_cache before weighting.
    """
    try:
        items = list(ib.portfolio(account))
    except Exception as e:
        logger.warning(f"portfolio() failed for {account}: {e}")
        return []
    if not items:
        return []

    # Base currency = the account's NLV currency (same convention the tools
    # layer uses). accountSummary is cheap and already fetched elsewhere.
    base_currency = "USD"
    try:
        summary = ib.accountSummary(account)
        for item in summary:
            if item.tag == "NetLiquidation":
                base_currency = item.currency or "USD"
                break
    except Exception as e:
        logger.warning(f"accountSummary failed for {account}, assuming USD base: {e}")

    # {ccy: rate_to_base} for every non-base currency held. apply_fx_fallbacks
    # backfills static rates when the live snapshot is unavailable so a foreign
    # leg never silently keeps its native (unconverted) value.
    currencies = {p.contract.currency for p in items}
    fx_cache = await build_fx_cache(ib, currencies, base_currency)
    used_fallbacks = apply_fx_fallbacks(fx_cache, currencies, base_currency)
    estimated = bool(used_fallbacks)

    def _base_value(p) -> Decimal:
        """abs(marketValue) converted to base currency (Decimal money math)."""
        mv = abs(Decimal(str(p.marketValue)))
        ccy = p.contract.currency
        if ccy != base_currency:
            rate = fx_cache.get(ccy)
            if rate is not None:
                mv *= rate
            else:
                # No live rate and no fallback: leg is left native. Flag the
                # whole response so downstream knows the weights are approximate
                # rather than silently mixing units.
                nonlocal estimated
                estimated = True
        return mv

    base_values = {id(p): _base_value(p) for p in items}
    total_value = sum(base_values.values())
    items.sort(key=lambda p: base_values[id(p)], reverse=True)

    return [
        {
            "symbol": p.contract.symbol,
            "sec_type": p.contract.secType,
            "shares": p.position,
            "avg_cost": p.averageCost,
            "market_price": p.marketPrice,
            "market_value": p.marketValue,
            # base-currency value — clients must sum THIS, not the native
            # market_value, or CAD legs inflate gross/leverage (Jeff 2026-08-05)
            "market_value_base": float(base_values[id(p)]),
            "unrealized_pnl": p.unrealizedPNL if not math.isnan(p.unrealizedPNL) else None,
            "currency": p.contract.currency,
            "weight_pct": round(
                float(base_values[id(p)] / total_value * 100), 2
            ) if total_value else 0.0,
            "estimated": estimated,
            "account": account,
        }
        for p in items
    ]


def _combine_account_summaries(results: list[dict]) -> dict:
    """Combine per-account summaries into a headline NLV banner.

    Summing NLVs across accounts is only meaningful when they share a base
    currency. The old code summed at 1:1 with no FX normalization and stamped
    the total with whatever currency the LAST account reported — a numerically
    meaningless figure under an arbitrary label the moment two accounts differ
    (e.g. one CAD margin book + one USD account). We don't have an FX rate in
    this path, so rather than emit a wrong number we refuse: if currencies
    differ, combined_nlv is None and mixed_currency flags the frontend to show
    per-account values instead. Accounts with no NLV (errored) are skipped.

    Returns {combined_nlv, currency, mixed_currency}.
    """
    funded = [r for r in results if r.get("nlv") is not None and r.get("currency")]
    if not funded:
        return {"combined_nlv": None, "currency": None, "mixed_currency": False}

    currencies = {r["currency"] for r in funded}
    if len(currencies) > 1:
        return {"combined_nlv": None, "currency": None, "mixed_currency": True}

    return {
        "combined_nlv": sum(r["nlv"] for r in funded),
        "currency": next(iter(currencies)),
        "mixed_currency": False,
    }


@mcp.custom_route("/api/summary", methods=["GET"])
@_param_errors_to_400
async def api_summary(request: Request) -> JSONResponse:
    """Account summary — structured JSON for NLV banner and margin bars."""
    account = request.query_params.get("account")

    if account:
        ib = _get_ib(account)
        return JSONResponse({"accounts": [_account_summary_json(ib, account)]})

    accounts = _get_accounts()
    results = [_account_summary_json(_get_ib(acc), acc) for acc in accounts]

    return JSONResponse({"accounts": results, **_combine_account_summaries(results)})


@mcp.custom_route("/api/positions", methods=["GET"])
@_param_errors_to_400
async def api_positions(request: Request) -> JSONResponse:
    """Portfolio positions — structured JSON for tables."""
    account = request.query_params.get("account")

    if account:
        ib = _get_ib(account)
        return JSONResponse({"positions": await _positions_json(ib, account)})

    all_positions = []
    for acc in _get_accounts():
        all_positions.extend(await _positions_json(_get_ib(acc), acc))

    # Merged view (same symbol across accounts)
    merged: dict[str, dict] = {}
    for p in all_positions:
        key = f"{p['symbol']}_{p['currency']}"
        if key not in merged:
            merged[key] = {"symbol": p["symbol"], "currency": p["currency"],
                           "shares": 0, "market_value": 0.0, "unrealized_pnl": 0.0}
        merged[key]["shares"] += p["shares"]
        merged[key]["market_value"] += p["market_value"]
        if p["unrealized_pnl"] is not None:
            merged[key]["unrealized_pnl"] += p["unrealized_pnl"]

    merged_list = sorted(merged.values(), key=lambda x: abs(x["market_value"]), reverse=True)
    return JSONResponse({"positions": all_positions, "merged": merged_list})


# ── Yahoo Finance Prices ─────────────────────────────────────────────

def _fetch_yahoo_quote(symbol: str) -> dict:
    now = time.time()

    with _cache_lock:
        # Evict stale entries on read (keeps cache bounded over long-running sessions)
        if len(_price_cache) > 100:
            stale = [k for k, (_, ts) in _price_cache.items() if now - ts > YAHOO_CACHE_TTL * 10]
            for k in stale:
                del _price_cache[k]

        cached = _price_cache.get(symbol)
        if cached and (now - cached[1]) < YAHOO_CACHE_TTL:
            return cached[0]

    # The yfinance call is deliberately OUTSIDE the lock — it is a blocking
    # network round-trip, and holding the lock across it would serialize the
    # whole fan-out that _fetch_yahoo_quote is being threaded for.
    try:
        info = yf.Ticker(symbol).fast_info
        data = {
            "symbol": symbol,
            "price": getattr(info, "last_price", None),
            "previous_close": getattr(info, "previous_close", None),
            "open": getattr(info, "open", None),
            "day_high": getattr(info, "day_high", None),
            "day_low": getattr(info, "day_low", None),
            "currency": getattr(info, "currency", "USD"),
            "timestamp": now,
        }
        if data["price"] and data["previous_close"]:
            data["change"] = data["price"] - data["previous_close"]
            data["change_pct"] = data["change"] / data["previous_close"] * 100
        else:
            data["change"] = data["change_pct"] = None
        with _cache_lock:
            _price_cache[symbol] = (data, now)
        return data
    except Exception as e:
        logger.warning(f"Yahoo Finance error for {symbol}: {e}")
        return {"symbol": symbol, "error": str(e), "timestamp": now}


async def _fetch_overnight_prices(symbols: list[str]) -> dict:
    """Live overnight prints off IB's OVERNIGHT venue, same JSON shape as
    the yahoo path. Best-effort: any failure returns {} and the caller falls
    back to yahoo — quotes are garnish, never an outage."""
    from ib_insync import Stock

    from core.formatting import price_or_none
    from core.sessions import is_overnight

    if not is_overnight() or not symbols:
        return {}
    # only plain US-equity tickers have an overnight book — yahoo-style
    # symbols (BTC-USD, DX-Y.NYB, ^VIX, ES=F) just spam Error 200 at the
    # qualify step, one per sweep
    import re as _re
    equities = [s for s in symbols if _re.fullmatch(r"[A-Z]{1,5}", s)]
    if not equities:
        return {}
    try:
        await _ensure_connected_and_ctx()
        ib = _get_ib()
        contracts = [Stock(sym, "OVERNIGHT", "USD") for sym in equities]
        qualified = [c for c in await ib.qualifyContractsAsync(*contracts) if c.conId]
        if not qualified:
            return {}
        for c in qualified:
            ib.reqMktData(c, "", True, False)
        await asyncio.sleep(SNAPSHOT_SETTLE_SECS)
        now = time.time()
        out: dict = {}
        for c in qualified:
            t = ib.ticker(c)
            last = price_or_none(t.last) if t else None
            close = price_or_none(t.close) if t else None
            if last is None:
                continue                      # no print → let yahoo answer
            row = {
                "symbol": c.symbol, "price": last,
                "previous_close": close,
                "open": None, "day_high": None, "day_low": None,
                "currency": "USD", "timestamp": now,
                "session": "overnight", "source": "ibkr_overnight",
            }
            row["change"] = (last - close) if close else None
            row["change_pct"] = (row["change"] / close * 100) if close else None
            out[c.symbol] = row
        return out
    except Exception as e:                    # noqa: BLE001 — garnish, not outage
        logger.warning(f"overnight prices failed, falling back to yahoo: {e}")
        return {}


@mcp.custom_route("/api/prices", methods=["GET"])
@_param_errors_to_400
async def api_prices(request: Request) -> JSONResponse:
    """Live prices. Overnight (Sun 20:00 → Fri 20:00 ET, ex-daytime) the
    IBKR OVERNIGHT venue answers first — yahoo's REST side freezes at the
    20:00 print all night. Yahoo fills whatever IB has no book for."""
    raw = request.query_params.get("symbols", "")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    ibkr = await _fetch_overnight_prices(symbols)
    remaining = [s for s in symbols if s not in ibkr]
    # yfinance is blocking, so each symbol goes to a worker thread and they run
    # concurrently. Serially inline this stalled the event loop for the sum of
    # every symbol's round-trip, freezing all other /api routes meanwhile.
    fetched = await asyncio.gather(
        *(asyncio.to_thread(_fetch_yahoo_quote, sym) for sym in remaining)
    )
    results = {**dict(zip(remaining, fetched)), **ibkr}
    return JSONResponse({"prices": results,
                         "source": "ibkr_overnight+yahoo" if ibkr else "yahoo_finance"})


# ── Tool Proxy Routes (return markdown) ──────────────────────────────

@mcp.custom_route("/api/margin", methods=["GET"])
@_param_errors_to_400
async def api_margin(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    params = MarginInput(
        detail=request.query_params.get("detail", "summary"),
        symbol=request.query_params.get("symbol"),
        account=request.query_params.get("account"),
    )
    return JSONResponse({"markdown": await ibkr_margin(params, ctx)})


@mcp.custom_route("/api/account-summary", methods=["GET"])
@_param_errors_to_400
async def api_account_summary_md(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    params = AccountInput(account=request.query_params.get("account"))
    return JSONResponse({"markdown": await ibkr_get_account_summary(params, ctx)})


@mcp.custom_route("/api/account-pnl", methods=["GET"])
@_param_errors_to_400
async def api_account_pnl(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    params = AccountInput(account=request.query_params.get("account"))
    return JSONResponse({"markdown": await ibkr_get_account_pnl(params, ctx)})


@mcp.custom_route("/api/currency", methods=["GET"])
@_param_errors_to_400
async def api_currency(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    params = CurrencyInput(account=request.query_params.get("account"))
    return JSONResponse({"markdown": await ibkr_currency(params, ctx)})


@mcp.custom_route("/api/stress", methods=["GET"])
@_param_errors_to_400
async def api_stress(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    dd = qp.get("drawdown_pct")
    params = StressTestInput(
        scenario=qp.get("scenario", "preflight"),
        drawdown_pct=float(dd) if dd else None,
        sigma_multiplier=float(qp.get("sigma_multiplier", "2.0")),
        account=qp.get("account"),
    )
    return JSONResponse({"markdown": await ibkr_stress_test(params, ctx)})


@mcp.custom_route("/api/what-if", methods=["GET"])
@_param_errors_to_400
async def api_what_if(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = WhatIfInput(
        action=qp["action"], symbol=qp["symbol"],
        quantity=int(qp["quantity"]), account=qp.get("account"),
    )
    return JSONResponse({"markdown": await ibkr_what_if(params, ctx)})


@mcp.custom_route("/api/fills", methods=["GET"])
@_param_errors_to_400
async def api_fills(request: Request) -> JSONResponse:
    """Structured executions (JSON, not markdown) — feeds fragwire's fills
    ledger. IB serves ~7 days back; the ledger's daily sweep + exec_id
    dedupe turns that window into permanent history."""
    from datetime import datetime, timedelta

    from ib_insync import ExecutionFilter

    from core.formatting import fills_to_json
    await _ensure_connected_and_ctx()
    qp = request.query_params
    account = qp.get("account") or _get_accounts()[0]
    days = min(int(qp.get("days", "7")), 7)
    ib = _get_ib(account)
    since = datetime.now() - timedelta(days=days)
    fills = await ib.reqExecutionsAsync(
        ExecutionFilter(acctCode=account, time=since.strftime("%Y%m%d 00:00:00")))
    fills = [f for f in fills if f.execution.acctNumber == account]
    return JSONResponse({"account": account, "fills": fills_to_json(fills)})


@mcp.custom_route("/api/trades", methods=["GET"])
@_param_errors_to_400
async def api_trades(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = TradesInput(
        view=qp.get("view", "fills"),
        account=qp.get("account"),
        symbol_filter=qp.get("symbol_filter"),
    )
    return JSONResponse({"markdown": await ibkr_trades(params, ctx)})


@mcp.custom_route("/api/orders", methods=["GET"])
@_param_errors_to_400
async def api_orders(request: Request) -> JSONResponse:
    """Open orders from IBKR's live read-only order view."""
    ctx = await _ensure_connected_and_ctx()
    params = OrdersInput(account=request.query_params.get("account"))
    return JSONResponse({"markdown": await ibkr_get_orders(params, ctx)})


@mcp.custom_route("/api/dividends", methods=["GET"])
@_param_errors_to_400
async def api_dividends(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = DividendInput(
        scope=qp.get("scope", "calendar"),
        symbol=qp.get("symbol"),
        account=qp.get("account"),
    )
    return JSONResponse({"markdown": await ibkr_dividends(params, ctx)})


@mcp.custom_route("/api/technicals", methods=["GET"])
@_param_errors_to_400
async def api_technicals(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = TechnicalsInput(symbol=qp["symbol"], sections=qp.get("sections", "all"))
    return JSONResponse({"markdown": await ibkr_technicals(params, ctx)})


@mcp.custom_route("/api/margin-ladder", methods=["GET"])
@_param_errors_to_400
async def api_margin_ladder(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    params = MarginLadderInput(account=request.query_params.get("account"))
    return JSONResponse({"markdown": await ibkr_margin_ladder(params, ctx)})


@mcp.custom_route("/api/margin-history", methods=["GET"])
@_param_errors_to_400
async def api_margin_history(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = MarginHistoryInput(
        days=int(qp.get("days", "30")), account=qp.get("account"),
    )
    return JSONResponse({"markdown": await ibkr_margin_history(params, ctx)})


@mcp.custom_route("/api/news", methods=["GET"])
@_param_errors_to_400
async def api_news(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = NewsInput(
        symbol=qp.get("symbol"),
        days=int(qp.get("days", "3")),
        max_items=int(qp.get("max_items", "10")),
        article_id=qp.get("article_id"),
        account=qp.get("account"),
    )
    return JSONResponse({"markdown": await ibkr_news(params, ctx)})


@mcp.custom_route("/api/fundamentals", methods=["GET"])
@_param_errors_to_400
async def api_fundamentals(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = FundamentalsInput(
        symbol=qp["symbol"], report=qp.get("report", "snapshot"),
    )
    return JSONResponse({"markdown": await ibkr_fundamentals(params, ctx)})


@mcp.custom_route("/api/scanner", methods=["GET"])
@_param_errors_to_400
async def api_scanner(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    kwargs = {
        "scan_code": qp.get("scan_code", "TOP_PERC_GAIN"),
        "instrument": qp.get("instrument", "STK"),
        "location": qp.get("location", "STK.US.MAJOR"),
        "max_rows": int(qp.get("max_rows", "15")),
    }
    if qp.get("above_price"):
        kwargs["above_price"] = float(qp["above_price"])
    if qp.get("above_volume"):
        kwargs["above_volume"] = int(qp["above_volume"])
    if qp.get("market_cap_above"):
        kwargs["market_cap_above"] = float(qp["market_cap_above"])
    return JSONResponse({"markdown": await ibkr_scanner(ScannerInput(**kwargs), ctx)})


@mcp.custom_route("/api/shortable", methods=["GET"])
@_param_errors_to_400
async def api_shortable(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = ShortableInput(symbols=qp.get("symbols"), account=qp.get("account"))
    return JSONResponse({"markdown": await ibkr_shortable(params, ctx)})


@mcp.custom_route("/api/volatility", methods=["GET"])
@_param_errors_to_400
async def api_volatility(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = VolatilityInput(
        symbol=qp.get("symbol"),
        lookback_days=int(qp.get("lookback_days", "252")),
        account=qp.get("account"),
    )
    return JSONResponse({"markdown": await ibkr_volatility(params, ctx)})


@mcp.custom_route("/api/options", methods=["GET"])
@_param_errors_to_400
async def api_options(request: Request) -> JSONResponse:
    """Available expirations or a live near-ATM option chain."""
    qp = request.query_params
    params = OptionChainInput(
        symbol=qp["symbol"].strip().upper(),
        expiration=qp.get("expiration"),
        strikes_around_atm=int(qp.get("strikes_around_atm", "5")),
    )
    ctx = await _ensure_connected_and_ctx()
    return JSONResponse({"markdown": await ibkr_get_option_chain(params, ctx)})


# --- Tier-1 risk / intelligence routes (ported for /gateway commands) ---

@mcp.custom_route("/api/drawdown", methods=["GET"])
@_param_errors_to_400
async def api_drawdown(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    peak = qp.get("peak_nlv")
    params = DrawdownInput(peak_nlv=float(peak) if peak else None,
                           account=qp.get("account"))
    return JSONResponse({"markdown": await ibkr_drawdown_tracker(params, ctx)})


@mcp.custom_route("/api/var", methods=["GET"])
@_param_errors_to_400
async def api_var(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = VarInput(lookback_days=int(qp.get("lookback_days", "252")),
                      confidence=qp.get("confidence", "both"),
                      account=qp.get("account"))
    return JSONResponse({"markdown": await ibkr_var_estimate(params, ctx)})


@mcp.custom_route("/api/correlation", methods=["GET"])
@_param_errors_to_400
async def api_correlation(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = CorrelationInput(lookback_days=int(qp.get("lookback_days", "60")),
                              account=qp.get("account"))
    return JSONResponse({"markdown": await ibkr_correlation_matrix(params, ctx)})


@mcp.custom_route("/api/sector", methods=["GET"])
@_param_errors_to_400
async def api_sector(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    params = SectorInput(account=request.query_params.get("account"))
    return JSONResponse({"markdown": await ibkr_sector_exposure(params, ctx)})


@mcp.custom_route("/api/beta", methods=["GET"])
@_param_errors_to_400
async def api_beta(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = BetaInput(benchmark=qp.get("benchmark", "SPY"),
                       lookback_days=int(qp.get("lookback_days", "60")),
                       account=qp.get("account"))
    return JSONResponse({"markdown": await ibkr_portfolio_beta(params, ctx)})


@mcp.custom_route("/api/geopolitical", methods=["GET"])
@_param_errors_to_400
async def api_geopolitical(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    params = GeopoliticalInput(account=request.query_params.get("account"))
    return JSONResponse({"markdown": await ibkr_geopolitical_risk(params, ctx)})


@mcp.custom_route("/api/thesis", methods=["GET"])
@_param_errors_to_400
async def api_thesis(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    news = request.query_params.get("news_item", "")
    if not news:
        return JSONResponse({"error": "news_item required"}, status_code=400)
    params = ThesisCheckInput(news_item=news)
    return JSONResponse({"markdown": await ibkr_thesis_check(params, ctx)})


@mcp.custom_route("/api/rebalance", methods=["GET"])
@_param_errors_to_400
async def api_rebalance(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    targets = qp.get("targets", "")
    if not targets:
        return JSONResponse({"error": "targets required"}, status_code=400)
    params = RebalanceInput(targets=targets, account=qp.get("account"))
    return JSONResponse({"markdown": await ibkr_rebalance_planner(params, ctx)})


@mcp.custom_route("/api/compare", methods=["GET"])
@_param_errors_to_400
async def api_compare(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    symbols = qp.get("symbols", "")
    if not symbols:
        return JSONResponse({"error": "symbols required"}, status_code=400)
    params = PerformanceInput(symbols=symbols, duration=qp.get("duration", "1 M"),
                              currency=qp.get("currency", "USD"))
    return JSONResponse({"markdown": await ibkr_compare_performance(params, ctx)})


@mcp.custom_route("/api/status", methods=["GET"])
@_param_errors_to_400
async def api_status(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    return JSONResponse({"markdown": await ibkr_connection_status(ctx)})


@mcp.custom_route("/api/health", methods=["GET"])
@_param_errors_to_400
async def api_health(request: Request) -> JSONResponse:
    sh = _sh()
    accounts = _get_accounts()

    # Check actual liveness — isConnected() can lie after silent TCP drops
    primary_live = (
        sh._ib is not None
        and sh._ib.isConnected()
        and sh._health.connected
    )
    secondary_live = (
        sh._ib2 is not None
        and sh._ib2.isConnected()
        and sh._health2.connected
    ) if sh.IB_PORT_2 else None  # None = not configured

    # Staleness: how long since last real data from IB
    primary_stale_s = (
        round(time.time() - sh._health.last_data_time, 1)
        if sh._health.last_data_time > 0 else None
    )
    secondary_stale_s = (
        round(time.time() - sh._health2.last_data_time, 1)
        if sh._health2.last_data_time > 0 else None
    ) if sh.IB_PORT_2 else None

    # Overall status: degraded if any configured gateway is down
    if not accounts:
        status = "offline"
    elif (not primary_live) or (sh.IB_PORT_2 and not secondary_live):
        status = "degraded"
    else:
        status = "ok"

    return JSONResponse({
        "status": status,
        "accounts": accounts,
        "primary": {"connected": primary_live, "last_data_age_s": primary_stale_s},
        "secondary": {"connected": secondary_live, "last_data_age_s": secondary_stale_s}
            if sh.IB_PORT_2 else None,
    })


# ── Gemini API Passthrough (Query tab only) ──────────────────────────

SYSTEM_PROMPT = (
    "You are a portfolio data assistant connected to IBKR via MCP.\n"
    "RULES:\n"
    "- Do NOT use ibkr_quote — no market data subscription.\n"
    "- When asked for multiple accounts, call each tool for each account in the SAME turn.\n"
    "- Return raw tool output. Minimal commentary."
)

# Try newer model first; fall back to 2.5-flash if the model ID is rejected.
_GEMINI_MODELS = ["gemini-3.0-flash", "gemini-2.5-flash"]


@mcp.custom_route("/api/query", methods=["POST"])
@_param_errors_to_400
async def api_query(request: Request) -> JSONResponse:
    """Gemini-powered natural language query with local MCP client loop."""
    if not GEMINI_API_KEY:
        return JSONResponse({"error": "GEMINI_API_KEY not configured"})
    if not MCP_URL:
        return JSONResponse({"error": "MCP_URL not configured"})

    body = await request.json()
    prompt = body.get("prompt", "")
    # F-03: a caller-supplied `system` must NOT replace the read-only guardrail
    # (that let any caller strip "Do NOT use ibkr_quote / return raw output").
    # Append it instead, so the guardrail always leads and can't be overridden.
    caller_system = (body.get("system") or "").strip()
    system = f"{SYSTEM_PROMPT}\n\n{caller_system}" if caller_system else SYSTEM_PROMPT

    try:
        gemini = genai.Client(api_key=GEMINI_API_KEY)

        async with streamablehttp_client(MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_resp = await session.list_tools()

                # Allowed JSON Schema keywords for Gemini FunctionDeclaration.
                # Gemini's Pydantic model rejects anything else (including $ref,
                # $defs, exclusiveMinimum, default, title, etc).
                _ALLOWED_KEYS = {
                    "type", "description", "enum", "properties", "required",
                    "items", "format", "nullable", "any_of", "anyOf",
                    "minimum", "maximum", "min_items", "max_items",
                    "min_length", "max_length", "pattern",
                    "min_properties", "max_properties",
                    "property_ordering", "example",
                }

                # Keys whose VALUE is a name->schema map (don't apply allowlist to map keys)
                _MAP_KEYS = {"properties", "$defs"}

                def _resolve_refs(schema, defs):
                    if isinstance(schema, dict):
                        if "$ref" in schema:
                            ref = schema["$ref"]
                            if ref.startswith("#/$defs/"):
                                key = ref.split("/")[-1]
                                return _resolve_refs(defs.get(key, {}), defs)
                        out = {}
                        for k, v in schema.items():
                            if k == "$defs" or k not in _ALLOWED_KEYS:
                                continue
                            if k in _MAP_KEYS and isinstance(v, dict):
                                # Don't filter the map's keys (they are field names);
                                # only recurse into the values.
                                out[k] = {mk: _resolve_refs(mv, defs) for mk, mv in v.items()}
                            else:
                                out[k] = _resolve_refs(v, defs)
                        return out
                    if isinstance(schema, list):
                        return [_resolve_refs(v, defs) for v in schema]
                    return schema

                def _flatten(input_schema):
                    if not isinstance(input_schema, dict):
                        return input_schema
                    defs = input_schema.get("$defs", {})
                    resolved = _resolve_refs(input_schema, defs)
                    # MCP wraps tool inputs as {properties: {params: <inner>}}.
                    # Unwrap so Gemini sees the inner schema directly — the
                    # wrapped form trips Gemini's "property is not defined"
                    # validator. We re-wrap on the call side.
                    props = resolved.get("properties") or {}
                    if (
                        list(props.keys()) == ["params"]
                        and isinstance(props["params"], dict)
                        and props["params"].get("type") == "object"
                    ):
                        return props["params"]
                    return resolved

                # Track which tools we unwrapped (for re-wrap on call)
                _unwrapped: dict[str, bool] = {}
                tool_decls = []
                for t in tools_resp.tools:
                    flat = _flatten(t.inputSchema)
                    orig_props = (t.inputSchema or {}).get("properties") or {}
                    _unwrapped[t.name] = list(orig_props.keys()) == ["params"]
                    tool_decls.append(
                        types.FunctionDeclaration(
                            name=t.name,
                            description=t.description or "",
                            parameters=flat,
                        )
                    )

                config = types.GenerateContentConfig(
                    system_instruction=system,
                    tools=[types.Tool(function_declarations=tool_decls)],
                    temperature=0.2,
                )
                texts: list[str] = []
                tool_results: list[str] = []

                # Pick model lazily: try preferred; if the API rejects it as
                # unknown on the first real call, fall back to 2.5-flash.
                chosen_model = _GEMINI_MODELS[0]
                contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]

                for iteration in range(8):
                    try:
                        resp = await gemini.aio.models.generate_content(
                            model=chosen_model, contents=contents, config=config,
                        )
                    except Exception as model_err:
                        msg = str(model_err).lower()
                        if (
                            iteration == 0
                            and chosen_model != _GEMINI_MODELS[-1]
                            and ("not found" in msg or "invalid" in msg or "404" in msg)
                        ):
                            logger.info(f"Model {chosen_model} unavailable, falling back")
                            chosen_model = _GEMINI_MODELS[-1]
                            resp = await gemini.aio.models.generate_content(
                                model=chosen_model, contents=contents, config=config,
                            )
                        else:
                            raise
                    cand = resp.candidates[0]
                    parts = cand.content.parts or []
                    fcs = [p.function_call for p in parts if p.function_call]
                    texts_this = [p.text for p in parts if p.text]

                    if not fcs:
                        texts.extend(texts_this)
                        break

                    # Model wants to call tools — execute via MCP
                    contents.append(cand.content)
                    fr_parts = []
                    for fc in fcs:
                        raw_args = dict(fc.args) if fc.args else {}
                        call_args = {"params": raw_args} if _unwrapped.get(fc.name) else raw_args
                        result = await session.call_tool(fc.name, call_args)
                        text_out = "\n".join(
                            c.text for c in result.content if hasattr(c, "text")
                        )
                        tool_results.append(text_out)
                        fr_parts.append(
                            types.Part.from_function_response(
                                name=fc.name, response={"result": text_out}
                            )
                        )
                    contents.append(types.Content(role="user", parts=fr_parts))

        return JSONResponse({
            "texts": texts,
            "tool_results": tool_results,
            "response": "\n\n".join(texts) or "\n\n".join(tool_results) or "No response.",
        })

    except BaseExceptionGroup as eg:
        import traceback
        leaves = []
        def _flatten(group):
            for sub in group.exceptions:
                if isinstance(sub, BaseExceptionGroup):
                    _flatten(sub)
                else:
                    leaves.append(sub)
        _flatten(eg)
        for sub in leaves:
            logger.error(f"Query leaf-exception: {type(sub).__name__}: {sub}")
            logger.error("".join(traceback.format_exception(type(sub), sub, sub.__traceback__)))
        details = [f"{type(s).__name__}: {s}" for s in leaves]
        return JSONResponse({"error": f"TaskGroup leaves: {' | '.join(details) or 'unknown'}"})
    except Exception as e:
        import traceback
        logger.error(f"Query error: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        return JSONResponse({"error": f"{type(e).__name__}: {e}"})
