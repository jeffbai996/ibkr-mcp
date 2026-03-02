"""
Shared FastMCP instance.

Lives here (not in server.py) to avoid the __main__ vs module-name
double-import problem. Both server.py and tools/* import from here,
so they all reference the same mcp object.
"""

from mcp.server.fastmcp import FastMCP
from core.connection import ib_lifespan

mcp = FastMCP("ibkr_mcp", lifespan=ib_lifespan)
