const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8080";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`API ${path} failed: ${res.status}`);
  return res.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`API POST ${path} failed: ${res.status}`);
  return res.json();
}

// ── types ──────────────────────────────────────────────────────

export interface AnalysisRun {
  id: number;
  market: string;
  horizon: string;
  runAt: string;
  status: string;
  resultCount: number;
}

export interface AnalysisResult {
  id: number;
  runId: number;
  rank: number;
  stockCode: string;
  stockName: string;
  market: string;
  horizon: string;
  currentPrice: number;
  changePct: number;
  tradingValue: number;
  finalScore: number;
  winRatePct: number;
  backtestReturn: number;
  maxDrawdown: number;
  tradeCount: number;
  createdAt: string;
}

export interface Trade {
  id: number;
  tradedAt: string;
  stockCode: string;
  stockName: string;
  market: string;
  side: "BUY" | "SELL";
  quantity: number;
  price: number;
  amount: number;
  currency: string;
  mode: string;
  strategy: string | null;
  realizedPnl: number | null;
  pnlPct: number | null;
}

export interface TradePage {
  content: Trade[];
  totalElements: number;
  totalPages: number;
  number: number;
}

export interface Position {
  id: number | string;
  stockCode: string;
  stockName: string;
  market: string;
  quantity: number;
  avgPrice: number;
  currency: string;
  currentPrice: number;
  marketValue: number;
  unrealizedPnl: number | null;
  unrealizedPct: number | null;
  mode: string;
  openedAt: string;
  updatedAt: string;
}

export interface PendingOrder {
  id: string;
  orderNo: string;
  side: "BUY" | "SELL";
  stockCode: string;
  stockName: string;
  market: string;
  quantity: number;
  filledQuantity: number;
  remainingQuantity: number;
  requestedPrice: number;
  currency: string;
  mode: string;
  strategy: string;
  reason: string;
  createdAt: string;
}

export interface PnlSummary {
  totalRealizedPnl: number;
  totalTrades: number;
  winningTrades: number;
  winRate: number;
  avgPnlPerTrade: number;
}

export interface AccountBalance {
  market: string;
  mode: string;
  currency: string;
  cash: number;
  totalAssets: number;
  positionValue: number;
  positionCount: number;
  totalPnl?: number;
  totalPnlPct?: number;
  updatedAt: string;
}

export interface PnlChart {
  date: string;
  totalValue: number;
  cumulativePnl: number;
  dailyPnl: number;
}

export interface StockPerformance {
  stockCode: string;
  stockName: string;
  tradePairs: number;
  wins: number;
  winRate: number;
  totalPnl: number;
  avgPnl: number;
  maxPnl: number;
  minPnl: number;
}

export interface DailyReport {
  date: string;
  tradePairs: number;
  wins: number;
  losses: number;
  winRate: number;
  totalPnl: number;
  maxPnl: number;
  minPnl: number;
}

export interface BacktestResult {
  id: number;
  stockCode: string;
  stockName: string;
  market: string;
  periodDays: number;
  totalReturnPct: number;
  winRatePct: number;
  maxDrawdownPct: number;
  tradeCount: number;
  sharpeRatio: number;
  runAt: string;
}

// ── API calls ──────────────────────────────────────────────────

export const api = {
  analysis: {
    runs: (market = "domestic", horizon = "swing") =>
      get<AnalysisRun[]>(`/api/analysis/runs?market=${market}&horizon=${horizon}`),
    latest: (market = "domestic", horizon = "swing") =>
      get<AnalysisResult[]>(`/api/analysis?market=${market}&horizon=${horizon}`),
    byRun: (runId: number) =>
      get<AnalysisResult[]>(`/api/analysis/run/${runId}`),
  },
  trades: {
    list: (mode = "paper", page = 0, period = "all", stockCode = "") =>
      get<TradePage>(
        `/api/trades?mode=${mode}&page=${page}&size=20&period=${period}` +
        `${stockCode ? `&stockCode=${encodeURIComponent(stockCode)}` : ""}`,
      ),
    pnlSummary: (mode = "paper") =>
      get<PnlSummary>(`/api/trades/pnl/summary?mode=${mode}`),
    pnlChart: (mode = "paper", days = 30) =>
      get<PnlChart[]>(`/api/trades/pnl/chart?mode=${mode}&days=${days}`),
    positions: (mode = "paper") =>
      get<Position[]>(`/api/trades/positions?mode=${mode}`),
    stockPerformance: (mode = "paper", period = "month") =>
      get<StockPerformance[]>(`/api/trades/performance/stocks?mode=${mode}&period=${period}`),
    dailyReports: (mode = "paper", period = "month") =>
      get<DailyReport[]>(`/api/trades/reports/daily?mode=${mode}&period=${period}`),
  },
  backtest: {
    list: (market = "domestic", limit = 20) =>
      get<BacktestResult[]>(`/api/backtest?market=${market}&limit=${limit}`),
  },
  command: {
    analyze: (market = "domestic", horizon = "swing") =>
      post<{ run_id: number; status: string }>("/api/command/analyze", { market, horizon }),
    tradeStart: (market = "domestic", mode?: string) =>
      post("/api/command/trade/start", { market, mode }),
    tradeStop: () => post("/api/command/trade/stop"),
    setMode: (mode: "paper" | "live") =>
      post<{ status: string; mode: string }>("/api/command/mode", { mode }),
    health: () => get<{ status: string; trading_active: boolean; mode?: string }>("/api/command/health"),
    accountBalance: (market = "domestic", mode = "paper") =>
      get<AccountBalance>(`/api/command/account/balance?market=${market}&mode=${mode}`),
  },
};
