import { useEffect, useState, useCallback, useRef } from 'react'
import { FloorCanvas } from './FloorCanvas'
import { AgentDetail } from './AgentDetail'
import { TradeFeed } from './TradeFeed'
import { SettingsPanel } from './SettingsPanel'
import { supabase } from './supabase'
import { AGENT_COLORS, AGENT_HOMES, STRATEGY_META } from './types'
import type { Agent, BotState, BotConfig, LiveMarketData, Trade } from './types'
import './app.css'

const FUNCTION_BASE = import.meta.env.VITE_SUPABASE_URL + '/functions/v1'
const ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY as string

function createAgentsFromDb(dbStrategies: Array<Record<string, unknown>>): Agent[] {
  return Object.keys(AGENT_HOMES).map((id) => {
    const dbStrategy = dbStrategies.find((s) => s.id === id)
    const meta = STRATEGY_META[id]
    const [hx, hy] = AGENT_HOMES[id]
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
      pnl: 0,
      tradesCount: 0,
      winRate: 50,
      strategy: meta.strategy,
      description: meta.description,
      is_enabled: dbStrategy?.is_enabled ?? true,
      symbol: (dbStrategy?.symbol as string) || 'ETHRUSDPERP',
      params: (dbStrategy?.params as Record<string, unknown>) || {},
      initialized: false,
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
  const [selectedAgent, setSelectedAgent] = useState<string | null>('arbitrage')
  const [showSettings, setShowSettings] = useState(false)
  const [engineRunning, setEngineRunning] = useState(false)
  const [engineResult, setEngineResult] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const fetchCounter = useRef(0)

  // Initial load: fetch strategies and config from Supabase
  useEffect(() => {
    async function init() {
      try {
        setLoading(true)
        const [strategiesRes, configRes, tradesRes] = await Promise.all([
          supabase.from('bot_strategies').select('*'),
          supabase.from('bot_config').select('*').eq('id', 1).maybeSingle(),
          supabase.from('bot_trade_history').select('*').order('timestamp', { ascending: false }).limit(50),
        ])

        const agents = createAgentsFromDb(strategiesRes.data || [])
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
        }))

        setState((prev) => ({
          ...prev,
          agents,
          trades,
          totalTrades: trades.length,
          connected: true,
        }))
        setConfig(configRes.data as BotConfig)
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to load')
      } finally {
        setLoading(false)
      }
    }
    init()
  }, [])

  // Poll live market data from edge function
  useEffect(() => {
    async function fetchLiveData() {
      try {
        const res = await fetch(`${FUNCTION_BASE}/reya-market`, {
          headers: { Authorization: `Bearer ${ANON_KEY}` },
        })
        if (!res.ok) {
          setError(`Market fetch failed: ${res.status}`)
          return
        }
        const data: LiveMarketData = await res.json()
        setLiveData(data)
        setError(null)

        // Update markets in state
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
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Market fetch error')
      }
    }

    fetchLiveData()
    const interval = setInterval(fetchLiveData, 5000)
    return () => clearInterval(interval)
  }, [])

  // Animate agents on canvas (movement, state changes)
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
            if (r < 0.1) {
              newState = 'EXECUTING'
              newTask = `Evaluating ${a.symbol}...`
              newThought = 'scanning signals'
            } else if (r < 0.25) {
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

          // Subtle movement
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
  }, [])

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

      // Refresh trades after engine runs
      const { data: newTrades } = await supabase
        .from('bot_trade_history')
        .select('*')
        .order('timestamp', { ascending: false })
        .limit(50)

      if (newTrades) {
        const trades: Trade[] = newTrades.map((t: Record<string, unknown>) => ({
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
        }))
        setState((prev) => ({ ...prev, trades, totalTrades: trades.length }))
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Engine run failed')
    } finally {
      setEngineRunning(false)
    }
  }, [])

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

  const handleManualTrade = useCallback(async (params: {
    symbol: string
    isBuy: boolean
    limitPx: string
    qty: string
    orderType: string
  }) => {
    try {
      const res = await fetch(`${FUNCTION_BASE}/reya-trade`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${ANON_KEY}`,
        },
        body: JSON.stringify({ ...params, strategyId: 'manual', strategyName: 'MANUAL' }),
      })
      const result = await res.json()
      if (!result.success) {
        setError(result.error || 'Trade failed')
      }
      // Refresh trades
      const { data: newTrades } = await supabase
        .from('bot_trade_history')
        .select('*')
        .order('timestamp', { ascending: false })
        .limit(50)
      if (newTrades) {
        const trades: Trade[] = newTrades.map((t: Record<string, unknown>) => ({
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
        }))
        setState((prev) => ({ ...prev, trades, totalTrades: trades.length }))
      }
      return result
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Trade submission failed')
      return null
    }
  }, [])

  const selected = state.agents.find((a) => a.id === selectedAgent) || null
  const isConfigured = !!(config?.wallet_address && config?.account_id)

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-left">
          <div className="header-logo">
            <span className="logo-mark">R</span>
            <span className="logo-text">REYA TRADING BOT</span>
          </div>
          <span className="header-subtitle">AUTONOMOUS STRATEGY OPERATIONS</span>
        </div>
        <div className="header-right">
          <button className="header-btn" onClick={() => setShowSettings(true)}>
            SETTINGS
          </button>
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
            <span className="floor-deck">DECK 07 · STRATEGY STATION · {state.connected ? 'SYNCED' : 'SYNCING'}</span>
          </div>
          <h1 className="floor-title">Operations Floor</h1>
          <p className="floor-subtitle">
            {isConfigured
              ? 'Six autonomous trading strategies connected to the Reya network.'
              : 'Configure your wallet and account to start trading on Reya.'}
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
              <span className="stat-label">ENGINE STATUS</span>
              <span className="stat-value stat-status">
                <span className={`status-dot ${config?.is_active ? 'connected' : 'linking'}`} />
                {config?.is_active ? 'ACTIVE' : 'STOPPED'}
              </span>
            </div>
            <div className="stat-item">
              <span className="stat-label">WALLET</span>
              <span className="stat-value stat-wallet">
                {config?.wallet_address
                  ? `${config.wallet_address.slice(0, 6)}...${config.wallet_address.slice(-4)}`
                  : 'NOT SET'}
              </span>
            </div>
            <div className="stat-item">
              <span className="stat-label">ACCOUNT ID</span>
              <span className="stat-value">{config?.account_id || 'NOT SET'}</span>
            </div>
            <div className="stat-item">
              <span className="stat-label">TOTAL TRADES</span>
              <span className="stat-value">{state.totalTrades}</span>
            </div>
          </div>

          <div className="engine-controls">
            <button
              className="engine-btn"
              onClick={handleRunEngine}
              disabled={engineRunning || !isConfigured}
            >
              {engineRunning ? 'RUNNING ENGINE...' : 'RUN STRATEGY ENGINE'}
            </button>
            {!isConfigured && (
              <span className="engine-hint">Configure wallet in settings to enable trading</span>
            )}
            {engineResult && (
              <span className="engine-result">
                {engineResult.status === 'ok'
                  ? `Engine cycle complete: ${Object.keys(engineResult.strategies || {}).length} strategies evaluated`
                  : engineResult.status || 'Done'}
              </span>
            )}
          </div>
        </div>

        <aside className="side-panel">
          <AgentDetail agent={selected} onToggle={handleToggleStrategy} onTrade={handleManualTrade} liveData={liveData} />
          <TradeFeed trades={state.trades} />
        </aside>
      </main>

      <footer className="app-footer">
        <span>REYA TRADING BOT ARCHITECTURE · OPERATIONS FLOOR</span>
        <span className="footer-meta">ONE NETWORK · SIX STRATEGIES · FLOOR STATUS: {state.connected ? 'ACTIVE' : 'LINKING'}</span>
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
