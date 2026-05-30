import { api } from "@/lib/api";
import AnalyzeButton from "./AnalyzeButton";
import Link from "next/link";

const HORIZONS = [
  { key: "long", label: "장타", caption: "일봉 추세" },
  { key: "swing", label: "스윙", caption: "일봉 눌림/돌파" },
  { key: "daytrade", label: "단타", caption: "1/5/15분봉" },
] as const;

function fmtDate(iso: string) {
  const d = new Date(iso);
  return d.toLocaleDateString("ko-KR", { month: "2-digit", day: "2-digit" })
    + " " + d.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
}

function formatTradingValue(value?: number) {
  const amount = value ?? 0;
  if (amount >= 1_000_000_000_000) return `${(amount / 1_000_000_000_000).toFixed(1)}조`;
  if (amount >= 100_000_000) return `${Math.round(amount / 100_000_000).toLocaleString()}억`;
  return `${Math.round(amount / 10_000).toLocaleString()}만`;
}

export default async function AnalysisPage({
  searchParams,
}: {
  searchParams: Promise<{ market?: string; horizon?: string; runId?: string }>;
}) {
  const { market = "domestic", horizon: rawHorizon = "swing", runId } = await searchParams;
  const horizon = HORIZONS.some((h) => h.key === rawHorizon) ? rawHorizon : "swing";
  const selectedHorizon = HORIZONS.find((h) => h.key === horizon)!;

  const [runs, results] = await Promise.all([
    api.analysis.runs(market, horizon).catch(() => []),
    runId
      ? api.analysis.byRun(Number(runId)).catch(() => [])
      : api.analysis.latest(market, horizon).catch(() => []),
  ]);

  const selectedRun = runId
    ? runs.find((r) => r.id === Number(runId))
    : runs[0];

  return (
    <div className="space-y-5 max-w-6xl">
      {/* 헤더 */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-bold">추천 종목 분석</h1>
          {selectedRun && (
            <p className="text-xs text-gray-500 mt-1">
              {fmtDate(selectedRun.runAt)} 기준 · {selectedHorizon.label} · {selectedRun.resultCount}개 종목
            </p>
          )}
        </div>
        <div className="flex gap-3 items-center flex-wrap">
          {/* 시장 탭 */}
          <nav className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 text-sm">
            {["domestic", "overseas"].map((m) => (
              <Link
                key={m}
                href={`/analysis?market=${m}&horizon=${horizon}`}
                className={`px-3 py-1 rounded-md transition-colors ${
                  market === m
                    ? "bg-indigo-600 text-white"
                    : "text-gray-400 hover:text-gray-100"
                }`}
              >
                {m === "domestic" ? "국내" : "미국"}
              </Link>
            ))}
          </nav>
          <AnalyzeButton market={market} horizon={horizon} />
        </div>
      </div>

      <nav className="grid grid-cols-3 gap-2">
        {HORIZONS.map((h) => (
          <Link
            key={h.key}
            href={`/analysis?market=${market}&horizon=${h.key}`}
            className={`rounded-lg border px-4 py-3 transition-colors ${
              horizon === h.key
                ? "border-indigo-500 bg-indigo-600/15 text-white"
                : "border-gray-800 bg-gray-900 text-gray-400 hover:text-gray-100"
            }`}
          >
            <div className="text-sm font-semibold">{h.label}</div>
            <div className="text-xs text-gray-500 mt-1">{h.caption}</div>
          </Link>
        ))}
      </nav>

      <div className="flex gap-5">
        {/* 날짜 목록 사이드바 */}
        {runs.length > 0 && (
          <aside className="w-44 shrink-0 space-y-1">
            <p className="text-xs text-gray-500 uppercase tracking-wide px-1 mb-2">분석 이력</p>
            {runs.map((run) => {
              const isSelected = selectedRun?.id === run.id;
              return (
                <Link
                  key={run.id}
                  href={`/analysis?market=${market}&horizon=${horizon}&runId=${run.id}`}
                  className={`block px-3 py-2 rounded-lg text-xs transition-colors ${
                    isSelected
                      ? "bg-indigo-600 text-white"
                      : "text-gray-400 hover:bg-gray-800 hover:text-gray-100"
                  }`}
                >
                  <div className="font-medium">
                    {new Date(run.runAt).toLocaleDateString("ko-KR", {
                      month: "2-digit", day: "2-digit",
                    })}
                  </div>
                  <div className={`${isSelected ? "text-indigo-200" : "text-gray-600"}`}>
                    {new Date(run.runAt).toLocaleTimeString("ko-KR", {
                      hour: "2-digit", minute: "2-digit",
                    })} · {run.resultCount}개
                  </div>
                </Link>
              );
            })}
          </aside>
        )}

        {/* 결과 테이블 */}
        <div className="flex-1 min-w-0">
          {results.length === 0 ? (
            <div className="text-gray-600 text-sm py-8 text-center">
              {runs.length === 0
                ? "분석 결과가 없습니다. 분석 실행 버튼을 눌러주세요."
                : "선택한 날짜의 결과가 없습니다."}
            </div>
          ) : (
            <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-800 text-gray-500 text-xs">
                    <th className="text-left px-4 py-3 w-10">순위</th>
                    <th className="text-left px-4 py-3">종목</th>
                    <th className="text-right px-4 py-3">현재가</th>
                    <th className="text-right px-4 py-3">등락</th>
                    <th className="text-right px-4 py-3">거래대금</th>
                    <th className="text-right px-4 py-3">점수</th>
                    <th className="text-right px-4 py-3">승률</th>
                    <th className="text-right px-4 py-3">백테스트</th>
                    <th className="text-right px-4 py-3">MDD</th>
                    <th className="text-right px-4 py-3">거래수</th>
                  </tr>
                </thead>
                <tbody>
                  {results.map((r) => (
                    <tr key={r.id} className="border-b border-gray-800/50 hover:bg-gray-800/40">
                      <td className="px-4 py-3">
                        <span className="w-6 h-6 inline-flex items-center justify-center bg-indigo-900/60 text-indigo-300 rounded-full text-xs font-bold">
                          {r.rank}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <div className="font-medium">{r.stockName}</div>
                        <div className="text-gray-500 text-xs">{r.stockCode}</div>
                      </td>
                      <td className="px-4 py-3 text-right">{r.currentPrice?.toLocaleString()}</td>
                      <td className={`px-4 py-3 text-right font-medium ${(r.changePct ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                        {(r.changePct ?? 0) >= 0 ? "+" : ""}{r.changePct?.toFixed(2)}%
                      </td>
                      <td className="px-4 py-3 text-right text-gray-300">{formatTradingValue(r.tradingValue)}</td>
                      <td className="px-4 py-3 text-right text-indigo-300 font-bold">{r.finalScore?.toFixed(1)}</td>
                      <td className="px-4 py-3 text-right">{r.winRatePct?.toFixed(1)}%</td>
                      <td className={`px-4 py-3 text-right ${(r.backtestReturn ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                        {(r.backtestReturn ?? 0) >= 0 ? "+" : ""}{r.backtestReturn?.toFixed(2)}%
                      </td>
                      <td className="px-4 py-3 text-right text-red-400">
                        -{r.maxDrawdown?.toFixed(2)}%
                      </td>
                      <td className="px-4 py-3 text-right text-gray-400">{r.tradeCount}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
