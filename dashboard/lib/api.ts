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

export interface PnlSummary {
  totalRealizedPnl: number;
  totalTrades: number;
  winningTrades: number;
  winRate: number;
  avgPnlPerTrade: number;
}

export interface PnlChart {
  date: string;
  totalValue: number;
  cumulativePnl: number;
  dailyPnl: number;
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
    list: (mode = "paper", page = 0) =>
      get<TradePage>(`/api/trades?mode=${mode}&page=${page}&size=20`),
    pnlSummary: (mode = "paper") =>
      get<PnlSummary>(`/api/trades/pnl/summary?mode=${mode}`),
    pnlChart: (mode = "paper", days = 30) =>
      get<PnlChart[]>(`/api/trades/pnl/chart?mode=${mode}&days=${days}`),
  },
  backtest: {
    list: (market = "domestic", limit = 20) =>
      get<BacktestResult[]>(`/api/backtest?market=${market}&limit=${limit}`),
  },
  command: {
    analyze: (market = "domestic", horizon = "swing") =>
      post<{ run_id: number; status: string }>("/api/command/analyze", { market, horizon }),
    tradeStart: (market = "domestic") =>
      post("/api/command/trade/start", { market }),
    tradeStop: () => post("/api/command/trade/stop"),
    health: () => get<{ status: string; trading_active: boolean }>("/api/command/health"),
  },
};
