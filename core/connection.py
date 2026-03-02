"""
IB Gateway connection manager.

Uses FastMCP's lifespan pattern to maintain a persistent IB connection
across all tool calls. The IB object connects on server startup and
disconnects on shutdown.

WHY LIFESPAN:
ib_insync's IB() object maintains a TCP connection to IB Gateway and
auto-syncs account state (positions, orders, etc). Creating a new
connection per tool call would be slow (~4s handshake + sync) and wasteful.
The lifespan pattern keeps one connection alive for the server's lifetime.
"""

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from ib_insync import IB
from mcp.server.fastmcp import FastMCP

from config import IB_HOST, IB_PORT, IB_CLIENT_ID, IB_TIMEOUT, IB_READONLY, PRIMARY_ACCOUNT

# Log to stderr — stdio transport uses stdout for MCP protocol messages
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("ibkr_mcp.connection")


@asynccontextmanager
async def ib_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """
    Connect to IB Gateway on startup, disconnect on shutdown.

    This is passed to FastMCP as the lifespan parameter. FastMCP calls it
    once when the server starts, and the yielded dict is available to
    all tool functions via ctx.request_context.lifespan_context.

    We yield a plain dict (not a dataclass) for maximum compatibility
    across FastMCP versions.
    """
    ib = IB()

    # Use PID-based client ID to avoid collisions from stale connections.
    # IB Gateway holds onto client IDs from unclean disconnects — using a
    # unique ID per process sidesteps the problem entirely.
    client_id = IB_CLIENT_ID + (os.getpid() % 9000)

    try:
        logger.info(f"Connecting to IB Gateway at {IB_HOST}:{IB_PORT} "
                     f"(clientId={client_id}, readonly={IB_READONLY})...")

        await ib.connectAsync(
            host=IB_HOST,
            port=IB_PORT,
            clientId=client_id,
            timeout=IB_TIMEOUT,
            readonly=IB_READONLY,
        )

        # Determine primary account
        accounts = ib.managedAccounts()
        primary = PRIMARY_ACCOUNT
        if not primary and len(accounts) == 1:
            primary = accounts[0]
        elif not primary and len(accounts) > 1:
            primary = accounts[0]
            logger.info(f"Multiple accounts detected: {accounts}. "
                        f"Defaulting to {primary}. Set PRIMARY_ACCOUNT in .env to override.")

        logger.info(f"Connected. Accounts: {accounts}. Primary: {primary}")

        yield {"ib": ib, "primary_account": primary}

    except Exception as e:
        logger.error(f"Failed to connect to IB Gateway: {e}")
        raise
    finally:
        if ib.isConnected():
            ib.disconnect()
            logger.info("Disconnected from IB Gateway.")


def get_ib(ctx) -> IB:
    """
    Helper to extract IB instance from tool context.

    Usage inside a tool:
        ib = get_ib(ctx)
        positions = ib.portfolio()
    """
    return ctx.request_context.lifespan_context["ib"]


def get_primary_account(ctx) -> str:
    """Helper to get the primary account ID from context."""
    return ctx.request_context.lifespan_context["primary_account"]


def resolve_account(ctx, account: str | None) -> str:
    """
    Resolve which account to use.
    If account is provided, use it. Otherwise fall back to primary.
    """
    if account:
        return account
    return get_primary_account(ctx)
