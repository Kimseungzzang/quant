import { api } from "@/lib/api";
import StatCard from "@/components/StatCard";
import TradeControl from "@/components/TradeControl";
import MarketRegimePanel from "@/components/MarketRegimePanel";
import Link from "next/link";

function fmt(n: number, prefix = "") {
  const abs = Math.abs(n);
  const s = abs >= 1_000_000
    ? `${(abs / 1_000_000).toFixed(2)}M`
    : abs >= 1_000
    ? `${(abs / 1_000).toFixed(1)}K`
    : abs.toFixed(0);
  return `${prefix}${n < 0 ? "-" : ""}${s}`;
}

export default async function DashboardPage() {
  const [summary, chart, analysis] = await Promise.allSettled([
    api.trades.pnlSummary("paper"),
    api.trades.pnlChart("paper", 30),
    api.analysis.latest("domestic"),
  ]);

  const pnl    = summary.status === "fulfilled" ? summary.value : null;
  const candles = chart.status === "fulfilled" ? chart.value : [];
  const tops   = analysis.status === "fulfilled" ? analysis.value.slice(0, 5) : [];

  const pnlColor = !pnl ? "default" : pnl.totalRealizedPnl > 0 ? "green" : pnl.totalRealizedPnl < 0 ? "red" : "default";

  return (
    <div className="space-y-8 max-w-5xl">
      <h1 className="text-xl font-bold text-gray-100">대시보드</h1>

      {/* 상단: 통계 + 매매 제어 */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div className="md:col-span-2 grid grid-cols-2 gap-4 content-start">
              <StatCard
            label="누적 실현손익"
            value={pnl ? fmt(pnl.totalRealizedPnl, "₩") : "—"}
            color={pnlColor}
          />
          <StatCard
            label="승률"
            value={pnl ? `${pnl.winRate.toFixed(1)}%` : "—"}
            sub={pnl ? `${pnl.winningTrades}/${pnl.totalTrades}건` : undefined}
            color="indigo"
          />
          <StatCard
            label="총 매매"
            value={pnl ? `${pnl.totalTrades}건` : "—"}
          />
          <StatCard
            label="평균 손익/건"
            value={pnl ? fmt(pnl.avgPnlPerTrade, "₩") : "—"}
            color={pnl && pnl.avgPnlPerTrade >= 0 ? "green" : "red"}
          />
        </div>

        {/* 매매 제어 + 장세 패널 */}
        <div className="space-y-4">
          <TradeControl />
          <MarketRegimePanel />
        </div>
      </div>

      {/* P&L 미니 테이블 (최근 30일) */}
      {candles.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wide">최근 30일 누적 P&L</h2>
          <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-800 text-gray-500 text-xs">
                  <th className="text-left px-4 py-2">날짜</th>
                  <th className="text-right px-4 py-2">당일 손익</th>
                  <th className="text-right px-4 py-2">누적</th>
                </tr>
              </thead>
              <tbody>
                {candles.slice(-10).reverse().map((c) => (
                  <tr key={c.date} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                    <td className="px-4 py-2 text-gray-400">{c.date}</td>
                    <td className={`px-4 py-2 text-right ${c.dailyPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      {fmt(c.dailyPnl, "₩")}
                    </td>
                    <td className={`px-4 py-2 text-right ${c.cumulativePnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      {fmt(c.cumulativePnl, "₩")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* 추천 종목 Top 5 */}
      {tops.length > 0 && (
        <section>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">추천 종목 Top 5</h2>
            <Link href="/analysis" className="text-xs text-indigo-400 hover:text-indigo-300">전체 보기 →</Link>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-800 text-gray-500 text-xs">
                  <th className="text-left px-4 py-2">순위</th>
                  <th className="text-left px-4 py-2">종목</th>
                  <th className="text-right px-4 py-2">현재가</th>
                  <th className="text-right px-4 py-2">등락</th>
                  <th className="text-right px-4 py-2">점수</th>
                  <th className="text-right px-4 py-2">승률</th>
                </tr>
              </thead>
              <tbody>
                {tops.map((r) => (
                  <tr key={r.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                    <td className="px-4 py-2 text-gray-500">{r.rank}</td>
                    <td className="px-4 py-2">
                      <span className="font-medium">{r.stockName}</span>
                      <span className="text-gray-500 text-xs ml-2">{r.stockCode}</span>
                    </td>
                    <td className="px-4 py-2 text-right">{r.currentPrice.toLocaleString()}</td>
                    <td className={`px-4 py-2 text-right ${r.changePct >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      {r.changePct >= 0 ? "+" : ""}{r.changePct?.toFixed(2)}%
                    </td>
                    <td className="px-4 py-2 text-right text-indigo-400">{r.finalScore?.toFixed(1)}</td>
                    <td className="px-4 py-2 text-right">{r.winRatePct?.toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {tops.length === 0 && candles.length === 0 && (
        <div className="text-gray-600 text-sm">
          분석 데이터가 없습니다.{" "}
          <Link href="/analysis" className="text-indigo-400 hover:underline">분석 실행 →</Link>
        </div>
      )}
    </div>
  );
}
