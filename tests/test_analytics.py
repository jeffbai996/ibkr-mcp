"""Integration tests for tools/analytics.py — what-if, concentration, stress tests."""

import pytest
from unittest.mock import AsyncMock

from ib_insync import Stock

from tools.analytics import (
    ibkr_what_if_sell,
    ibkr_what_if_buy,
    ibkr_portfolio_concentration,
    ibkr_margin_stress_test,
    WhatIfInput,
    ConcentrationInput,
    StressTestInput,
)
from tests.conftest import (
    make_ctx, make_mock_ib, make_summary, make_positions,
    make_portfolio_item, make_order_state, TEST_ACCOUNT,
)


# --- What-If Sell ---

class TestWhatIfSell:
    @pytest.mark.anyio
    async def test_margin_relief(self):
        """Selling should show margin relief (negative init_margin_change)."""
        ib = make_mock_ib()
        ib.whatIfOrder.return_value = make_order_state(
            init_margin_change="-50000",
            maint_margin_change="-37500",
            equity_change="0",
        )
        ctx = make_ctx(ib=ib)

        result = await ibkr_what_if_sell(
            WhatIfInput(symbol="AAPL", quantity=500), ctx
        )

        assert "SELL 500 AAPL" in result
        assert "Margin Impact" in result
        assert "Relief" in result
        # init relief = -(-50000) = +$50,000
        assert "+$50,000.00 USD" in result

    @pytest.mark.anyio
    async def test_current_vs_post_trade(self):
        ib = make_mock_ib()
        ib.whatIfOrder.return_value = make_order_state(
            init_margin_change="-100000",
            maint_margin_change="-75000",
            equity_change="-5000",
        )
        ctx = make_ctx(ib=ib)

        result = await ibkr_what_if_sell(
            WhatIfInput(symbol="MSFT", quantity=1000), ctx
        )

        assert "Current State" in result
        assert "Post-Trade Estimate" in result

    @pytest.mark.anyio
    async def test_contract_not_found(self):
        ib = make_mock_ib()
        ib.qualifyContractsAsync = AsyncMock(return_value=[])
        ctx = make_ctx(ib=ib)

        result = await ibkr_what_if_sell(
            WhatIfInput(symbol="ZZZZ", quantity=100), ctx
        )

        assert "Could not find contract" in result

    @pytest.mark.anyio
    async def test_what_if_fails(self):
        ib = make_mock_ib()
        order_state = make_order_state()
        order_state.initMarginChange = None
        ib.whatIfOrder.return_value = order_state
        ctx = make_ctx(ib=ib)

        result = await ibkr_what_if_sell(
            WhatIfInput(symbol="AAPL", quantity=100), ctx
        )

        assert "simulation failed" in result.lower()


# --- What-If Buy ---

class TestWhatIfBuy:
    @pytest.mark.anyio
    async def test_margin_consumed(self):
        ib = make_mock_ib()
        ib.whatIfOrder.return_value = make_order_state(
            init_margin_change="200000",
            maint_margin_change="150000",
        )
        ctx = make_ctx(ib=ib)

        result = await ibkr_what_if_buy(
            WhatIfInput(symbol="AAPL", quantity=1000), ctx
        )

        assert "BUY 1,000 AAPL" in result
        assert "$200,000.00 USD" in result

    @pytest.mark.anyio
    async def test_margin_deficit_warning(self):
        """Buying so much that excess goes negative should trigger warning."""
        ib = make_mock_ib()
        # init_change of 5M would push init from 6M to 11M,
        # which exceeds 10M equity -> negative excess
        ib.whatIfOrder.return_value = make_order_state(
            init_margin_change="5000000",
            maint_margin_change="3750000",
        )
        ctx = make_ctx(ib=ib)

        result = await ibkr_what_if_buy(
            WhatIfInput(symbol="AAPL", quantity=50000), ctx
        )

        assert "WARNING" in result
        assert "margin deficit" in result.lower()

    @pytest.mark.anyio
    async def test_no_warning_when_safe(self):
        ib = make_mock_ib()
        ib.whatIfOrder.return_value = make_order_state(
            init_margin_change="100000",
            maint_margin_change="75000",
        )
        ctx = make_ctx(ib=ib)

        result = await ibkr_what_if_buy(
            WhatIfInput(symbol="MSFT", quantity=100), ctx
        )

        assert "WARNING" not in result


# --- Concentration ---

class TestConcentration:
    @pytest.mark.anyio
    async def test_hhi_and_weights(self):
        ctx = make_ctx()
        result = await ibkr_portfolio_concentration(ConcentrationInput(), ctx)

        assert "HHI" in result
        assert "Top 1" in result
        assert "Top 3" in result
        assert "Top 5" in result

    @pytest.mark.anyio
    async def test_over_20_flagged(self):
        """Positions over 20% of NLV should be flagged."""
        positions = [
            # 3M out of 10M NLV = 30% -> should be flagged
            make_portfolio_item("AAPL", 20000, 150.0, 3000000.0, 100.0, 1000000.0),
            make_portfolio_item("MSFT", 5000, 95.0, 475000.0, 80.0, 75000.0),
        ]
        ib = make_mock_ib(positions=positions)
        ctx = make_ctx(ib=ib)

        result = await ibkr_portfolio_concentration(ConcentrationInput(), ctx)

        assert "AAPL" in result
        assert ">20%" in result or "Positions >20%" in result

    @pytest.mark.anyio
    async def test_single_position_hhi_10000(self):
        """One position = 100% weight -> HHI should be 10000."""
        positions = [
            make_portfolio_item("AAPL", 100, 100.0, 10000.0, 80.0, 2000.0),
        ]
        # NLV = 10M in default summary, but weight is abs(mkt_value)/NLV
        # 10000 / 10M = 0.1% -> HHI ≈ 0.01. That's not 10000.
        # To get HHI=10000, NLV should match the position value.
        summary = make_summary({"NetLiquidation": ("10000", "USD")})
        ib = make_mock_ib(summary=summary, positions=positions)
        ctx = make_ctx(ib=ib)

        result = await ibkr_portfolio_concentration(ConcentrationInput(), ctx)

        # 100% weight -> HHI = 100^2 = 10000
        assert "10000" in result

    @pytest.mark.anyio
    async def test_no_positions(self):
        ib = make_mock_ib(positions=[])
        ctx = make_ctx(ib=ib)

        result = await ibkr_portfolio_concentration(ConcentrationInput(), ctx)

        assert "No positions" in result


# --- Stress Test ---

class TestStressTest:
    @pytest.mark.anyio
    async def test_mild_drawdown_no_warnings(self):
        """5% drawdown on a healthy portfolio should not trigger warnings."""
        ctx = make_ctx()
        result = await ibkr_margin_stress_test(
            StressTestInput(drawdown_pct=5.0), ctx
        )

        assert "Stress Test" in result
        assert "Current State" in result
        assert "MARGIN CALL" not in result
        assert "FORCED LIQUIDATION" not in result

    @pytest.mark.anyio
    async def test_stressed_values(self):
        """Verify the stress math: loss = GPV * dd, stressed NLV = NLV - loss."""
        ctx = make_ctx()
        result = await ibkr_margin_stress_test(
            StressTestInput(drawdown_pct=10.0), ctx
        )

        # GPV=18M, dd=10% -> loss = 1.8M
        # stressed NLV = 10M - 1.8M = 8.2M
        assert "$8,200,000.00 USD" in result
        # Estimated loss displayed (negative)
        assert "$-1,800,000.00 USD" in result

    @pytest.mark.anyio
    async def test_margin_call_warning(self):
        """A big enough drawdown should trigger margin call warning."""
        # With default: equity=10M, init=6M, GPV=18M
        # At dd%, equity_stressed = 10M - 18M*dd, init_stressed = 6M*(1-dd)
        # Margin call when equity_stressed <= init_stressed
        # 10M - 18M*dd <= 6M*(1-dd) -> 10M - 18M*dd <= 6M - 6M*dd
        # 4M <= 12M*dd -> dd >= 33.3%
        ctx = make_ctx()
        result = await ibkr_margin_stress_test(
            StressTestInput(drawdown_pct=40.0), ctx
        )

        assert "MARGIN CALL" in result

    @pytest.mark.anyio
    async def test_forced_liquidation_warning(self):
        """Even bigger drawdown should trigger forced liquidation warning."""
        # Forced liq when equity_stressed <= maint_stressed
        # 10M - 18M*dd <= 4.5M*(1-dd) -> 5.5M <= 13.5M*dd -> dd >= 40.7%
        ctx = make_ctx()
        result = await ibkr_margin_stress_test(
            StressTestInput(drawdown_pct=50.0), ctx
        )

        assert "FORCED LIQUIDATION" in result

    @pytest.mark.anyio
    async def test_max_drawdown_before_trouble(self):
        ctx = make_ctx()
        result = await ibkr_margin_stress_test(
            StressTestInput(drawdown_pct=5.0), ctx
        )

        assert "Max DD before margin call" in result
        assert "Max DD before forced liq" in result

    @pytest.mark.anyio
    async def test_insufficient_data(self):
        summary = make_summary({
            "GrossPositionValue": ("0", "USD"),
            "InitMarginReq": ("0", "USD"),
            "MaintMarginReq": ("0", "USD"),
        })
        ib = make_mock_ib(summary=summary)
        ctx = make_ctx(ib=ib)

        result = await ibkr_margin_stress_test(
            StressTestInput(drawdown_pct=10.0), ctx
        )

        # GPV=0, init=0, maint=0 -> all([...]) is False
        assert "Insufficient account data" in result

    @pytest.mark.anyio
    async def test_approximation_disclaimer(self):
        ctx = make_ctx()
        result = await ibkr_margin_stress_test(
            StressTestInput(drawdown_pct=5.0), ctx
        )

        assert "approximation" in result.lower()
