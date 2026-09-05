import { useEffect, useState, useCallback, useRef } from 'react'
import { FloorCanvas } from './FloorCanvas'
import { AgentDetail } from './AgentDetail'
import { TradeFeed } from './TradeFeed'
import { SettingsPanel } from './SettingsPanel'
import { supabase } from './supabase'
import { AGENT_COLORS, AGENT_HOMES, STRATEGY_META } from './types'
import type { Agent, BotState, BotConfig, LiveMarketData, Trade, PaperAccount, PaperPosition } from './types'
import './app.css'

const FUNCTION_BASE = import.meta.env.VITE_SUPABASE_URL + '/functions/v1'
const ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY as string

function createAgentsFromDb(
  dbStrategies: Array<Record<string, unknown>>,
  accounts: PaperAccount[],
): Agent[] {
  return Object.keys(AGENT_HOMES).map((id) => {
    const dbStrategy = dbStrategies.find((s) => s.id === id)
    const meta = STRATEGY_META[id]
    const [hx, hy] = AGENT_HOMES[id]
    const acc = accounts.find((a) => a.id === id)
    return {
      id,
      name: (dbStrategy?.name as string) || id.toUpperCase(),
      role: (dbStrategy?.role as string) || meta.role,
      color: AGENT_COLORS[id],
      x: hx,
      y: hy,
      tx: hx,
      ty: hy,
      task: 'Initializing...',
      thought: 'standing by',
      state: 'IDLE',
      moving: false,
      speed: 0,
      pnl: acc?.total_pnl || 0,
      tradesCount: acc?.total_trades || 0,
      winRate: acc && acc.total_trades > 0 ? (acc.winning_trades / acc.total_trades) * 100 : 50,
      strategy: meta.strategy,
      description: meta.description,
      is_enabled: dbStrategy?.is_enabled ?? true,
      symbol: (dbStrategy?.symbol as string) || 'ETHRUSDPERP',
      params: (dbStrategy?.params as Record<string, unknown>) || {},
      initialized: false,
      paperBalance: acc?.cash_balance || 500,
      startingBalance: acc?.starting_balance || 500,
      realizedPnl: acc?.realized_pnl || 0,
      unrealizedPnl: acc?.unrealized_pnl || 0,
      winningTrades: acc?.winning_trades || 0,
    }
  })
}

export function App() {
  const [state, setState] = useState<BotState>({
    agents: [],
    trades: [],
    markets: [],
    totalPnl: 0,
    totalTrades: 0,
    cycleId: 0,
    connected: false,
    lastUpdate: Date.now(),
  })
  const [config, setConfig] = useState<BotConfig | null>(null)
  const [liveData, setLiveData] = useState<LiveMarketData | null>(null)
  const [paperPositions, setPaperPositions] = useState<PaperPosition[]>([])
  const [selectedAgent, setSelectedAgent] = useState<string | null>('arbitrage')
  const [showSettings, setShowSettings] = useState(false)
  const [engineRunning, setEngineRunning] = useState(false)
  const [engineResult, setEngineResult] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [autoRun, setAutoRun] = useState(true)
  const fetchCounter = useRef(0)

  // Fetch paper accounts and update agent PnL
  const refreshPaperData = useCallback(async () => {
    const [accountsRes, tradesRes, positionsRes] = await Promise.all([
      supabase.from('paper_accounts').select('*'),
      supabase.from('paper_trades').select('*').order('timestamp', { ascending: false }).limit(50),
      supabase.from('paper_positions').select('*').eq('status', 'OPEN'),
    ])

    const rawAccounts = (accountsRes.data || []) as unknown as Record<string, unknown>[]
    const accounts: PaperAccount[] = rawAccounts.map((a) => ({
      id: a.id as string,
      strategy_name: a.strategy_name as string,
      starting_balance: Number(a.starting_balance),
      cash_balance: Number(a.cash_balance),
      position_value: Number(a.position_value),
      realized_pnl: Number(a.realized_pnl),
      unrealized_pnl: Number(a.unrealized_pnl),
      total_pnl: Number(a.total_pnl),
      total_trades: Number(a.total_trades),
      winning_trades: Number(a.winning_trades),
    }))
    const trades: Trade[] = (tradesRes.data || []).map((t: Record<string, unknown>) => ({
      id: t.id as string,
      agentId: t.strategy_id as string,
      agentName: t.strategy_name as string,
      symbol: t.symbol as string,
      side: t.side as Trade['side'],
      qty: Number(t.qty),
      price: Number(t.price),
      timestamp: Number(t.timestamp),
      pnl: Number(t.pnl),
      status: t.status as Trade['status'],
      action: t.action as string,
      reason: t.reason as string,
    }))
    const rawPositions = (positionsRes.data || []) as unknown as Record<string, unknown>[]
    const positions: PaperPosition[] = rawPositions.map((p) => ({
      id: p.id as string,
      strategy_id: p.strategy_id as string,
      symbol: p.symbol as string,
      side: p.side as string,
      size: Number(p.size),
      entry_price: Number(p.entry_price),
      current_price: Number(p.current_price),
      unrealized_pnl: Number(p.unrealized_pnl),
      status: p.status as string,
      opened_at: Number(p.opened_at),
    }))

    setPaperPositions(positions)
    setState((prev) => {
      const agents = prev.agents.map((a) => {
        const acc = accounts.find((ac) => ac.id === a.id)
        if (!acc) return a
        return {
          ...a,
          pnl: acc.total_pnl,
          tradesCount: acc.total_trades,
          winRate: acc.total_trades > 0 ? (acc.winning_trades / acc.total_trades) * 100 : 50,
          paperBalance: acc.cash_balance,
          realizedPnl: acc.realized_pnl,
          unrealizedPnl: acc.unrealized_pnl,
          winningTrades: acc.winning_trades,
        }
      })
      const totalPnl = accounts.reduce((sum, a) => sum + (a.total_pnl || 0), 0)
      const totalTrades = accounts.reduce((sum, a) => sum + (a.total_trades || 0), 0)
      return { ...prev, agents, trades, totalPnl, totalTrades }
    })
  }, [])

  // Initial load
  useEffect(() => {
    async function init() {
      try {
        setLoading(true)
        const [strategiesRes, configRes, accountsRes] = await Promise.all([
          supabase.from('bot_strategies').select('*'),
          supabase.from('bot_config').select('*').eq('id', 1).maybeSingle(),
          supabase.from('paper_accounts').select('*'),
        ])

        const rawAccounts = (accountsRes.data || []) as unknown as Record<string, unknown>[]
        const accounts: PaperAccount[] = rawAccounts.map((a) => ({
          id: a.id as string,
          strategy_name: a.strategy_name as string,
          starting_balance: Number(a.starting_balance),
          cash_balance: Number(a.cash_balance),
          position_value: Number(a.position_value),
          realized_pnl: Number(a.realized_pnl),
          unrealized_pnl: Number(a.unrealized_pnl),
          total_pnl: Number(a.total_pnl),
          total_trades: Number(a.total_trades),
          winning_trades: Number(a.winning_trades),
        }))
        const agents = createAgentsFromDb(strategiesRes.data || [], accounts)

        setState((prev) => ({
          ...prev,
          agents,
          connected: true,
        }))
        setConfig(configRes.data as BotConfig)
        await refreshPaperData()
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to load')
      } finally {
        setLoading(false)
      }
    }
    init()
  }, [refreshPaperData])

  // Poll live market data
  useEffect(() => {
    async function fetchLiveData() {
      try {
        const res = await fetch(`${FUNCTION_BASE}/reya-market`, {
          headers: { Authorization: `Bearer ${ANON_KEY}` },
        })
        if (!res.ok) return
        const data: LiveMarketData = await res.json()
        setLiveData(data)
        setError(null)

        if (data.prices && data.prices.length > 0) {
          setState((prev) => {
            const newMarkets = data.prices.map((p) => {
              const existing = prev.markets.find((m) => m.symbol === p.symbol)
              const isPerp = p.symbol.toUpperCase().endsWith('PERP')
              return {
                symbol: p.symbol,
                type: isPerp ? 'PERP' as const : 'SPOT' as const,
                price: p.price,
                prevPrice: existing?.price || p.price,
                change24h: p.change24h || 0,
                volume24h: 0,
              }
            })
            return { ...prev, markets: newMarkets, connected: true, lastUpdate: Date.now() }
          })
        }
      } catch {
        // silent — will retry
      }
    }

    fetchLiveData()
    const interval = setInterval(fetchLiveData, 5000)
    return () => clearInterval(interval)
  }, [])

  // Auto-run engine every 15 seconds
  useEffect(() => {
    if (!autoRun) return
    async function runEngine() {
      setEngineRunning(true)
      try {
        const res = await fetch(`${FUNCTION_BASE}/reya-engine`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${ANON_KEY}`,
          },
        })
        const result = await res.json()
        setEngineResult(result)
        await refreshPaperData()
      } catch {
        // silent
      } finally {
        setEngineRunning(false)
      }
    }

    runEngine()
    const interval = setInterval(runEngine, 3000)
    return () => clearInterval(interval)
  }, [autoRun, refreshPaperData])

  // Animate agents
  useEffect(() => {
    const interval = setInterval(() => {
      setState((prev) => {
        if (prev.agents.length === 0) return prev
        fetchCounter.current++
        const agents = prev.agents.map((a) => {
          if (!a.initialized) {
            return { ...a, initialized: true, state: a.is_enabled ? 'ACTIVE' as const : 'IDLE' as const, task: `${a.strategy} module loaded`, thought: 'monitoring' }
          }
          const r = Math.random()
          let newState = a.state
          let newTask = a.task
          let newThought = a.thought

          if (a.is_enabled) {
            if (engineRunning) {
              newState = 'EXECUTING'
              newTask = `Evaluating ${a.symbol}...`
              newThought = 'scanning signals'
            } else if (r < 0.1) {
              newState = 'THINKING'
              newThought = 'analyzing'
            } else if (r < 0.9) {
              newState = 'ACTIVE'
              newTask = `Monitoring ${a.symbol}`
              newThought = 'watching'
            } else {
              newState = 'IDLE'
              newThought = 'idle'
            }
          } else {
            newState = 'IDLE'
            newTask = 'Strategy disabled'
            newThought = 'disabled'
          }

          const dx = a.tx - a.x
          const dy = a.ty - a.y
          const dist = Math.sqrt(dx * dx + dy * dy)
          let nx = a.x
          let ny = a.y
          let moving = a.moving
          let speed = a.speed
          if (dist > 1) {
            speed = Math.min(2, dist * 0.04)
            nx = a.x + (dx / dist) * speed
            ny = a.y + (dy / dist) * speed
            moving = true
          } else {
            moving = false
            speed = 0
            if (Math.random() < 0.01) {
              const [hx, hy] = AGENT_HOMES[a.id]
              nx = hx + (Math.random() - 0.5) * 30
              ny = hy + (Math.random() - 0.5) * 20
            }
          }

          return { ...a, state: newState, task: newTask, thought: newThought, x: nx, y: ny, moving, speed }
        })

        return { ...prev, agents, cycleId: prev.cycleId + 1 }
      })
    }, 900)
    return () => clearInterval(interval)
  }, [engineRunning])

  const handleSelectAgent = useCallback((id: string) => {
    setSelectedAgent(id)
  }, [])

  const handleRunEngine = useCallback(async () => {
    setEngineRunning(true)
    setEngineResult(null)
    try {
      const res = await fetch(`${FUNCTION_BASE}/reya-engine`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${ANON_KEY}`,
        },
      })
      const result = await res.json()
      setEngineResult(result)
      await refreshPaperData()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Engine run failed')
    } finally {
      setEngineRunning(false)
    }
  }, [refreshPaperData])

  const handleSaveConfig = useCallback(async (newConfig: Partial<BotConfig>) => {
    try {
      const { data } = await supabase
        .from('bot_config')
        .upsert({ id: 1, ...newConfig })
        .select()
        .maybeSingle()
      if (data) setConfig(data as BotConfig)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save config')
    }
  }, [])

  const handleToggleStrategy = useCallback(async (strategyId: string, enabled: boolean) => {
    await supabase.from('bot_strategies').update({ is_enabled: enabled }).eq('id', strategyId)
    setState((prev) => ({
      ...prev,
      agents: prev.agents.map((a) =>
        a.id === strategyId ? { ...a, is_enabled: enabled, state: enabled ? 'ACTIVE' : 'IDLE' } : a
      ),
    }))
  }, [])

  const handleResetPaper = useCallback(async () => {
    if (!confirm('Reset all paper accounts to $500 and clear all trades/positions?')) return
    await supabase.from('paper_trades').delete().neq('id', '00000000-0000-0000-0000-000000000000')
    await supabase.from('paper_positions').delete().neq('id', '00000000-0000-0000-0000-000000000000')
    const { data: strategies } = await supabase.from('bot_strategies').select('*')
    for (const s of (strategies || [])) {
      await supabase.from('paper_accounts').update({
        cash_balance: 500,
        position_value: 0,
        realized_pnl: 0,
        unrealized_pnl: 0,
        total_pnl: 0,
        total_trades: 0,
        winning_trades: 0,
      }).eq('id', s.id)
    }
    await refreshPaperData()
  }, [refreshPaperData])

  const selected = state.agents.find((a) => a.id === selectedAgent) || null
  const selectedPositions = paperPositions.filter((p) => p.strategy_id === selectedAgent)

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-left">
          <div className="header-logo">
            <span className="logo-mark">R</span>
            <span className="logo-text">REYA TRADING BOT</span>
          </div>
          <span className="header-subtitle">PAPER TRADING · LIVE MARKET DATA</span>
        </div>
        <div className="header-right">
          <button className="header-btn" onClick={handleResetPaper}>RESET PAPER</button>
          <span className="header-divider">/</span>
          <button className="header-btn" onClick={() => setAutoRun(!autoRun)}>
            {autoRun ? 'AUTO: ON' : 'AUTO: OFF'}
          </button>
          <span className="header-divider">/</span>
          <button className="header-btn" onClick={() => setShowSettings(true)}>SETTINGS</button>
          <span className="header-divider">/</span>
          <a href="https://reya.xyz" target="_blank" rel="noopener noreferrer" className="header-link">REYA NETWORK</a>
          <span className="header-divider">/</span>
          <span className="header-status">
            <span className={`status-dot ${state.connected ? 'connected' : 'linking'}`} />
            {state.connected ? 'SYNCED' : 'LINKING'}
          </span>
        </div>
      </header>

      <main className="app-main">
        <div className="floor-container">
          <div className="floor-header">
            <span className="floor-deck">DECK 07 · STRATEGY STATION · PAPER MODE · {state.connected ? 'SYNCED' : 'SYNCING'}</span>
          </div>
          <h1 className="floor-title">Operations Floor</h1>
          <p className="floor-subtitle">
            Six paper trading accounts starting at $500 each, trading live Reya market prices. Auto-runs every 15 seconds.
          </p>

          {error && (
            <div className="error-banner">
              {error}
              <button className="error-dismiss" onClick={() => setError(null)}>DISMISS</button>
            </div>
          )}

          <div className="canvas-wrapper">
            {loading ? (
              <div className="canvas-loading">Loading operations floor...</div>
            ) : (
              <FloorCanvas state={state} selectedAgent={selectedAgent} onSelectAgent={handleSelectAgent} />
            )}
          </div>

          <div className="floor-stats">
            <div className="stat-item">
              <span className="stat-label">ENGINE</span>
              <span className="stat-value stat-status">
                <span className={`status-dot ${engineRunning ? 'connected' : 'linking'}`} />
                {engineRunning ? 'RUNNING' : autoRun ? 'AUTO' : 'IDLE'}
              </span>
            </div>
            <div className="stat-item">
              <span className="stat-label">TOTAL PAPER PNL</span>
              <span className="stat-value" style={{ color: state.totalPnl >= 0 ? '#a6df55' : '#ff6557' }}>
                {state.totalPnl >= 0 ? '+' : ''}${state.totalPnl.toFixed(2)}
              </span>
            </div>
            <div className="stat-item">
              <span className="stat-label">TOTAL TRADES</span>
              <span className="stat-value">{state.totalTrades}</span>
            </div>
            <div className="stat-item">
              <span className="stat-label">CYCLE</span>
              <span className="stat-value">{state.cycleId}</span>
            </div>
          </div>

          <div className="agent-balances">
            {state.agents.map((a) => {
              const pct = ((a.paperBalance + a.unrealizedPnl - a.startingBalance) / a.startingBalance) * 100
              return (
                <div key={a.id} className="agent-balance-card" onClick={() => setSelectedAgent(a.id)}>
                  <div className="abc-header">
                    <span className="abc-dot" style={{ background: a.color }} />
                    <span className="abc-name">{a.name}</span>
                  </div>
                  <div className="abc-balance">${(a.paperBalance + a.unrealizedPnl).toFixed(2)}</div>
                  <div className="abc-pnl" style={{ color: a.pnl >= 0 ? '#a6df55' : '#ff6557' }}>
                    {a.pnl >= 0 ? '+' : ''}{a.pnl.toFixed(2)} ({pct >= 0 ? '+' : ''}{pct.toFixed(1)}%)
                  </div>
                  <div className="abc-trades">{a.tradesCount} trades · {a.winRate.toFixed(0)}% win</div>
                </div>
              )
            })}
          </div>

          <div className="engine-controls">
            <button
              className="engine-btn"
              onClick={handleRunEngine}
              disabled={engineRunning}
            >
              {engineRunning ? 'RUNNING ENGINE...' : 'RUN STRATEGY ENGINE NOW'}
            </button>
            {engineResult && (
              <span className="engine-result">
                {engineResult.status === 'ok'
                  ? `Cycle complete: ${Object.keys(engineResult.strategies || {}).length} strategies evaluated`
                  : engineResult.status || 'Done'}
              </span>
            )}
          </div>
        </div>

        <aside className="side-panel">
          <AgentDetail agent={selected} onToggle={handleToggleStrategy} liveData={liveData} paperPositions={selectedPositions} />
          <TradeFeed trades={state.trades} />
        </aside>
      </main>

      <footer className="app-footer">
        <span>REYA TRADING BOT · PAPER TRADING MODE</span>
        <span className="footer-meta">$500 PER AGENT · LIVE REYA PRICES · {autoRun ? 'AUTO-RUNNING' : 'MANUAL'}</span>
      </footer>

      {showSettings && (
        <SettingsPanel
          config={config}
          onClose={() => setShowSettings(false)}
          onSave={handleSaveConfig}
        />
      )}
    </div>
  )
}
