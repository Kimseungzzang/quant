import type { Position } from "@/lib/api";
import Link from "next/link";

function fmtWon(value?: number | null) {
  if (value === null || value === undefined) return "—";
  const abs = Math.abs(value);
  const text = abs >= 100_000_000
    ? `${(abs / 100_000_000).toFixed(1)}억`
    : abs >= 10_000
    ? `${Math.round(abs / 10_000).toLocaleString()}만`
    : Math.round(abs).toLocaleString();
  return `${value < 0 ? "-" : ""}₩${text}`;
}

function fmtMoney(value?: number | null, currency = "KRW") {
  if (currency === "USD") {
    if (value === null || value === undefined) return "—";
    return `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  return fmtWon(value);
}

function fmtPct(value?: number | null) {
  if (value === null || value === undefined) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

export default function PositionSummary({ positions }: { positions: Position[] }) {
  const totals = positions.reduce(
    (acc, p) => {
      const key = p.currency === "USD" ? "USD" : "KRW";
      acc[key].value += p.marketValue ?? 0;
      acc[key].pnl += p.unrealizedPnl ?? 0;
      return acc;
    },
    { KRW: { value: 0, pnl: 0 }, USD: { value: 0, pnl: 0 } },
  );
  const totalText = [
    totals.KRW.value || totals.KRW.pnl
      ? `KRW 평가 ${fmtMoney(totals.KRW.value)} · 미실현 ${fmtMoney(totals.KRW.pnl)}`
      : "",
    totals.USD.value || totals.USD.pnl
      ? `USD 평가 ${fmtMoney(totals.USD.value, "USD")} · 미실현 ${fmtMoney(totals.USD.pnl, "USD")}`
      : "",
  ].filter(Boolean).join(" / ") || "평가 ₩0 · 미실현 ₩0";

  return (
    <section>
      <div className="flex items-center justify-between mb-3">
        <div>
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">보유 포지션</h2>
          <p className="text-xs text-gray-500 mt-1">
            {totalText}
          </p>
        </div>
        <Link href="/positions" className="text-xs text-indigo-400 hover:text-indigo-300">전체 보기 →</Link>
      </div>

      {positions.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl px-4 py-10 text-center text-sm text-gray-600">
          보유 포지션이 없습니다.
        </div>
      ) : (
        <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-800 text-gray-500 text-xs">
                <th className="text-left px-4 py-3">종목</th>
                <th className="text-right px-4 py-3">수량</th>
                <th className="text-right px-4 py-3">평가금액</th>
                <th className="text-right px-4 py-3">미실현</th>
              </tr>
            </thead>
            <tbody>
              {positions.slice(0, 5).map((p) => (
                <tr key={p.id} className="border-b border-gray-800/50 hover:bg-gray-800/40">
                  <td className="px-4 py-3">
                    <div className="font-medium">{p.stockName}</div>
                    <div className="text-xs text-gray-500">{p.stockCode}</div>
                  </td>
                  <td className="px-4 py-3 text-right">{p.quantity.toLocaleString()}</td>
                  <td className="px-4 py-3 text-right">{fmtMoney(p.marketValue, p.currency)}</td>
                  <td className={`px-4 py-3 text-right font-medium ${(p.unrealizedPnl ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                    <div>{fmtMoney(p.unrealizedPnl, p.currency)}</div>
                    <div className="text-xs">{fmtPct(p.unrealizedPct)}</div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
