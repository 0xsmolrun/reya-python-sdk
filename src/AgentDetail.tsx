import type { Agent } from './types'

interface AgentDetailProps {
  agent: Agent | null
}

export function AgentDetail({ agent }: AgentDetailProps) {
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
          <div className="detail-label">PNL</div>
          <div className="detail-pnl" style={{ color: agent.pnl >= 0 ? '#a6df55' : '#ff6557' }}>
            {agent.pnl >= 0 ? '+' : ''}${agent.pnl.toFixed(2)}
          </div>
        </div>
      </div>

      <div className="detail-row">
        <div className="detail-cell">
          <div className="detail-label">TRADES</div>
          <div className="detail-value">{agent.tradesCount}</div>
        </div>
        <div className="detail-cell">
          <div className="detail-label">WIN RATE</div>
          <div className="detail-value">{agent.winRate.toFixed(1)}%</div>
        </div>
      </div>

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
