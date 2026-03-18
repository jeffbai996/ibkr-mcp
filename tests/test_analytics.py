"""Integration tests for what-if and stress test tools (formerly tools/analytics.py).

What-if and stress test tools are now in tools/risk.py.
Concentration was absorbed into ibkr_risk_dashboard (tools/monitoring.py).
"""

import pytest
from unittest.mock import AsyncMock

from ib_insync import Stock

from tools.risk import (
    ibkr_what_if,
    ibkr_stress_test,
    WhatIfInput,
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

        result = await ibkr_what_if(
            WhatIfInput(action="sell", symbol="AAPL", quantity=500), ctx
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

        result = await ibkr_what_if(
            WhatIfInput(action="sell", symbol="MSFT", quantity=1000), ctx
        )

        assert "Current State" in result
        assert "Post-Trade Estimate" in result

    @pytest.mark.anyio
    async def test_contract_not_found(self):
        ib = make_mock_ib()
        ib.qualifyContractsAsync = AsyncMock(return_value=[])
        ctx = make_ctx(ib=ib)

        result = await ibkr_what_if(
            WhatIfInput(action="sell", symbol="ZZZZ", quantity=100), ctx
        )

        assert "Could not find contract" in result

    @pytest.mark.anyio
    async def test_what_if_fails(self):
        ib = make_mock_ib()
        order_state = make_order_state()
        order_state.initMarginChange = None
        ib.whatIfOrder.return_value = order_state
        ctx = make_ctx(ib=ib)

        result = await ibkr_what_if(
            WhatIfInput(action="sell", symbol="AAPL", quantity=100), ctx
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

        result = await ibkr_what_if(
            WhatIfInput(action="buy", symbol="AAPL", quantity=1000), ctx
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

        result = await ibkr_what_if(
            WhatIfInput(action="buy", symbol="AAPL", quantity=50000), ctx
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

        result = await ibkr_what_if(
            WhatIfInput(action="buy", symbol="MSFT", quantity=100), ctx
        )

        assert "WARNING" not in result


# --- Stress Test ---

class TestStressTest:
    @pytest.mark.anyio
    async def test_mild_drawdown_no_warnings(self):
        """5% drawdown on a healthy portfolio should not trigger warnings."""
        ctx = make_ctx()
        result = await ibkr_stress_test(
            StressTestInput(scenario="drawdown", drawdown_pct=5.0), ctx
        )

        assert "Stress Test" in result
        assert "Current State" in result
        assert "MARGIN CALL" not in result
        assert "FORCED LIQUIDATION" not in result

    @pytest.mark.anyio
    async def test_stressed_values(self):
        """Verify the stress math: loss = GPV * dd, stressed NLV = NLV - loss."""
        ctx = make_ctx()
        result = await ibkr_stress_test(
            StressTestInput(scenario="drawdown", drawdown_pct=10.0), ctx
        )

        # GPV=18M, dd=10% -> loss = 1.8M
        # stressed NLV = 10M - 1.8M = 8.2M
        assert "$8,200,000.00 USD" in result
        # Estimated loss displayed (negative)
        assert "$-1,800,000.00 USD" in result

    @pytest.mark.anyio
    async def test_margin_call_warning(self):
        """A big enough drawdown should trigger margin call warning."""
        ctx = make_ctx()
        result = await ibkr_stress_test(
            StressTestInput(scenario="drawdown", drawdown_pct=40.0), ctx
        )

        assert "MARGIN CALL" in result

    @pytest.mark.anyio
    async def test_forced_liquidation_warning(self):
        """Even bigger drawdown should trigger forced liquidation warning."""
        ctx = make_ctx()
        result = await ibkr_stress_test(
            StressTestInput(scenario="drawdown", drawdown_pct=50.0), ctx
        )

        assert "FORCED LIQUIDATION" in result

    @pytest.mark.anyio
    async def test_max_drawdown_before_trouble(self):
        ctx = make_ctx()
        result = await ibkr_stress_test(
            StressTestInput(scenario="drawdown", drawdown_pct=5.0), ctx
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

        result = await ibkr_stress_test(
            StressTestInput(scenario="drawdown", drawdown_pct=10.0), ctx
        )

        # GPV=0, init=0, maint=0 -> all([...]) is False
        assert "Insufficient account data" in result

    @pytest.mark.anyio
    async def test_approximation_disclaimer(self):
        ctx = make_ctx()
        result = await ibkr_stress_test(
            StressTestInput(scenario="drawdown", drawdown_pct=5.0), ctx
        )

        assert "approximation" in result.lower()
