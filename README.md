# ibkr_terminal — Interactive Brokers MCP Server

Talk about, analyze, research, or optimize your Interactive Brokers portfolio in natural language in your terminal, courtesy of Claude Code.

## What This Does

An MCP server that connects to IB Gateway and exposes 15 tools across 4 categories:

| Category | Tools | What They Do |
|----------|-------|-------------|
| **Account** | 4 | NAV, margin, buying power, P&L |
| **Portfolio** | 4 | Positions, currency breakdown, concentration, snapshots |
| **Market Data** | 3 | Quotes, historical bars, contract details |
| **Analytics** | 4 | What-if margin scenarios, stress tests, concentration risk |

## Quick Start (on literal:your-machine)

### 1. Clone & Setup

```bash
cd ~/projects
# Copy the ibkr-mcp folder to literal:your-machine (or git clone if you've pushed it)

cd ibkr-mcp
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
nano .env
```

Set your IB Gateway connection:
```
IB_HOST=127.0.0.1
IB_PORT=4001
IB_CLIENT_ID=10
IB_TIMEOUT=10
IB_READONLY=false
PRIMARY_ACCOUNT=U1234567    # Your main account ID
```

### 3. Test Connection

```bash
# Quick test — does it connect to Gateway?
python -c "
from ib_insync import IB
ib = IB()
ib.connect('127.0.0.1', 4001, clientId=99, timeout=10)
print('Connected! Accounts:', ib.managedAccounts())
print('Positions:', len(ib.positions()))
ib.disconnect()
"
```

### 4. Register with Claude Code

Add to `~/.claude.json` on literal:your-machine:

```json
{
  "mcpServers": {
    "ibkr": {
      "command": "literal:/path/to/ibkr-terminal/.venv/bin/python",
      "args": ["literal:/path/to/ibkr-terminal/server.py"],
      "env": {
        "IB_HOST": "127.0.0.1",
        "IB_PORT": "4001",
        "IB_CLIENT_ID": "10",
        "PRIMARY_ACCOUNT": "U1234567"
      }
    }
  }
}
```

Or per-project in `~/projects/trading-dashboard/.claude.json`:

```json
{
  "mcpServers": {
    "ibkr": {
      "command": "literal:/path/to/ibkr-terminal/.venv/bin/python",
      "args": ["literal:/path/to/ibkr-terminal/server.py"]
    }
  }
}
```

### 5. Use It

Open Claude Code and talk to your portfolio:

```
> Show me my portfolio
> What's MSFT trading at?
> What happens to my margin if I sell 1000 MSFT?
> If the market drops 5%, am I safe?
> Show me literal:partner's positions (account literal:UXXXXXXX)
> What's my CAD vs USD exposure?
```

## Tool Reference

See `TOOL_CATALOG.md` for the full specification of every tool, including
input schemas, IB API mappings, and example outputs.

## Phase 2 (Future)

- Order placement with explicit confirmation safety latch
- Open orders and execution history
- Cancel order / cancel all
- Enhanced stress tests with volatility scaling

## Dependencies

- `mcp` — Model Context Protocol SDK (FastMCP)
- `ib_insync` — IBKR TWS API wrapper (asyncio-native)
- `pydantic` — Input validation
- `python-dotenv` — Environment config
