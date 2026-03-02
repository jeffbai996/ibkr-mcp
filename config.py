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
IB_READONLY: bool = os.environ.get("IB_READONLY", "false").lower() == "true"

# Account settings
PRIMARY_ACCOUNT: str = os.environ.get("PRIMARY_ACCOUNT", "")
