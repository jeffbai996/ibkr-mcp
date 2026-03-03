"""Tests for core/formatting.py — financial value formatting with Decimal precision."""

import json
import math
from decimal import Decimal

import pytest

from core.formatting import fmt_price, fmt_pct, fmt_pnl, fmt_shares, to_json, _json_default


# --- fmt_price ---

class TestFmtPrice:
    def test_basic_usd(self):
        assert fmt_price(1234.56) == "$1,234.56 USD"

    def test_none_returns_na(self):
        assert fmt_price(None) == "N/A"

    def test_zero(self):
        assert fmt_price(0) == "$0.00 USD"

    def test_negative(self):
        assert fmt_price(-500.75, "CAD") == "$-500.75 CAD"

    def test_large_value_with_commas(self):
        assert fmt_price(26_000_000) == "$26,000,000.00 USD"

    def test_decimal_input(self):
        assert fmt_price(Decimal("99.99"), "USD") == "$99.99 USD"

    def test_rounding_half_up(self):
        # 0.005 should round UP to 0.01, not banker's round to 0.00
        assert fmt_price(0.005) == "$0.01 USD"

    def test_rounding_down(self):
        assert fmt_price(0.004) == "$0.00 USD"

    def test_custom_currency(self):
        assert fmt_price(100, "HKD") == "$100.00 HKD"

    def test_empty_currency_omits_suffix(self):
        assert fmt_price(100, "") == "$100.00"

    def test_small_fraction(self):
        assert fmt_price(0.01) == "$0.01 USD"

    def test_float_nan_returns_na(self):
        assert fmt_price(float("nan")) == "N/A"

    def test_decimal_nan_returns_na(self):
        assert fmt_price(Decimal("NaN")) == "N/A"


# --- fmt_pct ---

class TestFmtPct:
    def test_positive_gets_plus_sign(self):
        assert fmt_pct(12.34) == "+12.34%"

    def test_negative(self):
        assert fmt_pct(-5.67) == "-5.67%"

    def test_zero_no_sign(self):
        assert fmt_pct(0) == "0.00%"

    def test_none_returns_na(self):
        assert fmt_pct(None) == "N/A"

    def test_decimal_input(self):
        assert fmt_pct(Decimal("3.14")) == "+3.14%"

    def test_rounding(self):
        assert fmt_pct(1.005) == "+1.01%"

    def test_very_small_positive(self):
        # 0.001 rounds to 0.00 — should show as 0.00% (no sign)
        assert fmt_pct(0.001) == "0.00%"

    def test_large_percentage(self):
        assert fmt_pct(150.5) == "+150.50%"

    def test_float_nan_returns_na(self):
        assert fmt_pct(float("nan")) == "N/A"

    def test_decimal_nan_returns_na(self):
        assert fmt_pct(Decimal("NaN")) == "N/A"


# --- fmt_pnl ---

class TestFmtPnl:
    def test_positive_pnl(self):
        assert fmt_pnl(1234.56) == "+$1,234.56 USD"

    def test_negative_pnl(self):
        assert fmt_pnl(-567.89) == "$-567.89 USD"

    def test_zero_pnl(self):
        assert fmt_pnl(0) == "$0.00 USD"

    def test_none_returns_na(self):
        assert fmt_pnl(None) == "N/A"

    def test_custom_currency(self):
        assert fmt_pnl(100, "CAD") == "+$100.00 CAD"

    def test_decimal_input(self):
        assert fmt_pnl(Decimal("-42.50"), "USD") == "$-42.50 USD"

    def test_large_loss(self):
        assert fmt_pnl(-1_000_000) == "$-1,000,000.00 USD"

    def test_float_nan_returns_na(self):
        assert fmt_pnl(float("nan")) == "N/A"

    def test_decimal_nan_returns_na(self):
        assert fmt_pnl(Decimal("NaN")) == "N/A"


# --- fmt_shares ---

class TestFmtShares:
    def test_whole_number(self):
        assert fmt_shares(100) == "100"

    def test_thousands_get_commas(self):
        assert fmt_shares(1234) == "1,234"

    def test_fractional_shares(self):
        assert fmt_shares(2.5) == "2.50"

    def test_none_returns_na(self):
        assert fmt_shares(None) == "N/A"

    def test_zero(self):
        assert fmt_shares(0) == "0"

    def test_large_position(self):
        assert fmt_shares(50000) == "50,000"

    def test_float_that_is_whole(self):
        # 100.0 should format as whole number
        assert fmt_shares(100.0) == "100"

    def test_float_nan_returns_na(self):
        assert fmt_shares(float("nan")) == "N/A"


# --- to_json / _json_default ---

class TestToJson:
    def test_simple_dict(self):
        result = json.loads(to_json({"key": "value"}))
        assert result == {"key": "value"}

    def test_decimal_serialized_as_float(self):
        result = json.loads(to_json({"price": Decimal("99.95")}))
        assert result["price"] == 99.95

    def test_nested_decimals(self):
        data = {"positions": [{"value": Decimal("1000")}, {"value": Decimal("2000")}]}
        result = json.loads(to_json(data))
        assert result["positions"][0]["value"] == 1000.0
        assert result["positions"][1]["value"] == 2000.0

    def test_non_serializable_raises(self):
        with pytest.raises(TypeError, match="not JSON serializable"):
            to_json({"bad": object()})

    def test_json_default_decimal(self):
        assert _json_default(Decimal("3.14")) == 3.14

    def test_json_default_non_decimal_raises(self):
        with pytest.raises(TypeError):
            _json_default(set())
