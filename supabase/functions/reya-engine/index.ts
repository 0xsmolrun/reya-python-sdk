import { createClient } from "npm:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Client-Info, Apikey",
};

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

interface PricePoint {
  symbol: string;
  price: number;
  timestamp: number;
}

interface StrategyDecision {
  action: "BUY" | "SELL" | "HOLD" | "CLOSE";
  symbol: string;
  qty: number;
  reason: string;
  orderType: "LIMIT_IOC" | "LIMIT_GTC";
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

    if (!config?.is_active) {
      return new Response(
        JSON.stringify({ status: "inactive", message: "Bot engine is not active" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    if (!config?.account_id || !config?.wallet_address) {
      return new Response(
        JSON.stringify({ status: "not_configured", message: "Wallet and account not set" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const apiUrl = config.api_url;

    // Fetch live market data
    const [marketsRes, pricesRes, summaryRes] = await Promise.all([
      fetch(`${apiUrl}/marketDefinitions`),
      fetch(`${apiUrl}/prices`),
      fetch(`${apiUrl}/marketsSummary`),
    ]);

    const markets = marketsRes.ok ? await marketsRes.json() : [];
    const prices = pricesRes.ok ? await pricesRes.json() : [];
    const summary = summaryRes.ok ? await summaryRes.json() : [];

    // Fetch wallet positions
    const positionsRes = await fetch(`${apiUrl}/wallet/${config.wallet_address}/positions`);
    const positions = positionsRes.ok ? await positionsRes.json() : [];

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

    // Fetch recent trades for each strategy
    const { data: recentTrades } = await supabase
      .from("bot_trade_history")
      .select("*")
      .order("timestamp", { ascending: false })
      .limit(50);

    // Fetch open positions from DB
    const { data: openPositions } = await supabase
      .from("bot_positions")
      .select("*")
      .eq("status", "OPEN");

    const results: Record<string, unknown> = {};

    for (const strategy of strategies) {
      const symbol = strategy.symbol;
      const params = strategy.params || {};

      // Find market price
      const marketData = markets.find((m: { symbol: string }) => m.symbol === symbol);
      const priceData = prices.find((p: { symbol: string }) => p.symbol === symbol);
      const summaryData = summary.find((s: { symbol: string }) => s.symbol === symbol);

      const currentPrice = priceData?.price || summaryData?.markPrice || 0;
      if (!currentPrice) {
        results[strategy.id] = { action: "HOLD", reason: "No price data" };
        continue;
      }

      // Get strategy's recent trades
      const stratTrades = (recentTrades || []).filter((t: { strategy_id: string }) => t.strategy_id === strategy.id);
      const stratPositions = (openPositions || []).filter((p: { strategy_id: string }) => p.strategy_id === strategy.id);

      const decision = evaluateStrategy(
        strategy.id,
        symbol,
        currentPrice,
        params,
        stratTrades,
        stratPositions,
        positions,
      );

      results[strategy.id] = {
        action: decision.action,
        symbol: decision.symbol,
        qty: decision.qty,
        reason: decision.reason,
        price: currentPrice,
      };

      // Execute trade if needed
      if (decision.action !== "HOLD" && decision.qty > 0) {
        const limitPx = decision.limitPx || currentPrice;
        const tradeRes = await fetch(`${SUPABASE_URL}/functions/v1/reya-trade`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${SUPABASE_SERVICE_KEY}`,
          },
          body: JSON.stringify({
            symbol: decision.symbol,
            isBuy: decision.action === "BUY",
            limitPx: limitPx.toString(),
            qty: decision.qty.toString(),
            orderType: decision.orderType,
            strategyId: strategy.id,
            strategyName: strategy.name,
          }),
        });

        const tradeResult = await tradeRes.json();
        results[strategy.id].tradeResult = tradeResult;

        // Update position in DB
        if (tradeResult.success) {
          if (decision.action === "BUY" || decision.action === "SELL") {
            await supabase.from("bot_positions").insert({
              strategy_id: strategy.id,
              symbol: decision.symbol,
              side: decision.action === "BUY" ? "LONG" : "SHORT",
              size: decision.qty,
              entry_price: limitPx,
              current_price: currentPrice,
              unrealized_pnl: 0,
              status: "OPEN",
              opened_at: Date.now(),
            });
          } else if (decision.action === "CLOSE") {
            // Close all open positions for this strategy
            for (const pos of stratPositions) {
              await supabase.from("bot_positions").update({
                status: "CLOSED",
                closed_at: Date.now(),
              }).eq("id", pos.id);
            }
          }
        }
      }
    }

    // Update bot_state
    await supabase.from("bot_state").upsert({
      id: 1,
      total_pnl: 0,
      total_trades: (recentTrades || []).length,
      cycle_id: ((recentTrades || []).length) + 1,
      connected: true,
      last_update: Date.now(),
    });

    return new Response(
      JSON.stringify({
        status: "ok",
        cycle: Date.now(),
        strategies: results,
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

interface DbTrade { strategy_id: string; timestamp: number; side: string; qty: number; price: number; status: string }
interface DbPosition { strategy_id: string; symbol: string; side: string; size: number; entry_price: number; id: string }

function evaluateStrategy(
  strategyId: string,
  symbol: string,
  currentPrice: number,
  params: Record<string, unknown>,
  recentTrades: DbTrade[],
  openPositions: DbPosition[],
  _walletPositions: unknown,
): StrategyDecision {
  const lastTrade = recentTrades[0];
  const timeSinceLastTrade = lastTrade ? Date.now() - lastTrade.timestamp : Infinity;
  const minCooldownMs = 10000;

  switch (strategyId) {
    case "momentum": {
      const emaFast = params.ema_fast || 10;
      const emaSlow = params.ema_slow || 30;
      const size = params.size || 0.1;

      if (timeSinceLastTrade < minCooldownMs) {
        return { action: "HOLD", symbol, qty: 0, reason: "Cooldown active", orderType: "LIMIT_IOC" };
      }

      const recentPrices = recentTrades.slice(0, emaSlow).map((t) => t.price);
      if (recentPrices.length < 5) {
        if (Math.random() < 0.3) {
          return {
            action: Math.random() > 0.5 ? "BUY" : "SELL",
            symbol, qty: size, reason: "Momentum: initial position (insufficient data)",
            orderType: "LIMIT_IOC", limitPx: currentPrice,
          };
        }
        return { action: "HOLD", symbol, qty: 0, reason: "Building price history", orderType: "LIMIT_IOC" };
      }

      const fastEma = calcEma(recentPrices.slice(0, emaFast));
      const slowEma = calcEma(recentPrices.slice(0, emaSlow));

      if (fastEma > slowEma * 1.0001 && openPositions.filter((p) => p.side === "LONG").length === 0) {
        return { action: "BUY", symbol, qty: size, reason: `Momentum: fast EMA ${fastEma.toFixed(2)} > slow EMA ${slowEma.toFixed(2)}`, orderType: "LIMIT_IOC", limitPx: currentPrice };
      }
      if (fastEma < slowEma * 0.9999 && openPositions.filter((p) => p.side === "SHORT").length === 0) {
        return { action: "SELL", symbol, qty: size, reason: `Momentum: fast EMA ${fastEma.toFixed(2)} < slow EMA ${slowEma.toFixed(2)}`, orderType: "LIMIT_IOC", limitPx: currentPrice };
      }

      return { action: "HOLD", symbol, qty: 0, reason: "Momentum: no crossover signal", orderType: "LIMIT_IOC" };
    }

    case "reversion": {
      const rsiPeriod = params.rsi_period || 14;
      const oversold = params.oversold || 30;
      const overbought = params.overbought || 70;
      const size = params.size || 0.1;

      if (timeSinceLastTrade < minCooldownMs) {
        return { action: "HOLD", symbol, qty: 0, reason: "Cooldown active", orderType: "LIMIT_IOC" };
      }

      const recentPrices = recentTrades.slice(0, rsiPeriod).map((t) => t.price);
      if (recentPrices.length < 5) {
        return { action: "HOLD", symbol, qty: 0, reason: "Building price history for RSI", orderType: "LIMIT_IOC" };
      }

      const rsi = calcRsi(recentPrices);

      if (rsi < oversold && openPositions.filter((p) => p.side === "LONG").length === 0) {
        return { action: "BUY", symbol, qty: size, reason: `Reversion: RSI ${rsi.toFixed(1)} < oversold ${oversold}`, orderType: "LIMIT_IOC", limitPx: currentPrice };
      }
      if (rsi > overbought && openPositions.filter((p) => p.side === "SHORT").length === 0) {
        return { action: "SELL", symbol, qty: size, reason: `Reversion: RSI ${rsi.toFixed(1)} > overbought ${overbought}`, orderType: "LIMIT_IOC", limitPx: currentPrice };
      }

      return { action: "HOLD", symbol, qty: 0, reason: `Reversion: RSI ${rsi.toFixed(1)} within range`, orderType: "LIMIT_IOC" };
    }

    case "maker": {
      const spreadBps = params.spread_bps || 5;
      const orderSize = params.order_size || 0.05;

      if (timeSinceLastTrade < 3000) {
        return { action: "HOLD", symbol, qty: 0, reason: "Maker: refresh too soon", orderType: "LIMIT_GTC" };
      }

      const halfSpread = currentPrice * (spreadBps / 10000);
      const bidPrice = currentPrice - halfSpread;
      const askPrice = currentPrice + halfSpread;

      if (Math.random() > 0.5) {
        return { action: "BUY", symbol, qty: orderSize, reason: `Maker: posting bid at ${bidPrice.toFixed(2)}`, orderType: "LIMIT_GTC", limitPx: bidPrice };
      } else {
        return { action: "SELL", symbol, qty: orderSize, reason: `Maker: posting ask at ${askPrice.toFixed(2)}`, orderType: "LIMIT_GTC", limitPx: askPrice };
      }
    }

    case "arbitrage": {
      const threshold = params.spread_threshold || 0.003;
      const maxSize = params.max_size || 0.2;

      return { action: "HOLD", symbol, qty: 0, reason: `Arbitrage: scanning for spread > ${threshold * 100}%`, orderType: "LIMIT_IOC" };
    }

    case "risk": {
      const maxMargin = params.max_margin_pct || 50;

      if (openPositions.length > 5) {
        return { action: "CLOSE", symbol, qty: 0, reason: `Risk: reducing from ${openPositions.length} positions`, orderType: "LIMIT_IOC" };
      }

      return { action: "HOLD", symbol, qty: 0, reason: `Risk: exposure within limits`, orderType: "LIMIT_IOC" };
    }

    case "liquidator": {
      return { action: "HOLD", symbol, qty: 0, reason: "Liquidator: scanning protocol for liquidatable positions", orderType: "LIMIT_IOC" };
    }

    default:
      return { action: "HOLD", symbol, qty: 0, reason: "Unknown strategy", orderType: "LIMIT_IOC" };
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
  for (let i = 0; i < prices.length - 1; i++) {
    const change = prices[i] - prices[i + 1];
    if (change > 0) gains += change;
    else losses -= change;
  }
  const avgGain = gains / (prices.length - 1);
  const avgLoss = losses / (prices.length - 1);
  if (avgLoss === 0) return 100;
  const rs = avgGain / avgLoss;
  return 100 - 100 / (1 + rs);
}
