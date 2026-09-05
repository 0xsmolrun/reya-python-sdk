import { createClient } from "npm:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Client-Info, Apikey",
};

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const TRADING_SYMBOL = "ETHRUSDPERP";

interface StrategyDecision {
  action: "BUY" | "SELL" | "HOLD" | "CLOSE";
  symbol: string;
  qty: number;
  reason: string;
  limitPx?: number;
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

    // Fetch live market data (marketsSummary endpoint doesn't exist on this API)
    const [marketsRes, pricesRes] = await Promise.all([
      fetch(`${apiUrl}/marketDefinitions`),
      fetch(`${apiUrl}/prices`),
    ]);

    const markets = marketsRes.ok ? await marketsRes.json() : [];
    const prices = pricesRes.ok ? await pricesRes.json() : [];

    // Get current ETHRUSDPERP price
    // API returns oraclePrice/poolPrice as strings
    const priceData = prices.find((p: { symbol: string }) => p.symbol === TRADING_SYMBOL) as
      | { symbol: string; oraclePrice?: string; poolPrice?: string; price?: number }
      | undefined;
    const currentPrice = priceData
      ? Number(priceData.oraclePrice || priceData.poolPrice || priceData.price || 0)
      : 0;

    if (!currentPrice) {
      return new Response(
        JSON.stringify({ status: "no_price", message: "No live price for " + TRADING_SYMBOL }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Fetch all strategies
    const { data: strategies } = await supabase
      .from("bot_strategies")
      .select("*");

    if (!strategies || strategies.length === 0) {
      return new Response(
        JSON.stringify({ status: "no_strategies", message: "No strategies found" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Ensure all strategies have paper accounts
    const { data: existingAccounts } = await supabase
      .from("paper_accounts")
      .select("*");

    for (const strat of strategies) {
      if (!existingAccounts?.find((a: { id: string }) => a.id === strat.id)) {
        await supabase.from("paper_accounts").insert({
          id: strat.id,
          strategy_name: strat.name,
          starting_balance: 500,
          cash_balance: 500,
        });
      }
    }

    // Refetch accounts
    const { data: allAccountsRaw } = await supabase
      .from("paper_accounts")
      .select("*");
    const accountMap = new Map<string, Record<string, unknown>>();
    for (const a of (allAccountsRaw || [])) {
      const row = a as Record<string, unknown>;
      accountMap.set(row.id as string, {
        ...row,
        cash_balance: Number(row.cash_balance),
        realized_pnl: Number(row.realized_pnl),
        total_pnl: Number(row.total_pnl),
        total_trades: Number(row.total_trades),
        winning_trades: Number(row.winning_trades),
        position_value: Number(row.position_value),
        unrealized_pnl: Number(row.unrealized_pnl),
      });
    }

    // Fetch open positions
    const { data: openPositionsRaw } = await supabase
      .from("paper_positions")
      .select("*")
      .eq("status", "OPEN");
    const openPositions = (openPositionsRaw || []).map((p: Record<string, unknown>) => ({
      id: p.id as string,
      strategy_id: p.strategy_id as string,
      symbol: p.symbol as string,
      side: p.side as string,
      size: Number(p.size),
      entry_price: Number(p.entry_price),
      current_price: Number(p.current_price),
      unrealized_pnl: Number(p.unrealized_pnl),
    }));

    // Fetch recent trades for price history
    const { data: recentTradesRaw } = await supabase
      .from("paper_trades")
      .select("*")
      .order("timestamp", { ascending: false })
      .limit(500);

    const recentTrades = (recentTradesRaw || []).map((t: Record<string, unknown>) => ({
      id: t.id as string,
      strategy_id: t.strategy_id as string,
      symbol: t.symbol as string,
      side: t.side as string,
      qty: Number(t.qty),
      price: Number(t.price),
      timestamp: Number(t.timestamp),
      action: t.action as string,
      pnl: Number(t.pnl),
    }));

    // Build price history from all trades + current price
    const priceHistory: number[] = recentTrades
      .filter((t) => t.symbol === TRADING_SYMBOL)
      .map((t) => t.price)
      .reverse();
    priceHistory.push(currentPrice);

    // Keep last 100 prices
    const trimmedHistory = priceHistory.slice(-100);

    const results: Record<string, unknown> = {};
    const now = Date.now();
    const enabledStrategies = strategies.filter((s: Record<string, unknown>) => s.is_enabled !== false);

    for (const strategy of enabledStrategies) {
      const strat = strategy as Record<string, unknown>;
      const stratId = strat.id as string;
      const stratName = strat.name as string;
      const params = (strat.params as Record<string, unknown>) || {};
      const account = accountMap.get(stratId);

      const stratPositions = openPositions.filter((p) => p.strategy_id === stratId);
      const stratTrades = recentTrades.filter((t) => t.strategy_id === stratId);

      const decision = evaluateStrategy(
        stratId,
        TRADING_SYMBOL,
        currentPrice,
        params,
        trimmedHistory,
        stratTrades,
        stratPositions,
        Number(account?.cash_balance ?? 500),
      );

      results[stratId] = {
        action: decision.action,
        symbol: decision.symbol,
        qty: decision.qty,
        reason: decision.reason,
        price: currentPrice,
        balance: Number(account?.cash_balance ?? 500),
      };

      // Execute paper trade
      if (decision.action !== "HOLD" && decision.qty > 0) {
        const fillPrice = decision.limitPx || currentPrice;
        let realizedPnl = 0;
        let tradeAction = "OPEN";
        let tradeSide: "BUY" | "SELL" = decision.action === "BUY" ? "BUY" : "SELL";
        let didTrade = false;

        // Step 1: Handle closing existing positions (CLOSE action or position flip)
        if (decision.action === "CLOSE" && stratPositions.length > 0) {
          tradeAction = "CLOSE";
          for (const pos of stratPositions) {
            if (pos.side === "LONG") {
              realizedPnl += (fillPrice - pos.entry_price) * pos.size;
            } else {
              realizedPnl += (pos.entry_price - fillPrice) * pos.size;
            }
            // Credit: return original margin + PnL
            const margin = pos.size * pos.entry_price;
            const acc = accountMap.get(stratId);
            if (acc) {
              acc.cash_balance = Number(acc.cash_balance) + margin + realizedPnl;
            }
            await supabase.from("paper_positions").update({
              status: "CLOSED",
              closed_at: now,
              realized_pnl: realizedPnl,
              current_price: fillPrice,
            }).eq("id", pos.id);
          }
          tradeSide = stratPositions[0].side === "LONG" ? "SELL" : "BUY";
          didTrade = true;
        } else if ((decision.action === "BUY" || decision.action === "SELL") && stratPositions.length > 0) {
          const wantSide = decision.action === "BUY" ? "LONG" : "SHORT";
          const existingPos = stratPositions[0];

          if (existingPos.side !== wantSide) {
            // Close existing position first
            tradeAction = "CLOSE";
            if (existingPos.side === "LONG") {
              realizedPnl += (fillPrice - existingPos.entry_price) * existingPos.size;
              tradeSide = "SELL";
            } else {
              realizedPnl += (existingPos.entry_price - fillPrice) * existingPos.size;
              tradeSide = "BUY";
            }
            const margin = existingPos.size * existingPos.entry_price;
            const acc = accountMap.get(stratId);
            if (acc) {
              acc.cash_balance = Number(acc.cash_balance) + margin + realizedPnl;
            }
            await supabase.from("paper_positions").update({
              status: "CLOSED",
              closed_at: now,
              realized_pnl: realizedPnl,
              current_price: fillPrice,
            }).eq("id", existingPos.id);
            didTrade = true;

            // Now open new position in opposite direction
            const cost = decision.qty * fillPrice;
            const cashBal = Number(accountMap.get(stratId)?.cash_balance ?? 500);
            if (cost <= cashBal) {
              await supabase.from("paper_positions").insert({
                strategy_id: stratId,
                symbol: TRADING_SYMBOL,
                side: wantSide,
                size: decision.qty,
                entry_price: fillPrice,
                current_price: currentPrice,
                unrealized_pnl: 0,
                status: "OPEN",
                opened_at: now,
              });
              // Debit cost
              const acc2 = accountMap.get(stratId);
              if (acc2) {
                acc2.cash_balance = Number(acc2.cash_balance) - cost;
              }
              // Record open trade
              await supabase.from("paper_trades").insert({
                strategy_id: stratId,
                strategy_name: stratName,
                symbol: TRADING_SYMBOL,
                side: decision.action === "BUY" ? "BUY" : "SELL",
                qty: decision.qty,
                price: fillPrice,
                pnl: 0,
                status: "FILLED",
                action: "OPEN",
                reason: decision.reason,
                timestamp: now,
              });
              didTrade = true;
            }
          } else {
            // Already in same direction — skip
            results[stratId] = { action: "HOLD", reason: `${stratId}: already ${wantSide}`, qty: 0, symbol: TRADING_SYMBOL };
          }
        } else if ((decision.action === "BUY" || decision.action === "SELL") && stratPositions.length === 0) {
          // Open new position
          const wantSide = decision.action === "BUY" ? "LONG" : "SHORT";
          const cost = decision.qty * fillPrice;
          const cashBal = Number(accountMap.get(stratId)?.cash_balance ?? 500);
          if (cost <= cashBal) {
            await supabase.from("paper_positions").insert({
              strategy_id: stratId,
              symbol: TRADING_SYMBOL,
              side: wantSide,
              size: decision.qty,
              entry_price: fillPrice,
              current_price: currentPrice,
              unrealized_pnl: 0,
              status: "OPEN",
              opened_at: now,
            });
            // Debit cost
            const acc = accountMap.get(stratId);
            if (acc) {
              acc.cash_balance = Number(acc.cash_balance) - cost;
            }
            didTrade = true;
          } else {
            results[stratId] = { action: "HOLD", reason: "Insufficient paper balance", qty: 0, symbol: TRADING_SYMBOL };
          }
        }

        // Record the trade (for CLOSE or OPEN)
        if (didTrade && (tradeAction === "CLOSE" || tradeAction === "OPEN")) {
          await supabase.from("paper_trades").insert({
            strategy_id: stratId,
            strategy_name: stratName,
            symbol: TRADING_SYMBOL,
            side: tradeSide,
            qty: decision.qty,
            price: fillPrice,
            pnl: realizedPnl,
            status: "FILLED",
            action: tradeAction,
            reason: decision.reason,
            timestamp: now,
          });
        }

        // Update paper account stats
        if (didTrade) {
          const acc = accountMap.get(stratId);
          if (acc) {
            const oldRealized = Number(acc.realized_pnl);
            const oldTrades = Number(acc.total_trades);
            const oldWins = Number(acc.winning_trades);

            const newRealized = oldRealized + realizedPnl;
            const newTrades = oldTrades + 1;
            const newWins = oldWins + (realizedPnl > 0 ? 1 : 0);

            await supabase.from("paper_accounts").update({
              cash_balance: Number(acc.cash_balance),
              realized_pnl: newRealized,
              total_trades: newTrades,
              winning_trades: newWins,
              updated_at: new Date().toISOString(),
            }).eq("id", stratId);

            acc.realized_pnl = newRealized;
            acc.total_trades = newTrades;
          }
        }
      }

      // Update unrealized PnL for open positions
      let totalUnrealized = 0;
      let totalPosValue = 0;
      for (const pos of openPositions.filter((p) => p.strategy_id === stratId)) {
        let uPnl = 0;
        if (pos.side === "LONG") {
          uPnl = (currentPrice - pos.entry_price) * pos.size;
        } else {
          uPnl = (pos.entry_price - currentPrice) * pos.size;
        }
        totalUnrealized += uPnl;
        totalPosValue += pos.size * currentPrice;
        await supabase.from("paper_positions").update({
          current_price: currentPrice,
          unrealized_pnl: uPnl,
        }).eq("id", pos.id);
      }

      // Update total PnL
      const acc = accountMap.get(stratId);
      if (acc) {
        const totalPnl = Number(acc.realized_pnl) + totalUnrealized;
        await supabase.from("paper_accounts").update({
          unrealized_pnl: totalUnrealized,
          position_value: totalPosValue,
          total_pnl: totalPnl,
        }).eq("id", stratId);
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
        symbol: TRADING_SYMBOL,
        price: currentPrice,
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
  recentTrades: Array<{ timestamp: number; price: number; side: string; action: string }>,
  openPositions: Array<{ id: string; side: string; size: number; entry_price: number }>,
  cashBalance: number,
): StrategyDecision {
  const hasOpen = openPositions.length > 0;
  const lastTrade = recentTrades[0];
  const timeSinceLastTrade = lastTrade ? Date.now() - lastTrade.timestamp : Infinity;

  // Very short cooldown — just to prevent duplicate within same cycle
  const minCooldownMs = 2000;

  switch (strategyId) {
    case "momentum": {
      const emaFastPeriod = Math.min(Number(params.ema_fast) || 5, priceHistory.length);
      const emaSlowPeriod = Math.min(Number(params.ema_slow) || 15, priceHistory.length);
      const size = Number(params.size) || 0.2;

      if (timeSinceLastTrade < minCooldownMs) {
        return { action: "HOLD", symbol, qty: 0, reason: "Cooldown" };
      }

      if (priceHistory.length < 3) {
        // Not enough data — take a small initial position to start trading
        if (!hasOpen) {
          return { action: "BUY", symbol, qty: size, reason: "Momentum: initial position (building history)" };
        }
        return { action: "HOLD", symbol, qty: 0, reason: "Building history" };
      }

      const fastEma = calcEma(priceHistory.slice(-Math.max(emaFastPeriod, 2)));
      const slowEma = calcEma(priceHistory.slice(-Math.max(emaSlowPeriod, 3)));

      // If we have a position, check for exit
      if (hasOpen) {
        const pos = openPositions[0];
        if (pos.side === "LONG" && fastEma < slowEma) {
          return { action: "CLOSE", symbol, qty: pos.size, reason: `Momentum: exit LONG (fast ${fastEma.toFixed(2)} < slow ${slowEma.toFixed(2)})` };
        }
        if (pos.side === "SHORT" && fastEma > slowEma) {
          return { action: "CLOSE", symbol, qty: pos.size, reason: `Momentum: exit SHORT (fast ${fastEma.toFixed(2)} > slow ${slowEma.toFixed(2)})` };
        }
        return { action: "HOLD", symbol, qty: 0, reason: `Momentum: holding ${pos.side} (fast ${fastEma.toFixed(2)}, slow ${slowEma.toFixed(2)})` };
      }

      // No position — look for entry
      if (fastEma > slowEma) {
        return { action: "BUY", symbol, qty: size, reason: `Momentum: fast EMA ${fastEma.toFixed(2)} > slow EMA ${slowEma.toFixed(2)}` };
      }
      if (fastEma < slowEma) {
        return { action: "SELL", symbol, qty: size, reason: `Momentum: fast EMA ${fastEma.toFixed(2)} < slow EMA ${slowEma.toFixed(2)}` };
      }

      return { action: "HOLD", symbol, qty: 0, reason: `Momentum: no signal` };
    }

    case "reversion": {
      const rsiPeriod = Math.min(Number(params.rsi_period) || 7, priceHistory.length);
      const oversold = Number(params.oversold) || 40;
      const overbought = Number(params.overbought) || 60;
      const size = Number(params.size) || 0.05;

      if (timeSinceLastTrade < minCooldownMs) {
        return { action: "HOLD", symbol, qty: 0, reason: "Cooldown" };
      }

      if (priceHistory.length < 3) {
        if (!hasOpen) {
          return { action: "SELL", symbol, qty: size, reason: "Reversion: initial short (building history)" };
        }
        return { action: "HOLD", symbol, qty: 0, reason: "Building RSI history" };
      }

      const rsi = calcRsi(priceHistory.slice(-Math.max(rsiPeriod, 2)));

      if (hasOpen) {
        const pos = openPositions[0];
        // Exit when RSI crosses midline
        if (pos.side === "LONG" && rsi > 50) {
          return { action: "CLOSE", symbol, qty: pos.size, reason: `Reversion: exit LONG (RSI ${rsi.toFixed(1)} > 50)` };
        }
        if (pos.side === "SHORT" && rsi < 50) {
          return { action: "CLOSE", symbol, qty: pos.size, reason: `Reversion: exit SHORT (RSI ${rsi.toFixed(1)} < 50)` };
        }
        return { action: "HOLD", symbol, qty: 0, reason: `Reversion: holding ${pos.side} (RSI ${rsi.toFixed(1)})` };
      }

      if (rsi < oversold) {
        return { action: "BUY", symbol, qty: size, reason: `Reversion: RSI ${rsi.toFixed(1)} < oversold ${oversold}` };
      }
      if (rsi > overbought) {
        return { action: "SELL", symbol, qty: size, reason: `Reversion: RSI ${rsi.toFixed(1)} > overbought ${overbought}` };
      }

      return { action: "HOLD", symbol, qty: 0, reason: `Reversion: RSI ${rsi.toFixed(1)} neutral` };
    }

    case "maker": {
      const spreadBps = Number(params.spread_bps) || 3;
      const orderSize = Number(params.order_size) || 0.05;

      if (timeSinceLastTrade < 1000) {
        return { action: "HOLD", symbol, qty: 0, reason: "Maker: too soon" };
      }

      // Maker: if has position, close it at opposite side; if no position, open at bid/ask
      if (hasOpen) {
        const pos = openPositions[0];
        const halfSpread = currentPrice * (spreadBps / 10000);
        // Close at favorable price
        if (pos.side === "LONG") {
          return { action: "SELL", symbol, qty: pos.size, reason: `Maker: closing LONG at ask ${(currentPrice + halfSpread).toFixed(2)}`, limitPx: currentPrice + halfSpread };
        } else {
          return { action: "BUY", symbol, qty: pos.size, reason: `Maker: closing SHORT at bid ${(currentPrice - halfSpread).toFixed(2)}`, limitPx: currentPrice - halfSpread };
        }
      }

      // No position — post bid or ask
      const halfSpread = currentPrice * (spreadBps / 10000);
      if (Math.random() > 0.5) {
        return { action: "BUY", symbol, qty: orderSize, reason: `Maker: bid at ${(currentPrice - halfSpread).toFixed(2)}`, limitPx: currentPrice - halfSpread };
      } else {
        return { action: "SELL", symbol, qty: orderSize, reason: `Maker: ask at ${(currentPrice + halfSpread).toFixed(2)}`, limitPx: currentPrice + halfSpread };
      }
    }

    case "arbitrage": {
      // In paper mode, simulate spread detection
      const threshold = Number(params.spread_threshold) || 0.001;
      const size = Number(params.max_size) || 0.3;

      // Use price history to detect volatility as a proxy for spread
      if (priceHistory.length < 3) {
        if (!hasOpen) {
          return { action: "BUY", symbol, qty: size, reason: "Arbitrage: initial position" };
        }
        return { action: "HOLD", symbol, qty: 0, reason: "Arbitrage: building history" };
      }

      const recentChange = Math.abs(currentPrice - priceHistory[priceHistory.length - 2]) / currentPrice;
      if (recentChange > threshold) {
        // Price moved enough — take position in direction of move
        const wentUp = currentPrice > priceHistory[priceHistory.length - 2];
        if (hasOpen) {
          const pos = openPositions[0];
          // Close on any significant move
          return { action: "CLOSE", symbol, qty: pos.size, reason: `Arbitrage: spread ${(recentChange * 100).toFixed(2)}% > threshold, closing ${pos.side}` };
        }
        return { action: wentUp ? "BUY" : "SELL", symbol, qty: size, reason: `Arbitrage: spread ${(recentChange * 100).toFixed(2)}% detected` };
      }

      return { action: "HOLD", symbol, qty: 0, reason: `Arbitrage: spread ${(recentChange * 100).toFixed(3)}% < threshold ${(threshold * 100).toFixed(1)}%` };
    }

    case "risk": {
      // Risk manager: takes positions when exposure is low, closes when high
      if (openPositions.length > 2) {
        return { action: "CLOSE", symbol, qty: 0, reason: `Risk: reducing from ${openPositions.length} positions` };
      }

      if (timeSinceLastTrade < 3000) {
        return { action: "HOLD", symbol, qty: 0, reason: "Risk: cooldown" };
      }

      if (!hasOpen) {
        // Take a small position to participate
        const size = 0.05;
        return { action: Math.random() > 0.5 ? "BUY" : "SELL", symbol, qty: size, reason: "Risk: opening hedge position" };
      }

      return { action: "HOLD", symbol, qty: 0, reason: "Risk: exposure within limits" };
    }

    case "liquidator": {
      // Simulate liquidation scanning — trade on large price moves
      if (priceHistory.length < 3) {
        if (!hasOpen) {
          return { action: "BUY", symbol, qty: 0.05, reason: "Liquidator: initial scan position" };
        }
        return { action: "HOLD", symbol, qty: 0, reason: "Liquidator: scanning" };
      }

      const recentChange = Math.abs(currentPrice - priceHistory[priceHistory.length - 2]) / currentPrice;
      if (recentChange > 0.002 && !hasOpen) {
        const wentUp = currentPrice > priceHistory[priceHistory.length - 2];
        return { action: wentUp ? "BUY" : "SELL", symbol, qty: 0.05, reason: `Liquidator: large move ${(recentChange * 100).toFixed(2)}% detected` };
      }

      if (hasOpen && recentChange < 0.0005) {
        const pos = openPositions[0];
        return { action: "CLOSE", symbol, qty: pos.size, reason: "Liquidator: calmed down, closing scan position" };
      }

      return { action: "HOLD", symbol, qty: 0, reason: "Liquidator: scanning for liquidatable positions" };
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
