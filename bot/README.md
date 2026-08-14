# Reya 15m Fair Value Gap Bot

A production-ready automated trading bot for [Reya Network](https://docs.reya.xyz/developers)
perpetuals, trading a 15-minute Fair Value Gap (ICT / Smart Money Concepts)
strategy, with a full Matrix-themed terminal dashboard.

Built directly on the [`reya-python-sdk`](https://github.com/Reya-Labs/reya-python-sdk)
in this repository — REST for order entry and account state, WebSocket for
low-latency prices, depth, positions, orders and executions.

---

## ⚠️ Risk warning

**This software can lose money. Perpetual futures are leveraged instruments and
you can lose your entire deposit.**

- Fair Value Gaps are a discretionary concept mechanised here into fixed rules.
  There is no evidence that any parameter set in this repository is profitable.
- The defaults are conservative but they are *defaults*, not advice.
- Run in `paper` mode, then on testnet, for long enough to see the strategy lose
  as well as win, before considering real funds.
- Backtest results are an upper bound, not a forecast: fills are assumed, gap
  risk is ignored, and funding is not modelled.
- Nothing here is financial advice. You are responsible for every order this bot
  sends on your behalf.

Start with `--mode paper`. It needs no private key at all.

---

## Quick start

```bash
# 1. Install (adds rich, textual, PyYAML and aiohttp to the SDK)
poetry install --extras bot
poetry shell

# 2. Configure
cp bot/.env.example .env      # network + credentials
$EDITOR .env
$EDITOR config.yaml           # strategy, risk and UI

# 3. Check everything resolves (loads markets and candles; sends nothing)
python -m bot.main --check

# 4. Paper trade against live prices, with the dashboard
python -m bot.main --mode paper
```

Press **`s`** to arm the strategy. Nothing is traded until you do.

---

## Modes

| Mode | What it does | Needs a private key |
| --- | --- | --- |
| `paper` | Simulates fills against live prices. Same strategy, risk and engine code as live. | No |
| `live` | Signs and sends real orders through `/v2/createOrder`. | Yes |
| `backtest` | Replays historical 15m candles and prints a performance report. | No |

```bash
python -m bot.main --mode paper                     # simulated fills, live data
python -m bot.main --mode live                      # real orders
python -m bot.main --mode live --dry-run            # live data, orders logged not sent
python -m bot.main --mode live --no-ui              # headless, logs only
python -m bot.main --mode backtest --bars 2000      # backtest the last 2000 bars
python -m bot.main --symbol BTCRUSDPERP --symbol ETHRUSDPERP
```

Useful flags: `--config path.yaml`, `--log-level DEBUG`, `--autostart`,
`--candles file.json` / `--save-candles file.json` (backtest), `--check`.

---

## The strategy

### What a Fair Value Gap is

A Fair Value Gap is a three-bar imbalance: price moved so fast that bar `i` never
traded in the same range as bar `i-2`. The untraded band between them is the gap,
and price often returns to it before continuing.

For bars `i-2`, `i-1`, `i` where `i` is the most recent **closed** bar:

```
Bullish FVG    low[i] > high[i-2]      zone = [ high[i-2] .. low[i] ]
Bearish FVG    high[i] < low[i-2]      zone = [ high[i]   .. low[i-2] ]
```

Bar `i-1` is the *displacement* candle that did the work. It is not part of the
zone.

```
        ┌─┐  ← bar i          A bullish FVG. Price ran up so hard that
        │ │                   bar i's low never reached bar i-2's high.
    ┌───┼─┼───┐               The shaded band is the imbalance; the bot
    │▒▒▒│ │▒▒▒│  ← the gap    waits for price to fall back into it and
    └───┼─┼───┘               buys the retest.
   ┌─┐  └─┘
   │ │  ← bar i-1 (displacement)
   └─┘
 ┌─┐
 │ │ ← bar i-2
 └─┘
```

### How the bot trades it

1. **Detect on closed candles only.** An in-progress bar's high and low still
   move, so a gap found against it is not real yet. `CandleFeed` separates closed
   bars from the forming one and the strategy never sees the latter.

2. **Filter for significance.** A gap must clear *both* an ATR threshold
   (`min_fvg_atr_mult × ATR(14)`) and a percentage-of-price floor
   (`min_fvg_price_pct`). The second filter matters when the tape goes quiet and
   ATR collapses — otherwise dust gaps would qualify.

3. **Filter for quality** (each optional, each cuts signal count):
   - `require_three_candle_run` — bars `i-2`, `i-1`, `i` all close in the gap's
     direction, marking a genuine impulse rather than a single spike.
   - `require_displacement` — the middle bar's real body is at least
     `displacement_atr_mult` ATRs.
   - `require_liquidity_sweep` — the move first ran a recent swing low (bullish)
     or high (bearish) and closed back, i.e. a stop-run preceded it.

4. **Wait for the retest.** A bullish gap is entered from above as price falls
   into it. The entry limit sits at `entry_zone_ratio` through the zone: `0` is
   the far edge (best price, often never reached), `1` the near edge (fills most
   often, worst price), `0.5` the midpoint.

5. **Respect the higher-level bias.** With `bias_mode: ma`, longs need price
   above a *rising* EMA and shorts below a *falling* one — requiring both
   conditions keeps the bot out of chop around a flat average. `structure` uses
   higher highs/higher lows instead. `none` disables the filter.

6. **Track each gap's lifecycle.** `FRESH` → `TESTED` (price entered the zone) →
   `MITIGATED` (the imbalance filled) or `EXPIRED` (too old). Only `FRESH` and
   `TESTED` gaps can signal, and each gap signals at most once.

   State is recomputed from the candle history every bar rather than
   accumulated, so it is identical after a restart or a dropped WebSocket.

### Risk and exits

- **Stop loss** sits `sl_atr_buffer_mult` ATRs beyond the *far* edge of the gap,
  with a floor of `min_stop_atr_mult` ATRs so a razor-thin gap cannot produce an
  enormous position.
- **Take profit** is either a fixed multiple of the stop distance
  (`tp_mode: rr`) or the nearest unfilled opposing gap (`tp_mode: opposing_fvg`),
  which falls back to the fixed multiple when the gap would pay less than
  `min_risk_reward`.
- **Size** is risk-first: whatever quantity puts exactly `risk_per_trade_pct` of
  equity between entry and stop, then trimmed by the notional and leverage caps
  and rounded **down** onto the market's quantity step, so realised risk is never
  above budget.
- **Circuit breakers**: max open positions, max trades per day, a daily loss
  limit that halts trading until the next UTC day, and a cooldown after
  `consecutive_loss_limit` losses in a row.

---

## Execution details

These are consequences of how the Reya API works, and they shape the design:

**Entries are marketable IOC limits.** There is no "market" order type for perps
at the API level. The bot prices an IOC limit `entry_slippage_bps` through the
book, which behaves like a market order while capping slippage explicitly. An IOC
that does not trade is treated as a *miss*, not an error — the gap is re-armed
and can fire again on a later tick.

**Order entry is REST, not WebSocket.** The SDK exposes no WebSocket order entry;
`ReyaSocket` carries market and wallet data only. Orders go through
`create_limit_order` / `create_trigger_order`, which handle EIP-712 signing,
nonces and deadlines. The WebSocket is used for everything latency-sensitive that
*is* available: prices, L2 depth, the public tape, and the wallet's own
positions, order changes and executions. Order submission sits behind a `Broker`
interface, so a WebSocket transport can be dropped in without touching the engine
if the API gains one.

**Brackets are position-level.** `create_trigger_order` takes no quantity: an SL
or TP applies to the whole position. So a bracket is exactly one SL plus one TP,
and because the exchange does not link them, the bot cancels the surviving leg
itself when one fires.

**Reconciliation is authoritative.** Positions and orders are re-read over REST
every `reconcile_interval_s`, immediately after every WebSocket reconnect, and
after every fill. If a position turns out to be missing a bracket leg — a dropped
message, a rejected trigger — it is re-attached automatically. If a stop cannot
be placed at all, the position is closed rather than left unprotected.

---

## The Matrix dashboard

```
┌ MARKET ── PRICE ── 24H ── SESSION PnL ── OPEN PnL ── EQUITY ── FUNDING ── MODE ┐
├───────┬──────────────────────┬─────────────────────────────────────────────────┤
│ rain  │ 15m candles + FVG    │ DEPTH          │ POSITIONS                      │
│       │ zones, live price,   │ (L2 book with  │ ORDERS (SL / TP)               │
│       │ entry / SL / TP      │  size bars)    │ TRADE HISTORY                  │
│       ├──────────────────────┼────────────────┤ SESSION (metrics + risk state) │
│       │ FAIR VALUE GAPS      │ TAPE           │ STRATEGY LOG                   │
├───────┴──────────────────────┴────────────────┴────────────────────────────────┤
│ ◆ ETHRUSDPERP   ◇ BTCRUSDPERP                                                  │
│ ARMED │ ● LINK UP │ reya-live@reya-mainnet │ risk 0.5% │ up 01:22:41 │ › …      │
└────────────────────────────────────────────────────────────────────────────────┘
```

Pure black ground, phosphor green throughout, glowing panel borders, and
animated digital rain down both edges that **intensifies on every fill**. Panels
repaint every `refresh_ms` (400 ms by default) without flicker; the rain animates
independently at `rain_ms`.

The candle chart draws Fair Value Gap zones as shaded bands behind the price
action — green for bullish, red for bearish — with the live price as a dashed
line and the active entry, stop and target as horizontal markers.

### Keys

| Key | Action |
| --- | --- |
| `s` | Arm the strategy (allow new entries) |
| `x` | Disarm (open positions keep their brackets) |
| `space` | Pause / resume entries |
| `f` | Flatten the focused symbol |
| `F` | Flatten every position |
| `[` / `]` | Decrease / increase risk per trade |
| `n` / `p` / `tab` | Next / previous symbol |
| `r` | Reset risk limits after a halt or cooldown |
| `q` | Quit |

Set `ui.rain_enabled: false` on a slow terminal.

---

## Configuration

Three layers, later winning: dataclass defaults → `config.yaml` → environment.
Secrets are only ever read from the environment, so `config.yaml` stays safe to
commit. Unknown keys are rejected rather than silently ignored — a typo in a risk
limit fails at startup instead of at 3am.

### Environment (`.env`)

```bash
CHAIN_ID=1729                                # 89346162 for testnet
REYA_API_BASE_URL=https://api.reya.xyz/v2
REYA_WS_URL=wss://ws.reya.xyz/
ACCOUNT_ID=your_account_id
PRIVATE_KEY=your_private_key                 # live mode only
OWNER_WALLET_ADDRESS=your_wallet_address
```

The SDK's own `PERP_ACCOUNT_ID_1` / `PERP_PRIVATE_KEY_1` / `PERP_WALLET_ADDRESS_1`
are accepted as fallbacks, so an existing SDK `.env` works unchanged.

Find your account id at
`https://api.reya.xyz/v2/wallet/<your_wallet_address>/accounts`.

### `config.yaml`

Every option is documented inline in [`config.yaml`](../config.yaml). The
settings worth understanding before going live:

| Setting | Default | Why it matters |
| --- | --- | --- |
| `risk.risk_per_trade_pct` | `0.5` | Percentage of equity risked per trade. |
| `risk.max_daily_loss_pct` | `3.0` | Halts trading for the UTC day. |
| `risk.max_open_positions` | `2` | Across all symbols. |
| `risk.risk_reward` | `2.0` | Reward multiple of the stop distance. |
| `strategy.min_fvg_atr_mult` | `0.25` | Higher = fewer, larger gaps. |
| `strategy.entry_zone_ratio` | `0.5` | Fill probability vs. entry price. |
| `strategy.bias_mode` | `ma` | `none` doubles the signal count. |
| `execution.entry_slippage_bps` | `15` | Slippage cap on entries. |
| `execution.close_positions_on_shutdown` | `false` | On = flatten on exit. |

---

## Architecture

```
bot/
├── main.py               CLI: live / paper / backtest / check
├── config.py             layered configuration and validation
├── engine.py             event loop, entry path, position lifecycle
├── state.py              shared state the UI reads
├── metrics.py            win rate, profit factor, drawdown, expectancy
├── backtest.py           event-driven backtester
├── notifier.py           optional Telegram / Discord alerts
├── strategy/
│   ├── indicators.py     ATR, EMA, swings, structure, liquidity sweeps
│   ├── fvg.py            gap detection, lifecycle, signal generation
│   └── risk.py           sizing, brackets, circuit breakers
├── exchange/
│   ├── base.py           Broker interface and shared types
│   ├── reya_client.py    live broker + unauthenticated market data client
│   ├── paper.py          fill simulator
│   └── data_feed.py      candle pagination + WebSocket bridge
├── ui/
│   ├── matrix_app.py     the Textual app
│   ├── panels.py         Rich renderables for each panel
│   ├── chart.py          ASCII candle chart with FVG zones
│   ├── rain.py           digital rain widget
│   ├── theme.py          the palette
│   └── matrix.tcss       layout and styling
└── utils/                decimal helpers, logging
```

**Decimals everywhere.** Prices and quantities are `Decimal` from the API through
to the signed order. Binary floats silently break tick and step constraints.

**One broker interface, two implementations.** Paper mode runs the same strategy,
risk and engine code as live; only the fills are invented. That is what makes
paper results meaningful as a rehearsal.

**Supervised tasks.** Every background loop restarts with backoff if it raises. A
crashed task would otherwise leave positions unmanaged, which is the worst
failure mode a trading bot has.

**Async throughout.** The SDK's WebSocket client is thread-based, so it is
bridged into asyncio with a threadsafe queue and its own reconnect supervisor.
The UI only reads state and never blocks the trading loop.

---

## Safety behaviour

| Situation | What the bot does |
| --- | --- |
| Stop-loss order rejected | Closes the position immediately rather than leave it unprotected |
| Bracket leg missing at reconcile | Re-attaches it |
| WebSocket silent > 90s | Suspends new entries, resumes automatically on recovery |
| WebSocket reconnects | Forces a REST reconciliation before trusting its view |
| Background task crashes | Logs, alerts, and restarts it with backoff |
| Daily loss limit hit | Halts new entries until the next UTC day |
| 3 losses in a row | Cooldown (default 2 hours) |
| Shutdown | Cancels resting orders; flattens only if configured to |

---

## Testing

```bash
poetry run pytest tests/test_bot -v
```

The suite is offline and deterministic: no network, no credentials, no event
loop for anything but the UI tests. It covers FVG detection and every filter,
zone geometry, gap lifecycle, signal generation, stop/target placement, position
sizing and every circuit breaker, the backtester, configuration layering and
validation, and the dashboard (driven headlessly through Textual's pilot).

```bash
make lint          # black, isort, flake8, pylint, bandit, mypy via pre-commit
```

---

## Extending it

- **Another market**: add the symbol to `config.yaml`. Anything Reya lists as a
  perp works; `--check` verifies it and prints the tick and step sizes.
- **Another timeframe**: set `strategy.timeframe` (`1m`…`1d`). The gap logic is
  timeframe-agnostic; only the poll cadence assumes 15m-ish bars.
- **Another strategy**: implement the `update_candles` / `evaluate` pair of
  `FVGStrategy` and hand the engine your class. Risk, execution, reconciliation
  and UI are all strategy-agnostic.
- **WebSocket order entry**: implement `Broker` and pass it to the engine.
