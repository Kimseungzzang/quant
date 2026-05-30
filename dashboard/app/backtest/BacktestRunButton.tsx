"use client";

import { useState } from "react";
import { FlaskConical, Loader2, X, TrendingUp, TrendingDown } from "lucide-react";

function toDateInput(d: Date) {
  return d.toISOString().slice(0, 10);
}

const today    = new Date();
const sixtyAgo = new Date(today);
sixtyAgo.setDate(today.getDate() - 60);

interface Trade {
  strategy: string;
  entry_time: string;
  entry_price: number;
  exit_time: string;
  exit_price: number;
  pnl_pct: number;
  exit_reason: string;
}

interface BacktestResponse {
  stock_code: string;
  stock_name: string;
  start_date: string;
  end_date: string;
  period_days: number;
  total_return_pct: number;
  win_rate_pct: number;
  max_drawdown_pct: number;
  sharpe_ratio: number;
  total_trades: number;
  trades: Trade[];
}

const STRATEGY_LABEL: Record<string, string> = {
  breakout: "돌파",
  pullback: "눌림목",
  gap:      "갭",
};

export default function BacktestRunButton({ market }: { market: string }) {
  const [code,      setCode]      = useState("");
  const [startDate, setStartDate] = useState(toDateInput(sixtyAgo));
  const [endDate,   setEndDate]   = useState(toDateInput(today));
  const [loading,   setLoading]   = useState(false);
  const [result,    setResult]    = useState<BacktestResponse | null>(null);

  async function handleRun() {
    if (!code.trim()) return;
    setLoading(true);
    setResult(null);
    try {
      const res = await fetch("http://localhost:8080/api/command/backtest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stockCode: code.trim(), market, startDate, endDate }),
      });
      if (!res.ok) throw new Error();
      const data: BacktestResponse = await res.json();
      setResult(data);
    } catch {
      alert("백테스트 실행 중 오류가 발생했습니다.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      {/* 입력 폼 */}
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="종목코드 (예: 005930)"
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-600 w-40 focus:outline-none focus:border-indigo-500"
        />
        <input
          type="date"
          value={startDate}
          max={endDate}
          onChange={(e) => setStartDate(e.target.value)}
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500"
        />
        <span className="text-gray-600 text-sm">~</span>
        <input
          type="date"
          value={endDate}
          min={startDate}
          max={toDateInput(today)}
          onChange={(e) => setEndDate(e.target.value)}
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500"
        />
        <button
          onClick={handleRun}
          disabled={loading || !code.trim()}
          className="flex items-center gap-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-sm px-4 py-2 rounded-lg transition-colors"
        >
          {loading ? <Loader2 size={14} className="animate-spin" /> : <FlaskConical size={14} />}
          실행
        </button>
      </div>

      {/* 결과 모달 */}
      {result && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
          <div className="bg-gray-950 border border-gray-800 rounded-2xl w-full max-w-4xl max-h-[90vh] flex flex-col">

            {/* 모달 헤더 */}
            <div className="flex items-center justify-between px-6 py-4 border-b border-gray-800">
              <div>
                <h2 className="text-base font-bold">
                  {result.stock_name} ({result.stock_code})
                </h2>
                <p className="text-xs text-gray-500 mt-0.5">
                  {result.start_date} ~ {result.end_date} ({result.period_days}일)
                </p>
              </div>
              <button
                onClick={() => { setResult(null); window.location.reload(); }}
                className="text-gray-600 hover:text-gray-300 transition-colors"
              >
                <X size={18} />
              </button>
            </div>

            {/* 요약 지표 */}
            <div className="grid grid-cols-4 gap-3 px-6 py-4 border-b border-gray-800">
              {[
                { label: "누적 수익률", value: `${result.total_return_pct >= 0 ? "+" : ""}${result.total_return_pct.toFixed(2)}%`, color: result.total_return_pct >= 0 ? "text-emerald-400" : "text-red-400" },
                { label: "승률",       value: `${result.win_rate_pct.toFixed(1)}%`,                                                  color: "text-gray-200" },
                { label: "MDD",        value: `-${result.max_drawdown_pct.toFixed(2)}%`,                                              color: "text-red-400" },
                { label: "총 거래수",  value: `${result.total_trades}회`,                                                            color: "text-gray-200" },
              ].map(({ label, value, color }) => (
                <div key={label} className="bg-gray-900 rounded-xl p-3 text-center">
                  <p className="text-xs text-gray-500 mb-1">{label}</p>
                  <p className={`text-sm font-bold ${color}`}>{value}</p>
                </div>
              ))}
            </div>

            {/* 거래 내역 테이블 */}
            <div className="overflow-y-auto flex-1">
              {result.trades.length === 0 ? (
                <p className="text-center text-gray-600 text-sm py-10">거래 내역 없음</p>
              ) : (
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-gray-950 border-b border-gray-800">
                    <tr className="text-gray-500">
                      <th className="text-left px-4 py-3">#</th>
                      <th className="text-left px-4 py-3">전략</th>
                      <th className="text-right px-4 py-3">진입 시각</th>
                      <th className="text-right px-4 py-3">진입가</th>
                      <th className="text-right px-4 py-3">청산 시각</th>
                      <th className="text-right px-4 py-3">청산가</th>
                      <th className="text-right px-4 py-3">손익</th>
                      <th className="text-left px-4 py-3">청산 사유</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.trades.map((t, i) => (
                      <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-900/60">
                        <td className="px-4 py-2.5 text-gray-600">{i + 1}</td>
                        <td className="px-4 py-2.5">
                          <span className="px-1.5 py-0.5 rounded text-[10px] bg-indigo-900/50 text-indigo-300 border border-indigo-800">
                            {STRATEGY_LABEL[t.strategy] ?? t.strategy}
                          </span>
                        </td>
                        <td className="px-4 py-2.5 text-right text-gray-400">{t.entry_time}</td>
                        <td className="px-4 py-2.5 text-right font-mono">{t.entry_price.toLocaleString()}</td>
                        <td className="px-4 py-2.5 text-right text-gray-400">{t.exit_time}</td>
                        <td className="px-4 py-2.5 text-right font-mono">{t.exit_price.toLocaleString()}</td>
                        <td className={`px-4 py-2.5 text-right font-bold flex items-center justify-end gap-1 ${t.pnl_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                          {t.pnl_pct >= 0
                            ? <TrendingUp size={11} />
                            : <TrendingDown size={11} />}
                          {t.pnl_pct >= 0 ? "+" : ""}{t.pnl_pct.toFixed(2)}%
                        </td>
                        <td className="px-4 py-2.5 text-gray-500 max-w-[180px] truncate">{t.exit_reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
