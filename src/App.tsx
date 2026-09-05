import { useEffect, useState, useCallback, useRef } from 'react'
import { FloorCanvas } from './FloorCanvas'
import { AgentDetail } from './AgentDetail'
import { TradeFeed } from './TradeFeed'
import { createInitialState, tick } from './simulation'
import { syncAgentsToDb, syncTradesToDb, syncMarketsToDb, syncStateToDb } from './supabase'
import type { BotState } from './types'
import './app.css'

export function App() {
  const [state, setState] = useState<BotState>(() => createInitialState())
  const [selectedAgent, setSelectedAgent] = useState<string | null>('arbitrage')
  const syncCounter = useRef(0)

  useEffect(() => {
    const interval = setInterval(() => {
      setState((prev) => {
        const next = tick(prev)
        syncCounter.current++
        if (syncCounter.current % 5 === 0) {
          syncAgentsToDb(next.agents.map((a) => ({
            id: a.id, name: a.name, role: a.role, strategy: a.strategy,
            description: a.description, color: a.color, state: a.state,
            task: a.task, thought: a.thought, pnl: a.pnl, trades_count: a.tradesCount,
            win_rate: a.winRate, x: a.x, y: a.y, updated_at: new Date().toISOString(),
          })))
          syncMarketsToDb(next.markets.map((m) => ({
            id: m.symbol, type: m.type, price: m.price, prev_price: m.prevPrice,
            change_24h: m.change24h, volume_24h: m.volume24h, updated_at: new Date().toISOString(),
          })))
          syncStateToDb({
            id: 1, total_pnl: next.totalPnl, total_trades: next.totalTrades,
            cycle_id: next.cycleId, connected: next.connected, last_update: next.lastUpdate,
          })
          if (next.trades.length > 0) {
            syncTradesToDb(next.trades.slice(0, 20).map((t) => ({
              id: t.id, agent_id: t.agentId, agent_name: t.agentName, symbol: t.symbol,
              side: t.side, qty: t.qty, price: t.price, pnl: t.pnl, status: t.status,
              timestamp: t.timestamp,
            })))
          }
        }
        return next
      })
    }, 900)
    return () => clearInterval(interval)
  }, [])

  const handleSelectAgent = useCallback((id: string) => {
    setSelectedAgent(id)
  }, [])

  const selected = state.agents.find((a) => a.id === selectedAgent) || null

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
          <p className="floor-subtitle">Connecting six autonomous trading strategies to the Reya network.</p>

          <div className="canvas-wrapper">
            <FloorCanvas state={state} selectedAgent={selectedAgent} onSelectAgent={handleSelectAgent} />
          </div>

          <div className="floor-stats">
            <div className="stat-item">
              <span className="stat-label">MISSION STATUS</span>
              <span className="stat-value stat-status">
                <span className={`status-dot ${state.connected ? 'connected' : 'linking'}`} />
                {state.connected ? 'ACTIVE' : 'LINKING'}
              </span>
            </div>
            <div className="stat-item">
              <span className="stat-label">TOTAL PNL</span>
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
        </div>

        <aside className="side-panel">
          <AgentDetail agent={selected} />
          <TradeFeed trades={state.trades} />
        </aside>
      </main>

      <footer className="app-footer">
        <span>REYA TRADING BOT ARCHITECTURE · OPERATIONS FLOOR</span>
        <span className="footer-meta">ONE NETWORK · SIX STRATEGIES · FLOOR STATUS: {state.connected ? 'ACTIVE' : 'LINKING'}</span>
      </footer>
    </div>
  )
}
