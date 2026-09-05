/*
# Paper Trading Account Tables

## Purpose
Track each strategy agent's paper trading balance, starting at $500 each.
The engine simulates fills at live Reya prices and updates balances.

## New Tables

### paper_accounts
- id (text, primary key) — strategy ID (e.g. "momentum")
- strategy_name (text) — display name
- starting_balance (numeric) — initial balance ($500)
- cash_balance (numeric) — current cash available
- position_value (numeric) — current market value of open position
- realized_pnl (numeric) — cumulative realized PnL
- unrealized_pnl (numeric) — current unrealized PnL
- total_pnl (numeric) — realized + unrealized
- total_trades (integer) — number of filled trades
- winning_trades (integer) — number of profitable closes
- updated_at (timestamptz)

### paper_positions
- id (uuid, primary key)
- strategy_id (text) — which agent
- symbol (text) — market symbol
- side (text) — LONG or SHORT
- size (numeric) — position size in base asset
- entry_price (numeric) — average entry price
- current_price (numeric) — latest mark price
- unrealized_pnl (numeric)
- status (text) — OPEN or CLOSED
- opened_at (bigint, ms)
- closed_at (bigint, ms, nullable)
- realized_pnl (numeric, nullable) — PnL captured on close
- created_at (timestamptz)

### paper_trades
- id (uuid, primary key)
- strategy_id (text)
- strategy_name (text)
- symbol (text)
- side (text) — BUY or SELL
- qty (numeric)
- price (numeric)
- pnl (numeric) — realized PnL if closing, 0 if opening
- status (text) — FILLED (always filled in paper mode)
- action (text) — OPEN or CLOSE
- timestamp (bigint, ms)
- reason (text) — strategy reasoning
- created_at (timestamptz)

## Security
- All tables have RLS enabled with public read/write (no-auth app).
*/

CREATE TABLE IF NOT EXISTS paper_accounts (
  id text PRIMARY KEY,
  strategy_name text NOT NULL,
  starting_balance numeric NOT NULL DEFAULT 500,
  cash_balance numeric NOT NULL DEFAULT 500,
  position_value numeric NOT NULL DEFAULT 0,
  realized_pnl numeric NOT NULL DEFAULT 0,
  unrealized_pnl numeric NOT NULL DEFAULT 0,
  total_pnl numeric NOT NULL DEFAULT 0,
  total_trades integer NOT NULL DEFAULT 0,
  winning_trades integer NOT NULL DEFAULT 0,
  updated_at timestamptz DEFAULT now()
);

ALTER TABLE paper_accounts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "pa_select" ON paper_accounts;
CREATE POLICY "pa_select" ON paper_accounts FOR SELECT
  TO anon, authenticated USING (true);
DROP POLICY IF EXISTS "pa_insert" ON paper_accounts;
CREATE POLICY "pa_insert" ON paper_accounts FOR INSERT
  TO anon, authenticated WITH CHECK (true);
DROP POLICY IF EXISTS "pa_update" ON paper_accounts;
CREATE POLICY "pa_update" ON paper_accounts FOR UPDATE
  TO anon, authenticated USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "pa_delete" ON paper_accounts;
CREATE POLICY "pa_delete" ON paper_accounts FOR DELETE
  TO anon, authenticated USING (true);

CREATE TABLE IF NOT EXISTS paper_positions (
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
  realized_pnl numeric,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE paper_positions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "pp_select" ON paper_positions;
CREATE POLICY "pp_select" ON paper_positions FOR SELECT
  TO anon, authenticated USING (true);
DROP POLICY IF EXISTS "pp_insert" ON paper_positions;
CREATE POLICY "pp_insert" ON paper_positions FOR INSERT
  TO anon, authenticated WITH CHECK (true);
DROP POLICY IF EXISTS "pp_update" ON paper_positions;
CREATE POLICY "pp_update" ON paper_positions FOR UPDATE
  TO anon, authenticated USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "pp_delete" ON paper_positions;
CREATE POLICY "pp_delete" ON paper_positions FOR DELETE
  TO anon, authenticated USING (true);

CREATE TABLE IF NOT EXISTS paper_trades (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id text NOT NULL,
  strategy_name text NOT NULL,
  symbol text NOT NULL,
  side text NOT NULL,
  qty numeric NOT NULL DEFAULT 0,
  price numeric NOT NULL DEFAULT 0,
  pnl numeric NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'FILLED',
  action text NOT NULL DEFAULT 'OPEN',
  reason text NOT NULL DEFAULT '',
  timestamp bigint NOT NULL DEFAULT 0,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE paper_trades ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "pt_select" ON paper_trades;
CREATE POLICY "pt_select" ON paper_trades FOR SELECT
  TO anon, authenticated USING (true);
DROP POLICY IF EXISTS "pt_insert" ON paper_trades;
CREATE POLICY "pt_insert" ON paper_trades FOR INSERT
  TO anon, authenticated WITH CHECK (true);
DROP POLICY IF EXISTS "pt_delete" ON paper_trades;
CREATE POLICY "pt_delete" ON paper_trades FOR DELETE
  TO anon, authenticated USING (true);

CREATE INDEX IF NOT EXISTS idx_paper_trades_timestamp ON paper_trades (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_paper_positions_strategy ON paper_positions (strategy_id) WHERE status = 'OPEN';
