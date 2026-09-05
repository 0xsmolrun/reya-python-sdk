import type { Trade } from './types'

interface TradeFeedProps {
  trades: Trade[]
}

export function TradeFeed({ trades }: TradeFeedProps) {
  return (
    <div className="trade-feed">
      <div className="feed-header">
        <span className="feed-title">LIVE TRADE FEED</span>
        <span className="feed-count">{trades.length} events</span>
      </div>
      <div className="feed-body">
        {trades.length === 0 && (
          <div className="feed-empty">Waiting for trade execution events...</div>
        )}
        {trades.slice(0, 30).map((t) => (
          <div key={t.id} className="feed-row">
            <span className="feed-time">{new Date(t.timestamp).toLocaleTimeString('en-US', { hour12: false })}</span>
            <span className="feed-agent" style={{ color: getAgentColor(t.agentId) }}>{t.agentName}</span>
            <span className={`feed-side feed-side-${t.side.toLowerCase()}`}>{t.side}</span>
            <span className="feed-qty">{t.qty}</span>
            <span className="feed-symbol">{t.symbol}</span>
            <span className="feed-price">@ ${t.price.toFixed(2)}</span>
            <span className="feed-pnl" style={{ color: t.pnl >= 0 ? '#a6df55' : '#ff6557' }}>
              {t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(2)}
            </span>
            <span className={`feed-status feed-status-${t.status.toLowerCase()}`}>{t.status}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function getAgentColor(id: string): string {
  const colors: Record<string, string> = {
    arbitrage: '#52d5c9',
    maker: '#a6df55',
    momentum: '#f3ad3d',
    reversion: '#b798ff',
    risk: '#ff6557',
    liquidator: '#53a9e9',
  }
  return colors[id] || '#c4c4bd'
}
