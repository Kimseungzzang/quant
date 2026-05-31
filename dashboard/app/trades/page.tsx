import { api } from "@/lib/api";
import StatCard from "@/components/StatCard";
import PnlChart from "./PnlChart";

const PERIODS = [
  { key: "today", label: "오늘", days: 1 },
  { key: "week", label: "1주", days: 7 },
  { key: "month", label: "1달", days: 30 },
  { key: "quarter", label: "3달", days: 90 },
  { key: "all", label: "전체", days: 365 },
];

function fmtPnl(n: number | null) {
  if (n === null) return "—";
  const s = Math.abs(n) >= 1000 ? `${(Math.abs(n) / 1000).toFixed(1)}K` : Math.abs(n).toFixed(0);
  return `${n < 0 ? "-" : "+"}₩${s}`;
}

export default async function TradesPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string; page?: string; period?: string; stockCode?: string }>;
}) {
  const { mode = "paper", page = "0", period = "month", stockCode = "" } = await searchParams;
  const selectedPeriod = PERIODS.some((p) => p.key === period) ? period : "month";
  const chartDays = PERIODS.find((p) => p.key === selectedPeriod)?.days ?? 30;
  const pageNum = parseInt(page, 10);

  const [tradesResult, summaryResult, chartResult, performanceResult] = await Promise.allSettled([
    api.trades.list(mode, pageNum, selectedPeriod, stockCode),
    api.trades.pnlSummary(mode),
    api.trades.pnlChart(mode, chartDays),
    api.trades.stockPerformance(mode, selectedPeriod),
  ]);

  const trades  = tradesResult.status  === "fulfilled" ? tradesResult.value  : null;
  const summary = summaryResult.status === "fulfilled" ? summaryResult.value : null;
  const chart   = chartResult.status   === "fulfilled" ? chartResult.value   : [];
  const performance = performanceResult.status === "fulfilled" ? performanceResult.value : [];

  return (
    <div className="space-y-6 max-w-5xl">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <h1 className="text-xl font-bold">매매 이력</h1>
        <div className="flex gap-2">
          <nav className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 text-sm">
            {["paper", "live"].map((m) => (
              <a
                key={m}
                href={`/trades?mode=${m}&period=${selectedPeriod}${stockCode ? `&stockCode=${stockCode}` : ""}`}
                className={`px-3 py-1 rounded-md transition-colors ${
                  mode === m ? "bg-indigo-600 text-white" : "text-gray-400 hover:text-gray-100"
                }`}
              >
                {m === "paper" ? "모의" : "실전"}
              </a>
            ))}
          </nav>
          <nav className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 text-sm">
            {PERIODS.map((p) => (
              <a
                key={p.key}
                href={`/trades?mode=${mode}&period=${p.key}${stockCode ? `&stockCode=${stockCode}` : ""}`}
                className={`px-3 py-1 rounded-md transition-colors ${
                  selectedPeriod === p.key ? "bg-gray-700 text-white" : "text-gray-400 hover:text-gray-100"
                }`}
              >
                {p.label}
              </a>
            ))}
          </nav>
        </div>
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
              <h2 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wide">누적 손익 곡선</h2>
              <PnlChart data={chart} />
            </section>
      )}

      {performance.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wide">종목별 성과</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {performance.slice(0, 6).map((s) => (
              <a
                key={s.stockCode}
                href={`/trades?mode=${mode}&period=${selectedPeriod}&stockCode=${s.stockCode}`}
                className={`block bg-gray-900 border rounded-xl p-4 transition-colors ${
                  stockCode === s.stockCode ? "border-indigo-500" : "border-gray-800 hover:border-gray-700"
                }`}
              >
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="font-medium">{s.stockName}</div>
                    <div className="text-xs text-gray-500">{s.stockCode} · {s.tradePairs}건 · 승률 {s.winRate.toFixed(1)}%</div>
                  </div>
                  <div className={`text-right font-semibold ${s.totalPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                    {fmtPnl(s.totalPnl)}
                  </div>
                </div>
              </a>
            ))}
          </div>
        </section>
      )}

      {/* 매매 이력 테이블 */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">
            거래 내역{stockCode ? ` · ${stockCode}` : ""}
          </h2>
          {stockCode && (
            <a href={`/trades?mode=${mode}&period=${selectedPeriod}`} className="text-xs text-indigo-400 hover:text-indigo-300">
              필터 해제
            </a>
          )}
        </div>
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
                    href={`/trades?mode=${mode}&period=${selectedPeriod}&page=${i}${stockCode ? `&stockCode=${stockCode}` : ""}`}
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
