#!/usr/bin/env python3
"""
ibkr_mcp — HTTP transport entry point for remote MCP clients (claude.ai).

Runs the same MCP server as server.py but over streamable HTTP instead of
stdio. This lets claude.ai (and any other remote MCP client) connect over
the network.

KEY DIFFERENCE FROM server.py:
In streamable HTTP, the lifespan fires per-session (not once at startup
like stdio). So we connect to IB Gateway lazily on the first session and
cache the connection for all subsequent sessions.

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

import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from ib_insync import IB
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

from config import IB_HOST, IB_PORT, IB_CLIENT_ID, IB_TIMEOUT, IB_READONLY, PRIMARY_ACCOUNT

logger = logging.getLogger("ibkr_mcp.http")

# --- Shared IB connection (lazily created on first session) ---
_ib: IB | None = None
_primary_account: str = ""
_ib_lock = asyncio.Lock()


async def _connect_ib() -> tuple[IB, str]:
    """Connect to IB Gateway once. Called lazily on first session."""
    ib = IB()
    # PID-based offset in 5000-9999 range to avoid collisions
    # with stdio server's PID-based IDs (which use 0-8999 range)
    client_id = IB_CLIENT_ID + 5000 + (os.getpid() % 5000)

    logger.info(f"Connecting to IB Gateway at {IB_HOST}:{IB_PORT} "
                f"(clientId={client_id}, readonly={IB_READONLY})...")

    await ib.connectAsync(
        host=IB_HOST,
        port=IB_PORT,
        clientId=client_id,
        timeout=IB_TIMEOUT,
        readonly=IB_READONLY,
    )

    accounts = ib.managedAccounts()
    primary = PRIMARY_ACCOUNT
    if not primary and accounts:
        primary = accounts[0]

    logger.info(f"Connected. Accounts: {accounts}. Primary: {primary}")
    return ib, primary


@asynccontextmanager
async def http_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """
    Per-session lifespan that lazily connects to IB Gateway.

    First session triggers the connection. All subsequent sessions
    reuse the cached connection. If the connection drops, the next
    session reconnects automatically.
    """
    global _ib, _primary_account

    async with _ib_lock:
        if _ib is None or not _ib.isConnected():
            _ib, _primary_account = await _connect_ib()

    yield {"ib": _ib, "primary_account": _primary_account}


# --- Replace app.py's mcp BEFORE tool imports ---
# Tool modules do `from app import mcp` and decorate with @mcp.tool().
# By swapping app.mcp before those imports run, all tools register on
# our HTTP-configured instance with the lazy-connect lifespan.
import app  # noqa: E402
app.mcp = FastMCP("ibkr_mcp", lifespan=http_lifespan)
mcp = app.mcp

# --- Register tool modules (they import mcp from app.py) ---
import tools.account      # noqa: F401, E402
import tools.portfolio    # noqa: F401, E402
import tools.market_data  # noqa: F401, E402
import tools.analytics    # noqa: F401, E402
import tools.orders       # noqa: F401, E402

MCP_HTTP_HOST = os.environ.get("MCP_HTTP_HOST", "0.0.0.0")
MCP_HTTP_PORT = int(os.environ.get("MCP_HTTP_PORT", "8000"))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print(f"Starting ibkr_mcp HTTP server on {MCP_HTTP_HOST}:{MCP_HTTP_PORT}")
    print("Transport: streamable-http")
    print("Endpoint: /mcp/")
    print()
    print("To expose via Tailscale Funnel:")
    print(f"  tailscale funnel {MCP_HTTP_PORT}")

    mcp.settings.host = MCP_HTTP_HOST
    mcp.settings.port = MCP_HTTP_PORT

    # JSON response mode — claude.ai's initial probe only sends
    # Accept: application/json (without text/event-stream), which the
    # MCP SDK rejects with 406 in default SSE mode. JSON mode relaxes
    # the Accept header check to only require application/json.
    mcp.settings.json_response = True

    # Disable DNS rebinding protection — requests come through Tailscale
    # Funnel with Host: literal:your-machine.your-tailnet.ts.net, not localhost
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )

    mcp.run(transport="streamable-http")
