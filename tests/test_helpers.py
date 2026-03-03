"""Tests for the _get_decimal/_get_dec/_safe_dec helpers.

These are currently duplicated across account.py, portfolio.py, and analytics.py.
Testing all three copies to ensure they behave identically until consolidation.
"""

from decimal import Decimal

import pytest

# Import the duplicated helpers from each module
from tools.account import _get_decimal as account_get_decimal
from tools.portfolio import _get_dec as portfolio_get_dec
from tools.analytics import _get_dec as analytics_get_dec, _safe_dec


# --- _get_decimal / _get_dec (same logic, different names) ---
# All three take a dict of {tag: (value, currency)} and extract a Decimal.

# Parametrize across all three copies so we catch divergence.
GET_DEC_FUNCS = [
    pytest.param(account_get_decimal, id="account._get_decimal"),
    pytest.param(portfolio_get_dec, id="portfolio._get_dec"),
    pytest.param(analytics_get_dec, id="analytics._get_dec"),
]


@pytest.mark.parametrize("get_dec", GET_DEC_FUNCS)
class TestGetDecimal:
    def test_valid_numeric_string(self, get_dec):
        vals = {"NetLiquidation": ("26000000.50", "USD")}
        assert get_dec(vals, "NetLiquidation") == Decimal("26000000.50")

    def test_integer_string(self, get_dec):
        vals = {"BuyingPower": ("500000", "USD")}
        assert get_dec(vals, "BuyingPower") == Decimal("500000")

    def test_missing_tag_returns_none(self, get_dec):
        vals = {"SomeTag": ("100", "USD")}
        assert get_dec(vals, "MissingTag") is None

    def test_empty_dict(self, get_dec):
        assert get_dec({}, "Anything") is None

    def test_none_value_in_tuple(self, get_dec):
        vals = {"Cushion": (None, "USD")}
        # str(None) = "None" which Decimal can't parse -> should return None
        assert get_dec(vals, "Cushion") is None

    def test_non_numeric_string(self, get_dec):
        vals = {"BadTag": ("not_a_number", "USD")}
        assert get_dec(vals, "BadTag") is None

    def test_zero(self, get_dec):
        vals = {"Cash": ("0", "USD")}
        assert get_dec(vals, "Cash") == Decimal("0")

    def test_negative_value(self, get_dec):
        vals = {"TotalCashValue": ("-15000.00", "USD")}
        assert get_dec(vals, "TotalCashValue") == Decimal("-15000.00")

    def test_decimal_precision_preserved(self, get_dec):
        vals = {"Cushion": ("0.1234567", "USD")}
        result = get_dec(vals, "Cushion")
        assert result == Decimal("0.1234567")

    def test_scientific_notation(self, get_dec):
        vals = {"Tiny": ("1.5e-4", "USD")}
        result = get_dec(vals, "Tiny")
        # Decimal("1.5e-4") = Decimal("0.00015")
        assert result == Decimal("0.00015")


# --- _safe_dec (only in analytics.py) ---

class TestSafeDec:
    def test_none_returns_none(self):
        assert _safe_dec(None) is None

    def test_float_value(self):
        assert _safe_dec(1234.56) == Decimal("1234.56")

    def test_string_value(self):
        assert _safe_dec("999.99") == Decimal("999.99")

    def test_already_decimal(self):
        d = Decimal("42.0")
        assert _safe_dec(d) == Decimal("42.0")

    def test_integer(self):
        assert _safe_dec(100) == Decimal("100")

    def test_non_numeric_returns_none(self):
        assert _safe_dec("garbage") is None

    def test_zero(self):
        assert _safe_dec(0) == Decimal("0")

    def test_negative(self):
        assert _safe_dec(-500.25) == Decimal("-500.25")

    def test_empty_string_returns_none(self):
        assert _safe_dec("") is None
