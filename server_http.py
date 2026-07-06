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
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NoReturn

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
# Live context dict — shared with MCP tool context. Background loop
# updates this in-place so tools always see current gateway state.
_live_ctx: dict = {}


def _sync_live_ctx() -> None:
    """Push current global state into the live context dict.

    The dict is yielded by http_lifespan and read by tools via
    ctx.request_context.lifespan_context. Updating in-place ensures
    tools always see the latest gateway connections, even when the
    secondary connects after the session started.
    """
    _live_ctx.update({
        "ib": _ib,
        "ib2": _ib2,
        "primary_account": _primary_account,
        "secondary_account": _secondary_account,
        "account_map": _account_map,
        "health": _health,
        "health_map": _health_map,
    })


def _build_account_map(ib, health, ib2, health2):
    """Map every managed account → its serving IB connection, across BOTH
    gateways. Pure function (no globals) so it's unit-testable.

    Returns (account_map, health_map). A connection contributes its accounts
    only if it's present and connected. Building from both connections every
    time prevents a primary reconnect from silently dropping the secondary's
    accounts (which made /api/positions?account=<secondary> fall back to the
    primary connection and return an empty portfolio).
    """
    amap: dict = {}
    hmap: dict = {}
    for conn, h in ((ib, health), (ib2, health2)):
        if conn is not None and conn.isConnected():
            for acc in conn.managedAccounts():
                amap[acc] = conn
                hmap[acc] = h
    return amap, hmap


async def _try_connect(host: str, port: int, client_id: int,
                       timeout: int, readonly: bool,
                       account: str = "") -> IB:
    """Attempt a single IB Gateway connection. Cleans up on failure.

    Returns connected IB instance or raises on failure.
    Always calls ib.disconnect() if connectAsync fails, so the gateway
    frees the clientId slot even on partial TCP connections.

    After successful connect, sets market data type to 4 (delayed-frozen).
    Type 4 means: live if subscribed, delayed if not, frozen (last known)
    if market closed. Without this call the default is type 1 (live), which
    hard-fails snapshot + historical requests when no market data
    subscription is active — the symptom seen in 2026-05 when reqMktData
    returned no data and reqHistoricalData timed out with Error 162.
    """
    ib = IB()
    try:
        kwargs: dict = {
            "host": host, "port": port, "clientId": client_id,
            "timeout": timeout, "readonly": readonly,
        }
        if account:
            kwargs["account"] = account
        await ib.connectAsync(**kwargs)
        try:
            ib.reqMarketDataType(4)
            logger.info(f"clientId={client_id}: market data type set to "
                        f"delayed-frozen (4)")
        except Exception as md_err:
            logger.warning(f"clientId={client_id}: reqMarketDataType(4) "
                           f"failed: {md_err}")
        return ib
    except Exception:
        try:
            ib.disconnect()
        except Exception:
            pass
        raise


async def _connect_ib() -> tuple[IB, str]:
    """Connect to primary IB Gateway.

    Tries the configured clientId first. If Error 326 (clientId in use),
    falls back to configured + 100 to bypass stale gateway sessions from
    a prior crash. Fixed fallback avoids tab explosion from random IDs.
    """
    global _ib, _health

    # Disconnect old instance FIRST to free the client ID slot on the gateway
    if _ib is not None:
        try:
            _ib.disconnect()
        except Exception:
            pass
        _ib = None

    # Try configured ID first, then fixed fallback (not random — avoids gateway tab explosion)
    ids_to_try = [IB_CLIENT_ID, IB_CLIENT_ID + 100]

    for i, client_id in enumerate(ids_to_try):
        label = "" if i == 0 else " (fallback)"
        logger.info(f"Connecting to IB Gateway at {IB_HOST}:{IB_PORT} "
                    f"(clientId={client_id}, readonly={IB_READONLY}){label}...")
        try:
            ib = await _try_connect(IB_HOST, IB_PORT, client_id,
                                    IB_TIMEOUT, IB_READONLY)

            # Attach health monitoring
            _health = ConnectionHealth()
            _health.attach(ib)

            accounts = ib.managedAccounts()
            primary = PRIMARY_ACCOUNT
            if not primary and accounts:
                primary = accounts[0]

            masked = [f"...{a[-4:]}" for a in accounts]
            logger.info(f"Connected{label}. {len(accounts)} account(s): {masked}. "
                        f"Primary: ...{primary[-4:] if primary else 'auto'}")
            return ib, primary
        except Exception as e:
            if i < len(ids_to_try) - 1:
                logger.warning(f"clientId {client_id} failed: {e}. "
                               "Trying fallback ID...")
                continue
            raise


async def _connect_ib2() -> tuple[IB | None, str]:
    """Connect to secondary IB Gateway. Returns (None, "") if not configured or fails.

    Same fallback strategy as _connect_ib() — tries configured ID first,
    then fixed fallback on Error 326.
    """
    global _ib2, _health2

    if not IB_PORT_2:
        return None, ""

    # Disconnect old instance FIRST to free the client ID slot
    if _ib2 is not None:
        try:
            _ib2.disconnect()
        except Exception:
            pass
        _ib2 = None

    target_account = SECONDARY_ACCOUNT or ""
    ids_to_try = [IB_CLIENT_ID_2, IB_CLIENT_ID_2 + 100]

    try:
        ib2 = None
        for i, client_id_2 in enumerate(ids_to_try):
            label = "" if i == 0 else " (fallback)"
            logger.info(f"Connecting to secondary IB Gateway at {IB_HOST}:{IB_PORT_2} "
                         f"(clientId={client_id_2}, account={target_account or 'auto'}){label}")
            try:
                ib2 = await asyncio.wait_for(
                    _try_connect(IB_HOST, IB_PORT_2, client_id_2,
                                 IB_TIMEOUT, IB_READONLY, account=target_account),
                    timeout=15,
                )
                break  # Connected
            except Exception as e:
                if i < len(ids_to_try) - 1:
                    logger.warning(f"Secondary clientId {client_id_2} failed: {e}. "
                                   "Trying fallback ID...")
                    continue
                raise
        if ib2 is None:
            raise RuntimeError("All client IDs failed")

        # Attach health monitoring
        _health2 = ConnectionHealth()
        _health2.attach(ib2)

        accounts_2 = ib2.managedAccounts()
        secondary = SECONDARY_ACCOUNT or (accounts_2[0] if accounts_2 else "")
        masked_2 = [f"...{a[-4:]}" for a in accounts_2]
        logger.info(f"Secondary connected. {len(accounts_2)} account(s): {masked_2}. "
                    f"Secondary: ...{secondary[-4:] if secondary else 'auto'}")

        # Diagnostic: confirm portfolio data populated after connectAsync(account=)
        n_positions = len(ib2.positions())
        n_portfolio = len(list(ib2.portfolio(secondary))) if secondary else 0
        logger.info(f"Secondary diagnostics: positions()={n_positions}, "
                    f"portfolio('{secondary}')={n_portfolio}")

        return ib2, secondary
    except Exception as e:
        # Clean up partial connection (TCP may be open even if order sync timed out)
        if ib2 is not None:
            try:
                ib2.disconnect()
            except Exception:
                pass
        msg = "timed out (15s)" if isinstance(e, asyncio.TimeoutError) else str(e)
        logger.warning(f"Failed to connect to secondary IB Gateway on port {IB_PORT_2}: {msg}. "
                       "Continuing with primary only.")
        return None, ""


_reconnect_task: asyncio.Task | None = None


def _on_reconnect_task_done(task: asyncio.Task) -> None:
    """Callback when the reconnect task exits — log why and restart it.

    The reconnect loop should never exit. If it does, something went wrong
    (CancelledError during shutdown, unhandled exception that slipped past
    the catch-all). Log the cause and restart unless we're shutting down.
    """
    global _reconnect_task
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        logger.info("Reconnect loop cancelled (shutdown).")
        return

    if exc:
        logger.error(f"Reconnect loop died with exception: {exc}", exc_info=exc)
    else:
        logger.warning("Reconnect loop exited unexpectedly (no exception).")

    # Restart the loop — get the running loop safely
    try:
        loop = asyncio.get_running_loop()
        _reconnect_task = loop.create_task(
            _background_reconnect_loop(),
            name="ibkr_reconnect",
        )
        _reconnect_task.add_done_callback(_on_reconnect_task_done)
        logger.info("Reconnect loop restarted.")
    except RuntimeError:
        logger.error("Cannot restart reconnect loop — no running event loop.")


async def _background_reconnect_loop() -> NoReturn:
    """Periodically try to reconnect to IB Gateway(s) when offline.

    Runs every 30s. When the gateway comes back (e.g., after wife logs
    out of TWS), this restores the connection automatically without
    needing a new MCP session.
    """
    global _ib, _ib2, _primary_account, _secondary_account, _account_map, _health_map

    # Try immediately on first run — secondary was deferred from lifespan
    _first_run = True

    while True:
        if _first_run:
            _first_run = False
            await asyncio.sleep(1)  # Brief yield to let lifespan finish
        else:
            await asyncio.sleep(30)

        # Primary: skip if already connected
        primary_ok = _ib is not None and _ib.isConnected() and _health.connected
        secondary_ok = (
            not IB_PORT_2
            or (_ib2 is not None and _ib2.isConnected() and _health2.connected)
        )

        # Periodic cache eviction (piggybacks on this loop's 30s tick)
        try:
            from core.cache import cache_evict
            evicted = cache_evict()
            if evicted:
                logger.debug(f"Cache eviction: removed {evicted} expired entries")
        except Exception as e:
            logger.warning(f"Cache eviction failed: {e}")

        # Heartbeat: ping connections that appear OK to catch silent TCP drops.
        # WSL/Hyper-V drops idle TCP sessions without sending a disconnect event,
        # so isConnected() stays True while the socket is dead. reqCurrentTimeAsync
        # forces a round-trip — if it times out, we force-disconnect so the
        # reconnect block below picks it up immediately.
        if primary_ok:
            try:
                await asyncio.wait_for(_ib.reqCurrentTimeAsync(), timeout=5.0)
            except Exception:
                logger.warning("Primary heartbeat timed out — forcing reconnect")
                try:
                    _ib.disconnect()
                except Exception:
                    pass
                primary_ok = False

        if secondary_ok and IB_PORT_2:
            try:
                await asyncio.wait_for(_ib2.reqCurrentTimeAsync(), timeout=5.0)
            except Exception:
                logger.warning("Secondary heartbeat timed out — forcing reconnect")
                try:
                    _ib2.disconnect()
                except Exception:
                    pass
                secondary_ok = False

        if primary_ok and secondary_ok:
            continue

        try:
            async with _ib_lock:
                # Re-check after acquiring lock
                if not (_ib is not None and _ib.isConnected() and _health.connected):
                    try:
                        _ib, _primary_account = await _connect_ib()
                        # Rebuild from BOTH live connections — wiping to {} and
                        # re-adding only the primary's accounts dropped the
                        # secondary's mapping whenever the primary flapped, so
                        # /api/positions?account=<secondary> fell back to the
                        # primary connection and returned an empty portfolio.
                        _account_map, _health_map = _build_account_map(
                            _ib, _health, _ib2, _health2
                        )
                        _sync_live_ctx()
                        # Prime account summary — see secondary block below
                        # for rationale.
                        try:
                            await _ib.accountSummaryAsync()
                            logger.info("Primary: account summary primed")
                        except Exception as prime_err:
                            logger.warning(f"Primary: accountSummary prime failed: {prime_err}")
                        logger.info("Background reconnect: primary gateway restored")
                    except Exception as e:
                        logger.warning(f"Background reconnect (primary) failed: {e}")

                # Secondary
                if IB_PORT_2 and not (
                    _ib2 is not None and _ib2.isConnected() and _health2.connected
                ):
                    try:
                        _ib2, _secondary_account = await _connect_ib2()
                        if _ib2:
                            # Rebuild from both so neither connection's accounts
                            # get lost regardless of which one just reconnected.
                            _account_map, _health_map = _build_account_map(
                                _ib, _health, _ib2, _health2
                            )
                            _sync_live_ctx()
                            # Prime account summary + portfolio subscriptions.
                            # ib_insync's sync accountSummary() triggers
                            # reqAccountSummaryAsync on demand, but if the
                            # first dashboard hit lands before the subscription
                            # snapshot finishes, it can see empty results and
                            # "No summary available" leaks out. Priming here
                            # guarantees the wrapper's acctSummary dict is
                            # populated before any tool call.
                            try:
                                await _ib2.accountSummaryAsync()
                                logger.info("Secondary: account summary primed")
                            except Exception as prime_err:
                                logger.warning(f"Secondary: accountSummary prime failed: {prime_err}")
                            logger.info("Background reconnect: secondary gateway restored")
                    except Exception as e:
                        logger.warning(f"Background reconnect (secondary) failed: {e}")
        except Exception as e:
            # Catch-all so the loop never dies silently
            logger.error(f"Background reconnect loop error: {e}", exc_info=True)


@asynccontextmanager
async def http_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """
    Per-session lifespan — yields current IB state without blocking.

    NEVER attempts to connect. The background reconnect loop is solely
    responsible for establishing/restoring gateway connections. This
    prevents lock contention cascades when the gateway is down.

    Tools handle ib=None via GatewayOfflineError + @cached_tool.
    """
    global _reconnect_task

    # Initialize persistence DB (idempotent, first call only)
    from core.persistence import init_db
    init_db()

    # Start background reconnect loop if not already running.
    # This is the ONLY place connections are attempted.
    if _reconnect_task is None or _reconnect_task.done():
        _reconnect_task = asyncio.create_task(
            _background_reconnect_loop(),
            name="ibkr_reconnect",
        )
        _reconnect_task.add_done_callback(_on_reconnect_task_done)

    # No lock, no connect attempt. Just sync and yield current state.
    _sync_live_ctx()
    yield _live_ctx


# --- Replace app.py's mcp BEFORE tool imports ---
# Tool modules do `from app import mcp` and decorate with @mcp.tool().
# By swapping app.mcp before those imports run, all tools register on
# our HTTP-configured instance with the lazy-connect lifespan.
#
# OAuth 2.1 (opt-in): when IBKR_MCP_OAUTH_ENABLED=1 + issuer URL are set,
# fastmcp_auth_kwargs() hands FastMCP an AuthSettings + provider — the SDK
# then auto-mounts /authorize, /token, /register, /.well-known/* at the
# origin root and gates /mcp with per-client bearer tokens. Off (default)
# → empty kwargs, byte-identical behavior. See auth/wiring.py + docs/OAUTH.md.
from auth import wiring as oauth_wiring  # noqa: E402

import app  # noqa: E402
app.mcp = FastMCP("ibkr_mcp", lifespan=http_lifespan,
                  **oauth_wiring.fastmcp_auth_kwargs())
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
import tools.briefing     # noqa: F401, E402
import tools.selftest     # noqa: F401, E402
import tools.news         # noqa: F401, E402
import tools.fundamentals # noqa: F401, E402
import tools.scanner      # noqa: F401, E402
import tools.shortable    # noqa: F401, E402
import tools.volatility   # noqa: F401, E402

# --- Dashboard REST routes (registered via @mcp.custom_route) ---
import dashboard  # noqa: F401, E402


# --- OAuth admin surface (the squad inventory's OAuth panel) ---
# Lives under /api/* so the static bearer gate covers it; logic in
# auth/wiring.py (tested there), these are transport shims.
@mcp.custom_route("/api/oauth/status", methods=["GET"])
async def _oauth_status(request):  # noqa: ANN001
    from starlette.responses import JSONResponse
    return JSONResponse(oauth_wiring.status_payload())


@mcp.custom_route("/api/oauth/revoke_client", methods=["POST"])
async def _oauth_revoke_client(request):  # noqa: ANN001
    from starlette.responses import JSONResponse
    body = await request.json()
    client_id = (body or {}).get("client_id") or ""
    if not client_id:
        return JSONResponse({"error": "client_id required"}, status_code=400)
    return JSONResponse({"revoked": oauth_wiring.revoke_client(client_id)})

MCP_HTTP_HOST = os.environ.get("MCP_HTTP_HOST", "0.0.0.0")
MCP_HTTP_PORT = int(os.environ.get("MCP_HTTP_PORT", "8000"))


import hmac as _hmac
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse as _AuthJSONResponse


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Opt-in static bearer-token gate for the HTTP surface (F-01).

    The MCP + /api/* surface exposes the full portfolio read. When ``token`` is
    a non-empty string, every request must carry ``Authorization: Bearer
    <token>`` or get 401. When ``token`` is empty the middleware is a pure
    no-op — DEFAULT-OFF so enabling auth is a deliberate, coordinated step
    (set MCP_HTTP_TOKEN on the server AND update the claude.ai connector /
    dashboard in one push) rather than a silent lockout. Uses a constant-time
    compare to avoid a token-timing oracle.

    OAuth composition: when ``oauth_active`` is True, the OAuth flow paths
    (/authorize, /token, /register, /.well-known/*) and the MCP endpoint are
    EXEMPT from this static gate. The flow paths must be publicly reachable —
    a client has no token of any kind when it first hits them — and /mcp is
    gated by the SDK's own OAuth bearer verifier; double-gating would demand
    two different Authorization headers on one request. This middleware then
    only covers what OAuth cannot: the /api/* dashboard surface. That split is
    why the static gate stays instead of being deleted outright.
    """

    def __init__(self, app, token: str = "", oauth_active: bool = False):
        super().__init__(app)
        self._token = (token or "").strip()
        self._oauth_active = bool(oauth_active)

    async def dispatch(self, request, call_next):
        if not self._token:
            return await call_next(request)  # auth disabled — no-op
        if self._oauth_active:
            from auth.wiring import bearer_exempt
            if bearer_exempt(request.url.path):
                return await call_next(request)  # OAuth's surface, not ours
        header = request.headers.get("authorization", "")
        prefix = "Bearer "
        supplied = header[len(prefix):] if header.startswith(prefix) else ""
        if not (supplied and _hmac.compare_digest(supplied, self._token)):
            return _AuthJSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

# Dashboard frontend dist directory (built by Vite)
DASHBOARD_DIST = os.environ.get(
    "DASHBOARD_DIST",
    os.path.join(os.path.dirname(__file__), "..", "ibkr-dashboard", "frontend", "dist"),
)


if __name__ == "__main__":
    import uvicorn
    from pathlib import Path
    from starlette.routing import Mount
    from starlette.staticfiles import StaticFiles

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    logger.info(f"Starting ibkr_mcp HTTP server on {MCP_HTTP_HOST}:{MCP_HTTP_PORT}")
    logger.info("Transport: streamable-http")
    logger.info("Endpoint: /mcp/")
    logger.info(f"Config: IB_PORT={IB_PORT}, IB_CLIENT_ID={IB_CLIENT_ID}, "
                f"IB_PORT_2={IB_PORT_2}, IB_CLIENT_ID_2={IB_CLIENT_ID_2}, "
                f"SECONDARY_ACCOUNT={SECONDARY_ACCOUNT or '(auto)'}")

    mcp.settings.host = MCP_HTTP_HOST
    mcp.settings.port = MCP_HTTP_PORT

    # JSON response mode — claude.ai's initial probe only sends
    # Accept: application/json (without text/event-stream), which the
    # MCP SDK rejects with 406 in default SSE mode. JSON mode relaxes
    # the Accept header check to only require application/json.
    mcp.settings.json_response = True

    # Stateless mode — no session IDs, no GET SSE streams. Each request
    # is independent. Fixes: (1) Claude Code stale session bug (#27142)
    # where client caches dead session ID and never re-initializes,
    # (2) claude.ai 409 race condition from overlapping SSE streams.
    # Safe because all state lives in IB Gateway, not MCP sessions.
    mcp.settings.stateless_http = True

    # Disable DNS rebinding protection — requests come through Tailscale
    # Funnel with a non-localhost Host header, not 0.0.0.0
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )

    # Get the Starlette app from FastMCP, then add static file serving
    starlette_app = mcp.streamable_http_app()

    # --- Opt-in static bearer auth (F-01) + OAuth composition ---
    _oauth_on = oauth_wiring.oauth_enabled()
    starlette_app.add_middleware(
        BearerAuthMiddleware,
        token=os.environ.get("MCP_HTTP_TOKEN", "").strip(),
        oauth_active=_oauth_on,
    )
    if _oauth_on:
        logger.info("HTTP auth: OAuth 2.1 ENABLED on /mcp (issuer %s) — "
                    "discovery + flow routes at origin root",
                    oauth_wiring.oauth_issuer_url())
    if os.environ.get("MCP_HTTP_TOKEN", "").strip():
        logger.info("HTTP auth: static bearer gate ENABLED (MCP_HTTP_TOKEN set)%s",
                    " — covering /api/* only; /mcp + OAuth flow exempt" if _oauth_on else "")
    elif not _oauth_on:
        logger.warning("HTTP auth: DISABLED (MCP_HTTP_TOKEN unset) — surface is "
                       "unauthenticated. Set MCP_HTTP_TOKEN + update clients to enable.")

    dist_dir = Path(DASHBOARD_DIST)
    if dist_dir.exists():
        # Append static file mount AFTER MCP + API routes (lower priority)
        starlette_app.router.routes.append(
            Mount("/", app=StaticFiles(directory=str(dist_dir), html=True), name="dashboard")
        )
        logger.info(f"Dashboard: serving frontend from {dist_dir}")
    else:
        logger.info(f"Dashboard: no frontend found at {dist_dir} (API-only mode)")

    logger.info("To expose via Tailscale Funnel: tailscale funnel %d", MCP_HTTP_PORT)

    def _shutdown_ib():
        """Disconnect IB connections on server shutdown to free client IDs."""
        global _ib, _ib2
        for label, conn in [("primary", _ib), ("secondary", _ib2)]:
            if conn is not None:
                try:
                    conn.disconnect()
                    logger.info(f"Disconnected {label} IB connection.")
                except Exception as e:
                    logger.warning(f"Error disconnecting {label}: {e}")
        _ib = None
        _ib2 = None

    import atexit
    atexit.register(_shutdown_ib)

    # SIGTERM handler — ensures clean IB disconnect when killed by process manager
    def _handle_sigterm(signum, frame):
        global _reconnect_task
        logger.info("Received SIGTERM — shutting down cleanly.")
        # Cancel reconnect loop first so it doesn't block shutdown
        if _reconnect_task and not _reconnect_task.done():
            _reconnect_task.cancel()
        _shutdown_ib()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    def _free_port(port: int) -> None:
        """Kill any process LISTENING on port before we try to bind it.

        IMPORTANT: Only targets the listening socket, not established client
        connections. The old version used `lsof -ti :PORT` which returns ALL
        pids (listeners + clients), which would SIGTERM Claude Code sessions
        that were connected as MCP clients.
        """
        try:
            # -sTCP:LISTEN restricts to listening sockets only — won't catch
            # Claude Code or other clients with established connections
            result = subprocess.run(
                ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True,
            )
            pids = [p.strip() for p in result.stdout.splitlines() if p.strip()]
            if not pids:
                return
            own_pid = str(os.getpid())
            for pid in pids:
                if pid == own_pid:
                    continue
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    logger.info("Sent SIGTERM to pid %s listening on port %d", pid, port)
                except ProcessLookupError:
                    pass
            # Give processes a moment to exit cleanly
            time.sleep(0.5)
            # SIGKILL anything still alive
            result2 = subprocess.run(
                ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True,
            )
            for pid in [p.strip() for p in result2.stdout.splitlines() if p.strip()]:
                if pid == own_pid:
                    continue
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    logger.warning("SIGKILL pid %s (did not exit after SIGTERM)", pid)
                except ProcessLookupError:
                    pass
        except FileNotFoundError:
            # lsof not available — skip silently
            pass

    _free_port(MCP_HTTP_PORT)

    # Use async Server API to avoid nest_asyncio incompatibility with
    # uvicorn>=0.30 which passes loop_factory to asyncio.run() — a kwarg
    # the nest_asyncio-patched run() doesn't accept.
    async def _run_server() -> None:
        config = uvicorn.Config(starlette_app, host=MCP_HTTP_HOST, port=MCP_HTTP_PORT)
        server = uvicorn.Server(config)
        await server.serve()

    asyncio.run(_run_server())
