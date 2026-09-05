import type { Agent, LiveMarketData, PaperPosition } from './types'

interface AgentDetailProps {
  agent: Agent | null
  onToggle?: (strategyId: string, enabled: boolean) => void
  liveData?: LiveMarketData | null
  paperPositions?: PaperPosition[]
}

export function AgentDetail({ agent, onToggle, liveData, paperPositions }: AgentDetailProps) {
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

  const totalValue = agent.paperBalance + agent.unrealizedPnl
  const pnlPct = ((totalValue - agent.startingBalance) / agent.startingBalance) * 100
  const livePrice = liveData?.prices?.find((p) => p.symbol === agent.symbol)?.price

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
          <div className="detail-label">LIVE PRICE</div>
          <div className="detail-value">{livePrice ? `$${livePrice.toFixed(2)}` : '—'}</div>
        </div>
      </div>

      <div className="detail-section paper-section">
        <div className="detail-label">PAPER ACCOUNT</div>
        <div className="paper-grid">
          <div className="paper-cell">
            <span className="paper-label">EQUITY</span>
            <span className="paper-value">${totalValue.toFixed(2)}</span>
          </div>
          <div className="paper-cell">
            <span className="paper-label">CASH</span>
            <span className="paper-value">${agent.paperBalance.toFixed(2)}</span>
          </div>
          <div className="paper-cell">
            <span className="paper-label">REALIZED</span>
            <span className="paper-value" style={{ color: agent.realizedPnl >= 0 ? '#a6df55' : '#ff6557' }}>
              {agent.realizedPnl >= 0 ? '+' : ''}{agent.realizedPnl.toFixed(2)}
            </span>
          </div>
          <div className="paper-cell">
            <span className="paper-label">UNREALIZED</span>
            <span className="paper-value" style={{ color: agent.unrealizedPnl >= 0 ? '#a6df55' : '#ff6557' }}>
              {agent.unrealizedPnl >= 0 ? '+' : ''}{agent.unrealizedPnl.toFixed(2)}
            </span>
          </div>
          <div className="paper-cell">
            <span className="paper-label">TOTAL PNL</span>
            <span className="paper-value" style={{ color: agent.pnl >= 0 ? '#a6df55' : '#ff6557' }}>
              {agent.pnl >= 0 ? '+' : ''}{agent.pnl.toFixed(2)} ({pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(1)}%)
            </span>
          </div>
          <div className="paper-cell">
            <span className="paper-label">WIN RATE</span>
            <span className="paper-value">{agent.winRate.toFixed(0)}% ({agent.winningTrades}/{agent.tradesCount})</span>
          </div>
        </div>
      </div>

      {paperPositions && paperPositions.length > 0 && (
        <div className="detail-section">
          <div className="detail-label">OPEN PAPER POSITIONS</div>
          {paperPositions.map((pos) => (
            <div key={pos.id} className="detail-position">
              <span className="pos-side" style={{ color: pos.side === 'LONG' ? '#a6df55' : '#ff6557' }}>
                {pos.side}
              </span>
              <span>{pos.size} @ ${pos.entry_price.toFixed(2)}</span>
              <span style={{ color: pos.unrealized_pnl >= 0 ? '#a6df55' : '#ff6557' }}>
                uPnL: {pos.unrealized_pnl >= 0 ? '+' : ''}{pos.unrealized_pnl.toFixed(2)}
              </span>
            </div>
          ))}
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
    </div>
  )
}
