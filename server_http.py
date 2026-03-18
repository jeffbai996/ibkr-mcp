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

Supports dual-gateway mode: if IB_PORT_2 is set, lazily connects to a
second IB Gateway instance alongside the primary.

Security: Designed to run behind Tailscale Funnel, which provides HTTPS
and a non-guessable URL. All tools are read-only (no order placement).
Do NOT expose this directly to the public internet without auth.

Usage:
    # Run the HTTP server
    python server_http.py

    # Then expose via Tailscale Funnel (separate terminal)
    tailscale funnel 8000

    # Add the Funnel URL to claude.ai:
    # Settings > Connectors > Add > https://<your-machine>.<tailnet>.ts.net
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

from config import (
    IB_HOST, IB_PORT, IB_CLIENT_ID, IB_TIMEOUT, IB_READONLY,
    PRIMARY_ACCOUNT, IB_PORT_2, IB_CLIENT_ID_2, SECONDARY_ACCOUNT,
)
from core.connection import ConnectionHealth

logger = logging.getLogger("ibkr_mcp.http")

# --- Shared IB connections (lazily created on first session) ---
_ib: IB | None = None
_ib2: IB | None = None
_primary_account: str = ""
_secondary_account: str = ""
_account_map: dict[str, IB] = {}
_health: ConnectionHealth = ConnectionHealth()
_health2: ConnectionHealth = ConnectionHealth()
_health_map: dict[str, ConnectionHealth] = {}
_ib_lock = asyncio.Lock()


async def _connect_ib() -> tuple[IB, str]:
    """Connect to primary IB Gateway. Disconnects stale connection first."""
    global _ib, _health

    # Clean up stale connection before creating new one
    if _ib is not None:
        try:
            _ib.disconnect()
        except Exception:
            pass

    ib = IB()
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

    # Attach health monitoring
    _health = ConnectionHealth()
    _health.attach(ib)

    accounts = ib.managedAccounts()
    primary = PRIMARY_ACCOUNT
    if not primary and accounts:
        primary = accounts[0]

    masked = [f"...{a[-4:]}" for a in accounts]
    logger.info(f"Connected. {len(accounts)} account(s): {masked}. "
                f"Primary: ...{primary[-4:] if primary else 'auto'}")
    return ib, primary


async def _connect_ib2() -> tuple[IB | None, str]:
    """Connect to secondary IB Gateway. Returns (None, "") if not configured or fails."""
    global _ib2, _health2

    if not IB_PORT_2:
        return None, ""

    # Clean up stale connection
    if _ib2 is not None:
        try:
            _ib2.disconnect()
        except Exception:
            pass

    ib2 = IB()
    client_id_2 = IB_CLIENT_ID_2 + 5000 + (os.getpid() % 5000)

    try:
        logger.info(f"Connecting to secondary IB Gateway at {IB_HOST}:{IB_PORT_2} "
                     f"(clientId={client_id_2})...")
        await ib2.connectAsync(
            host=IB_HOST,
            port=IB_PORT_2,
            clientId=client_id_2,
            timeout=IB_TIMEOUT,
            readonly=IB_READONLY,
        )

        # Attach health monitoring
        _health2 = ConnectionHealth()
        _health2.attach(ib2)

        accounts_2 = ib2.managedAccounts()
        secondary = SECONDARY_ACCOUNT or (accounts_2[0] if accounts_2 else "")
        masked_2 = [f"...{a[-4:]}" for a in accounts_2]
        logger.info(f"Secondary connected. {len(accounts_2)} account(s): {masked_2}. "
                    f"Secondary: ...{secondary[-4:] if secondary else 'auto'}")
        return ib2, secondary
    except Exception as e:
        logger.warning(f"Failed to connect to secondary IB Gateway on port {IB_PORT_2}: {e}. "
                       "Continuing with primary only.")
        return None, ""


@asynccontextmanager
async def http_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """
    Per-session lifespan that lazily connects to IB Gateway(s).

    First session triggers the connection(s). All subsequent sessions
    reuse the cached connections. If a connection drops (TCP or upstream),
    the next session reconnects automatically with retry.
    """
    global _ib, _ib2, _primary_account, _secondary_account, _account_map, _health_map

    async with _ib_lock:
        # Primary connection — check TCP + upstream health
        needs_reconnect = (
            _ib is None
            or not _ib.isConnected()
            or not _health.connected
        )
        if needs_reconnect:
            for attempt in range(3):
                try:
                    _ib, _primary_account = await _connect_ib()
                    _account_map = {}
                    _health_map = {}
                    for acc in _ib.managedAccounts():
                        _account_map[acc] = _ib
                        _health_map[acc] = _health
                    break
                except Exception as e:
                    if attempt == 2:
                        raise ConnectionError(
                            f"IB Gateway unavailable after 3 attempts: {e}. "
                            "Gateway may be restarting or down."
                        ) from e
                    logger.warning(f"Connect attempt {attempt + 1} failed: {e}. Retrying in 2s...")
                    await asyncio.sleep(2)

        # Secondary connection
        needs_reconnect_2 = (
            IB_PORT_2
            and (_ib2 is None or not _ib2.isConnected() or not _health2.connected)
        )
        if needs_reconnect_2:
            _ib2, _secondary_account = await _connect_ib2()
            if _ib2:
                for acc in _ib2.managedAccounts():
                    _account_map[acc] = _ib2
                    _health_map[acc] = _health2

    yield {
        "ib": _ib,
        "ib2": _ib2,
        "primary_account": _primary_account,
        "secondary_account": _secondary_account,
        "account_map": _account_map,
        "health": _health,
        "health_map": _health_map,
    }


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
import tools.orders       # noqa: F401, E402
import tools.live_data    # noqa: F401, E402
import tools.risk         # noqa: F401, E402
import tools.intelligence # noqa: F401, E402
import tools.monitoring   # noqa: F401, E402

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
    # Funnel with a non-localhost Host header, not 0.0.0.0
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )

    mcp.run(transport="streamable-http")
