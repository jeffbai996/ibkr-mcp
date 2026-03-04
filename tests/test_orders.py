"""Tests for tools/orders.py — trades and open orders."""

import math
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from ib_insync import Stock
from ib_insync.objects import Fill, CommissionReport
from ib_insync.order import Trade, Order, OrderStatus

from tools.orders import ibkr_get_trades, ibkr_get_orders, TradesInput, OrdersInput
from tests.conftest import make_ctx, make_mock_ib, TEST_ACCOUNT


# --- Factories ---

def make_execution(**overrides):
    """Build a mock Execution object."""
    ex = MagicMock()
    ex.acctNumber = overrides.get("acctNumber", TEST_ACCOUNT)
    ex.side = overrides.get("side", "BUY")
    ex.shares = overrides.get("shares", 100.0)
    ex.price = overrides.get("price", 140.50)
    ex.execId = overrides.get("execId", "0001")
    return ex


def make_fill(symbol="AAPL", side="BUY", shares=100, price=140.50,
              commission=1.0, currency="USD", account=TEST_ACCOUNT):
    """Build a Fill namedtuple with realistic data."""
    contract = Stock(symbol, "SMART", currency)
    execution = make_execution(
        acctNumber=account, side=side, shares=shares, price=price,
    )
    cr = CommissionReport(
        execId="0001", commission=commission, currency=currency,
        realizedPNL=0.0, yield_=0.0, yieldRedemptionDate=0,
    )
    return Fill(
        contract=contract, execution=execution,
        commissionReport=cr, time=datetime(2026, 3, 3, 10, 30, 0),
    )


def make_trade(symbol="AAPL", action="BUY", qty=100, order_type="LMT",
               lmt_price=140.0, status="Submitted", filled=0, remaining=100):
    """Build a mock Trade object."""
    contract = Stock(symbol, "SMART", "USD")

    order = MagicMock(spec=Order)
    order.action = action
    order.totalQuantity = qty
    order.orderType = order_type
    order.lmtPrice = lmt_price
    order.auxPrice = 1.7976931348623157e+308  # UNSET_DOUBLE
    order.tif = "DAY"
    order.account = TEST_ACCOUNT

    order_status = MagicMock(spec=OrderStatus)
    order_status.status = status

    trade = MagicMock(spec=Trade)
    trade.contract = contract
    trade.order = order
    trade.orderStatus = order_status
    trade.filled.return_value = filled
    trade.remaining.return_value = remaining

    return trade


# --- Trades ---

class TestGetTrades:
    @pytest.mark.anyio
    async def test_basic_fills(self):
        ib = make_mock_ib()
        ib.fills.return_value = [
            make_fill("AAPL", "BUY", 100, 140.50, 1.0),
            make_fill("MSFT", "SELL", 200, 95.00, 1.50),
        ]
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_trades(TradesInput(), ctx)

        assert "Executions" in result
        assert "AAPL" in result
        assert "MSFT" in result
        assert "BUY" in result
        assert "SELL" in result
        assert "Total Executions**: 2" in result

    @pytest.mark.anyio
    async def test_commission_total(self):
        ib = make_mock_ib()
        ib.fills.return_value = [
            make_fill("AAPL", "BUY", 100, 140.0, 1.00),
            make_fill("MSFT", "BUY", 200, 95.0, 2.50),
        ]
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_trades(TradesInput(), ctx)

        # 1.00 + 2.50 = 3.50
        assert "$3.50" in result

    @pytest.mark.anyio
    async def test_symbol_filter(self):
        ib = make_mock_ib()
        ib.fills.return_value = [
            make_fill("AAPL", "BUY", 100, 140.0, 1.0),
            make_fill("MSFT", "SELL", 200, 95.0, 1.0),
        ]
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_trades(TradesInput(symbol_filter="AAPL"), ctx)

        assert "AAPL" in result
        assert "MSFT" not in result

    @pytest.mark.anyio
    async def test_no_fills(self):
        ib = make_mock_ib()
        ib.fills.return_value = []
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_trades(TradesInput(), ctx)

        assert "No executions" in result

    @pytest.mark.anyio
    async def test_account_filter(self):
        """Only fills for the requested account should appear."""
        ib = make_mock_ib()
        ib.fills.return_value = [
            make_fill("AAPL", account=TEST_ACCOUNT),
            make_fill("MSFT", account="U9999999"),
        ]
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_trades(TradesInput(), ctx)

        assert "AAPL" in result
        assert "MSFT" not in result

    @pytest.mark.anyio
    async def test_nan_commission_handled(self):
        """Commission could be NaN for pending fills."""
        fill = make_fill("AAPL")
        fill.commissionReport.commission = float("nan")
        ib = make_mock_ib()
        ib.fills.return_value = [fill]
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_trades(TradesInput(), ctx)

        assert "N/A" in result  # NaN commission shown as N/A


# --- Orders ---

class TestGetOrders:
    @pytest.mark.anyio
    async def test_basic_orders(self):
        ib = make_mock_ib()
        ib.openTrades.return_value = [
            make_trade("AAPL", "BUY", 100, "LMT", 135.0),
            make_trade("MSFT", "SELL", 200, "MKT", 0.0),
        ]
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_orders(OrdersInput(), ctx)

        assert "Open Orders" in result
        assert "AAPL" in result
        assert "MSFT" in result
        assert "BUY" in result
        assert "SELL" in result

    @pytest.mark.anyio
    async def test_limit_price_shown(self):
        ib = make_mock_ib()
        ib.openTrades.return_value = [
            make_trade("AAPL", "BUY", 100, "LMT", 135.50),
        ]
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_orders(OrdersInput(), ctx)

        assert "$135.50" in result

    @pytest.mark.anyio
    async def test_unset_price_shows_dash(self):
        """UNSET_DOUBLE for stop price should show as dash."""
        ib = make_mock_ib()
        trade = make_trade("AAPL", "BUY", 100, "LMT", 135.0)
        # auxPrice is already UNSET_DOUBLE from factory
        ib.openTrades.return_value = [trade]
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_orders(OrdersInput(), ctx)

        # Stop column should show "—"
        assert "—" in result

    @pytest.mark.anyio
    async def test_no_open_orders(self):
        ib = make_mock_ib()
        ib.openTrades.return_value = []
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_orders(OrdersInput(), ctx)

        assert "No open orders" in result

    @pytest.mark.anyio
    async def test_partially_filled_shows_count(self):
        ib = make_mock_ib()
        trade = make_trade("AAPL", "BUY", 500, "LMT", 135.0,
                           filled=200, remaining=300)
        ib.openTrades.return_value = [trade]
        ctx = make_ctx(ib=ib)

        result = await ibkr_get_orders(OrdersInput(), ctx)

        assert "200" in result  # filled count shown
        assert "filled" in result.lower()
