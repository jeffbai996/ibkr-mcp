#!/usr/bin/env python3
"""
ibkr_mcp — HTTP transport entry point for remote MCP clients (claude.ai).

Runs the same MCP server as server.py but over streamable HTTP instead of
stdio. This lets claude.ai (and any other remote MCP client) connect over
the network.

Security: Designed to run behind Tailscale Funnel, which provides HTTPS
and a non-guessable URL. All tools are read-only (no order placement).
Do NOT expose this directly to the public internet without auth.

Usage:
    # Run the HTTP server
    python server_http.py

    # Then expose via Tailscale Funnel (separate terminal)
    tailscale funnel 8000

    # Add the Funnel URL to claude.ai:
    # Settings > Connectors > Add > https://literal:your-machine.<tailnet>.ts.net
"""

# Patch asyncio before anything else — same reason as server.py
import nest_asyncio
nest_asyncio.apply()

import os
from dotenv import load_dotenv
load_dotenv()

from mcp.server.fastmcp.server import TransportSecuritySettings
from app import mcp  # noqa: E402

# Register tool modules (same as server.py)
import tools.account      # noqa: F401, E402
import tools.portfolio    # noqa: F401, E402
import tools.market_data  # noqa: F401, E402
import tools.analytics    # noqa: F401, E402

MCP_HTTP_HOST = os.environ.get("MCP_HTTP_HOST", "0.0.0.0")
MCP_HTTP_PORT = int(os.environ.get("MCP_HTTP_PORT", "8000"))


if __name__ == "__main__":
    print(f"Starting ibkr_mcp HTTP server on {MCP_HTTP_HOST}:{MCP_HTTP_PORT}")
    print("Transport: streamable-http")
    print("Endpoint: /mcp/")
    print()
    print("To expose via Tailscale Funnel:")
    print(f"  tailscale funnel {MCP_HTTP_PORT}")

    # Override host/port on the shared FastMCP instance — these are set
    # on the constructor normally, but we share app.py with the stdio server
    mcp.settings.host = MCP_HTTP_HOST
    mcp.settings.port = MCP_HTTP_PORT

    # Disable DNS rebinding protection — requests come through Tailscale
    # Funnel with Host: literal:your-machine.your-tailnet.ts.net, not localhost
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )

    mcp.run(transport="streamable-http")
