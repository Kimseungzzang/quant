"use client";

import { useState } from "react";
import { Shuffle, X, TrendingUp, BarChart2, Loader2 } from "lucide-react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8084";

interface RerankResult {
  rank: number;
  stock_code: string;
  stock_name: string;
  final_score: number;
  rerankScore: number;
  volRank: number;
  volBonus: number;
  gapBonus: number;
  change_pct: number;
  win_rate_pct: number;
  backtest_return: number;
}

interface RerankResponse {
  market: string;
  horizon: string;
  results: RerankResult[];
}

interface Props {
  market: string;
  horizon: string;
}

export default function RerankButton({ market, horizon }: Props) {
  const [loading, setLoading]   = useState(false);
  const [data, setData]         = useState<RerankResponse | null>(null);
  const [error, setError]       = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);

  async function handleRerank() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(
        `${API}/api/command/analyze/rerank?market=${market}&horizon=${horizon}`,
        { method: "POST", cache: "no-store" },
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail ?? `오류 ${res.status}`);
      }
      const json = await res.json();
      setData(json);
      setUpdatedAt(new Date().toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "재정렬 실패");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-3">
      {/* 버튼 */}
      <button
        onClick={handleRerank}
        disabled={loading}
        className="flex items-center gap-2 px-4 py-2 bg-amber-700 hover:bg-amber-600 disabled:opacity-40 text-white text-sm rounded-lg transition-colors"
      >
        {loading
          ? <Loader2 size={14} className="animate-spin" />
          : <Shuffle size={14} />}
        장시작 재정렬
      </button>

      {error && (
        <p className="text-xs text-red-400">{error}</p>
      )}

      {/* 재정렬 결과 패널 */}
      {data && (
        <div className="bg-gray-900 border border-amber-800/60 rounded-xl overflow-hidden">
          <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800 bg-amber-900/20">
            <div className="flex items-center gap-2">
              <Shuffle size={13} className="text-amber-400" />
              <span className="text-sm font-semibold text-amber-300">장시작 재정렬 결과</span>
              {updatedAt && (
                <span className="text-xs text-gray-500">{updatedAt} 기준</span>
              )}
            </div>
            <button
              onClick={() => setData(null)}
              className="text-gray-500 hover:text-gray-300 transition-colors"
            >
              <X size={14} />
            </button>
          </div>

          <table className="w-full text-sm">
            <thead>
              <tr className="text-gray-500 text-xs border-b border-gray-800">
                <th className="text-left px-4 py-2">순위</th>
                <th className="text-left px-4 py-2">종목</th>
                <th className="text-right px-4 py-2">재정렬점수</th>
                <th className="text-right px-4 py-2">거래량순위</th>
                <th className="text-right px-4 py-2">
                  <span className="flex items-center justify-end gap-1">
                    <BarChart2 size={10} />거래량+
                  </span>
                </th>
                <th className="text-right px-4 py-2">
                  <span className="flex items-center justify-end gap-1">
                    <TrendingUp size={10} />갭+
                  </span>
                </th>
                <th className="text-right px-4 py-2">기존점수</th>
                <th className="text-right px-4 py-2">등락</th>
              </tr>
            </thead>
            <tbody>
              {data.results.map((r, i) => (
                <tr key={r.stock_code} className="border-b border-gray-800/50 hover:bg-gray-800/40">
                  <td className="px-4 py-2">
                    <span className="w-5 h-5 inline-flex items-center justify-center bg-amber-900/60 text-amber-300 rounded-full text-xs font-bold">
                      {i + 1}
                    </span>
                  </td>
                  <td className="px-4 py-2">
                    <div className="font-medium">{r.stock_name}</div>
                    <div className="text-gray-500 text-xs">{r.stock_code}</div>
                  </td>
                  <td className="px-4 py-2 text-right text-amber-300 font-bold">
                    {r.rerankScore?.toFixed(1)}
                  </td>
                  <td className="px-4 py-2 text-right">
                    {r.volRank < 9999
                      ? <span className="text-emerald-400">{r.volRank + 1}위</span>
                      : <span className="text-gray-600">—</span>}
                  </td>
                  <td className="px-4 py-2 text-right text-emerald-400">
                    {r.volBonus > 0 ? `+${r.volBonus.toFixed(1)}` : "—"}
                  </td>
                  <td className="px-4 py-2 text-right text-blue-400">
                    {r.gapBonus > 0 ? `+${r.gapBonus.toFixed(1)}` : "—"}
                  </td>
                  <td className="px-4 py-2 text-right text-gray-400">
                    {r.final_score?.toFixed(1)}
                  </td>
                  <td className={`px-4 py-2 text-right ${(r.change_pct ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                    {(r.change_pct ?? 0) >= 0 ? "+" : ""}{r.change_pct?.toFixed(2)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
