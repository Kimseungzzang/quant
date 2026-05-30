import { api } from "@/lib/api";
import BacktestRunButton from "./BacktestRunButton";

function badge(value: number, good: "high" | "low" = "high") {
  const positive = good === "high" ? value >= 0 : value <= 0;
  return positive ? "text-emerald-400" : "text-red-400";
}

export default async function BacktestPage({
  searchParams,
}: {
  searchParams: Promise<{ market?: string }>;
}) {
  const { market = "domestic" } = await searchParams;
  const results = await api.backtest.list(market, 30).catch(() => []);

  return (
    <div className="space-y-6 max-w-5xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">백테스트 결과</h1>
        <div className="flex gap-3 items-center">
          <nav className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 text-sm">
            {["domestic", "overseas"].map((m) => (
              <a
                key={m}
                href={`/backtest?market=${m}`}
                className={`px-3 py-1 rounded-md transition-colors ${
                  market === m ? "bg-indigo-600 text-white" : "text-gray-400 hover:text-gray-100"
                }`}
              >
                {m === "domestic" ? "국내" : "미국"}
              </a>
            ))}
          </nav>
          <BacktestRunButton market={market} />
        </div>
      </div>

      {results.length === 0 ? (
        <p className="text-gray-600 text-sm">백테스트 결과가 없습니다.</p>
      ) : (
        <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-800 text-gray-500 text-xs">
                <th className="text-left px-4 py-3">종목</th>
                <th className="text-right px-4 py-3">기간</th>
                <th className="text-right px-4 py-3">수익률</th>
                <th className="text-right px-4 py-3">승률</th>
                <th className="text-right px-4 py-3">MDD</th>
                <th className="text-right px-4 py-3">Sharpe</th>
                <th className="text-right px-4 py-3">거래수</th>
                <th className="text-right px-4 py-3">실행일시</th>
              </tr>
            </thead>
            <tbody>
              {results.map((r) => (
                <tr key={r.id} className="border-b border-gray-800/50 hover:bg-gray-800/40">
                  <td className="px-4 py-3">
                    <div className="font-medium">{r.stockName}</div>
                    <div className="text-gray-500 text-xs">{r.stockCode}</div>
                  </td>
                  <td className="px-4 py-3 text-right text-gray-400">{r.periodDays}일</td>
                  <td className={`px-4 py-3 text-right font-bold ${badge(r.totalReturnPct)}`}>
                    {r.totalReturnPct >= 0 ? "+" : ""}{r.totalReturnPct?.toFixed(2)}%
                  </td>
                  <td className="px-4 py-3 text-right">{r.winRatePct?.toFixed(1)}%</td>
                  <td className="px-4 py-3 text-right text-red-400">
                    -{r.maxDrawdownPct?.toFixed(2)}%
                  </td>
                  <td className={`px-4 py-3 text-right ${badge(r.sharpeRatio)}`}>
                    {r.sharpeRatio?.toFixed(2)}
                  </td>
                  <td className="px-4 py-3 text-right text-gray-400">{r.tradeCount}</td>
                  <td className="px-4 py-3 text-right text-gray-500 text-xs whitespace-nowrap">
                    {new Date(r.runAt).toLocaleString("ko-KR", {
                      month: "2-digit", day: "2-digit",
                      hour: "2-digit", minute: "2-digit",
                    })}
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
