import { useEffect, useRef, useCallback } from 'react'
import type { BotState, Agent } from './types'
import { AGENT_COLORS, AGENT_HOMES } from './types'

interface FloorCanvasProps {
  state: BotState
  selectedAgent: string | null
  onSelectAgent: (id: string) => void
}

const CANVAS_W = 1000
const CANVAS_H = 620

function line(ctx: CanvasRenderingContext2D, x1: number, y1: number, x2: number, y2: number, color: string, width = 1) {
  ctx.strokeStyle = color
  ctx.lineWidth = width
  ctx.beginPath()
  ctx.moveTo(x1, y1)
  ctx.lineTo(x2, y2)
  ctx.stroke()
}

export function FloorCanvas({ state, selectedAgent, onSelectAgent }: FloorCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const stateRef = useRef(state)
  const selectedRef = useRef(selectedAgent)
  const animFrame = useRef<number>(0)

  stateRef.current = state
  selectedRef.current = selectedAgent

  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const s = stateRef.current
    const time = performance.now()
    const phase = time / 1000

    // Background
    ctx.fillStyle = '#090a09'
    ctx.fillRect(0, 0, CANVAS_W, CANVAS_H)

    // Ceiling area
    ctx.fillStyle = '#13110f'
    ctx.fillRect(0, 0, CANVAS_W, 152)

    // Floor grid
    for (let y = 152; y < CANVAS_H; y += 25) {
      line(ctx, 0, y, CANVAS_W, y, '#241e18')
    }
    for (let x = -80; x < 1080; x += 75) {
      line(ctx, x, 152, x + 30, CANVAS_H, '#201c18')
    }

    // Monitors
    drawTrainingMonitor(ctx, 14, 10, 270, 120, s, time, phase)
    drawPnlMonitor(ctx, 294, 10, 210, 120, s, time, phase)
    drawMarketMonitor(ctx, 514, 10, 380, 120, s, time, phase)
    drawClock(ctx, 950, 70, time)

    // Floor status bar
    ctx.fillStyle = '#080908'
    ctx.fillRect(0, 136, CANVAS_W, 16)
    ctx.fillStyle = '#77736a'
    ctx.font = 'bold 9px "JetBrains Mono", Consolas, monospace'
    ctx.textAlign = 'left'
    ctx.fillText('REYA TRADING BOT ARCHITECTURE · ONE NETWORK · SIX STRATEGIES · FLOOR STATUS: ' + (s.connected ? 'ACTIVE' : 'LINKING'), 30, 148)

    // Server racks
    drawServerRack(ctx, 40, 545)
    drawServerRack(ctx, 960, 545)

    // Plants
    drawPlant(ctx, 80, 580)
    drawPlant(ctx, 920, 580)

    // Desks and agents
    const desks = [
      { x: 165, y: 300, id: 'arbitrage' },
      { x: 500, y: 290, id: 'maker' },
      { x: 835, y: 300, id: 'momentum' },
      { x: 165, y: 523, id: 'reversion' },
      { x: 500, y: 530, id: 'risk' },
      { x: 835, y: 523, id: 'liquidator' },
    ]

    for (const desk of desks) {
      drawDesk(ctx, desk.x, desk.y, desk.id.toUpperCase(), s.agents.find((a) => a.id === desk.id)?.color || '#888', time)
    }

    // Connection lines between agents
    ctx.strokeStyle = 'rgba(80, 80, 80, 0.15)'
    ctx.lineWidth = 1
    const positions = desks.map((d) => {
      const a = s.agents.find((ag) => ag.id === d.id)
      return a ? { x: a.x, y: a.y } : { x: d.x, y: d.y - 55 }
    })
    for (let i = 0; i < positions.length; i++) {
      for (let j = i + 1; j < positions.length; j++) {
        ctx.beginPath()
        ctx.moveTo(positions[i].x, positions[i].y)
        ctx.lineTo(positions[j].x, positions[j].y)
        ctx.stroke()
      }
    }

    // Agents
    for (const agent of s.agents) {
      drawAgent(ctx, agent, time, phase, agent.id === selectedRef.current)
    }

    // Vignette
    const grad = ctx.createRadialGradient(CANVAS_W / 2, CANVAS_H / 2, 300, CANVAS_W / 2, CANVAS_H / 2, 700)
    grad.addColorStop(0, 'rgba(0,0,0,0)')
    grad.addColorStop(1, 'rgba(0,0,0,0.4)')
    ctx.fillStyle = grad
    ctx.fillRect(0, 0, CANVAS_W, CANVAS_H)

    animFrame.current = requestAnimationFrame(draw)
  }, [])

  useEffect(() => {
    animFrame.current = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(animFrame.current)
  }, [draw])

  const handleClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const scaleX = CANVAS_W / rect.width
    const scaleY = CANVAS_H / rect.height
    const cx = (e.clientX - rect.left) * scaleX
    const cy = (e.clientY - rect.top) * scaleY

    for (const agent of state.agents) {
      const dx = cx - agent.x
      const dy = cy - agent.y
      if (Math.sqrt(dx * dx + dy * dy) < 25) {
        onSelectAgent(agent.id)
        return
      }
    }
  }

  return (
    <canvas
      ref={canvasRef}
      width={CANVAS_W}
      height={CANVAS_H}
      onClick={handleClick}
      style={{ width: '100%', height: 'auto', display: 'block', cursor: 'pointer', imageRendering: 'auto' }}
    />
  )
}

function drawTrainingMonitor(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, s: BotState, time: number, phase: number) {
  ctx.fillStyle = '#0a0e0f'
  ctx.fillRect(x, y, w, h)
  ctx.strokeStyle = '#292d2d'
  ctx.strokeRect(x + 0.5, y + 0.5, w, h)
  ctx.fillStyle = '#696d68'
  ctx.font = '9px "JetBrains Mono", Consolas, monospace'
  ctx.textAlign = 'left'
  ctx.fillText('STRATEGY TRAINING / LIVE METRICS', x + 11, y + 15)

  ctx.save()
  ctx.beginPath()
  ctx.rect(x + 8, y + 19, w - 16, h - 25)
  ctx.clip()

  // Bar chart of agent PnL
  const barW = s.agents.length > 0 ? (w - 30) / s.agents.length : 0
  for (let i = 0; i < s.agents.length; i++) {
    const a = s.agents[i]
    const barH = Math.min(h - 35, Math.abs(a.pnl) / 5)
    const bx = x + 12 + i * barW
    const by = y + h - 8 - barH
    ctx.fillStyle = a.pnl >= 0 ? a.color + '60' : '#ff655760'
    ctx.fillRect(bx, by, barW - 4, barH)
    ctx.fillStyle = a.color
    ctx.font = '7px "JetBrains Mono", Consolas, monospace'
    ctx.fillText(a.name.slice(0, 5), bx, y + h - 2)
  }

  // Scanline
  const scanX = x + 10 + ((time / 8) % (w - 25))
  ctx.fillStyle = 'rgba(82, 213, 201, 0.15)'
  ctx.fillRect(scanX, y + 22, 2, h - 31)
  ctx.restore()
}

function drawPnlMonitor(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, s: BotState, time: number, phase: number) {
  ctx.fillStyle = '#0a0e0f'
  ctx.fillRect(x, y, w, h)
  ctx.strokeStyle = '#292d2d'
  ctx.strokeRect(x + 0.5, y + 0.5, w, h)
  ctx.fillStyle = '#696d68'
  ctx.font = '9px "JetBrains Mono", Consolas, monospace'
  ctx.textAlign = 'left'
  ctx.fillText('TOTAL PNL / USD', x + 11, y + 15)

  ctx.fillStyle = s.totalPnl >= 0 ? '#a6df55' : '#ff6557'
  ctx.font = 'bold 22px "JetBrains Mono", Consolas, monospace'
  const pnlStr = (s.totalPnl >= 0 ? '+' : '') + '$' + s.totalPnl.toFixed(2)
  ctx.fillText(pnlStr, x + 11, y + 45)

  ctx.fillStyle = '#696d68'
  ctx.font = '8px "JetBrains Mono", Consolas, monospace'
  ctx.fillText('TRADES: ' + s.totalTrades, x + 11, y + 60)
  ctx.fillText('CYCLE: ' + s.cycleId, x + 11, y + 72)
  ctx.fillText('AGENTS: ' + s.agents.length + ' ACTIVE', x + 11, y + 84)

  // Mini PnL sparkline
  ctx.save()
  ctx.beginPath()
  ctx.rect(x + 8, y + 88, w - 16, h - 95)
  ctx.clip()
  ctx.strokeStyle = s.totalPnl >= 0 ? '#a6df55' : '#ff6557'
  ctx.lineWidth = 1.5
  ctx.beginPath()
  for (let i = 0; i < 40; i++) {
    const px = x + 9 + i * ((w - 20) / 39)
    const py = y + h - 5 - Math.sin(i * 0.3 + phase * 2) * 8 + (s.totalPnl / 100) * Math.sin(i * 0.1)
    if (i) ctx.lineTo(px, py)
    else ctx.moveTo(px, py)
  }
  ctx.stroke()
  ctx.restore()
}

function drawMarketMonitor(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, s: BotState, time: number, phase: number) {
  ctx.fillStyle = '#0a0e0f'
  ctx.fillRect(x, y, w, h)
  ctx.strokeStyle = '#292d2d'
  ctx.strokeRect(x + 0.5, y + 0.5, w, h)
  ctx.fillStyle = '#696d68'
  ctx.font = '9px "JetBrains Mono", Consolas, monospace'
  ctx.textAlign = 'left'
  ctx.fillText('MARKET MONITOR / REYA LIVE PRICES', x + 11, y + 15)

  ctx.save()
  ctx.beginPath()
  ctx.rect(x + 8, y + 19, w - 16, h - 25)
  ctx.clip()

  for (let i = 0; i < s.markets.length && i < 4; i++) {
    const m = s.markets[i]
    const rowY = y + 30 + i * 24
    ctx.fillStyle = m.price >= m.prevPrice ? '#a6df55' : '#ff6557'
    ctx.font = 'bold 9px "JetBrains Mono", Consolas, monospace'
    ctx.textAlign = 'left'
    ctx.fillText(m.symbol, x + 12, rowY)
    ctx.fillStyle = '#c4c4bd'
    ctx.font = '9px "JetBrains Mono", Consolas, monospace'
    ctx.textAlign = 'right'
    ctx.fillText('$' + m.price.toFixed(2), x + w - 60, rowY)
    ctx.fillStyle = m.change24h >= 0 ? '#a6df55' : '#ff6557'
    ctx.font = '8px "JetBrains Mono", Consolas, monospace'
    ctx.fillText((m.change24h >= 0 ? '+' : '') + m.change24h.toFixed(2) + '%', x + w - 12, rowY)

    // Mini sparkline
    ctx.strokeStyle = m.price >= m.prevPrice ? '#a6df5540' : '#ff655740'
    ctx.lineWidth = 1
    ctx.beginPath()
    for (let j = 0; j < 20; j++) {
      const px = x + 100 + j * 4
      const py = rowY - 3 - Math.sin(j * 0.5 + phase + i) * 4
      if (j) ctx.lineTo(px, py)
      else ctx.moveTo(px, py)
    }
    ctx.stroke()
  }
  ctx.restore()
}

function drawClock(ctx: CanvasRenderingContext2D, x: number, y: number, time: number) {
  const now = new Date()
  const seconds = now.getSeconds() + now.getMilliseconds() / 1000
  const minutes = now.getMinutes() + seconds / 60
  const hours = now.getHours() % 12 + minutes / 60

  ctx.fillStyle = '#d7d2b9'
  ctx.beginPath()
  ctx.arc(x, y, 34, 0, Math.PI * 2)
  ctx.fill()
  ctx.strokeStyle = '#534936'
  ctx.lineWidth = 6
  ctx.stroke()

  for (let i = 0; i < 12; i++) {
    const angle = (i * Math.PI) / 6
    ctx.fillStyle = '#665d49'
    ctx.fillRect(x + Math.sin(angle) * 25 - 1, y - Math.cos(angle) * 25 - 1, 3, 3)
  }

  function hand(angle: number, length: number, width: number, color: string) {
    line(ctx, x, y, x + Math.sin(angle) * length, y - Math.cos(angle) * length, color, width)
  }

  hand((hours * Math.PI) / 6, 17, 4, '#332c20')
  hand((minutes * Math.PI) / 30, 24, 2.5, '#332c20')
  hand((seconds * Math.PI) / 30, 26, 1, '#ff6557')

  ctx.fillStyle = '#665d49'
  ctx.font = '8px "JetBrains Mono", Consolas, monospace'
  ctx.textAlign = 'center'
  ctx.fillText('UTC', x, y + 50)
}

function drawDesk(ctx: CanvasRenderingContext2D, cx: number, cy: number, label: string, color: string, time: number) {
  const x = cx - 112
  const y = cy - 72
  const w = 224

  // Desk surface
  ctx.fillStyle = '#211b15'
  ctx.fillRect(x, y, w, 58)
  ctx.strokeStyle = '#493928'
  ctx.strokeRect(x + 0.5, y + 0.5, w, 58)

  // Desk edge
  ctx.fillStyle = '#4a3724'
  ctx.fillRect(x - 6, y + 41, w + 12, 10)
  ctx.fillStyle = '#2c2118'
  ctx.fillRect(x, y + 51, w, 17)

  // Books/decor
  for (let i = 0; i < 6; i++) {
    ctx.fillStyle = i % 2 ? color : '#6b5740'
    ctx.fillRect(x + 10 + i * 31, y + 26 - (i % 3) * 5, 20, 5 + (i % 3) * 5)
  }

  // Monitor
  ctx.fillStyle = '#111719'
  ctx.fillRect(x + 15, y - 7, 59, 33)
  ctx.strokeStyle = '#2c3738'
  ctx.strokeRect(x + 15.5, y - 6.5, 59, 33)
  ctx.fillStyle = color
  ctx.fillRect(x + 22, y + 1, 39, 4)
  // Moving scanline on monitor
  ctx.fillRect(x + 22 + ((time / 35) % 36), y + 8, 2, 12)

  // Coffee mug
  ctx.fillStyle = '#17130f'
  ctx.fillRect(x + w - 68, y + 4, 53, 30)
  ctx.fillStyle = '#d0cbc0'
  ctx.fillRect(x + w - 61, y + 9, 15, 17)
  ctx.fillStyle = '#8f8b82'
  ctx.fillRect(x + w - 40, y + 13, 18, 13)

  // Label
  ctx.fillStyle = '#080908'
  ctx.fillRect(cx - 55, cy + 15, 110, 17)
  ctx.fillStyle = '#c4c4bd'
  ctx.font = 'bold 10px "JetBrains Mono", Consolas, monospace'
  ctx.textAlign = 'center'
  ctx.fillText(label, cx, cy + 27)
  ctx.textAlign = 'left'
}

function drawServerRack(ctx: CanvasRenderingContext2D, x: number, y: number) {
  ctx.fillStyle = '#101415'
  ctx.fillRect(x - 25, y, 38, 170)
  ctx.strokeStyle = '#333b3b'
  ctx.strokeRect(x - 24.5, y + 0.5, 38, 170)
  for (let i = 0; i < 13; i++) {
    ctx.fillStyle = i % 3 === 0 ? '#b9e65a' : '#263232'
    ctx.fillRect(x - 18, y + 9 + i * 12, 5, 3)
    ctx.fillStyle = '#343b3c'
    ctx.fillRect(x - 7, y + 9 + i * 12, 13, 3)
  }
}

function drawPlant(ctx: CanvasRenderingContext2D, x: number, y: number) {
  ctx.fillStyle = '#4a3524'
  ctx.fillRect(x - 10, y + 24, 20, 18)
  ctx.fillStyle = '#314b32'
  ctx.fillRect(x - 3, y, 7, 28)
  ctx.fillRect(x - 16, y + 5, 14, 7)
  ctx.fillRect(x + 3, y + 10, 16, 7)
  ctx.fillRect(x - 11, y - 6, 10, 9)
}

function drawAgent(ctx: CanvasRenderingContext2D, agent: Agent, time: number, phase: number, selected: boolean) {
  const x = agent.x
  const y = agent.y

  // Shadow
  ctx.fillStyle = 'rgba(0,0,0,0.3)'
  ctx.beginPath()
  ctx.ellipse(x, y + 18, 14, 4, 0, 0, Math.PI * 2)
  ctx.fill()

  // Selection ring
  if (selected) {
    ctx.strokeStyle = agent.color
    ctx.lineWidth = 2
    ctx.setLineDash([4, 4])
    ctx.lineDashOffset = -time / 50
    ctx.beginPath()
    ctx.arc(x, y, 22, 0, Math.PI * 2)
    ctx.stroke()
    ctx.setLineDash([])
  }

  // Body
  ctx.fillStyle = agent.color
  ctx.beginPath()
  ctx.arc(x, y, 10, 0, Math.PI * 2)
  ctx.fill()
  ctx.strokeStyle = '#080908'
  ctx.lineWidth = 2
  ctx.stroke()

  // Inner dot
  ctx.fillStyle = '#080908'
  ctx.beginPath()
  ctx.arc(x, y, 4, 0, Math.PI * 2)
  ctx.fill()

  // State indicator
  const stateColors: Record<string, string> = {
    ACTIVE: '#a6df55',
    IDLE: '#696d68',
    THINKING: '#f1bd57',
    EXECUTING: '#52d5c9',
    ERROR: '#ff6557',
  }
  ctx.fillStyle = stateColors[agent.state] || '#696d68'
  ctx.beginPath()
  ctx.arc(x + 8, y - 8, 3, 0, Math.PI * 2)
  ctx.fill()

  // Pulsing aura when executing
  if (agent.state === 'EXECUTING') {
    const pulse = 0.5 + Math.sin(phase * 4) * 0.5
    ctx.strokeStyle = agent.color + Math.floor(pulse * 80).toString(16).padStart(2, '0')
    ctx.lineWidth = 2
    ctx.beginPath()
    ctx.arc(x, y, 14 + pulse * 4, 0, Math.PI * 2)
    ctx.stroke()
  }

  // Name label
  ctx.fillStyle = '#080908'
  ctx.fillRect(x - 28, y + 14, 56, 12)
  ctx.fillStyle = agent.color
  ctx.font = 'bold 8px "JetBrains Mono", Consolas, monospace'
  ctx.textAlign = 'center'
  ctx.fillText(agent.name, x, y + 22)
  ctx.textAlign = 'left'
}
