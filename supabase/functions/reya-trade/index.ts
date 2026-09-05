import { createClient } from "npm:@supabase/supabase-js@2";
import {
  privateKeyToAccount,
  signTypedData,
} from "npm:viem@2";
import {
  encodeAbiParameters,
  parseAbiParameters,
  hexToBigInt,
  toHex,
} from "npm:viem@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Client-Info, Apikey",
};

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const MAINNET_CHAIN_ID = 1729;
const DEX_ID = 2;

function getOrdersGatewayAddress(chainId: number): string {
  return chainId === MAINNET_CHAIN_ID
    ? "0xfc8c96be87da63cecddbf54abfa7b13ee8044739"
    : "0x5a0ac2f89e0bdeafc5c549e354842210a3e87ca5";
}

function getPoolAccountId(chainId: number): number {
  return chainId === MAINNET_CHAIN_ID ? 2 : 4;
}

function scale18(value: string): bigint {
  const [whole, frac = ""] = value.split(".");
  const fracPadded = (frac + "0".repeat(18)).slice(0, 18);
  return BigInt(whole) * 10n ** 18n + BigInt(fracPadded || "0");
}

function encodeLimitOrderInputs(isBuy: boolean, limitPx: string, qty: string): string {
  const signedQty = isBuy ? scale18(qty) : -scale18(qty);
  const scaledPx = scale18(limitPx);
  const encoded = encodeAbiParameters(
    parseAbiParameters("int256, uint256"),
    [signedQty, scaledPx],
  );
  return encoded;
}

function encodeTriggerOrderInputs(isBuy: boolean, triggerPx: string, limitPx: string): string {
  const encoded = encodeAbiParameters(
    parseAbiParameters("bool, uint256, uint256"),
    [isBuy, scale18(triggerPx), scale18(limitPx)],
  );
  return encoded;
}

function createOrdersGatewayNonce(
  accountId: number,
  marketId: number,
  timestampMs: number,
): bigint {
  return (
    (BigInt(accountId) << 98n) |
    (BigInt(timestampMs) << 32n) |
    BigInt(marketId)
  );
}

const ORDER_TYPES = {
  ConditionalOrder: [
    { name: "verifyingChainId", type: "uint256" },
    { name: "deadline", type: "uint256" },
    { name: "order", type: "ConditionalOrderDetails" },
  ],
  ConditionalOrderDetails: [
    { name: "accountId", type: "uint128" },
    { name: "marketId", type: "uint128" },
    { name: "exchangeId", type: "uint128" },
    { name: "counterpartyAccountIds", type: "uint128[]" },
    { name: "orderType", type: "uint8" },
    { name: "inputs", type: "bytes" },
    { name: "signer", type: "address" },
    { name: "nonce", type: "uint256" },
  ],
} as const;

const CANCEL_TYPES = {
  OrderCancel: [
    { name: "verifyingChainId", type: "uint64" },
    { name: "deadline", type: "uint64" },
    { name: "cancel", type: "OrderCancelDetails" },
  ],
  OrderCancelDetails: [
    { name: "accountId", type: "uint64" },
    { name: "marketId", type: "uint64" },
    { name: "orderId", type: "uint64" },
    { name: "clOrdId", type: "uint64" },
    { name: "nonce", type: "uint64" },
  ],
} as const;

interface TradeRequest {
  symbol: string;
  isBuy: boolean;
  limitPx: string;
  qty: string;
  orderType: "LIMIT_IOC" | "LIMIT_GTC" | "MARKET" | "TP" | "SL";
  reduceOnly?: boolean;
  triggerPx?: string;
  strategyId?: string;
  strategyName?: string;
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 200, headers: corsHeaders });
  }

  try {
    const privateKey = Deno.env.get("REYA_PRIVATE_KEY");
    if (!privateKey) {
      return new Response(
        JSON.stringify({ error: "REYA_PRIVATE_KEY secret not configured. Set it in Supabase edge function secrets." }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY);

    const { data: config } = await supabase
      .from("bot_config")
      .select("*")
      .eq("id", 1)
      .maybeSingle();

    if (!config?.account_id || !config?.wallet_address) {
      return new Response(
        JSON.stringify({ error: "Bot not configured. Set wallet address and account ID in settings." }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const chainId = config.chain_id;
    const accountId = config.account_id;
    const apiUrl = config.api_url;
    const ordersGateway = getOrdersGatewayAddress(chainId);
    const poolAccountId = getPoolAccountId(chainId);

    const body: TradeRequest = await req.json();
    const { symbol, isBuy, limitPx, qty, orderType, reduceOnly, triggerPx, strategyId, strategyName } = body;

    const account = privateKeyToAccount(privateKey as `0x${string}`);
    const signerWallet = account.address;

    // Fetch market ID for symbol
    const marketRes = await fetch(`${apiUrl}/marketDefinitions`);
    let marketId: number | null = null;
    if (marketRes.ok) {
      const markets = await marketRes.json();
      const market = markets.find((m: { symbol: string; marketId: number }) => m.symbol === symbol);
      if (market) marketId = market.marketId;
    }
    if (marketId === null) {
      return new Response(
        JSON.stringify({ error: `Could not resolve market ID for symbol ${symbol}` }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const isSpot = !symbol.toUpperCase().endsWith("PERP");
    const timestampMs = Date.now();
    const deadline = Math.floor(timestampMs / 1000) + 30;
    const conditionalDeadline = 10n ** 18n;

    let orderTypeInt: number;
    let inputs: string;
    let nonce: bigint;
    let finalDeadline: bigint;
    let counterpartyIds: number[];

    if (orderType === "TP" || orderType === "SL") {
      if (!triggerPx) throw new Error("triggerPx required for TP/SL orders");
      orderTypeInt = orderType === "TP" ? 1 : 0;
      inputs = encodeTriggerOrderInputs(isBuy, triggerPx, isBuy ? "100000000000000000000" : "0");
      nonce = createOrdersGatewayNonce(accountId, marketId, timestampMs);
      finalDeadline = conditionalDeadline;
      counterpartyIds = [poolAccountId];
    } else if (isSpot) {
      orderTypeInt = 6; // LIMIT_ORDER_SPOT
      inputs = encodeLimitOrderInputs(isBuy, limitPx, qty);
      nonce = BigInt(timestampMs * 1000);
      finalDeadline = BigInt(Math.floor(timestampMs / 1000) + 86400);
      counterpartyIds = [];
    } else {
      // Perp order
      inputs = encodeLimitOrderInputs(isBuy, limitPx, qty);
      nonce = createOrdersGatewayNonce(accountId, marketId, timestampMs);
      if (orderType === "LIMIT_GTC") {
        orderTypeInt = 2; // LIMIT_ORDER
        finalDeadline = conditionalDeadline;
      } else if (reduceOnly) {
        orderTypeInt = 4; // REDUCE_ONLY_MARKET_ORDER
        finalDeadline = BigInt(deadline);
      } else {
        orderTypeInt = 3; // MARKET_ORDER
        finalDeadline = BigInt(deadline);
      }
      counterpartyIds = [poolAccountId];
    }

    // Sign the order
    const signature = await signTypedData({
      account,
      domain: {
        name: "Reya",
        version: "1",
        verifyingContract: ordersGateway as `0x${string}`,
      },
      types: ORDER_TYPES,
      primaryType: "ConditionalOrder",
      message: {
        verifyingChainId: BigInt(chainId),
        deadline: finalDeadline,
        order: {
          accountId: BigInt(accountId),
          marketId: BigInt(marketId),
          exchangeId: BigInt(DEX_ID),
          counterpartyAccountIds: counterpartyIds.map(BigInt),
          orderType: orderTypeInt,
          inputs: inputs as `0x${string}`,
          signer: signerWallet,
          nonce: nonce,
        },
      },
    });

    // Build the order request
    const orderRequest: Record<string, unknown> = {
      exchangeId: DEX_ID,
      symbol,
      accountId,
      isBuy,
      limitPx,
      qty: orderType === "TP" || orderType === "SL" ? undefined : qty,
      orderType: orderType === "TP" ? "TP" : orderType === "SL" ? "SL" : "LIMIT",
      timeInForce: orderType === "LIMIT_IOC" ? "IOC" : orderType === "LIMIT_GTC" ? "GTC" : undefined,
      triggerPx: triggerPx,
      reduceOnly: reduceOnly && orderType === "LIMIT_IOC" ? reduceOnly : undefined,
      signature,
      nonce: nonce.toString(),
      signerWallet,
      expiresAfter: Number(finalDeadline),
    };

    // Remove undefined fields
    Object.keys(orderRequest).forEach((k) => orderRequest[k] === undefined && delete orderRequest[k]);

    // Submit to Reya API
    const createRes = await fetch(`${apiUrl}/createOrder`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(orderRequest),
    });

    const responseText = await createRes.text();
    let orderResponse;
    try {
      orderResponse = JSON.parse(responseText);
    } catch {
      orderResponse = { raw: responseText };
    }

    const status = createRes.ok ? "FILLED" : "REJECTED";
    const tradeRecord = {
      strategy_id: strategyId || "manual",
      strategy_name: strategyName || "MANUAL",
      symbol,
      side: isBuy ? "BUY" : "SELL",
      qty: parseFloat(qty) || 0,
      price: parseFloat(limitPx) || 0,
      pnl: 0,
      status,
      order_id: orderResponse?.orderId?.toString() || null,
      error: createRes.ok ? null : responseText,
      timestamp: timestampMs,
    };

    await supabase.from("bot_trade_history").insert(tradeRecord);

    return new Response(
      JSON.stringify({
        success: createRes.ok,
        status,
        orderResponse,
        trade: tradeRecord,
      }),
      {
        status: createRes.ok ? 200 : 400,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      },
    );
  } catch (err) {
    return new Response(
      JSON.stringify({ error: err instanceof Error ? err.message : "Unknown error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
