import { api } from "@/lib/api";
import StatCard from "@/components/StatCard";
import PnlChart from "./PnlChart";

function fmtPnl(n: number | null) {
  if (n === null) return "—";
  const s = Math.abs(n) >= 1000 ? `${(Math.abs(n) / 1000).toFixed(1)}K` : Math.abs(n).toFixed(0);
  return `${n < 0 ? "-" : "+"}₩${s}`;
}

export default async function TradesPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string; page?: string }>;
}) {
  const { mode = "paper", page = "0" } = await searchParams;
  const pageNum = parseInt(page, 10);

  const [tradesResult, summaryResult, chartResult] = await Promise.allSettled([
    api.trades.list(mode, pageNum),
    api.trades.pnlSummary(mode),
    api.trades.pnlChart(mode, 30),
  ]);

  const trades  = tradesResult.status  === "fulfilled" ? tradesResult.value  : null;
  const summary = summaryResult.status === "fulfilled" ? summaryResult.value : null;
  const chart   = chartResult.status   === "fulfilled" ? chartResult.value   : [];

  return (
    <div className="space-y-6 max-w-5xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">매매 이력</h1>
        <nav className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 text-sm">
          {["paper", "live"].map((m) => (
            <a
              key={m}
              href={`/trades?mode=${m}`}
              className={`px-3 py-1 rounded-md transition-colors ${
                mode === m ? "bg-indigo-600 text-white" : "text-gray-400 hover:text-gray-100"
              }`}
            >
              {m === "paper" ? "모의" : "실전"}
            </a>
          ))}
        </nav>
      </div>

      {/* 통계 */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          label="누적 실현손익"
          value={summary ? fmtPnl(summary.totalRealizedPnl) : "—"}
          color={summary ? (summary.totalRealizedPnl >= 0 ? "green" : "red") : "default"}
        />
        <StatCard
          label="승률"
          value={summary ? `${summary.winRate.toFixed(1)}%` : "—"}
          sub={summary ? `${summary.winningTrades}/${summary.totalTrades}건` : undefined}
          color="indigo"
        />
        <StatCard label="총 매매" value={summary ? `${summary.totalTrades}건` : "—"} />
        <StatCard
          label="평균 손익"
          value={summary ? fmtPnl(summary.avgPnlPerTrade) : "—"}
          color={summary ? (summary.avgPnlPerTrade >= 0 ? "green" : "red") : "default"}
        />
      </div>

      {/* P&L 차트 */}
      {chart.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wide">누적 손익 곡선 (30일)</h2>
          <PnlChart data={chart} />
        </section>
      )}

      {/* 매매 이력 테이블 */}
      <section>
        <h2 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wide">거래 내역</h2>
        {!trades || trades.content.length === 0 ? (
          <p className="text-gray-600 text-sm">거래 기록이 없습니다.</p>
        ) : (
          <>
            <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-800 text-gray-500 text-xs">
                    <th className="text-left px-4 py-3">일시</th>
                    <th className="text-left px-4 py-3">종목</th>
                    <th className="text-center px-4 py-3">구분</th>
                    <th className="text-right px-4 py-3">수량</th>
                    <th className="text-right px-4 py-3">가격</th>
                    <th className="text-right px-4 py-3">금액</th>
                    <th className="text-right px-4 py-3">손익</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.content.map((t) => (
                    <tr key={t.id} className="border-b border-gray-800/50 hover:bg-gray-800/40">
                      <td className="px-4 py-2 text-gray-500 text-xs whitespace-nowrap">
                        {new Date(t.tradedAt).toLocaleString("ko-KR", {
                          month: "2-digit", day: "2-digit",
                          hour: "2-digit", minute: "2-digit",
                        })}
                      </td>
                      <td className="px-4 py-2">
                        <div className="font-medium">{t.stockName}</div>
                        <div className="text-gray-500 text-xs">{t.stockCode}</div>
                      </td>
                      <td className="px-4 py-2 text-center">
                        <span className={`px-2 py-0.5 rounded text-xs font-bold ${
                          t.side === "BUY"
                            ? "bg-emerald-900/60 text-emerald-300"
                            : "bg-red-900/60 text-red-300"
                        }`}>
                          {t.side === "BUY" ? "매수" : "매도"}
                        </span>
                      </td>
                      <td className="px-4 py-2 text-right">{t.quantity.toLocaleString()}</td>
                      <td className="px-4 py-2 text-right">{t.price.toLocaleString()}</td>
                      <td className="px-4 py-2 text-right">{t.amount.toLocaleString()}</td>
                      <td className={`px-4 py-2 text-right font-medium ${
                        t.realizedPnl === null ? "text-gray-600"
                        : t.realizedPnl >= 0 ? "text-emerald-400" : "text-red-400"
                      }`}>
                        {t.realizedPnl !== null
                          ? `${t.realizedPnl >= 0 ? "+" : ""}${t.realizedPnl.toLocaleString()}`
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* 페이지네이션 */}
            {trades.totalPages > 1 && (
              <div className="flex justify-center gap-2 mt-4">
                {Array.from({ length: trades.totalPages }, (_, i) => (
                  <a
                    key={i}
                    href={`/trades?mode=${mode}&page=${i}`}
                    className={`px-3 py-1 rounded text-sm ${
                      i === trades.number
                        ? "bg-indigo-600 text-white"
                        : "bg-gray-800 text-gray-400 hover:bg-gray-700"
                    }`}
                  >
                    {i + 1}
                  </a>
                ))}
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
