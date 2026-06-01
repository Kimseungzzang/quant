"use client";

import type { Position } from "@/lib/api";
import { RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8080";

function fmtWon(value?: number | null) {
  if (value === null || value === undefined) return "—";
  return `₩${Math.round(value).toLocaleString()}`;
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

function holdingTime(openedAt: string) {
  const diff = Date.now() - new Date(openedAt).getTime();
  if (diff < 0) return "—";
  const minutes = Math.floor(diff / 60000);
  if (minutes < 60) return `${minutes}분`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours < 24) return `${hours}시간 ${rest}분`;
  return `${Math.floor(hours / 24)}일 ${hours % 24}시간`;
}

export default function PositionsClient({ initial, mode }: { initial: Position[]; mode: string }) {
  const [positions, setPositions] = useState(initial);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    async function refresh() {
      setLoading(true);
      try {
        const res = await fetch(`${API_BASE}/api/command/trade/positions/live?mode=${mode}`, { cache: "no-store" });
        if (!res.ok) return;
        const data = await res.json();
        if (alive) {
          setPositions(data);
          setUpdatedAt(new Date());
        }
      } finally {
        if (alive) setLoading(false);
      }
    }

    const timer = setInterval(refresh, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [mode]);

  const totals = useMemo(() => {
    return positions.reduce(
      (acc, p) => {
        const key = p.currency === "USD" ? "USD" : "KRW";
        acc[key].value += p.marketValue ?? 0;
        acc[key].pnl += p.unrealizedPnl ?? 0;
        return acc;
      },
      { KRW: { value: 0, pnl: 0 }, USD: { value: 0, pnl: 0 } },
    );
  }, [positions]);

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between gap-4">
        <div>
          <p className="text-sm text-gray-500">
            KRW 평가 {fmtMoney(totals.KRW.value)} · USD 평가 {fmtMoney(totals.USD.value, "USD")}
          </p>
          <p className={(totals.KRW.pnl + totals.USD.pnl) >= 0 ? "text-emerald-400 font-semibold" : "text-red-400 font-semibold"}>
            미실현 KRW {fmtMoney(totals.KRW.pnl)} · USD {fmtMoney(totals.USD.pnl, "USD")}
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-gray-500">
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
          2초 갱신
          {updatedAt ? updatedAt.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "-"}
        </div>
      </div>

      {positions.length === 0 ? (
        <div className="text-sm text-gray-600 py-8 text-center">보유 포지션이 없습니다.</div>
      ) : (
        <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-800 text-gray-500 text-xs">
                <th className="text-left px-4 py-3">종목</th>
                <th className="text-right px-4 py-3">수량</th>
                <th className="text-right px-4 py-3">평균가</th>
                <th className="text-right px-4 py-3">현재가</th>
                <th className="text-right px-4 py-3">평가금액</th>
                <th className="text-right px-4 py-3">미실현 손익</th>
                <th className="text-right px-4 py-3">보유시간</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => {
                const pnlPositive = (p.unrealizedPnl ?? 0) >= 0;
                const priceUp = p.currentPrice >= p.avgPrice;
                return (
                <tr key={p.id} className="border-b border-gray-800/50 hover:bg-gray-800/40">
                  <td className="px-4 py-3">
                    <div className="font-medium">{p.stockName}</div>
                    <div className="text-xs text-gray-500 flex items-center gap-1.5 mt-0.5">
                      <span>{p.stockCode}</span>
                      {p.strategy && (
                        <span className="px-1.5 py-0.5 rounded text-xs bg-indigo-900/60 text-indigo-300">
                          {p.strategy}
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-right">{p.quantity.toLocaleString()}</td>
                  <td className="px-4 py-3 text-right text-gray-300">{fmtMoney(p.avgPrice, p.currency)}</td>
                  <td className={`px-4 py-3 text-right font-medium ${priceUp ? "text-emerald-400" : "text-red-400"}`}>
                    {fmtMoney(p.currentPrice, p.currency)}
                  </td>
                  <td className="px-4 py-3 text-right">{fmtMoney(p.marketValue, p.currency)}</td>
                  <td className={`px-4 py-3 text-right font-medium ${pnlPositive ? "text-emerald-400" : "text-red-400"}`}>
                    <div>{pnlPositive ? "+" : ""}{fmtMoney(p.unrealizedPnl, p.currency)}</div>
                    <div className="text-xs">{fmtPct(p.unrealizedPct)}</div>
                  </td>
                  <td className="px-4 py-3 text-right text-gray-400">{holdingTime(p.openedAt)}</td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
