"""Integration tests for tools/account.py — account summary, margin, P&L, briefing."""

import math

import pytest
from ib_insync.objects import PnL, PnLSingle
from unittest.mock import AsyncMock, MagicMock

from tools.account import (
    ibkr_get_account_summary,
    ibkr_margin,
    ibkr_list_accounts,
    ibkr_get_account_pnl,
    ibkr_morning_briefing,
    AccountInput,
    BriefingInput,
    MarginInput,
)
from tests.conftest import (
    make_ctx, make_mock_ib, make_summary, make_account_value,
    make_positions, TEST_ACCOUNT,
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
        result = await ibkr_margin(MarginInput(detail="summary"), ctx)

        # equity(10M) - init(6M) = 4M above initial
        assert "$4,000,000.00 USD" in result
        # equity(10M) - maint(4.5M) = 5.5M above maint
        assert "$5,500,000.00 USD" in result

    @pytest.mark.anyio
    async def test_max_drawdown_percentages(self):
        ctx = make_ctx()
        result = await ibkr_margin(MarginInput(detail="summary"), ctx)

        # max_dd_call = dist(4M) / nlv(10M) * 100 = 40%
        assert "+40.00%" in result

    @pytest.mark.anyio
    async def test_empty_returns_message(self):
        ib = make_mock_ib(summary=[])
        ctx = make_ctx(ib=ib)
        result = await ibkr_margin(MarginInput(detail="summary"), ctx)

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

    @pytest.mark.anyio
    async def test_pnl_cleanup_on_error(self):
        """cancelPnL must run even when formatting throws (all-NaN data)."""
        ib = make_mock_ib()
        pnl = PnL(
            account=TEST_ACCOUNT, modelCode="",
            dailyPnL=float("nan"), unrealizedPnL=float("nan"),
            realizedPnL=float("nan"),
        )
        ib.reqPnL.return_value = pnl
        ctx = make_ctx(ib=ib)

        # Should not raise — fmt_pnl handles NaN → "N/A"
        result = await ibkr_get_account_pnl(AccountInput(), ctx)

        # Subscription must be cancelled regardless of data quality
        ib.cancelPnL.assert_called_once()
        assert "N/A" in result


# --- Morning Briefing ---

def _make_briefing_ib(
    positions=None,
    daily_pnl: float = 5000.0,
    with_orders: bool = False,
):
    """Build a mock IB with everything the briefing tool needs."""
    ib = make_mock_ib(positions=positions)
    pnl = PnL(
        account=TEST_ACCOUNT, modelCode="",
        dailyPnL=daily_pnl, unrealizedPnL=200000.0, realizedPnL=150.0,
    )
    ib.reqPnL.return_value = pnl

    # Per-position P&L: match conftest's 5-position portfolio
    pos = positions or make_positions()
    daily_values = [3000.0, 1500.0, 500.0, -200.0, -50.0]
    pnl_singles = []
    for i, p in enumerate(pos):
        daily = daily_values[i] if i < len(daily_values) else 0.0
        ps = PnLSingle(
            account=TEST_ACCOUNT, modelCode="", conId=p.contract.conId,
            dailyPnL=daily, unrealizedPnL=p.unrealizedPNL,
            realizedPnL=0.0, position=p.position, value=p.marketValue,
        )
        pnl_singles.append(ps)
    ib.reqPnLSingle.side_effect = pnl_singles

    # Open orders
    if with_orders:
        mock_trade = MagicMock()
        mock_trade.order.account = TEST_ACCOUNT
        mock_trade.order.action = "BUY"
        mock_trade.order.totalQuantity = 100
        mock_trade.order.orderType = "LMT"
        mock_trade.contract.symbol = "AAPL"
        ib.openTrades.return_value = [mock_trade]
    else:
        ib.openTrades.return_value = []

    # FX — default: no multi-currency, so qualifyContractsAsync won't be called
    ib.qualifyContractsAsync = AsyncMock(return_value=[])

    return ib


class TestMorningBriefing:
    @pytest.mark.anyio
    async def test_basic_output(self):
        ib = _make_briefing_ib()
        ctx = make_ctx(ib=ib)
        result = await ibkr_morning_briefing(BriefingInput(), ctx)

        assert "# Morning Briefing" in result
        assert "Account Health" in result
        assert "$10,000,000.00 USD" in result  # NLV
        assert "1.80x" in result  # leverage
        assert "Daily P&L" in result
        assert "+$5,000.00 USD" in result  # daily

    @pytest.mark.anyio
    async def test_top_movers(self):
        ib = _make_briefing_ib()
        ctx = make_ctx(ib=ib)
        result = await ibkr_morning_briefing(BriefingInput(), ctx)

        assert "Top Gainers" in result
        assert "MSFT" in result  # highest daily = 3000
        assert "JPM" in result  # second = 1500

    @pytest.mark.anyio
    async def test_open_orders_shown(self):
        ib = _make_briefing_ib(with_orders=True)
        ctx = make_ctx(ib=ib)
        result = await ibkr_morning_briefing(BriefingInput(), ctx)

        assert "1 open order" in result
        assert "AAPL" in result
        assert "BUY" in result

    @pytest.mark.anyio
    async def test_no_orders(self):
        ib = _make_briefing_ib()
        ctx = make_ctx(ib=ib)
        result = await ibkr_morning_briefing(BriefingInput(), ctx)

        assert "No open orders." in result

    @pytest.mark.anyio
    async def test_subscriptions_cleaned_up(self):
        """All P&L subscriptions must be cancelled in finally."""
        ib = _make_briefing_ib()
        ctx = make_ctx(ib=ib)
        await ibkr_morning_briefing(BriefingInput(), ctx)

        ib.cancelPnL.assert_called_once()
        assert ib.cancelPnLSingle.call_count == 5  # one per position

    @pytest.mark.anyio
    async def test_empty_portfolio(self):
        ib = _make_briefing_ib(positions=[])
        ib.reqPnLSingle.side_effect = []
        ctx = make_ctx(ib=ib)
        result = await ibkr_morning_briefing(BriefingInput(), ctx)

        assert "Morning Briefing" in result
        assert "No open orders." in result

    @pytest.mark.anyio
    async def test_nan_pnl_handled(self):
        """NaN daily P&L for a position shouldn't crash the briefing."""
        ib = _make_briefing_ib()
        pos = make_positions()
        # Override: first position has NaN daily
        pnl_singles = []
        for i, p in enumerate(pos):
            daily = float("nan") if i == 0 else 1000.0
            ps = PnLSingle(
                account=TEST_ACCOUNT, modelCode="", conId=p.contract.conId,
                dailyPnL=daily, unrealizedPnL=0.0,
                realizedPnL=0.0, position=p.position, value=p.marketValue,
            )
            pnl_singles.append(ps)
        ib.reqPnLSingle.side_effect = pnl_singles
        ctx = make_ctx(ib=ib)

        result = await ibkr_morning_briefing(BriefingInput(), ctx)

        # Should not crash, AAPL (NaN daily) excluded from movers
        assert "Morning Briefing" in result
