import { api } from "@/lib/api";
import StatCard from "@/components/StatCard";

const PERIODS = [
  { key: "today", label: "오늘" },
  { key: "week", label: "1주" },
  { key: "month", label: "1달" },
  { key: "quarter", label: "3달" },
  { key: "all", label: "전체" },
];

function fmtWon(value: number) {
  return `${value >= 0 ? "+" : "-"}₩${Math.abs(Math.round(value)).toLocaleString()}`;
}

export default async function ReportPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string; period?: string }>;
}) {
  const { mode = "paper", period = "month" } = await searchParams;
  const selectedPeriod = PERIODS.some((p) => p.key === period) ? period : "month";

  const [daily, stocks] = await Promise.all([
    api.trades.dailyReports(mode, selectedPeriod).catch(() => []),
    api.trades.stockPerformance(mode, selectedPeriod).catch(() => []),
  ]);

  const totalPnl = daily.reduce((sum, d) => sum + d.totalPnl, 0);
  const trades = daily.reduce((sum, d) => sum + d.tradePairs, 0);
  const wins = daily.reduce((sum, d) => sum + d.wins, 0);
  const winRate = trades > 0 ? wins / trades * 100 : 0;

  return (
    <div className="space-y-6 max-w-6xl">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <h1 className="text-xl font-bold">데일리 리포트</h1>
        <div className="flex gap-2">
          <nav className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 text-sm">
            {["paper", "live"].map((m) => (
              <a
                key={m}
                href={`/report?mode=${m}&period=${selectedPeriod}`}
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
                href={`/report?mode=${mode}&period=${p.key}`}
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

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label="기간 손익" value={fmtWon(totalPnl)} color={totalPnl >= 0 ? "green" : "red"} />
        <StatCard label="승률" value={`${winRate.toFixed(1)}%`} sub={`${wins}/${trades}건`} color="indigo" />
        <StatCard label="청산 거래" value={`${trades}건`} />
        <StatCard label="거래 종목" value={`${stocks.length}개`} />
      </div>

      <section>
        <h2 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wide">일별 손익</h2>
        {daily.length === 0 ? (
          <p className="text-sm text-gray-600">기간 내 청산 거래가 없습니다.</p>
        ) : (
          <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-800 text-gray-500 text-xs">
                  <th className="text-left px-4 py-3">날짜</th>
                  <th className="text-right px-4 py-3">거래</th>
                  <th className="text-right px-4 py-3">승률</th>
                  <th className="text-right px-4 py-3">총 손익</th>
                  <th className="text-right px-4 py-3">최대</th>
                  <th className="text-right px-4 py-3">최소</th>
                </tr>
              </thead>
              <tbody>
                {daily.map((d) => (
                  <tr key={d.date} className="border-b border-gray-800/50 hover:bg-gray-800/40">
                    <td className="px-4 py-3 text-gray-300">{d.date}</td>
                    <td className="px-4 py-3 text-right">{d.tradePairs}</td>
                    <td className="px-4 py-3 text-right">{d.winRate.toFixed(1)}%</td>
                    <td className={`px-4 py-3 text-right font-medium ${d.totalPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      {fmtWon(d.totalPnl)}
                    </td>
                    <td className="px-4 py-3 text-right text-emerald-400">{fmtWon(d.maxPnl)}</td>
                    <td className="px-4 py-3 text-right text-red-400">{fmtWon(d.minPnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section>
        <h2 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wide">종목별 성과</h2>
        {stocks.length === 0 ? (
          <p className="text-sm text-gray-600">종목별 성과가 없습니다.</p>
        ) : (
          <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-800 text-gray-500 text-xs">
                  <th className="text-left px-4 py-3">종목</th>
                  <th className="text-right px-4 py-3">거래</th>
                  <th className="text-right px-4 py-3">승률</th>
                  <th className="text-right px-4 py-3">총 손익</th>
                  <th className="text-right px-4 py-3">평균</th>
                </tr>
              </thead>
              <tbody>
                {stocks.map((s) => (
                  <tr key={s.stockCode} className="border-b border-gray-800/50 hover:bg-gray-800/40">
                    <td className="px-4 py-3">
                      <div className="font-medium">{s.stockName}</div>
                      <div className="text-xs text-gray-500">{s.stockCode}</div>
                    </td>
                    <td className="px-4 py-3 text-right">{s.tradePairs}</td>
                    <td className="px-4 py-3 text-right">{s.winRate.toFixed(1)}%</td>
                    <td className={`px-4 py-3 text-right font-medium ${s.totalPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      {fmtWon(s.totalPnl)}
                    </td>
                    <td className="px-4 py-3 text-right">{fmtWon(s.avgPnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
