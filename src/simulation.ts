import type { Agent, BotState, MarketInfo, Trade, OrderSide } from './types'
import { AGENT_COLORS, AGENT_HOMES, STRATEGIES } from './types'

const SYMBOLS = [
  { symbol: 'ETHRUSDPERP', type: 'PERP' as const, basePrice: 2845.3 },
  { symbol: 'BTCRUSDPERP', type: 'PERP' as const, basePrice: 67250.0 },
  { symbol: 'ETHRUSD', type: 'SPOT' as const, basePrice: 2843.1 },
  { symbol: 'BTCRUSD', type: 'SPOT' as const, basePrice: 67210.5 },
]

let tradeCounter = 0
let cycleCounter = 0

function randomWalk(price: number, volatility: number): number {
  const change = (Math.random() - 0.5) * volatility
  return Math.max(0.01, price + change)
}

function createAgents(): Agent[] {
  return Object.keys(AGENT_HOMES).map((id) => {
    const meta = STRATEGIES[id]
    const [hx, hy] = AGENT_HOMES[id]
    return {
      id,
      name: id.toUpperCase(),
      role: meta.role,
      color: AGENT_COLORS[id],
      x: hx,
      y: hy,
      tx: hx,
      ty: hy,
      task: 'Initializing strategy module.',
      thought: 'booting up',
      state: 'IDLE',
      moving: false,
      speed: 0,
      pnl: 0,
      tradesCount: 0,
      winRate: 50 + Math.random() * 20,
      strategy: meta.strategy,
      description: meta.description,
      initialized: false,
    }
  })
}

function createMarkets(): MarketInfo[] {
  return SYMBOLS.map((s) => ({
    symbol: s.symbol,
    type: s.type,
    price: s.basePrice,
    prevPrice: s.basePrice,
    change24h: (Math.random() - 0.5) * 4,
    volume24h: Math.random() * 5000000,
  }))
}

export function createInitialState(): BotState {
  return {
    agents: createAgents(),
    trades: [],
    markets: createMarkets(),
    totalPnl: 0,
    totalTrades: 0,
    cycleId: 0,
    connected: false,
    lastUpdate: Date.now(),
  }
}

const TASKS: Record<string, string[]> = {
  arbitrage: [
    'Scanning spot/perp spread on ETH.',
    'Spread within threshold, holding position.',
    'Detected 0.08% premium on ETH perp. Executing arb.',
    'Rebalancing delta exposure after fill.',
    'Monitoring BTC basis for divergence.',
  ],
  maker: [
    'Posting bid at 2844.50, ask at 2846.20.',
    'Refreshing quotes on ETHRUSD order book.',
    'Spread tightened, adjusting skew.',
    'Inventory skew detected, shifting quotes.',
    'Cancelling stale orders, reposting.',
  ],
  momentum: [
    'Computing 20-period EMA crossover.',
    'Bullish crossover detected on BTC. Entering long.',
    'Momentum fading on ETH. Scaling out.',
    'Waiting for confirmation candle.',
    'Trailing stop moved to lock profit.',
  ],
  reversion: [
    'RSI at 72 on ETH. Watching for reversal.',
    'Oversold signal on BTC. Placing limit buy.',
    'Mean reversion target hit. Closing position.',
    'Z-score within range, no action.',
    'Reversion trade filled, profit booked.',
  ],
  risk: [
    'Portfolio VaR within limits: $12,450.',
    'Margin utilization at 34%. Nominal.',
    'Correlation spike detected. Reducing size.',
    'Stress testing against 10% ETH drop.',
    'Adjusting position limits for volatility regime.',
  ],
  liquidator: [
    'Scanning protocol for under-collateralized accounts.',
    'Account 0x4f2a at 92% utilization. Watching.',
    'Liquidated position on ETHRUSDPERP. Reward: 0.15 ETH.',
    'No liquidatable positions found.',
    'Gas price too high for profitable liquidation. Waiting.',
  ],
}

const THOUGHTS: Record<string, string[]> = {
  arbitrage: ['scanning spread', 'idle', 'executing arb', 'rebalancing', 'monitoring basis'],
  maker: ['quoting', 'managing inventory', 'idle', 'adjusting skew', 'reposting'],
  momentum: ['waiting for signal', 'entering position', 'trailing stop', 'scaling out', 'idle'],
  reversion: ['watching RSI', 'idle', 'entering reversion', 'booking profit', 'monitoring z-score'],
  risk: ['monitoring exposure', 'idle', 'reducing risk', 'stress testing', 'adjusting limits'],
  liquidator: ['scanning accounts', 'idle', 'executing liquidation', 'waiting for gas', 'monitoring'],
}

function pick<T>(arr: T[]): T {
  return arr[Math.floor(Math.random() * arr.length)]
}

function generateTrade(agent: Agent, markets: MarketInfo[]): Trade | null {
  const market = pick(markets)
  const side: OrderSide = Math.random() > 0.5 ? 'BUY' : 'SELL'
  const qty = +(Math.random() * 2 + 0.01).toFixed(4)
  const price = +market.price.toFixed(2)
  const pnl = +((Math.random() - 0.35) * 500).toFixed(2)
  const status = Math.random() > 0.1 ? 'FILLED' : Math.random() > 0.5 ? 'PARTIAL' : 'REJECTED'

  tradeCounter++
  return {
    id: `T${tradeCounter.toString().padStart(5, '0')}`,
    agentId: agent.id,
    agentName: agent.name,
    symbol: market.symbol,
    side,
    qty,
    price,
    timestamp: Date.now(),
    pnl,
    status,
  }
}

export function tick(state: BotState): BotState {
  cycleCounter++
  const newMarkets = state.markets.map((m) => {
    const vol = m.symbol.includes('BTC') ? 15 : 3
    const newPrice = randomWalk(m.price, vol)
    return {
      ...m,
      prevPrice: m.price,
      price: newPrice,
      change24h: m.change24h + (newPrice - m.price) / m.price * 100,
      volume24h: m.volume24h + Math.random() * 10000,
    }
  })

  const newAgents = state.agents.map((a) => {
    if (!a.initialized) {
      return { ...a, initialized: true, state: 'ACTIVE' as const, task: pick(TASKS[a.id]), thought: pick(THOUGHTS[a.id]) }
    }

    const r = Math.random()
    let newState = a.state
    let newTask = a.task
    let newThought = a.thought
    let newPnl = a.pnl
    let newTrades = a.tradesCount
    let newWinRate = a.winRate

    if (r < 0.15) {
      newState = 'EXECUTING'
      newTask = pick(TASKS[a.id])
      newThought = pick(THOUGHTS[a.id])
    } else if (r < 0.3) {
      newState = 'THINKING'
      newThought = pick(THOUGHTS[a.id])
    } else if (r < 0.85) {
      newState = 'ACTIVE'
      newTask = pick(TASKS[a.id])
      newThought = pick(THOUGHTS[a.id])
    } else {
      newState = 'IDLE'
      newThought = 'idle'
    }

    if (newState === 'EXECUTING' && Math.random() > 0.4) {
      const trade = generateTrade(a, newMarkets)
      if (trade && trade.status !== 'REJECTED') {
        newPnl = a.pnl + trade.pnl
        newTrades = a.tradesCount + 1
        newWinRate = Math.min(99, Math.max(30, a.winRate + (trade.pnl > 0 ? 0.5 : -0.5)))
      }
    }

    const dx = a.tx - a.x
    const dy = a.ty - a.y
    const dist = Math.sqrt(dx * dx + dy * dy)
    let nx = a.x
    let ny = a.y
    let moving = a.moving
    let speed = a.speed

    if (dist > 1) {
      speed = Math.min(3, dist * 0.05)
      nx = a.x + (dx / dist) * speed
      ny = a.y + (dy / dist) * speed
      moving = true
    } else {
      moving = false
      speed = 0
      if (Math.random() < 0.02) {
        const [hx, hy] = AGENT_HOMES[a.id]
        const offsetX = (Math.random() - 0.5) * 40
        const offsetY = (Math.random() - 0.5) * 30
        // Will be handled by caller via targets
      }
    }

    return {
      ...a,
      state: newState,
      task: newTask,
      thought: newThought,
      pnl: newPnl,
      tradesCount: newTrades,
      winRate: newWinRate,
      x: nx,
      y: ny,
      moving,
      speed,
    }
  })

  const newTrades: Trade[] = []
  for (const a of newAgents) {
    if (a.state === 'EXECUTING' && Math.random() > 0.6) {
      const trade = generateTrade(a, newMarkets)
      if (trade) {
        newTrades.push(trade)
      }
    }
  }

  const allTrades = [...newTrades, ...state.trades].slice(0, 100)
  const totalPnl = newAgents.reduce((sum, a) => sum + a.pnl, 0)
  const totalTrades = newAgents.reduce((sum, a) => sum + a.tradesCount, 0)

  return {
    ...state,
    agents: newAgents,
    trades: allTrades,
    markets: newMarkets,
    totalPnl,
    totalTrades,
    cycleId: cycleCounter,
    connected: true,
    lastUpdate: Date.now(),
  }
}

export function setAgentTarget(agent: Agent, x: number, y: number): Agent {
  return { ...agent, tx: x, ty: y }
}
