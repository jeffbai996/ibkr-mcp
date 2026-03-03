"""Integration tests for tools/account.py — account summary, margin, P&L."""

import pytest
from ib_insync.objects import PnL
from unittest.mock import MagicMock

from tools.account import (
    ibkr_get_account_summary,
    ibkr_get_margin_summary,
    ibkr_list_accounts,
    ibkr_get_account_pnl,
    AccountInput,
)
from tests.conftest import (
    make_ctx, make_mock_ib, make_summary, make_account_value, TEST_ACCOUNT,
)


# --- Account Summary ---

class TestAccountSummary:
    @pytest.mark.anyio
    async def test_basic_output(self):
        ctx = make_ctx()
        result = await ibkr_get_account_summary(AccountInput(), ctx)

        assert f"# Account Summary: {TEST_ACCOUNT}" in result
        assert "$10,000,000.00 USD" in result  # NLV
        assert "$18,000,000.00 USD" in result  # GPV
        assert "1.80x" in result  # leverage = 18M/10M

    @pytest.mark.anyio
    async def test_margin_utilization(self):
        ctx = make_ctx()
        result = await ibkr_get_account_summary(AccountInput(), ctx)

        # init_margin / nlv * 100 = 6M / 10M * 100 = 60%
        assert "+60.00%" in result

    @pytest.mark.anyio
    async def test_cushion_displayed(self):
        ctx = make_ctx()
        result = await ibkr_get_account_summary(AccountInput(), ctx)

        # Cushion 0.55 * 100 = 55%
        assert "+55.00%" in result

    @pytest.mark.anyio
    async def test_empty_summary(self):
        ib = make_mock_ib(summary=[])
        ctx = make_ctx(ib=ib)
        result = await ibkr_get_account_summary(AccountInput(), ctx)

        assert "No account summary available" in result

    @pytest.mark.anyio
    async def test_explicit_account(self):
        ib = make_mock_ib()
        ctx = make_ctx(ib=ib)
        result = await ibkr_get_account_summary(
            AccountInput(account="U9999999"), ctx
        )

        # Should call with the explicit account, not primary
        ib.accountSummary.assert_called_with("U9999999")

    @pytest.mark.anyio
    async def test_zero_nlv_no_crash(self):
        """Division by zero guard: leverage and margin util when NLV = 0."""
        summary = make_summary({"NetLiquidation": ("0", "USD")})
        ib = make_mock_ib(summary=summary)
        ctx = make_ctx(ib=ib)
        result = await ibkr_get_account_summary(AccountInput(), ctx)

        assert "N/A" in result  # leverage/margin_util should fall back

    @pytest.mark.anyio
    async def test_ib_error(self):
        ib = make_mock_ib()
        ib.accountSummary.side_effect = ConnectionError("refused")
        ctx = make_ctx(ib=ib)
        result = await ibkr_get_account_summary(AccountInput(), ctx)

        assert "Not connected to IB Gateway" in result


# --- Margin Summary ---

class TestMarginSummary:
    @pytest.mark.anyio
    async def test_distances(self):
        ctx = make_ctx()
        result = await ibkr_get_margin_summary(AccountInput(), ctx)

        # equity(10M) - init(6M) = 4M above initial
        assert "$4,000,000.00 USD" in result
        # equity(10M) - maint(4.5M) = 5.5M above maint
        assert "$5,500,000.00 USD" in result

    @pytest.mark.anyio
    async def test_max_drawdown_percentages(self):
        ctx = make_ctx()
        result = await ibkr_get_margin_summary(AccountInput(), ctx)

        # max_dd_call = dist(4M) / nlv(10M) * 100 = 40%
        assert "+40.00%" in result

    @pytest.mark.anyio
    async def test_empty_returns_message(self):
        ib = make_mock_ib(summary=[])
        ctx = make_ctx(ib=ib)
        result = await ibkr_get_margin_summary(AccountInput(), ctx)

        assert "No margin data available" in result


# --- List Accounts ---

class TestListAccounts:
    @pytest.mark.anyio
    async def test_single_account_marked_primary(self):
        ctx = make_ctx()
        result = await ibkr_list_accounts(ctx)

        assert f"`{TEST_ACCOUNT}`" in result
        assert "primary" in result.lower()

    @pytest.mark.anyio
    async def test_multiple_accounts(self):
        ib = make_mock_ib(accounts=["U1234567", "literal:UXXXXXXX"])
        ctx = make_ctx(ib=ib)
        result = await ibkr_list_accounts(ctx)

        assert "U1234567" in result
        assert "literal:UXXXXXXX" in result
        # Only primary gets the marker
        lines = result.split("\n")
        primary_lines = [l for l in lines if "primary" in l.lower()]
        assert len(primary_lines) == 1


# --- Account P&L ---

class TestAccountPnl:
    @pytest.mark.anyio
    async def test_pnl_values(self):
        ib = make_mock_ib()
        pnl = PnL(
            account=TEST_ACCOUNT, modelCode="",
            dailyPnL=5000.0, unrealizedPnL=200000.0, realizedPnL=150.0,
        )
        ib.reqPnL.return_value = pnl
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_account_pnl(AccountInput(), ctx)

        assert "+$5,000.00 USD" in result
        assert "+$200,000.00 USD" in result
        assert "+$150.00 USD" in result

    @pytest.mark.anyio
    async def test_pnl_cancels_subscription(self):
        """reqPnL opens a subscription — must cancel after reading."""
        ib = make_mock_ib()
        pnl = PnL(
            account=TEST_ACCOUNT, modelCode="",
            dailyPnL=0.0, unrealizedPnL=0.0, realizedPnL=0.0,
        )
        ib.reqPnL.return_value = pnl
        ctx = make_ctx(ib=ib)

        await ibkr_get_account_pnl(AccountInput(), ctx)

        ib.cancelPnL.assert_called_once()

    @pytest.mark.anyio
    async def test_negative_pnl(self):
        ib = make_mock_ib()
        pnl = PnL(
            account=TEST_ACCOUNT, modelCode="",
            dailyPnL=-15000.0, unrealizedPnL=-50000.0, realizedPnL=0.0,
        )
        ib.reqPnL.return_value = pnl
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_account_pnl(AccountInput(), ctx)

        assert "$-15,000.00 USD" in result
        assert "$-50,000.00 USD" in result
