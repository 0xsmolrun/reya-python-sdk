import { useState } from 'react'
import type { Agent, LiveMarketData } from './types'

interface AgentDetailProps {
  agent: Agent | null
  onToggle?: (strategyId: string, enabled: boolean) => void
  onTrade?: (params: { symbol: string; isBuy: boolean; limitPx: string; qty: string; orderType: string }) => Promise<unknown>
  liveData?: LiveMarketData | null
}

export function AgentDetail({ agent, onToggle, onTrade, liveData }: AgentDetailProps) {
  const [showTradeForm, setShowTradeForm] = useState(false)
  const [tradeSymbol, setTradeSymbol] = useState('ETHRUSDPERP')
  const [tradeSide, setTradeSide] = useState(true)
  const [tradePrice, setTradePrice] = useState('')
  const [tradeQty, setTradeQty] = useState('0.1')
  const [tradeResult, setTradeResult] = useState<string | null>(null)

  if (!agent) {
    return (
      <div className="detail-panel">
        <div className="detail-header">
          <span className="detail-title">SELECTED AGENT</span>
        </div>
        <div className="detail-empty">
          <div className="detail-empty-text">Select an agent from the floor to inspect its strategy, state, and live metrics.</div>
        </div>
      </div>
    )
  }

  const stateColors: Record<string, string> = {
    ACTIVE: '#a6df55',
    IDLE: '#696d68',
    THINKING: '#f1bd57',
    EXECUTING: '#52d5c9',
    ERROR: '#ff6557',
  }

  const handleTrade = async () => {
    if (!onTrade) return
    setTradeResult('Submitting...')
    const result = await onTrade({
      symbol: tradeSymbol,
      isBuy: tradeSide,
      limitPx: tradePrice,
      qty: tradeQty,
      orderType: 'LIMIT_IOC',
    })
    setTradeResult(result && (result as Record<string, unknown>).success ? 'Order submitted' : 'Order failed')
    setTimeout(() => setTradeResult(null), 3000)
    setShowTradeForm(false)
  }

  // Find live position for this strategy's symbol
  const livePosition = liveData?.positions?.find((p) => p.symbol === agent.symbol)

  return (
    <div className="detail-panel">
      <div className="detail-header">
        <span className="detail-dot" style={{ background: agent.color }} />
        <span className="detail-title">{agent.name}</span>
        <span className="detail-role">{agent.role}</span>
      </div>

      <div className="detail-section">
        <div className="detail-label">STRATEGY</div>
        <div className="detail-value">{agent.strategy}</div>
        <div className="detail-desc">{agent.description}</div>
      </div>

      <div className="detail-row">
        <div className="detail-cell">
          <div className="detail-label">STATE</div>
          <div className="detail-state">
            <span className="state-dot" style={{ background: stateColors[agent.state] }} />
            {agent.state}
          </div>
        </div>
        <div className="detail-cell">
          <div className="detail-label">ENABLED</div>
          <label className="detail-toggle">
            <input
              type="checkbox"
              checked={agent.is_enabled}
              onChange={(e) => onToggle?.(agent.id, e.target.checked)}
            />
            <span>{agent.is_enabled ? 'ON' : 'OFF'}</span>
          </label>
        </div>
      </div>

      <div className="detail-row">
        <div className="detail-cell">
          <div className="detail-label">SYMBOL</div>
          <div className="detail-value">{agent.symbol}</div>
        </div>
        <div className="detail-cell">
          <div className="detail-label">TRADES</div>
          <div className="detail-value">{agent.tradesCount}</div>
        </div>
      </div>

      {livePosition && (
        <div className="detail-section">
          <div className="detail-label">LIVE POSITION</div>
          <div className="detail-position">
            <span>Size: {livePosition.notionalSize}</span>
            <span>Side: {livePosition.side}</span>
            <span>Entry: {livePosition.entryPrice}</span>
            <span style={{ color: livePosition.unrealizedPnl >= 0 ? '#a6df55' : '#ff6557' }}>
              uPnL: {livePosition.unrealizedPnl >= 0 ? '+' : ''}{livePosition.unrealizedPnl}
            </span>
          </div>
        </div>
      )}

      <div className="detail-section">
        <div className="detail-label">CURRENT TASK</div>
        <div className="detail-task">{agent.task}</div>
      </div>

      <div className="detail-section">
        <div className="detail-label">THOUGHT</div>
        <div className="detail-thought">
          <span className="thought-bracket">[</span>
          {agent.thought}
          <span className="thought-bracket">]</span>
        </div>
      </div>

      {onTrade && (
        <div className="detail-section">
          {showTradeForm ? (
            <div className="trade-form">
              <div className="trade-form-row">
                <select className="trade-select" value={tradeSymbol} onChange={(e) => setTradeSymbol(e.target.value)}>
                  <option value="ETHRUSDPERP">ETHRUSDPERP</option>
                  <option value="BTCRUSDPERP">BTCRUSDPERP</option>
                  <option value="ETHRUSD">ETHRUSD (spot)</option>
                  <option value="BTCRUSD">BTCRUSD (spot)</option>
                </select>
                <button
                  className={`trade-side-btn ${tradeSide ? 'buy' : 'sell'}`}
                  onClick={() => setTradeSide(!tradeSide)}
                >
                  {tradeSide ? 'BUY' : 'SELL'}
                </button>
              </div>
              <input
                className="form-input"
                type="text"
                placeholder="Price"
                value={tradePrice}
                onChange={(e) => setTradePrice(e.target.value)}
              />
              <input
                className="form-input"
                type="text"
                placeholder="Quantity"
                value={tradeQty}
                onChange={(e) => setTradeQty(e.target.value)}
              />
              <div className="trade-form-actions">
                <button className="btn-secondary" onClick={() => setShowTradeForm(false)}>CANCEL</button>
                <button className="btn-primary" onClick={handleTrade} disabled={!tradePrice || !tradeQty}>
                  SUBMIT ORDER
                </button>
              </div>
              {tradeResult && <div className="trade-result">{tradeResult}</div>}
            </div>
          ) : (
            <button className="btn-secondary" onClick={() => setShowTradeForm(true)}>
              MANUAL ORDER
            </button>
          )}
        </div>
      )}
    </div>
  )
}
