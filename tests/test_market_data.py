"""Integration tests for tools/market_data.py — quotes, bars, contract details."""

import math
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from ib_insync import Stock
from ib_insync.contract import ContractDetails
from ib_insync.ticker import Ticker

from tools.market_data import (
    ibkr_get_quote,
    ibkr_get_historical_bars,
    ibkr_get_contract_details,
    QuoteInput,
    HistoricalInput,
    ContractInput,
)
from tests.conftest import make_ctx, make_mock_ib, make_bar


# --- Quotes ---

class TestGetQuote:
    def _make_ticker(self, last=140.0, bid=139.90, ask=140.10,
                     close=135.0, volume=5_000_000):
        """Build a Ticker with known values. NaN for unset fields."""
        t = MagicMock(spec=Ticker)
        t.last = last
        t.bid = bid
        t.ask = ask
        t.close = close
        t.volume = volume
        return t

    @pytest.mark.anyio
    async def test_basic_quote(self):
        ib = make_mock_ib()
        ticker = self._make_ticker()
        ib.ticker.return_value = ticker
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_quote(QuoteInput(symbol="AAPL"), ctx)

        assert "# AAPL Quote" in result
        assert "$140.00 USD" in result  # last
        assert "$139.90 USD" in result  # bid
        assert "$140.10 USD" in result  # ask
        assert "5,000,000" in result    # volume

    @pytest.mark.anyio
    async def test_change_calculation(self):
        ib = make_mock_ib()
        ticker = self._make_ticker(last=140.0, close=135.0)
        ib.ticker.return_value = ticker
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_quote(QuoteInput(symbol="AAPL"), ctx)

        # change = 140 - 135 = +5
        assert "+$5.00 USD" in result
        # change_pct = 5 / 135 * 100 ≈ 3.70%
        assert "+3.70%" in result

    @pytest.mark.anyio
    async def test_spread_calculation(self):
        ib = make_mock_ib()
        ticker = self._make_ticker(bid=139.90, ask=140.10)
        ib.ticker.return_value = ticker
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_quote(QuoteInput(symbol="AAPL"), ctx)

        # spread = 140.10 - 139.90 = 0.20
        assert "$0.20" in result

    @pytest.mark.anyio
    async def test_nan_fields_handled(self):
        """IB returns NaN for missing data — should show N/A, not crash."""
        ib = make_mock_ib()
        ticker = self._make_ticker()
        ticker.last = float("nan")
        ticker.bid = float("nan")
        ticker.ask = float("nan")
        ticker.volume = float("nan")
        ticker.close = 135.0
        ib.ticker.return_value = ticker
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_quote(QuoteInput(symbol="AAPL"), ctx)

        # Should not crash, should have N/A for volume
        assert "N/A" in result

    @pytest.mark.anyio
    async def test_contract_not_found(self):
        ib = make_mock_ib()
        ib.qualifyContractsAsync = AsyncMock(return_value=[])
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_quote(QuoteInput(symbol="ZZZZ"), ctx)

        assert "Could not find contract" in result

    @pytest.mark.anyio
    async def test_no_ticker_data(self):
        ib = make_mock_ib()
        ib.ticker.return_value = None
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_quote(QuoteInput(symbol="AAPL"), ctx)

        assert "No market data available" in result


# --- Historical Bars ---

class TestGetHistoricalBars:
    @pytest.mark.anyio
    async def test_basic_bars(self):
        bars = [
            make_bar(date(2026, 2, 26), 130.0, 135.0, 128.0, 133.0, 1000000),
            make_bar(date(2026, 2, 27), 133.0, 140.0, 132.0, 138.0, 1200000),
            make_bar(date(2026, 2, 28), 138.0, 142.0, 136.0, 141.0, 1100000),
        ]
        ib = make_mock_ib()
        ib.reqHistoricalDataAsync = AsyncMock(return_value=bars)
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_historical_bars(
            HistoricalInput(symbol="AAPL"), ctx
        )

        assert "# AAPL Historical Data" in result
        assert "Period High" in result
        assert "Period Low" in result

    @pytest.mark.anyio
    async def test_summary_stats(self):
        bars = [
            make_bar(date(2026, 2, 26), 100.0, 110.0, 95.0, 105.0, 500000),
            make_bar(date(2026, 2, 27), 105.0, 120.0, 100.0, 115.0, 600000),
        ]
        ib = make_mock_ib()
        ib.reqHistoricalDataAsync = AsyncMock(return_value=bars)
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_historical_bars(
            HistoricalInput(symbol="MSFT"), ctx
        )

        assert "$120.00" in result     # period high
        assert "$95.00" in result      # period low
        # period return = (115 - 105) / 105 * 100 ≈ 9.52%
        assert "+9.52%" in result

    @pytest.mark.anyio
    async def test_no_data(self):
        ib = make_mock_ib()
        ib.reqHistoricalDataAsync = AsyncMock(return_value=[])
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_historical_bars(
            HistoricalInput(symbol="AAPL"), ctx
        )

        assert "No historical data" in result

    @pytest.mark.anyio
    async def test_last_10_bars_shown(self):
        """Only last 10 bars should appear in the table."""
        bars = [
            make_bar(date(2026, 1, i + 1), 100.0, 105.0, 95.0, 100.0, 100000)
            for i in range(15)
        ]
        ib = make_mock_ib()
        ib.reqHistoricalDataAsync = AsyncMock(return_value=bars)
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_historical_bars(
            HistoricalInput(symbol="AAPL"), ctx
        )

        assert "Bars**: 15" in result
        # Table rows (excluding header and separator)
        table_rows = [l for l in result.split("\n")
                      if l.startswith("| 2026")]
        assert len(table_rows) == 10


# --- Contract Details ---

class TestGetContractDetails:
    @pytest.mark.anyio
    async def test_basic_details(self):
        contract = Stock("AAPL", "SMART", "USD")
        contract.conId = 4815747
        contract.primaryExchange = "NASDAQ"

        details = ContractDetails()
        details.contract = contract
        details.longName = "SectorLeader Corp"
        details.industry = "Technology"
        details.category = "Semiconductors"
        details.subcategory = "Semiconductor - Broad Line"
        details.minTick = 0.01
        details.priceMagnifier = 1
        details.validExchanges = "SMART,NASDAQ,NYSE"

        ib = make_mock_ib()
        ib.reqContractDetailsAsync = AsyncMock(return_value=[details])
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_contract_details(
            ContractInput(symbol="AAPL"), ctx
        )

        assert "SectorLeader Corp" in result
        assert "AAPL" in result
        assert "Semiconductors" in result
        assert "NASDAQ" in result

    @pytest.mark.anyio
    async def test_not_found(self):
        ib = make_mock_ib()
        ib.reqContractDetailsAsync = AsyncMock(return_value=[])
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_contract_details(
            ContractInput(symbol="ZZZZ"), ctx
        )

        assert "No contract details found" in result
