import { createClient } from '@supabase/supabase-js'

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL as string
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string

export const supabase = createClient(supabaseUrl, supabaseAnonKey)

export interface DbAgent {
  id: string
  name: string
  role: string
  strategy: string
  description: string
  color: string
  state: string
  task: string
  thought: string
  pnl: number
  trades_count: number
  win_rate: number
  x: number
  y: number
  updated_at: string
}

export interface DbTrade {
  id: string
  agent_id: string
  agent_name: string
  symbol: string
  side: string
  qty: number
  price: number
  pnl: number
  status: string
  timestamp: number
}

export interface DbMarket {
  id: string
  type: string
  price: number
  prev_price: number
  change_24h: number
  volume_24h: number
  updated_at: string
}

export interface DbState {
  id: number
  total_pnl: number
  total_trades: number
  cycle_id: number
  connected: boolean
  last_update: number
}

export async function syncAgentsToDb(agents: DbAgent[]) {
  const { error } = await supabase.from('bot_agents').upsert(agents, { onConflict: 'id' })
  if (error) console.error('Failed to sync agents:', error.message)
}

export async function syncTradesToDb(trades: DbTrade[]) {
  if (trades.length === 0) return
  const { error } = await supabase.from('bot_trades').upsert(trades, { onConflict: 'id' })
  if (error) console.error('Failed to sync trades:', error.message)
}

export async function syncMarketsToDb(markets: DbMarket[]) {
  const { error } = await supabase.from('bot_markets').upsert(markets, { onConflict: 'id' })
  if (error) console.error('Failed to sync markets:', error.message)
}

export async function syncStateToDb(state: DbState) {
  const { error } = await supabase.from('bot_state').upsert(state, { onConflict: 'id' })
  if (error) console.error('Failed to sync state:', error.message)
}

export async function fetchInitialData() {
  const [agentsRes, tradesRes, marketsRes, stateRes] = await Promise.all([
    supabase.from('bot_agents').select('*'),
    supabase.from('bot_trades').select('*').order('timestamp', { ascending: false }).limit(50),
    supabase.from('bot_markets').select('*'),
    supabase.from('bot_state').select('*').eq('id', 1).maybeSingle(),
  ])

  return {
    agents: agentsRes.data || [],
    trades: tradesRes.data || [],
    markets: marketsRes.data || [],
    state: stateRes.data,
  }
}
