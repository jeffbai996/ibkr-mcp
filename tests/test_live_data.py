"""Tests for tools/live_data.py — FX, intraday, compare, options, performance."""

import math
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from ib_insync import Stock, Forex, Option
from ib_insync.ticker import Ticker

from tools.live_data import (
    ibkr_get_fx_rate,
    ibkr_get_intraday_snapshot,
    ibkr_get_option_chain,
    ibkr_compare_performance,
    FxInput,
    IntradayInput,
    OptionChainInput,
    PerformanceInput,
)
from tools.market_data import ibkr_quote, QuoteInput
from tests.conftest import make_ctx, make_mock_ib, make_bar


def _make_ticker(**kwargs):
    """Build a mock Ticker with given fields, NaN for unset."""
    t = MagicMock(spec=Ticker)
    t.last = kwargs.get("last", float("nan"))
    t.bid = kwargs.get("bid", float("nan"))
    t.ask = kwargs.get("ask", float("nan"))
    t.close = kwargs.get("close", float("nan"))
    t.high = kwargs.get("high", float("nan"))
    t.low = kwargs.get("low", float("nan"))
    t.volume = kwargs.get("volume", float("nan"))
    t.modelGreeks = kwargs.get("modelGreeks", None)
    t.dividends = kwargs.get("dividends", None)
    return t


# --- FX Rate ---

class TestFxRate:
    @pytest.mark.anyio
    async def test_basic_usdcad(self):
        ib = make_mock_ib()
        fx_contract = Forex("USDCAD")
        ib.qualifyContractsAsync = AsyncMock(return_value=[fx_contract])
        ib.ticker.return_value = _make_ticker(
            last=1.35500, bid=1.35490, ask=1.35510, close=1.35000,
        )
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_fx_rate(FxInput(pair="USDCAD"), ctx)

        assert "USDCAD" in result
        assert "1.3550" in result
        assert "Inverse" in result

    @pytest.mark.anyio
    async def test_invalid_pair_length(self):
        ctx = make_ctx()
        result = await ibkr_get_fx_rate(FxInput(pair="USD"), ctx)

        assert "Invalid pair" in result

    @pytest.mark.anyio
    async def test_pair_not_found(self):
        ib = make_mock_ib()
        ib.qualifyContractsAsync = AsyncMock(return_value=[])
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_fx_rate(FxInput(pair="AAABBB"), ctx)

        assert "Could not find FX pair" in result

    @pytest.mark.anyio
    async def test_midpoint_fallback(self):
        """When last is NaN, should use (bid+ask)/2."""
        ib = make_mock_ib()
        fx = Forex("USDCAD")
        ib.qualifyContractsAsync = AsyncMock(return_value=[fx])
        ib.ticker.return_value = _make_ticker(
            last=float("nan"), bid=1.35000, ask=1.36000, close=1.35000,
        )
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_fx_rate(FxInput(pair="USDCAD"), ctx)

        # midpoint = (1.35 + 1.36) / 2 = 1.355
        assert "1.3550" in result

    @pytest.mark.anyio
    async def test_no_data_at_all(self):
        ib = make_mock_ib()
        fx = Forex("USDCAD")
        ib.qualifyContractsAsync = AsyncMock(return_value=[fx])
        ib.ticker.return_value = _make_ticker()  # all NaN
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_fx_rate(FxInput(pair="USDCAD"), ctx)

        assert "No FX data" in result


# --- Intraday Snapshot ---

class TestIntradaySnapshot:
    @pytest.mark.anyio
    async def test_basic_snapshot(self):
        bars = [
            make_bar(datetime(2026, 3, 3, 10, 0), 140.0, 141.0, 139.5, 140.5, 10000),
            make_bar(datetime(2026, 3, 3, 10, 1), 140.5, 142.0, 140.0, 141.5, 12000),
            make_bar(datetime(2026, 3, 3, 10, 2), 141.5, 141.8, 140.5, 141.0, 8000),
        ]
        ib = make_mock_ib()
        ib.reqHistoricalDataAsync = AsyncMock(return_value=bars)
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_intraday_snapshot(
            IntradayInput(symbol="AAPL", minutes=3), ctx
        )

        assert "AAPL Intraday" in result
        assert "Current" in result
        assert "Session High" in result

    @pytest.mark.anyio
    async def test_move_calculation(self):
        bars = [
            make_bar(datetime(2026, 3, 3, 10, 0), 100.0, 101.0, 99.0, 100.0, 1000),
            make_bar(datetime(2026, 3, 3, 10, 1), 100.0, 105.0, 100.0, 105.0, 2000),
        ]
        ib = make_mock_ib()
        ib.reqHistoricalDataAsync = AsyncMock(return_value=bars)
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_intraday_snapshot(
            IntradayInput(symbol="AAPL", minutes=5), ctx
        )

        # move = 105 - 100 = +5, pct = +5%
        assert "+$5.00" in result
        assert "+5.00%" in result

    @pytest.mark.anyio
    async def test_no_data(self):
        ib = make_mock_ib()
        ib.reqHistoricalDataAsync = AsyncMock(return_value=[])
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_intraday_snapshot(
            IntradayInput(symbol="AAPL"), ctx
        )

        assert "No intraday data" in result

    @pytest.mark.anyio
    async def test_contract_not_found(self):
        ib = make_mock_ib()
        ib.qualifyContractsAsync = AsyncMock(return_value=[])
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_intraday_snapshot(
            IntradayInput(symbol="ZZZZ"), ctx
        )

        assert "Could not find contract" in result

    @pytest.mark.anyio
    async def test_long_snapshot_truncates(self):
        """>20 bars should show first 5 + last 10 with gap."""
        bars = [
            make_bar(datetime(2026, 3, 3, 10, i), 100.0, 101.0, 99.0, 100.0, 1000)
            for i in range(30)
        ]
        ib = make_mock_ib()
        ib.reqHistoricalDataAsync = AsyncMock(return_value=bars)
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_intraday_snapshot(
            IntradayInput(symbol="AAPL", minutes=30), ctx
        )

        assert "omitted" in result


# --- Compare Symbols ---

class TestCompareSymbols:
    """Compare symbols via ibkr_quote with multiple symbols."""

    @pytest.mark.anyio
    async def test_basic_comparison(self):
        ib = make_mock_ib()
        nvda = Stock("AAPL", "SMART", "USD"); nvda.conId = 1
        amd = Stock("AMD", "SMART", "USD"); amd.conId = 2
        ib.qualifyContractsAsync = AsyncMock(return_value=[nvda, amd])

        def ticker_for(contract):
            if contract.symbol == "AAPL":
                return _make_ticker(last=140.0, close=135.0, bid=139.9, ask=140.1, volume=5e6)
            return _make_ticker(last=120.0, close=118.0, bid=119.9, ask=120.1, volume=3e6)

        ib.ticker.side_effect = ticker_for
        ctx = make_ctx(ib=ib)

        result = await ibkr_quote(
            QuoteInput(symbols="AAPL,AMD"), ctx
        )

        assert "AAPL" in result
        assert "AMD" in result

    @pytest.mark.anyio
    async def test_failed_symbol_noted(self):
        ib = make_mock_ib()
        nvda = Stock("AAPL", "SMART", "USD"); nvda.conId = 1
        bad = Stock("ZZZZ", "SMART", "USD"); bad.conId = 0  # failed qualification
        ib.qualifyContractsAsync = AsyncMock(return_value=[nvda, bad])
        ib.ticker.return_value = _make_ticker(last=140.0, close=135.0)
        ctx = make_ctx(ib=ib)

        result = await ibkr_quote(
            QuoteInput(symbols="AAPL,ZZZZ"), ctx
        )

        assert "not found" in result or "ZZZZ" in result


# --- Option Chain ---

class TestOptionChain:
    def _make_chain(self, expirations=None, strikes=None):
        """Build a mock OptionChain (NamedTuple-like)."""
        chain = MagicMock()
        chain.exchange = "SMART"
        chain.underlyingConId = 12345
        chain.tradingClass = "AAPL"
        chain.multiplier = "100"
        chain.expirations = frozenset(expirations or ["20260320", "20260417", "20260515"])
        chain.strikes = frozenset(strikes or [130, 135, 140, 145, 150])
        return chain

    @pytest.mark.anyio
    async def test_show_expirations(self):
        """Without expiration param, shows available expirations."""
        ib = make_mock_ib()
        ib.ticker.return_value = _make_ticker(last=140.0)
        ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[self._make_chain()])
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_option_chain(
            OptionChainInput(symbol="AAPL"), ctx
        )

        assert "Available Expirations" in result
        assert "20260320" in result
        assert "20260417" in result

    @pytest.mark.anyio
    async def test_invalid_expiration(self):
        ib = make_mock_ib()
        ib.ticker.return_value = _make_ticker(last=140.0)
        ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[self._make_chain()])
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_option_chain(
            OptionChainInput(symbol="AAPL", expiration="20261231"), ctx
        )

        assert "not available" in result
        assert "Closest" in result

    @pytest.mark.anyio
    async def test_no_underlying_price(self):
        ib = make_mock_ib()
        ib.ticker.return_value = _make_ticker()  # all NaN
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_option_chain(
            OptionChainInput(symbol="AAPL"), ctx
        )

        assert "Cannot determine" in result

    @pytest.mark.anyio
    async def test_no_options_available(self):
        ib = make_mock_ib()
        ib.ticker.return_value = _make_ticker(last=140.0)
        ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[])
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_option_chain(
            OptionChainInput(symbol="AAPL"), ctx
        )

        assert "No options available" in result

    @pytest.mark.anyio
    async def test_chain_with_expiration(self):
        """With valid expiration, shows calls and puts tables."""
        ib = make_mock_ib()
        ib.ticker.return_value = _make_ticker(last=140.0, bid=2.50, ask=2.70)

        chain = self._make_chain()
        ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])

        # Mock option contract qualification
        opt = Option("AAPL", "20260320", 140, "C", "SMART")
        opt.conId = 99999
        ib.qualifyContractsAsync = AsyncMock(return_value=[opt])

        ctx = make_ctx(ib=ib)

        result = await ibkr_get_option_chain(
            OptionChainInput(symbol="AAPL", expiration="20260320"), ctx
        )

        assert "Calls" in result
        assert "Puts" in result
        assert "2026-03-20" in result


# --- Compare Performance ---

class TestComparePerformance:
    @pytest.mark.anyio
    async def test_basic_comparison(self):
        ib = make_mock_ib()
        nvda = Stock("AAPL", "SMART", "USD"); nvda.conId = 1
        spy = Stock("SPY", "SMART", "USD"); spy.conId = 2
        ib.qualifyContractsAsync = AsyncMock(return_value=[nvda, spy])

        nvda_bars = [
            make_bar(date(2026, 2, 1), 130.0, 135.0, 128.0, 133.0, 1e6),
            make_bar(date(2026, 3, 1), 133.0, 145.0, 130.0, 143.0, 1.2e6),
        ]
        spy_bars = [
            make_bar(date(2026, 2, 1), 500.0, 505.0, 498.0, 502.0, 5e6),
            make_bar(date(2026, 3, 1), 502.0, 510.0, 500.0, 508.0, 5.5e6),
        ]

        call_count = 0
        async def mock_hist(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return nvda_bars if call_count <= 1 else spy_bars

        ib.reqHistoricalDataAsync = mock_hist
        ctx = make_ctx(ib=ib)

        result = await ibkr_compare_performance(
            PerformanceInput(symbols="AAPL,SPY"), ctx
        )

        assert "Performance Comparison" in result
        assert "AAPL" in result
        assert "SPY" in result
        assert "Return" in result
        assert "Max DD" in result

    @pytest.mark.anyio
    async def test_sorted_by_return(self):
        """Best performer should appear first."""
        ib = make_mock_ib()
        a = Stock("A", "SMART", "USD"); a.conId = 1
        b = Stock("B", "SMART", "USD"); b.conId = 2
        ib.qualifyContractsAsync = AsyncMock(return_value=[a, b])

        # A: 100 -> 110 = +10%, B: 100 -> 120 = +20%
        a_bars = [
            make_bar(date(2026, 2, 1), 100.0, 105.0, 98.0, 100.0, 1000),
            make_bar(date(2026, 3, 1), 105.0, 112.0, 104.0, 110.0, 1000),
        ]
        b_bars = [
            make_bar(date(2026, 2, 1), 100.0, 105.0, 98.0, 100.0, 1000),
            make_bar(date(2026, 3, 1), 105.0, 122.0, 104.0, 120.0, 1000),
        ]

        call_count = 0
        async def mock_hist(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return a_bars if call_count <= 1 else b_bars

        ib.reqHistoricalDataAsync = mock_hist
        ctx = make_ctx(ib=ib)

        result = await ibkr_compare_performance(
            PerformanceInput(symbols="A,B"), ctx
        )

        # B (+20%) should appear before A (+10%)
        b_pos = result.index("B")
        a_pos = result.index("| A")
        assert b_pos < a_pos

    @pytest.mark.anyio
    async def test_spread_shown(self):
        ib = make_mock_ib()
        a = Stock("A", "SMART", "USD"); a.conId = 1
        b = Stock("B", "SMART", "USD"); b.conId = 2
        ib.qualifyContractsAsync = AsyncMock(return_value=[a, b])

        a_bars = [
            make_bar(date(2026, 2, 1), 100.0, 105.0, 98.0, 100.0, 1000),
            make_bar(date(2026, 3, 1), 100.0, 105.0, 98.0, 100.0, 1000),
        ]

        ib.reqHistoricalDataAsync = AsyncMock(return_value=a_bars)
        ctx = make_ctx(ib=ib)

        result = await ibkr_compare_performance(
            PerformanceInput(symbols="A,B"), ctx
        )

        assert "Spread" in result

    @pytest.mark.anyio
    async def test_no_symbols(self):
        ctx = make_ctx()
        result = await ibkr_compare_performance(
            PerformanceInput(symbols=""), ctx
        )

        assert "No symbols" in result

    @pytest.mark.anyio
    async def test_failed_symbols_noted(self):
        ib = make_mock_ib()
        a = Stock("A", "SMART", "USD"); a.conId = 1
        bad = Stock("ZZZZ", "SMART", "USD"); bad.conId = 0
        ib.qualifyContractsAsync = AsyncMock(return_value=[a, bad])
        ib.reqHistoricalDataAsync = AsyncMock(return_value=[
            make_bar(date(2026, 2, 1), 100.0, 105.0, 98.0, 100.0, 1000),
            make_bar(date(2026, 3, 1), 100.0, 105.0, 98.0, 100.0, 1000),
        ])
        ctx = make_ctx(ib=ib)

        result = await ibkr_compare_performance(
            PerformanceInput(symbols="A,ZZZZ"), ctx
        )

        assert "Could not find" in result
        assert "ZZZZ" in result
