"""Tests for the get_decimal/safe_decimal helpers in core/formatting.py.

Previously duplicated across account.py, portfolio.py, and analytics.py as
_get_decimal/_get_dec/_safe_dec. Now consolidated into core.formatting.
"""

from decimal import Decimal

import pytest

from core.formatting import get_decimal, safe_decimal


# --- get_decimal (extract Decimal from IB accountSummary dict) ---

class TestGetDecimal:
    def test_valid_numeric_string(self):
        vals = {"NetLiquidation": ("26000000.50", "USD")}
        assert get_decimal(vals, "NetLiquidation") == Decimal("26000000.50")

    def test_integer_string(self):
        vals = {"BuyingPower": ("500000", "USD")}
        assert get_decimal(vals, "BuyingPower") == Decimal("500000")

    def test_missing_tag_returns_none(self):
        vals = {"SomeTag": ("100", "USD")}
        assert get_decimal(vals, "MissingTag") is None

    def test_empty_dict(self):
        assert get_decimal({}, "Anything") is None

    def test_none_value_in_tuple(self):
        vals = {"Cushion": (None, "USD")}
        # str(None) = "None" which Decimal can't parse -> should return None
        assert get_decimal(vals, "Cushion") is None

    def test_non_numeric_string(self):
        vals = {"BadTag": ("not_a_number", "USD")}
        assert get_decimal(vals, "BadTag") is None

    def test_zero(self):
        vals = {"Cash": ("0", "USD")}
        assert get_decimal(vals, "Cash") == Decimal("0")

    def test_negative_value(self):
        vals = {"TotalCashValue": ("-15000.00", "USD")}
        assert get_decimal(vals, "TotalCashValue") == Decimal("-15000.00")

    def test_decimal_precision_preserved(self):
        vals = {"Cushion": ("0.1234567", "USD")}
        result = get_decimal(vals, "Cushion")
        assert result == Decimal("0.1234567")

    def test_scientific_notation(self):
        vals = {"Tiny": ("1.5e-4", "USD")}
        result = get_decimal(vals, "Tiny")
        # Decimal("1.5e-4") = Decimal("0.00015")
        assert result == Decimal("0.00015")


# --- safe_decimal (convert any value to Decimal) ---

class TestSafeDecimal:
    def test_none_returns_none(self):
        assert safe_decimal(None) is None

    def test_float_value(self):
        assert safe_decimal(1234.56) == Decimal("1234.56")

    def test_string_value(self):
        assert safe_decimal("999.99") == Decimal("999.99")

    def test_already_decimal(self):
        d = Decimal("42.0")
        assert safe_decimal(d) == Decimal("42.0")

    def test_integer(self):
        assert safe_decimal(100) == Decimal("100")

    def test_non_numeric_returns_none(self):
        assert safe_decimal("garbage") is None

    def test_zero(self):
        assert safe_decimal(0) == Decimal("0")

    def test_negative(self):
        assert safe_decimal(-500.25) == Decimal("-500.25")

    def test_empty_string_returns_none(self):
        assert safe_decimal("") is None
