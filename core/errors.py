"""
Error handling utilities for ibkr_mcp.

Consistent error formatting so Claude Code always gets actionable messages.
"""

import asyncio
from config import IB_HOST, IB_PORT


def handle_ib_error(e: Exception, context: str = "") -> str:
    """
    Format any IB-related exception into a helpful error message.

    Args:
        e: The exception that was caught
        context: What the tool was trying to do (e.g., "fetching positions")

    Returns:
        Human-readable error string with suggestions
    """
    prefix = f"Error {context}: " if context else "Error: "

    if isinstance(e, ConnectionError):
        return (f"{prefix}Not connected to IB Gateway. "
                f"Check that Gateway is running on {IB_HOST}:{IB_PORT} "
                f"and the API port is enabled.")

    if isinstance(e, asyncio.TimeoutError):
        return (f"{prefix}Request timed out. "
                f"IB Gateway may be overloaded or disconnected.")

    if isinstance(e, ValueError):
        return f"{prefix}{e}"

    # Generic fallback — include type for debugging
    return f"{prefix}{type(e).__name__}: {e}"
