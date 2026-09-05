import { createClient } from "npm:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Client-Info, Apikey",
};

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

interface StrategyDecision {
  action: "BUY" | "SELL" | "HOLD" | "CLOSE";
  symbol: string;
  qty: number;
  reason: string;
  limitPx?: number;
}

interface PriceHistory {
  symbol: string;
  prices: number[];
  timestamps: number[];
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 200, headers: corsHeaders });
  }

  try {
    const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY);

    const { data: config } = await supabase
      .from("bot_config")
      .select("*")
      .eq("id", 1)
      .maybeSingle();

    const apiUrl = config?.api_url || "https://api.reya.xyz/v2";

    // Fetch live market data
    const [marketsRes, pricesRes, summaryRes] = await Promise.all([
      fetch(`${apiUrl}/marketDefinitions`),
      fetch(`${apiUrl}/prices`),
      fetch(`${apiUrl}/marketsSummary`),
    ]);

    const markets = marketsRes.ok ? await marketsRes.json() : [];
    const prices = pricesRes.ok ? await pricesRes.json() : [];
    const summary = summaryRes.ok ? await summaryRes.json() : [];

    // Fetch enabled strategies
    const { data: strategies } = await supabase
      .from("bot_strategies")
      .select("*")
      .eq("is_enabled", true);

    if (!strategies || strategies.length === 0) {
      return new Response(
        JSON.stringify({ status: "no_strategies", message: "No enabled strategies" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Fetch paper accounts
    const { data: accounts } = await supabase
      .from("paper_accounts")
      .select("*");

    // Ensure all strategies have paper accounts
    for (const strat of strategies) {
      if (!accounts?.find((a: { id: string }) => a.id === strat.id)) {
        await supabase.from("paper_accounts").insert({
          id: strat.id,
          strategy_name: strat.name,
          starting_balance: 500,
          cash_balance: 500,
        });
      }
    }

    // Refetch accounts after potential inserts
    const { data: allAccounts } = await supabase
      .from("paper_accounts")
      .select("*");
    const accountMap = new Map<string, Record<string, unknown>>(
      (allAccounts || []).map((a: Record<string, unknown>) => [a.id as string, a])
    );

    // Fetch open paper positions
    const { data: openPositions } = await supabase
      .from("paper_positions")
      .select("*")
      .eq("status", "OPEN");

    // Fetch recent paper trades for price history
    const { data: recentTrades } = await supabase
      .from("paper_trades")
      .select("*")
      .order("timestamp", { ascending: false })
      .limit(200);

    // Build price history per symbol from trade prices + current prices
    const priceHistories = new Map<string, PriceHistory>();
    for (const trade of (recentTrades || [])) {
      const t = trade as { symbol: string; price: number; timestamp: number };
      let hist = priceHistories.get(t.symbol);
      if (!hist) {
        hist = { symbol: t.symbol, prices: [], timestamps: [] };
        priceHistories.set(t.symbol, hist);
      }
      hist.prices.unshift(t.price);
      hist.timestamps.unshift(t.timestamp);
    }

    const results: Record<string, unknown> = {};
    const now = Date.now();

    for (const strategy of strategies) {
      const symbol = strategy.symbol;
      const params = strategy.params || {};
      const account = accountMap.get(strategy.id);

      const priceData = prices.find((p: { symbol: string }) => p.symbol === symbol);
      const summaryData = summary.find((s: { symbol: string }) => s.symbol === symbol);
      const currentPrice = priceData?.price || summaryData?.markPrice || 0;

      if (!currentPrice) {
        results[strategy.id] = { action: "HOLD", reason: "No price data" };
        continue;
      }

      // Add current price to history
      let hist = priceHistories.get(symbol);
      if (!hist) {
        hist = { symbol, prices: [], timestamps: [] };
        priceHistories.set(symbol, hist);
      }
      hist.prices.push(currentPrice);
      hist.timestamps.push(now);

      const stratPositions = (openPositions || []).filter(
        (p: { strategy_id: string }) => p.strategy_id === strategy.id
      );
      const stratTrades = (recentTrades || []).filter(
        (t: { strategy_id: string }) => t.strategy_id === strategy.id
      );

      const decision = evaluateStrategy(
        strategy.id,
        symbol,
        currentPrice,
        params,
        hist.prices,
        stratTrades as Array<{ timestamp: number; price: number; side: string }>,
        stratPositions as Array<{ id: string; side: string; size: number; entry_price: number }>,
        Number(account?.cash_balance || 500),
      );

      results[strategy.id] = {
        action: decision.action,
        symbol: decision.symbol,
        qty: decision.qty,
        reason: decision.reason,
        price: currentPrice,
        balance: Number(account?.cash_balance || 500),
      };

      // Execute paper trade
      if (decision.action !== "HOLD" && decision.qty > 0) {
        const fillPrice = decision.limitPx || currentPrice;
        const tradeSide = decision.action === "BUY" ? "BUY" : "SELL";
        const isClose = decision.action === "CLOSE";

        let realizedPnl = 0;
        let tradeAction = "OPEN";

        if (isClose && stratPositions.length > 0) {
          // Close all open positions for this strategy
          tradeAction = "CLOSE";
          for (const pos of stratPositions) {
            const p = pos as { id: string; side: string; size: number; entry_price: number };
            const closePrice = fillPrice;
            if (p.side === "LONG") {
              realizedPnl += (closePrice - p.entry_price) * p.size;
            } else {
              realizedPnl += (p.entry_price - closePrice) * p.size;
            }
            await supabase.from("paper_positions").update({
              status: "CLOSED",
              closed_at: now,
              realized_pnl: realizedPnl,
              current_price: closePrice,
            }).eq("id", p.id);
          }
        } else if (decision.action === "BUY" || decision.action === "SELL") {
          // Check if we have an existing position to close/flip
          const existingPos = stratPositions[0] as { id: string; side: string; size: number; entry_price: number } | undefined;
          if (existingPos) {
            const wantSide = decision.action === "BUY" ? "LONG" : "SHORT";
            if (existingPos.side !== wantSide) {
              // Close existing position first
              tradeAction = "CLOSE";
              if (existingPos.side === "LONG") {
                realizedPnl += (fillPrice - existingPos.entry_price) * existingPos.size;
              } else {
                realizedPnl += (existingPos.entry_price - fillPrice) * existingPos.size;
              }
              await supabase.from("paper_positions").update({
                status: "CLOSED",
                closed_at: now,
                realized_pnl: realizedPnl,
                current_price: fillPrice,
              }).eq("id", existingPos.id);

              // Then open new position
              const cost = decision.qty * fillPrice;
              const cashBal = Number(account?.cash_balance || 500);
              if (cost <= cashBal) {
                await supabase.from("paper_positions").insert({
                  strategy_id: strategy.id,
                  symbol,
                  side: wantSide,
                  size: decision.qty,
                  entry_price: fillPrice,
                  current_price: currentPrice,
                  unrealized_pnl: 0,
                  status: "OPEN",
                  opened_at: now,
                });
                // Record the open trade too
                await supabase.from("paper_trades").insert({
                  strategy_id: strategy.id,
                  strategy_name: strategy.name,
                  symbol,
                  side: tradeSide,
                  qty: decision.qty,
                  price: fillPrice,
                  pnl: 0,
                  status: "FILLED",
                  action: "OPEN",
                  reason: decision.reason,
                  timestamp: now,
                });
              }
            } else {
              // Add to existing position (skip for simplicity in paper mode)
              results[strategy.id].action = "HOLD";
              results[strategy.id].reason = `${strategy.id}: already in ${wantSide} position`;
            }
          } else {
            // Open new position
            const cost = decision.qty * fillPrice;
            const cashBal = Number(account?.cash_balance || 500);
            if (cost <= cashBal) {
              await supabase.from("paper_positions").insert({
                strategy_id: strategy.id,
                symbol,
                side: decision.action === "BUY" ? "LONG" : "SHORT",
                size: decision.qty,
                entry_price: fillPrice,
                current_price: currentPrice,
                unrealized_pnl: 0,
                status: "OPEN",
                opened_at: now,
              });
            } else {
              results[strategy.id].action = "HOLD";
              results[strategy.id].reason = "Insufficient paper balance";
            }
          }
        }

        // Record the trade
        await supabase.from("paper_trades").insert({
          strategy_id: strategy.id,
          strategy_name: strategy.name,
          symbol,
          side: tradeSide,
          qty: decision.qty,
          price: fillPrice,
          pnl: realizedPnl,
          status: "FILLED",
          action: tradeAction,
          reason: decision.reason,
          timestamp: now,
        });

        // Update paper account
        const acc = accountMap.get(strategy.id);
        if (acc) {
          const oldCash = Number(acc.cash_balance);
          const oldRealized = Number(acc.realized_pnl);
          const oldTrades = Number(acc.total_trades);
          const oldWins = Number(acc.winning_trades);

          let newCash = oldCash;
          if (tradeAction === "OPEN") {
            newCash = oldCash - decision.qty * fillPrice;
          } else if (tradeAction === "CLOSE") {
            newCash = oldCash + realizedPnl + (stratPositions as Array<{ size: number; entry_price: number }>).reduce(
              (sum, p) => sum + p.size * p.entry_price, 0
            );
          }

          const newRealized = oldRealized + realizedPnl;
          const newTrades = oldTrades + 1;
          const newWins = oldWins + (realizedPnl > 0 ? 1 : 0);

          await supabase.from("paper_accounts").update({
            cash_balance: newCash,
            realized_pnl: newRealized,
            total_trades: newTrades,
            winning_trades: newWins,
            updated_at: new Date().toISOString(),
          }).eq("id", strategy.id);

          accountMap.get(strategy.id)!.cash_balance = newCash;
          accountMap.get(strategy.id)!.realized_pnl = newRealized;
          accountMap.get(strategy.id)!.total_trades = newTrades;
        }
      }

      // Update unrealized PnL for open positions
      const updatedPositions = (openPositions || []).filter(
        (p: { strategy_id: string }) => p.strategy_id === strategy.id
      );
      let totalUnrealized = 0;
      let totalPosValue = 0;
      for (const pos of updatedPositions) {
        const p = pos as { id: string; side: string; size: number; entry_price: number };
        let uPnl = 0;
        if (p.side === "LONG") {
          uPnl = (currentPrice - p.entry_price) * p.size;
        } else {
          uPnl = (p.entry_price - currentPrice) * p.size;
        }
        totalUnrealized += uPnl;
        totalPosValue += p.size * currentPrice;
        await supabase.from("paper_positions").update({
          current_price: currentPrice,
          unrealized_pnl: uPnl,
        }).eq("id", p.id);
      }

      // Update total PnL in account
      const acc = accountMap.get(strategy.id);
      if (acc) {
        const totalPnl = Number(acc.realized_pnl) + totalUnrealized;
        await supabase.from("paper_accounts").update({
          unrealized_pnl: totalUnrealized,
          position_value: totalPosValue,
          total_pnl: totalPnl,
        }).eq("id", strategy.id);
      }
    }

    // Compute totals
    let grandPnl = 0;
    let grandTrades = 0;
    for (const acc of accountMap.values()) {
      grandPnl += Number(acc.total_pnl || acc.realized_pnl || 0);
      grandTrades += Number(acc.total_trades || 0);
    }

    return new Response(
      JSON.stringify({
        status: "ok",
        cycle: now,
        strategies: results,
        totalPnl: grandPnl,
        totalTrades: grandTrades,
        marketCount: markets.length,
        priceCount: prices.length,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(
      JSON.stringify({ error: err instanceof Error ? err.message : "Unknown error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});

function evaluateStrategy(
  strategyId: string,
  symbol: string,
  currentPrice: number,
  params: Record<string, unknown>,
  priceHistory: number[],
  recentTrades: Array<{ timestamp: number; price: number; side: string }>,
  openPositions: Array<{ id: string; side: string; size: number; entry_price: number }>,
  cashBalance: number,
): StrategyDecision {
  const lastTrade = recentTrades[0];
  const timeSinceLastTrade = lastTrade ? Date.now() - lastTrade.timestamp : Infinity;
  const minCooldownMs = 15000;
  const hasOpen = openPositions.length > 0;

  switch (strategyId) {
    case "momentum": {
      const emaFastPeriod = Number(params.ema_fast) || 10;
      const emaSlowPeriod = Number(params.ema_slow) || 30;
      const size = Number(params.size) || 0.1;

      if (timeSinceLastTrade < minCooldownMs) {
        return { action: "HOLD", symbol, qty: 0, reason: "Cooldown active" };
      }

      if (priceHistory.length < 5) {
        // Seed initial position
        if (!hasOpen && Math.random() < 0.4) {
          const action = Math.random() > 0.5 ? "BUY" : "SELL";
          return { action, symbol, qty: size, reason: "Momentum: initial position (building history)" };
        }
        return { action: "HOLD", symbol, qty: 0, reason: "Building price history" };
      }

      const fastEma = calcEma(priceHistory.slice(-emaFastPeriod));
      const slowEma = calcEma(priceHistory.slice(-emaSlowPeriod));

      if (hasOpen) {
        const pos = openPositions[0];
        // Exit on reversal
        if (pos.side === "LONG" && fastEma < slowEma) {
          return { action: "CLOSE", symbol, qty: pos.size, reason: `Momentum: exit LONG (fast ${fastEma.toFixed(2)} < slow ${slowEma.toFixed(2)})` };
        }
        if (pos.side === "SHORT" && fastEma > slowEma) {
          return { action: "CLOSE", symbol, qty: pos.size, reason: `Momentum: exit SHORT (fast ${fastEma.toFixed(2)} > slow ${slowEma.toFixed(2)})` };
        }
        return { action: "HOLD", symbol, qty: 0, reason: `Momentum: holding ${pos.side} (fast ${fastEma.toFixed(2)}, slow ${slowEma.toFixed(2)})` };
      }

      if (fastEma > slowEma * 1.0002) {
        return { action: "BUY", symbol, qty: size, reason: `Momentum: fast EMA ${fastEma.toFixed(2)} > slow EMA ${slowEma.toFixed(2)}` };
      }
      if (fastEma < slowEma * 0.9998) {
        return { action: "SELL", symbol, qty: size, reason: `Momentum: fast EMA ${fastEma.toFixed(2)} < slow EMA ${slowEma.toFixed(2)}` };
      }

      return { action: "HOLD", symbol, qty: 0, reason: `Momentum: no crossover (fast ${fastEma.toFixed(2)}, slow ${slowEma.toFixed(2)})` };
    }

    case "reversion": {
      const rsiPeriod = Number(params.rsi_period) || 14;
      const oversold = Number(params.oversold) || 30;
      const overbought = Number(params.overbought) || 70;
      const size = Number(params.size) || 0.1;

      if (timeSinceLastTrade < minCooldownMs) {
        return { action: "HOLD", symbol, qty: 0, reason: "Cooldown active" };
      }

      if (priceHistory.length < 5) {
        return { action: "HOLD", symbol, qty: 0, reason: "Building price history for RSI" };
      }

      const rsi = calcRsi(priceHistory.slice(-rsiPeriod));

      if (hasOpen) {
        const pos = openPositions[0];
        // Exit when RSI returns to midrange
        if (pos.side === "LONG" && rsi > 50) {
          return { action: "CLOSE", symbol, qty: pos.size, reason: `Reversion: exit LONG (RSI ${rsi.toFixed(1)} back to mid)` };
        }
        if (pos.side === "SHORT" && rsi < 50) {
          return { action: "CLOSE", symbol, qty: pos.size, reason: `Reversion: exit SHORT (RSI ${rsi.toFixed(1)} back to mid)` };
        }
        return { action: "HOLD", symbol, qty: 0, reason: `Reversion: holding ${pos.side} (RSI ${rsi.toFixed(1)})` };
      }

      if (rsi < oversold) {
        return { action: "BUY", symbol, qty: size, reason: `Reversion: RSI ${rsi.toFixed(1)} < oversold ${oversold}` };
      }
      if (rsi > overbought) {
        return { action: "SELL", symbol, qty: size, reason: `Reversion: RSI ${rsi.toFixed(1)} > overbought ${overbought}` };
      }

      return { action: "HOLD", symbol, qty: 0, reason: `Reversion: RSI ${rsi.toFixed(1)} within range` };
    }

    case "maker": {
      const spreadBps = Number(params.spread_bps) || 5;
      const orderSize = Number(params.order_size) || 0.05;

      if (timeSinceLastTrade < 5000) {
        return { action: "HOLD", symbol, qty: 0, reason: "Maker: refresh too soon" };
      }

      // Maker alternates between bid and ask
      const halfSpread = currentPrice * (spreadBps / 10000);
      if (Math.random() > 0.5) {
        return { action: "BUY", symbol, qty: orderSize, reason: `Maker: posting bid at ${(currentPrice - halfSpread).toFixed(2)}`, limitPx: currentPrice - halfSpread };
      } else {
        return { action: "SELL", symbol, qty: orderSize, reason: `Maker: posting ask at ${(currentPrice + halfSpread).toFixed(2)}`, limitPx: currentPrice + halfSpread };
      }
    }

    case "arbitrage": {
      const threshold = Number(params.spread_threshold) || 0.003;
      const maxSize = Number(params.max_size) || 0.2;

      // In paper mode, just scan — would need spot+perp price comparison
      return { action: "HOLD", symbol, qty: 0, reason: `Arbitrage: scanning for spread > ${(threshold * 100).toFixed(1)}%` };
    }

    case "risk": {
      // Risk manager closes positions if exposure too high
      if (openPositions.length > 3) {
        return { action: "CLOSE", symbol, qty: 0, reason: `Risk: reducing from ${openPositions.length} positions` };
      }
      return { action: "HOLD", symbol, qty: 0, reason: "Risk: exposure within limits" };
    }

    case "liquidator": {
      return { action: "HOLD", symbol, qty: 0, reason: "Liquidator: scanning protocol for liquidatable positions" };
    }

    default:
      return { action: "HOLD", symbol, qty: 0, reason: "Unknown strategy" };
  }
}

function calcEma(prices: number[]): number {
  if (prices.length === 0) return 0;
  const k = 2 / (prices.length + 1);
  let ema = prices[0];
  for (let i = 1; i < prices.length; i++) {
    ema = prices[i] * k + ema * (1 - k);
  }
  return ema;
}

function calcRsi(prices: number[]): number {
  if (prices.length < 2) return 50;
  let gains = 0;
  let losses = 0;
  for (let i = 1; i < prices.length; i++) {
    const change = prices[i] - prices[i - 1];
    if (change > 0) gains += change;
    else losses -= change;
  }
  const avgGain = gains / (prices.length - 1);
  const avgLoss = losses / (prices.length - 1);
  if (avgLoss === 0) return 100;
  const rs = avgGain / avgLoss;
  return 100 - 100 / (1 + rs);
}
