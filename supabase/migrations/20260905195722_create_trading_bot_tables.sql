/*
# Create Trading Bot Operations Tables

## Purpose
Stores the live state of the autonomous trading bot operations floor —
agent states, trade executions, and market snapshots — so visitors can
inspect the floor and see real-time bot activity on the Reya network.

## New Tables

### bot_agents
- `id` (text, primary key) — agent identifier (e.g. "arbitrage")
- `name` (text) — display name
- `role` (text) — role description
- `strategy` (text) — strategy name
- `description` (text) — strategy description
- `color` (text) — hex color for UI rendering
- `state` (text) — current state (ACTIVE, IDLE, THINKING, EXECUTING, ERROR)
- `task` (text) — current task description
- `thought` (text) — current internal thought
- `pnl` (numeric) — agent PnL in USD
- `trades_count` (integer) — total trades executed
- `win_rate` (numeric) — win rate percentage
- `x` (numeric) — canvas X position
- `y` (numeric) — canvas Y position
- `updated_at` (timestamptz) — last state update

### bot_trades
- `id` (text, primary key) — trade identifier
- `agent_id` (text) — which agent executed
- `agent_name` (text) — agent display name
- `symbol` (text) — market symbol
- `side` (text) — BUY or SELL
- `qty` (numeric) — order quantity
- `price` (numeric) — execution price
- `pnl` (numeric) — trade PnL
- `status` (text) — FILLED, PARTIAL, REJECTED
- `timestamp` (bigint) — execution timestamp (ms)

### bot_markets
- `id` (text, primary key) — market symbol
- `type` (text) — PERP or SPOT
- `price` (numeric) — current price
- `prev_price` (numeric) — previous price
- `change_24h` (numeric) — 24h change percentage
- `volume_24h` (numeric) — 24h volume
- `updated_at` (timestamptz) — last update

### bot_state
- `id` (integer, primary key, default 1) — singleton row
- `total_pnl` (numeric) — aggregate PnL
- `total_trades` (integer) — aggregate trade count
- `cycle_id` (integer) — current simulation cycle
- `connected` (boolean) — connection status
- `last_update` (bigint) — last update timestamp (ms)

## Security
- All tables have RLS enabled.
- This is a no-auth public app — all policies use `TO anon, authenticated`
  with `USING (true)` / `WITH CHECK (true)` because the data is intentionally
  public/shared (visitors can inspect the floor without signing in).
*/

CREATE TABLE IF NOT EXISTS bot_agents (
  id text PRIMARY KEY,
  name text NOT NULL,
  role text NOT NULL,
  strategy text NOT NULL,
  description text NOT NULL DEFAULT '',
  color text NOT NULL DEFAULT '#888888',
  state text NOT NULL DEFAULT 'IDLE',
  task text NOT NULL DEFAULT 'Initializing.',
  thought text NOT NULL DEFAULT 'standing by',
  pnl numeric NOT NULL DEFAULT 0,
  trades_count integer NOT NULL DEFAULT 0,
  win_rate numeric NOT NULL DEFAULT 50,
  x numeric NOT NULL DEFAULT 0,
  y numeric NOT NULL DEFAULT 0,
  updated_at timestamptz DEFAULT now()
);

ALTER TABLE bot_agents ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_bot_agents" ON bot_agents;
CREATE POLICY "anon_select_bot_agents" ON bot_agents FOR SELECT
  TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "anon_insert_bot_agents" ON bot_agents;
CREATE POLICY "anon_insert_bot_agents" ON bot_agents FOR INSERT
  TO anon, authenticated WITH CHECK (true);

DROP POLICY IF EXISTS "anon_update_bot_agents" ON bot_agents;
CREATE POLICY "anon_update_bot_agents" ON bot_agents FOR UPDATE
  TO anon, authenticated USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "anon_delete_bot_agents" ON bot_agents;
CREATE POLICY "anon_delete_bot_agents" ON bot_agents FOR DELETE
  TO anon, authenticated USING (true);

CREATE TABLE IF NOT EXISTS bot_trades (
  id text PRIMARY KEY,
  agent_id text NOT NULL,
  agent_name text NOT NULL,
  symbol text NOT NULL,
  side text NOT NULL,
  qty numeric NOT NULL DEFAULT 0,
  price numeric NOT NULL DEFAULT 0,
  pnl numeric NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'FILLED',
  timestamp bigint NOT NULL DEFAULT 0
);

ALTER TABLE bot_trades ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_bot_trades" ON bot_trades;
CREATE POLICY "anon_select_bot_trades" ON bot_trades FOR SELECT
  TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "anon_insert_bot_trades" ON bot_trades;
CREATE POLICY "anon_insert_bot_trades" ON bot_trades FOR INSERT
  TO anon, authenticated WITH CHECK (true);

DROP POLICY IF EXISTS "anon_delete_bot_trades" ON bot_trades;
CREATE POLICY "anon_delete_bot_trades" ON bot_trades FOR DELETE
  TO anon, authenticated USING (true);

CREATE TABLE IF NOT EXISTS bot_markets (
  id text PRIMARY KEY,
  type text NOT NULL DEFAULT 'PERP',
  price numeric NOT NULL DEFAULT 0,
  prev_price numeric NOT NULL DEFAULT 0,
  change_24h numeric NOT NULL DEFAULT 0,
  volume_24h numeric NOT NULL DEFAULT 0,
  updated_at timestamptz DEFAULT now()
);

ALTER TABLE bot_markets ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_bot_markets" ON bot_markets;
CREATE POLICY "anon_select_bot_markets" ON bot_markets FOR SELECT
  TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "anon_insert_bot_markets" ON bot_markets;
CREATE POLICY "anon_insert_bot_markets" ON bot_markets FOR INSERT
  TO anon, authenticated WITH CHECK (true);

DROP POLICY IF EXISTS "anon_update_bot_markets" ON bot_markets;
CREATE POLICY "anon_update_bot_markets" ON bot_markets FOR UPDATE
  TO anon, authenticated USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "anon_delete_bot_markets" ON bot_markets;
CREATE POLICY "anon_delete_bot_markets" ON bot_markets FOR DELETE
  TO anon, authenticated USING (true);

CREATE TABLE IF NOT EXISTS bot_state (
  id integer PRIMARY KEY DEFAULT 1,
  total_pnl numeric NOT NULL DEFAULT 0,
  total_trades integer NOT NULL DEFAULT 0,
  cycle_id integer NOT NULL DEFAULT 0,
  connected boolean NOT NULL DEFAULT false,
  last_update bigint NOT NULL DEFAULT 0
);

ALTER TABLE bot_state ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_bot_state" ON bot_state;
CREATE POLICY "anon_select_bot_state" ON bot_state FOR SELECT
  TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "anon_insert_bot_state" ON bot_state;
CREATE POLICY "anon_insert_bot_state" ON bot_state FOR INSERT
  TO anon, authenticated WITH CHECK (true);

DROP POLICY IF EXISTS "anon_update_bot_state" ON bot_state;
CREATE POLICY "anon_update_bot_state" ON bot_state FOR UPDATE
  TO anon, authenticated USING (true) WITH CHECK (true);

-- Index for trade ordering
CREATE INDEX IF NOT EXISTS idx_bot_trades_timestamp ON bot_trades (timestamp DESC);
