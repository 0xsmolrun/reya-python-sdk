export type AgentState = 'ACTIVE' | 'IDLE' | 'THINKING' | 'EXECUTING' | 'ERROR'
export type OrderSide = 'BUY' | 'SELL'
export type MarketType = 'PERP' | 'SPOT'

export interface MarketInfo {
  symbol: string
  type: MarketType
  price: number
  prevPrice: number
  change24h: number
  volume24h: number
}

export interface Trade {
  id: string
  agentId: string
  agentName: string
  symbol: string
  side: OrderSide
  qty: number
  price: number
  timestamp: number
  pnl: number
  status: 'FILLED' | 'PARTIAL' | 'REJECTED' | 'PENDING'
  action: string
  reason: string
}

export interface Agent {
  id: string
  name: string
  role: string
  color: string
  x: number
  y: number
  tx: number
  ty: number
  task: string
  thought: string
  state: AgentState
  moving: boolean
  speed: number
  pnl: number
  tradesCount: number
  winRate: number
  strategy: string
  description: string
  is_enabled: boolean
  symbol: string
  params: Record<string, unknown>
  initialized: boolean
  paperBalance: number
  startingBalance: number
  realizedPnl: number
  unrealizedPnl: number
  winningTrades: number
}

export interface BotState {
  agents: Agent[]
  trades: Trade[]
  markets: MarketInfo[]
  totalPnl: number
  totalTrades: number
  cycleId: number
  connected: boolean
  lastUpdate: number
}

export interface BotConfig {
  id: number
  chain_id: number
  account_id: number | null
  wallet_address: string | null
  api_url: string
  ws_url: string
  is_active: boolean
}

export interface PaperAccount {
  id: string
  strategy_name: string
  starting_balance: number
  cash_balance: number
  position_value: number
  realized_pnl: number
  unrealized_pnl: number
  total_pnl: number
  total_trades: number
  winning_trades: number
}

export interface PaperPosition {
  id: string
  strategy_id: string
  symbol: string
  side: string
  size: number
  entry_price: number
  current_price: number
  unrealized_pnl: number
  status: string
  opened_at: number
}

export interface WalletPosition {
  symbol: string
  notionalSize: number
  side: string
  entryPrice: number
  unrealizedPnl: number
  margin: number
}

export interface WalletBalance {
  accountId: number
  assetSymbol: string
  balance: number
}

export interface LiveMarketData {
  config: BotConfig
  markets: Array<{ symbol: string; marketId: number; type: string }>
  prices: Array<{ symbol: string; price: number; change24h: number }>
  summary: Array<{ symbol: string; markPrice: number; indexPrice: number; fundingRate: number; volume24h: number }>
  positions: WalletPosition[]
  openOrders: unknown[]
  balances: WalletBalance[]
  accounts: unknown[]
  marketError?: string
  priceError?: string
  walletError?: string
}

export const AGENT_COLORS: Record<string, string> = {
  arbitrage: '#52d5c9',
  maker: '#a6df55',
  momentum: '#f3ad3d',
  reversion: '#b798ff',
  risk: '#ff6557',
  liquidator: '#53a9e9',
  cyan: '#52d5c9',
  acid: '#b9e65a',
  amber: '#f1bd57',
  red: '#ff6557',
}

export const AGENT_HOMES: Record<string, [number, number]> = {
  arbitrage: [160, 245],
  maker: [500, 235],
  momentum: [835, 245],
  reversion: [165, 468],
  risk: [500, 475],
  liquidator: [835, 468],
}

export const STRATEGY_META: Record<string, { role: string; strategy: string; description: string }> = {
  arbitrage: {
    role: 'Cross-market arbitrageur',
    strategy: 'Arbitrage',
    description: 'Monitors price discrepancies between spot and perp markets. Executes when spread exceeds threshold.',
  },
  maker: {
    role: 'Liquidity provider',
    strategy: 'Market Making',
    description: 'Posts bid/ask limit orders on both sides of the order book. Profits from spread capture.',
  },
  momentum: {
    role: 'Trend follower',
    strategy: 'Momentum',
    description: 'Detects directional momentum via moving average crossovers. Enters on breakout, exits on reversal.',
  },
  reversion: {
    role: 'Mean reversion trader',
    strategy: 'Mean Reversion',
    description: 'Identifies overbought/oversold conditions using RSI. Trades against short-term extremes.',
  },
  risk: {
    role: 'Risk manager',
    strategy: 'Risk Management',
    description: 'Monitors portfolio exposure, margin levels, and correlation. Adjusts positions to keep risk in check.',
  },
  liquidator: {
    role: 'Liquidation sentinel',
    strategy: 'Liquidation',
    description: 'Watches for under-collateralized positions across the protocol. Executes liquidations for reward.',
  },
}
