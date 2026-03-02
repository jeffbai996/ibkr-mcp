#!/usr/bin/env python3
"""
ibkr_mcp — Interactive Brokers MCP Server

Entry point for the MCP server. Creates the FastMCP instance with IB Gateway
lifespan, then imports tool modules which register their tools on the instance.

Usage:
    # Direct (for testing)
    python server.py

    # With Claude Code (add to ~/.claude.json)
    "ibkr": {
        "command": "python",
        "args": ["/path/to/ibkr-mcp/server.py"]
    }
"""

from mcp.server.fastmcp import FastMCP
from core.connection import ib_lifespan

# Create the MCP server with IB Gateway lifespan
# The lifespan connects to IB on startup, disconnects on shutdown.
# All tools access the IB instance through ctx.request_context.lifespan_context
mcp = FastMCP("ibkr_mcp", lifespan=ib_lifespan)

# --- Register tool modules ---
# Each module imports `mcp` from this file and uses @mcp.tool() decorators.
# Import them here so decorators fire and tools get registered.
import tools.account      # noqa: F401, E402
import tools.portfolio    # noqa: F401, E402
import tools.market_data  # noqa: F401, E402
import tools.analytics    # noqa: F401, E402
# import tools.orders     # Phase 2 — uncomment when ready


if __name__ == "__main__":
    # stdio transport — Claude Code runs this as a subprocess
    mcp.run()
