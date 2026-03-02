#!/usr/bin/env python3
"""
ibkr_mcp — Interactive Brokers MCP Server

Entry point for the MCP server. Imports the shared mcp instance from app.py,
then imports tool modules which register their tools on it.

Usage:
    # Direct (for testing)
    python server.py

    # With Claude Code (add to ~/.claude.json)
    "ibkr": {
        "command": "python",
        "args": ["/path/to/ibkr-mcp/server.py"]
    }
"""

# Patch asyncio BEFORE anything else imports or creates an event loop.
# ib_insync 0.9.86 removed its automatic nest_asyncio patching, but
# FastMCP runs its own event loop — without this patch, any ib_insync
# code path that calls loop.run_until_complete() will raise
# "RuntimeError: This event loop is already running".
import nest_asyncio
nest_asyncio.apply()

from app import mcp  # noqa: F401 — must import so tool modules can find it

# --- Register tool modules ---
# Each module imports `mcp` from app.py and uses @mcp.tool() decorators.
# Import them here so decorators fire and tools get registered.
import tools.account      # noqa: F401, E402
import tools.portfolio    # noqa: F401, E402
import tools.market_data  # noqa: F401, E402
import tools.analytics    # noqa: F401, E402
# import tools.orders     # Phase 2 — uncomment when ready


if __name__ == "__main__":
    # stdio transport — Claude Code runs this as a subprocess
    mcp.run()
