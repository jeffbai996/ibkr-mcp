# ibkr-mcp

An MCP server that lets a language model read and reason about an Interactive Brokers account. It exposes 41 read-only tools over the [Model Context Protocol](https://modelcontextprotocol.io), covering margin, risk, market data, news, fundamentals and portfolio analysis.

[![Python](https://img.shields.io/badge/python-3.12-a4262c?style=flat-square&logo=python&logoColor=white)](server_http.py)
[![MCP](https://img.shields.io/badge/MCP-41_tools-a4262c?style=flat-square)](https://modelcontextprotocol.io)
[![Read-only](https://img.shields.io/badge/broker_access-read--only-a4262c?style=flat-square)](#)

Ask a question in plain language, such as *"how far can the book fall before a margin call?"*. The model picks the tools, chains them, and answers with live numbers from the broker. Nothing here can place an order.

> This repository is the public server layer of a system I run for my own trading. The tool implementations (`core/`, `tools/`) are private and are stripped out when it is published, so the code here does not run on its own. It is published to show how the pieces fit together and what building against the IBKR API involves.

## What a session looks like

The examples below use invented tickers and numbers. The layout is what the tools return.

### `ibkr_briefing`: the morning read in one call

> *"Morning briefing, compact."*

```markdown
# Briefing: U████████
_2026-10-27 13:32:05 UTC_

## Account Health
| Metric               | Value              |
|----------------------|--------------------|
| NLV                  | $412,600.00 USD    |
| Gross Position Value | $968,400.00 USD    |
| Leverage             | 2.35x              |
| Cushion              | 12.4% — OK         |
| Margin Utilization   | 96.4%              |
| Excess Liquidity     | $51,150.00 USD     |
| Init Margin          | $397,600.00 USD    |
| Maint Margin         | $361,450.00 USD    |
_Cushion 12.4% (7d ago 10.9%, Δ+1.5pt; 30d peak 15.2%)_
_Leverage 2.35x (7d ago 2.48x, Δ-0.13x; 30d peak 2.61x)_

## Daily P&L
**Daily**: +$6,184.00 USD (+1.50% of NLV)
**Unrealized**: +$167,210.00 USD

## Positions
| Symbol | Shares | Price   | Value       | Daily P&L        | Unreal P&L         | Weight  |
|--------|--------|---------|-------------|------------------|--------------------|---------|
| KRYX   | 2,400  | $148.25 | $355,800.00 | +$7,320 (+2.1%)  | +$118,400 (+49.9%) | +86.23% |
| VLTX   | 1,850  | $121.40 | $224,590.00 | $-1,813 (-0.8%)  | +$31,200 (+16.1%)  | +54.43% |
| OMRA   |   900  | $212.30 | $191,070.00 | +$2,637 (+1.4%)  | $-8,930 (-4.5%)    | +46.31% |
| FELN   | 3,200  | $38.65  | $123,680.00 | $-2,400 (-1.9%)  | +$22,480 (+22.2%)  | +29.98% |
| HDRX   | 1,100  | $66.60  | $73,260.00  | +$440 (+0.6%)    | +$4,060 (+5.9%)    | +17.76% |

## Open Orders
No open orders.
```

One call combines the account summary, the portfolio, the P&L subscription and the stored margin history, so the model sees the whole account without fanning out. Weights are a share of net liquidation value, so a leveraged book sums past 100%.

- `since_last=true` returns only what changed since the last full briefing.
- `format="terse"` returns the same numbers as flat `key: value` lines, which costs fewer tokens.
- When the market is closed, the briefing adds a section that marks positions at after-hours prices.

### `ibkr_break_levels`: where the margin call is

> *"At what prices does this book get a margin call?"*

```markdown
# Break Levels: U████████
**Current cushion**: +12.40% · **NLV**: $412,600.00 USD · **Gross**: $968,400.00 USD

## Per-position — price at which portfolio cushion breaks
_All-else-equal: only this name moves; the rest of the book is held flat._

| Symbol | CCY | Price   | Cushion 5% | Cushion 2.5% | Cushion 0% |
|--------|-----|---------|------------|--------------|------------|
| KRYX   | USD | $148.25 | $126.20    | $119.98      | $114.24    |
| VLTX   | USD | $121.40 | $92.80     | $84.72       | $77.28     |
| OMRA   | USD | $212.30 | $153.51    | $136.91      | $121.63    |
| FELN   | USD | $38.65  | $22.12     | $17.45       | $13.15     |
| HDRX   | USD | $66.60  | $18.49     | $4.92        | —          |

## Whole-book — uniform market move to each cushion level
| Cushion target | Book drop | NLV at break        |
|----------------|-----------|---------------------|
| 5.00%          | 5.46%     | $359,685.00 USD     |
| 2.50%          | 7.01%     | $344,742.00 USD     |
| 0.00%          | 8.43%     | $330,989.00 USD     |
```

This is the inverse of a stress test. A stress test asks what a given drop would do. Break levels ask how large a drop the account can take, one name at a time and for the whole book. A dash means that name alone cannot break the account.

### `ibkr_stress_test`: is a given drop survivable

> *"Preflight a 10% drawdown."*

```markdown
# Preflight Check: 10.0% Drawdown
Account: U████████

## Verdict: 🚨 DANGER

**Current Equity**: $412,600.00 USD
**Gross Position Value**: $968,400.00 USD
**Current Maint Margin**: $361,450.00 USD

**Stressed Equity** (after 10.0% drop): $315,760.00 USD
**Stressed Maint Margin**: $325,305.00 USD
**Buffer**: $-9,545.00 USD
**Buffer (vol-adj ×1.0318 maint)**: $-19,890.00 USD
**Init Margin Buffer**: $-42,080.00 USD
⚠️ **Margin call zone** — init margin breached by $42,080.00 USD

🚨 **Maintenance margin breached by $9,545.00 USD.**
Forced liquidation would begin at this drawdown level.

_Shock model: equity-beta by signed exposure — long equity 100.0% of gross loses, short equity 0.0% gains, zero-beta 0.0% flat; maint re-rated on the post-shock book at the current rate._
```

The other two scenarios are `drawdown`, which steps the drop in 1% increments and reports where each margin threshold is crossed, and `overnight`, which gives each position's worst-case gap at a chosen number of standard deviations.

### More from the same session

```text
"If I buy 500 OMRA, what happens to excess liquidity?"     -> ibkr_what_if (IB's whatIfOrder; no order is sent)
"Are these five names really five bets?"                   -> ibkr_correlation_matrix, 60 or 252 days
"What is the one-day 99% VaR, and which name drives it?"   -> ibkr_var_estimate, with a per-position breakdown
"How many days of cushion at a -1σ daily drift?"           -> ibkr_cushion_runway
"Rebalance to KRYX:30 VLTX:30 OMRA:40."                    -> ibkr_rebalance_planner
```

## Architecture

```
  PUBLIC (this repository)                 PRIVATE (stripped at publish)
┌──────────────────────────────┐         ┌──────────────────────────────┐
│  server_http.py  streamable  │         │  core/                       │
│                  HTTP, /mcp  │         │    connection · cache · fx   │
│  server.py       stdio       │  uses   │    calc · formatting         │
│  app.py          FastMCP     │ ◄────── │    persistence · technicals  │
│  dashboard.py    /api/* REST │         │                              │
│  config.py       env vars    │         │  tools/                      │
└──────────┬───────────────────┘         │    41 tools, registered      │
           │  ib_insync                  │    with @mcp.tool()          │
           ▼                             └──────────────────────────────┘
┌──────────────────────────────┐
│  IB Gateway / TWS            │
│  primary + optional second   │
└──────────────────────────────┘
```

- **One tool surface, two transports.** `server_http.py` is the transport in use. It owns a reconnect loop, periodic account snapshots and an offline cache. `server.py` (stdio) has none of these and is kept for local testing. Both share the FastMCP instance in `app.py`. `server_http.py` replaces that instance with a stateless HTTP one before the tool modules import, so the `@mcp.tool()` decorators bind to the right transport.
- **Dashboard on the same port.** `dashboard.py` adds `/api/*` REST routes to the same app (positions, summary, FX, technicals, history) for a separate frontend.
- **Built to survive gateway outages.** IB Gateway restarts daily, and it drops sessions when you log in from another machine. A background loop reconnects every 30 seconds. During an outage, tools answer from the cache with a staleness banner instead of failing.

## Design decisions

- **Read-only.** The IBKR API can place orders; this server exposes no order path. What-if simulation runs through IB's margin engine with nothing sent to market. OAuth scopes are `read` and `propose`, so authenticating a client never widens what it can do.
- **Stateless HTTP.** Clients that cached dead session IDs, or raced on overlapping SSE streams, broke stateful mode. Every request stands alone, and all real state lives in the gateway.
- **Short tool docstrings.** Each is one or two sentences, so the schema for all 41 tools stays small at session start.
- **Currency is never assumed.** Base currency comes from the account's `NetLiquidation` currency. When it is missing, monetary tools stop rather than assume USD.
- **One private repository, a generated public one.** The tool layer carries portfolio-specific calibration: thesis pillars, sector maps, risk factors. Development happens in one private repository. A build step copies only an allowlist of files into this one, after a PII scan.

## Tools

All 41 tools, grouped by what they answer. The implementations are private; this is the surface the model works with.

| Group | Tools |
|---|---|
| Account | `ibkr_get_account_summary` NLV, cash, buying power, margin, cushion, leverage · `ibkr_margin` distance to call and per-symbol efficiency · `ibkr_list_accounts` · `ibkr_get_account_pnl` daily realized and unrealized · `ibkr_consolidated_view` across two gateways, FX-converted |
| Briefing | `ibkr_briefing` |
| Portfolio | `ibkr_get_positions` cost basis, P&L, weights |
| Market data | `ibkr_quote` single, comparison or portfolio (alias `ibkr_get_quote`) · `ibkr_get_historical_bars` · `ibkr_get_contract_details` · `ibkr_get_option_chain` with Greeks · `ibkr_dividends` calendar and income estimate · `ibkr_search_contracts` |
| Live data | `ibkr_get_fx_rate` · `ibkr_get_intraday` 1-minute bars · `ibkr_compare_performance` · `ibkr_technicals` SMA, RSI, MACD, Bollinger |
| Risk | `ibkr_what_if` · `ibkr_margin_ladder` per-name liquidation prices · `ibkr_break_levels` · `ibkr_correlation_matrix` · `ibkr_portfolio_beta` · `ibkr_stress_test` · `ibkr_var_estimate` parametric VaR with component breakdown |
| Intelligence | `ibkr_sector_exposure` with HHI · `ibkr_currency` exposure by currency · `ibkr_position_detail` one-name deep dive · `ibkr_rebalance_planner` |
| Monitoring | `ibkr_drawdown_tracker` · `ibkr_margin_history` leverage and cushion over time, with margin-creep warnings · `ibkr_cushion_runway` · `ibkr_connection_status` · `ibkr_selftest` runs every read-only tool once |
| Orders (read) | `ibkr_trades` fills, journal, realized gains · `ibkr_get_orders` |
| News, fundamentals | `ibkr_news` headlines and full articles · `ibkr_fundamentals` ratios, estimates, targets |
| Scanner, borrow, volatility | `ibkr_scanner` IB market scanners with filters · `ibkr_shortable` borrow availability · `ibkr_volatility` realized against implied, IV rank |

## Install

Python 3.11+, and a running IB Gateway or TWS with the API enabled.

```sh
pip install -r requirements.txt
cp env.example .env        # every variable is documented there
python server_http.py      # streamable HTTP on MCP_HTTP_PORT, endpoint /mcp/
```

Connect a client, for example Claude Code:

```sh
claude mcp add --transport http ibkr http://127.0.0.1:8000/mcp/ \
  --header "Authorization: Bearer $MCP_HTTP_TOKEN"
```

## Configuration

Environment variables are loaded from `.env`; `env.example` lists them with defaults. The ones that matter first:

| Variable | Default | Notes |
|---|---|---|
| `IB_PORT` / `IB_PORT_2` | `4001` / `0` | 4001 is IB Gateway, 7497 is TWS. A non-zero `IB_PORT_2` adds a second gateway. |
| `IB_CLIENT_ID` | `10` | Must be unique per connection and not 0. |
| `IB_READONLY` | `true` | IB rejects `whatIfOrder` on read-only connections, so what-if, stress preflight and the cushion trim probe need `false`. No order-placement code exists either way. |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | `127.0.0.1` / `8000` | A non-loopback host refuses to start without `MCP_HTTP_TOKEN`. |
| `MCP_HTTP_TOKEN` | unset | Bearer token for `/mcp` and `/api/*`. Unset means no auth. |
| `IBKR_MCP_OAUTH_ENABLED` | off | OAuth 2.1 on `/mcp` (PKCE, dynamic client registration) for clients such as claude.ai. Needs `IBKR_MCP_OAUTH_ISSUER_URL`. `/api/*` still uses the bearer token. |

`thesis_config.json` holds the thesis pillars and `risk_factors.json` the geopolitical risk factors, both read by the dashboard's thesis and risk panels. Both ship here as generic templates.

## Stack

Python, `mcp` (FastMCP), `ib_insync`, `pydantic`, `uvicorn`, `httpx`, `yfinance` as the dashboard's price fallback, and SQLite for NLV and margin history.

## License

Proprietary. See [LICENSE](LICENSE). The code is published as a reference, not for production use.
