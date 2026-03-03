"""Tests for core/errors.py — IB exception handling."""

import asyncio

from core.errors import handle_ib_error


class TestHandleIbError:
    def test_connection_error_mentions_gateway(self):
        result = handle_ib_error(ConnectionError("refused"), "fetching positions")
        assert "Not connected to IB Gateway" in result
        assert "fetching positions" in result

    def test_connection_error_includes_host_port(self):
        result = handle_ib_error(ConnectionError())
        # Should reference the configured host:port so user knows where to look
        assert "127.0.0.1" in result or ":" in result

    def test_timeout_error(self):
        result = handle_ib_error(asyncio.TimeoutError(), "fetching quote")
        assert "timed out" in result.lower()
        assert "fetching quote" in result

    def test_value_error_includes_message(self):
        result = handle_ib_error(ValueError("invalid symbol"), "qualifying contract")
        assert "invalid symbol" in result
        assert "qualifying contract" in result

    def test_generic_exception_includes_type(self):
        result = handle_ib_error(RuntimeError("something broke"))
        assert "RuntimeError" in result
        assert "something broke" in result

    def test_no_context(self):
        result = handle_ib_error(ValueError("bad input"))
        assert result.startswith("Error: ")
        assert "bad input" in result

    def test_with_context(self):
        result = handle_ib_error(ValueError("bad"), "doing something")
        assert result.startswith("Error doing something: ")
