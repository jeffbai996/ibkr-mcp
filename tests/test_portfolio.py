"""Integration tests for tools/portfolio.py — positions, snapshots, currency grouping."""

import pytest

from tools.portfolio import (
    ibkr_get_positions,
    ibkr_get_portfolio_snapshot,
    ibkr_get_portfolio_by_currency,
    PortfolioInput,
)
from tests.conftest import (
    make_ctx, make_mock_ib, make_portfolio_item, make_positions,
    make_summary, TEST_ACCOUNT,
)


# --- Positions ---

class TestGetPositions:
    @pytest.mark.anyio
    async def test_basic_table(self):
        ctx = make_ctx()
        result = await ibkr_get_positions(PortfolioInput(), ctx)

        assert f"# Positions: {TEST_ACCOUNT}" in result
        assert "AAPL" in result
        assert "MSFT" in result
        assert "SPY" in result
        # Should have markdown table headers
        assert "| Symbol |" in result

    @pytest.mark.anyio
    async def test_sorted_by_abs_value(self):
        """Largest positions should appear first."""
        ctx = make_ctx()
        result = await ibkr_get_positions(PortfolioInput(), ctx)

        lines = result.split("\n")
        data_lines = [l for l in lines if l.startswith("| ") and "Symbol" not in l and "---" not in l]
        # SPY (2.005M) should be first, MSFT (950K) second
        assert "SPY" in data_lines[0]
        assert "MSFT" in data_lines[1]

    @pytest.mark.anyio
    async def test_symbol_filter(self):
        ctx = make_ctx()
        result = await ibkr_get_positions(
            PortfolioInput(symbol_filter="MSFT"), ctx
        )

        assert "MSFT" in result
        assert "AAPL" not in result
        assert "SPY" not in result

    @pytest.mark.anyio
    async def test_filter_case_insensitive(self):
        ctx = make_ctx()
        result = await ibkr_get_positions(
            PortfolioInput(symbol_filter="nvda"), ctx
        )

        assert "AAPL" in result

    @pytest.mark.anyio
    async def test_no_positions(self):
        ib = make_mock_ib(positions=[])
        ctx = make_ctx(ib=ib)
        result = await ibkr_get_positions(PortfolioInput(), ctx)

        assert "No positions found" in result

    @pytest.mark.anyio
    async def test_filter_no_match(self):
        ctx = make_ctx()
        result = await ibkr_get_positions(
            PortfolioInput(symbol_filter="AAPL"), ctx
        )

        assert "No positions found" in result
        assert "AAPL" in result

    @pytest.mark.anyio
    async def test_totals_shown(self):
        ctx = make_ctx()
        result = await ibkr_get_positions(PortfolioInput(), ctx)

        assert "Total Market Value" in result
        assert "Total Unrealized P&L" in result
        assert "Positions" in result

    @pytest.mark.anyio
    async def test_weight_sums_to_roughly_100(self):
        """Position weights should add up close to 100%."""
        positions = [
            make_portfolio_item("A", 100, 50.0, 5000.0, 40.0, 1000.0),
            make_portfolio_item("B", 100, 50.0, 5000.0, 40.0, 1000.0),
        ]
        ib = make_mock_ib(positions=positions)
        ctx = make_ctx(ib=ib)
        result = await ibkr_get_positions(PortfolioInput(), ctx)

        # Each should be ~50% weight
        assert "+50.00%" in result


# --- Portfolio Snapshot ---

class TestPortfolioSnapshot:
    @pytest.mark.anyio
    async def test_top_5_shown(self):
        ctx = make_ctx()
        result = await ibkr_get_portfolio_snapshot(PortfolioInput(), ctx)

        assert "Top Holdings" in result
        assert "AAPL" in result
        assert "MSFT" in result

    @pytest.mark.anyio
    async def test_concentration_metrics(self):
        ctx = make_ctx()
        result = await ibkr_get_portfolio_snapshot(PortfolioInput(), ctx)

        assert "Concentration" in result
        assert "Top 1 weight" in result
        assert "HHI" in result

    @pytest.mark.anyio
    async def test_leverage_displayed(self):
        ctx = make_ctx()
        result = await ibkr_get_portfolio_snapshot(PortfolioInput(), ctx)

        assert "1.80x" in result

    @pytest.mark.anyio
    async def test_nav_shown(self):
        ctx = make_ctx()
        result = await ibkr_get_portfolio_snapshot(PortfolioInput(), ctx)

        assert "$10,000,000.00 USD" in result


# --- Portfolio by Currency ---

class TestPortfolioByCurrency:
    @pytest.mark.anyio
    async def test_single_currency(self):
        """All USD positions should appear under one group."""
        ctx = make_ctx()
        result = await ibkr_get_portfolio_by_currency(PortfolioInput(), ctx)

        assert "## USD" in result
        assert "AAPL" in result

    @pytest.mark.anyio
    async def test_multi_currency_grouping(self):
        positions = [
            make_portfolio_item("AAPL", 100, 140.0, 14000.0, 100.0, 4000.0, currency="USD"),
            make_portfolio_item("RY", 200, 150.0, 30000.0, 120.0, 6000.0, currency="CAD"),
        ]
        ib = make_mock_ib(positions=positions)
        ctx = make_ctx(ib=ib)
        result = await ibkr_get_portfolio_by_currency(PortfolioInput(), ctx)

        assert "## USD" in result
        assert "## CAD" in result
        assert "AAPL" in result
        assert "RY" in result

    @pytest.mark.anyio
    async def test_currency_percentage(self):
        positions = [
            make_portfolio_item("A", 100, 100.0, 10000.0, 80.0, 2000.0, currency="USD"),
            make_portfolio_item("B", 100, 100.0, 10000.0, 80.0, 2000.0, currency="CAD"),
        ]
        ib = make_mock_ib(positions=positions)
        ctx = make_ctx(ib=ib)
        result = await ibkr_get_portfolio_by_currency(PortfolioInput(), ctx)

        # Each currency is 50% of portfolio
        assert "+50.00%" in result

    @pytest.mark.anyio
    async def test_empty_portfolio(self):
        ib = make_mock_ib(positions=[])
        ctx = make_ctx(ib=ib)
        result = await ibkr_get_portfolio_by_currency(PortfolioInput(), ctx)

        assert "No positions found" in result
