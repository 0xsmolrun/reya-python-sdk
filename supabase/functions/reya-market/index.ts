import { createClient } from "npm:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Client-Info, Apikey",
};

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

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
    const walletAddress = config?.wallet_address;

    const url = new URL(req.url);
    const action = url.searchParams.get("action") || "all";

    const result: Record<string, unknown> = { config };

    if (action === "all" || action === "markets") {
      try {
        const marketRes = await fetch(`${apiUrl}/marketDefinitions`);
        if (marketRes.ok) {
          result.markets = await marketRes.json();
        } else {
          result.markets = [];
          result.marketError = `Market definitions returned ${marketRes.status}`;
        }
      } catch (e) {
        result.markets = [];
        result.marketError = e instanceof Error ? e.message : "Failed to fetch markets";
      }
    }

    if (action === "all" || action === "prices") {
      try {
        const pricesRes = await fetch(`${apiUrl}/prices`);
        if (pricesRes.ok) {
          const rawPrices = await pricesRes.json();
          // Normalize: API returns oraclePrice/poolPrice as strings
          result.prices = (rawPrices as Array<Record<string, unknown>>).map((p) => ({
            symbol: p.symbol,
            price: Number(p.oraclePrice || p.poolPrice || 0),
            change24h: 0,
          }));
        } else {
          result.prices = [];
          result.priceError = `Prices returned ${pricesRes.status}`;
        }
      } catch (e) {
        result.prices = [];
        result.priceError = e instanceof Error ? e.message : "Failed to fetch prices";
      }
    }

    if (action === "all" || action === "summary") {
      try {
        const summaryRes = await fetch(`${apiUrl}/marketsSummary`);
        if (summaryRes.ok) {
          result.summary = await summaryRes.json();
        } else {
          result.summary = [];
        }
      } catch {
        result.summary = [];
      }
    }

    if (walletAddress && (action === "all" || action === "wallet")) {
      try {
        const [positionsRes, ordersRes, balancesRes, accountsRes] = await Promise.all([
          fetch(`${apiUrl}/wallet/${walletAddress}/positions`),
          fetch(`${apiUrl}/wallet/${walletAddress}/openOrders`),
          fetch(`${apiUrl}/wallet/${walletAddress}/accountBalances`),
          fetch(`${apiUrl}/wallet/${walletAddress}/accounts`),
        ]);

        result.positions = positionsRes.ok ? await positionsRes.json() : [];
        result.openOrders = ordersRes.ok ? await ordersRes.json() : [];
        result.balances = balancesRes.ok ? await balancesRes.json() : [];
        result.accounts = accountsRes.ok ? await accountsRes.json() : [];
      } catch (e) {
        result.walletError = e instanceof Error ? e.message : "Failed to fetch wallet data";
      }
    }

    return new Response(JSON.stringify(result), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (err) {
    return new Response(
      JSON.stringify({ error: err instanceof Error ? err.message : "Unknown error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
