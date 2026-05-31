"use client";

import { useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import type { AccountBalance, PendingOrder, PnlSummary, Position, Trade, TradePage } from "@/lib/api";
import PositionSummary from "@/components/PositionSummary";
import StatCard from "@/components/StatCard";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8080";
const POLL_MS = 2000;

function fmt(n: number, prefix = "") {
  const abs = Math.abs(n);
  const s = abs >= 1_000_000
    ? `${(abs / 1_000_000).toFixed(2)}M`
    : abs >= 1_000
    ? `${(abs / 1_000).toFixed(1)}K`
    : abs.toFixed(0);
  return `${prefix}${n < 0 ? "-" : ""}${s}`;
}

function fmtMoney(value?: number | null, currency = "KRW") {
  if (value === null || value === undefined) return "-";
  if (currency === "USD") {
    return `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  return `₩${Math.round(value).toLocaleString()}`;
}

function tradeTime(value: string) {
  return new Date(value).toLocaleTimeString("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} failed`);
  return res.json();
}

export default function RealtimeDashboard({
  mode,
  initialSummary,
  initialPositions,
}: {
  mode: "paper" | "live";
  initialSummary: PnlSummary | null;
  initialPositions: Position[];
}) {
  const [summary, setSummary] = useState<PnlSummary | null>(initialSummary);
  const [positions, setPositions] = useState(initialPositions);
  const [pendingOrders, setPendingOrders] = useState<PendingOrder[]>([]);
  const [balance, setBalance] = useState<AccountBalance | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;

    async function refresh() {
      setLoading(true);
      try {
        const [nextSummary, nextPositions, nextPendingOrders, nextBalance, nextTrades] = await Promise.all([
          fetchJson<PnlSummary>(`/api/trades/pnl/summary?mode=${mode}`),
          fetchJson<Position[]>(`/api/command/trade/positions/live?mode=${mode}`),
          fetchJson<PendingOrder[]>(`/api/command/trade/orders/pending?mode=${mode}`),
          fetchJson<AccountBalance>(`/api/command/account/balance?market=domestic&mode=${mode}`),
          fetchJson<TradePage>(`/api/trades?mode=${mode}&page=0&size=8&period=all`),
        ]);
        if (!alive) return;
        setSummary(nextSummary);
        setPositions(nextPositions);
        setPendingOrders(nextPendingOrders);
        setBalance(nextBalance);
        setTrades(nextTrades.content);
        setUpdatedAt(new Date());
      } catch {
        if (alive) setUpdatedAt(new Date());
      } finally {
        if (alive) setLoading(false);
      }
    }

    refresh();
    const timer = setInterval(refresh, POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [mode]);

  const pnlColor = !summary
    ? "default"
    : summary.totalRealizedPnl > 0
    ? "green"
    : summary.totalRealizedPnl < 0
    ? "red"
    : "default";

  const latestTradeId = useMemo(() => trades[0]?.id, [trades]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-end gap-2 text-xs text-gray-500">
        <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
        <span>2초 갱신</span>
        <span>{updatedAt ? updatedAt.toLocaleTimeString("ko-KR") : "연결 중"}</span>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <StatCard
          label="계좌 평가"
          value={balance ? fmtMoney(balance.totalAssets, balance.currency) : "-"}
          sub={balance ? `현금 ${fmtMoney(balance.cash, balance.currency)} · ${balance.positionCount}종목` : undefined}
        />
        <StatCard
          label="누적 실현손익"
          value={summary ? fmt(summary.totalRealizedPnl, "₩") : "-"}
          color={pnlColor}
        />
        <StatCard
          label="승률"
          value={summary ? `${summary.winRate.toFixed(1)}%` : "-"}
          sub={summary ? `${summary.winningTrades}/${summary.totalTrades}건` : undefined}
          color="indigo"
        />
        <StatCard
          label="총 매매"
          value={summary ? `${summary.totalTrades}건` : "-"}
        />
        <StatCard
          label="평균 손익/건"
          value={summary ? fmt(summary.avgPnlPerTrade, "₩") : "-"}
          color={summary && summary.avgPnlPerTrade >= 0 ? "green" : "red"}
        />
      </div>

      <PositionSummary positions={positions} />

      {pendingOrders.length > 0 && (
        <section>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">대기 주문</h2>
            <span className="text-xs text-gray-500">체결통보 대기 {pendingOrders.length}건</span>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-800 text-gray-500 text-xs">
                  <th className="text-left px-4 py-3">구분</th>
                  <th className="text-left px-4 py-3">종목</th>
                  <th className="text-right px-4 py-3">수량</th>
                  <th className="text-right px-4 py-3">주문가</th>
                  <th className="text-right px-4 py-3">주문번호</th>
                </tr>
              </thead>
              <tbody>
                {pendingOrders.map((order) => (
                  <tr key={order.id} className="border-b border-gray-800/50 hover:bg-gray-800/40">
                    <td className="px-4 py-3">
                      <span className={`inline-flex min-w-12 justify-center rounded-md px-2 py-1 text-xs font-semibold ${
                        order.side === "BUY"
                          ? "bg-emerald-950 text-emerald-300 border border-emerald-900"
                          : "bg-red-950 text-red-300 border border-red-900"
                      }`}>
                        {order.side === "BUY" ? "매수" : "매도"}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <div className="font-medium">{order.stockName}</div>
                      <div className="text-xs text-gray-500">{order.stockCode}</div>
                    </td>
                    <td className="px-4 py-3 text-right">
                      {order.filledQuantity.toLocaleString()}/{order.quantity.toLocaleString()}
                    </td>
                    <td className="px-4 py-3 text-right">{fmtMoney(order.requestedPrice, order.currency)}</td>
                    <td className="px-4 py-3 text-right text-gray-500">{order.orderNo}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">실시간 매매 흐름</h2>
          <span className="text-xs text-gray-500">최근 {trades.length}건</span>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
          {trades.length === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-gray-600">
              아직 매매 체결이 없습니다.
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-800 text-gray-500 text-xs">
                  <th className="text-left px-4 py-3">시간</th>
                  <th className="text-left px-4 py-3">구분</th>
                  <th className="text-left px-4 py-3">종목</th>
                  <th className="text-right px-4 py-3">수량</th>
                  <th className="text-right px-4 py-3">가격</th>
                  <th className="text-right px-4 py-3">손익</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((trade) => (
                  <tr
                    key={trade.id}
                    className={`border-b border-gray-800/50 hover:bg-gray-800/40 ${
                      trade.id === latestTradeId ? "bg-gray-800/30" : ""
                    }`}
                  >
                    <td className="px-4 py-3 text-gray-400">{tradeTime(trade.tradedAt)}</td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex min-w-12 justify-center rounded-md px-2 py-1 text-xs font-semibold ${
                        trade.side === "BUY"
                          ? "bg-emerald-950 text-emerald-300 border border-emerald-900"
                          : "bg-red-950 text-red-300 border border-red-900"
                      }`}>
                        {trade.side === "BUY" ? "매수" : "매도"}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <div className="font-medium">{trade.stockName}</div>
                      <div className="text-xs text-gray-500">{trade.stockCode}</div>
                    </td>
                    <td className="px-4 py-3 text-right">{trade.quantity.toLocaleString()}</td>
                    <td className="px-4 py-3 text-right">{fmtMoney(trade.price, trade.currency)}</td>
                    <td className={`px-4 py-3 text-right ${(trade.realizedPnl ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      {trade.side === "SELL" ? fmtMoney(trade.realizedPnl, trade.currency) : "-"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>
    </div>
  );
}
