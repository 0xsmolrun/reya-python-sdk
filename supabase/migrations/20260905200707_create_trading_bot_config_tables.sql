/*
# Create Trading Bot Configuration and Trade History Tables

## Purpose
Stores bot configuration (wallet, account, strategy settings) and
records every trade executed by the bot against the Reya network.

## New Tables

### bot_config
- `id` (integer, primary key, default 1) — singleton config row
- `chain_id` (integer) — Reya chain ID (1729 mainnet, 89346162 testnet)
- `account_id` (integer) — Reya account ID for trading
- `wallet_address` (text) — owner wallet address
- `api_url` (text) — Reya API base URL
- `ws_url` (text) — Reya WebSocket URL
- `is_active` (boolean) — whether the bot engine is running
- `created_at` (timestamptz)

### bot_strategies
- `id` (text, primary key) — strategy identifier (e.g. "momentum")
- `name` (text) — display name
- `role` (text) — role description
- `description` (text) — strategy description
- `color` (text) — UI color
- `is_enabled` (boolean) — whether this strategy is active
- `symbol` (text) — market symbol to trade
- `params` (jsonb) — strategy parameters (thresholds, sizes, etc.)
- `updated_at` (timestamptz)

### bot_trade_history
- `id` (uuid, primary key)
- `strategy_id` (text) — which strategy triggered this trade
- `strategy_name` (text) — display name
- `symbol` (text) — market symbol
- `side` (text) — BUY or SELL
- `qty` (numeric) — order quantity
- `price` (numeric) — execution price
- `pnl` (numeric) — realized PnL
- `status` (text) — FILLED, PARTIAL, REJECTED, PENDING
- `order_id` (text) — Reya order ID
- `tx_hash` (text) — transaction hash if available
- `error` (text) — error message if failed
- `timestamp` (bigint) — execution timestamp (ms)
- `created_at` (timestamptz)

### bot_positions
- `id` (uuid, primary key)
- `strategy_id` (text) — strategy that opened this position
- `symbol` (text) — market symbol
- `side` (text) — LONG or SHORT
- `size` (numeric) — position size
- `entry_price` (numeric) — average entry price
- `current_price` (numeric) — latest mark price
- `unrealized_pnl` (numeric) — unrealized PnL
- `status` (text) — OPEN, CLOSED
- `opened_at` (bigint) — timestamp opened (ms)
- `closed_at` (bigint) — timestamp closed (ms, nullable)
- `created_at` (timestamptz)

## Security
- All tables have RLS enabled.
- No-auth public app — all policies use `TO anon, authenticated` with
  `USING (true)` / `WITH CHECK (true)` because the data is intentionally
  public/shared.
*/

CREATE TABLE IF NOT EXISTS bot_config (
  id integer PRIMARY KEY DEFAULT 1,
  chain_id integer NOT NULL DEFAULT 1729,
  account_id integer,
  wallet_address text,
  api_url text NOT NULL DEFAULT 'https://api.reya.xyz/v2',
  ws_url text NOT NULL DEFAULT 'wss://ws.reya.xyz/',
  is_active boolean NOT NULL DEFAULT false,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE bot_config ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_bot_config" ON bot_config;
CREATE POLICY "anon_select_bot_config" ON bot_config FOR SELECT
  TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "anon_insert_bot_config" ON bot_config;
CREATE POLICY "anon_insert_bot_config" ON bot_config FOR INSERT
  TO anon, authenticated WITH CHECK (true);

DROP POLICY IF EXISTS "anon_update_bot_config" ON bot_config;
CREATE POLICY "anon_update_bot_config" ON bot_config FOR UPDATE
  TO anon, authenticated USING (true) WITH CHECK (true);

CREATE TABLE IF NOT EXISTS bot_strategies (
  id text PRIMARY KEY,
  name text NOT NULL,
  role text NOT NULL,
  description text NOT NULL DEFAULT '',
  color text NOT NULL DEFAULT '#888888',
  is_enabled boolean NOT NULL DEFAULT true,
  symbol text NOT NULL DEFAULT 'ETHRUSDPERP',
  params jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz DEFAULT now()
);

ALTER TABLE bot_strategies ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_bot_strategies" ON bot_strategies;
CREATE POLICY "anon_select_bot_strategies" ON bot_strategies FOR SELECT
  TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "anon_insert_bot_strategies" ON bot_strategies;
CREATE POLICY "anon_insert_bot_strategies" ON bot_strategies FOR INSERT
  TO anon, authenticated WITH CHECK (true);

DROP POLICY IF EXISTS "anon_update_bot_strategies" ON bot_strategies;
CREATE POLICY "anon_update_bot_strategies" ON bot_strategies FOR UPDATE
  TO anon, authenticated USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "anon_delete_bot_strategies" ON bot_strategies;
CREATE POLICY "anon_delete_bot_strategies" ON bot_strategies FOR DELETE
  TO anon, authenticated USING (true);

CREATE TABLE IF NOT EXISTS bot_trade_history (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id text NOT NULL,
  strategy_name text NOT NULL,
  symbol text NOT NULL,
  side text NOT NULL,
  qty numeric NOT NULL DEFAULT 0,
  price numeric NOT NULL DEFAULT 0,
  pnl numeric NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'PENDING',
  order_id text,
  tx_hash text,
  error text,
  timestamp bigint NOT NULL DEFAULT 0,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE bot_trade_history ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_bot_trade_history" ON bot_trade_history;
CREATE POLICY "anon_select_bot_trade_history" ON bot_trade_history FOR SELECT
  TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "anon_insert_bot_trade_history" ON bot_trade_history;
CREATE POLICY "anon_insert_bot_trade_history" ON bot_trade_history FOR INSERT
  TO anon, authenticated WITH CHECK (true);

DROP POLICY IF EXISTS "anon_delete_bot_trade_history" ON bot_trade_history;
CREATE POLICY "anon_delete_bot_trade_history" ON bot_trade_history FOR DELETE
  TO anon, authenticated USING (true);

CREATE INDEX IF NOT EXISTS idx_bot_trade_history_timestamp ON bot_trade_history (timestamp DESC);

CREATE TABLE IF NOT EXISTS bot_positions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id text NOT NULL,
  symbol text NOT NULL,
  side text NOT NULL,
  size numeric NOT NULL DEFAULT 0,
  entry_price numeric NOT NULL DEFAULT 0,
  current_price numeric NOT NULL DEFAULT 0,
  unrealized_pnl numeric NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'OPEN',
  opened_at bigint NOT NULL DEFAULT 0,
  closed_at bigint,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE bot_positions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_bot_positions" ON bot_positions;
CREATE POLICY "anon_select_bot_positions" ON bot_positions FOR SELECT
  TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "anon_insert_bot_positions" ON bot_positions;
CREATE POLICY "anon_insert_bot_positions" ON bot_positions FOR INSERT
  TO anon, authenticated WITH CHECK (true);

DROP POLICY IF EXISTS "anon_update_bot_positions" ON bot_positions;
CREATE POLICY "anon_update_bot_positions" ON bot_positions FOR UPDATE
  TO anon, authenticated USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "anon_delete_bot_positions" ON bot_positions;
CREATE POLICY "anon_delete_bot_positions" ON bot_positions FOR DELETE
  TO anon, authenticated USING (true);
