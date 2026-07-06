"""
Configuration for ibkr_mcp server.
Loads from environment variables / .env file.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# IB Gateway connection settings
IB_HOST: str = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT: int = int(os.environ.get("IB_PORT", "4001"))
IB_CLIENT_ID: int = int(os.environ.get("IB_CLIENT_ID", "10"))
IB_TIMEOUT: float = float(os.environ.get("IB_TIMEOUT", "10"))
# Default TRUE: a fresh deployment gets a connection that cannot place orders,
# matching the "all read-only" promise in the README. Set false explicitly to
# enable whatIfOrder-based tools (what-if margin sim, stress preflight, cushion
# trim probe) — IB rejects whatIf requests on read-only API connections.
IB_READONLY: bool = os.environ.get("IB_READONLY", "true").lower() == "true"

# Account settings
PRIMARY_ACCOUNT: str = os.environ.get("PRIMARY_ACCOUNT", "")

# Secondary gateway (dual-account support)
# Set IB_PORT_2 to connect to a second IB Gateway instance.
# If 0 or empty, single-gateway mode — everything works as before.
IB_PORT_2: int = int(os.environ.get("IB_PORT_2", "0") or "0")
IB_CLIENT_ID_2: int = int(os.environ.get("IB_CLIENT_ID_2", "20"))
SECONDARY_ACCOUNT: str = os.environ.get("SECONDARY_ACCOUNT", "")
