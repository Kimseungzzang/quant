"use client";

import { useState, useEffect, useCallback } from "react";
import { TrendingUp, TrendingDown, Minus, Activity, Clock, Zap, AlertTriangle } from "lucide-react";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8084";

interface Regime {
  trend: "up" | "down" | "sideways";
  trend_strength: number;
  volatility: "low" | "normal" | "high";
  session: string;
  index_change_pct: number;
  preferred_strategies: string[];
  tradeable: boolean;
  reason: string;
}

interface RegimeResponse {
  domestic: Regime | null;
  overseas: Regime | null;
  // 기존 단일 포맷 호환 필드들
  trend?: string;
}

const SESSION_LABEL: Record<string, string> = {
  pre:       "장 전",
  opening:   "개장",
  morning:   "오전",
  midday:    "점심",
  afternoon: "오후장",
  closing:   "마감",
};

const STRATEGY_LABEL: Record<string, string> = {
  breakout:  "돌파",
  pullback:  "눌림목",
  gap:       "갭",
  afternoon: "오후 반등",
};

const STRATEGY_COLOR: Record<string, string> = {
  breakout:  "bg-blue-900/60 text-blue-300 border-blue-800",
  pullback:  "bg-violet-900/60 text-violet-300 border-violet-800",
  gap:       "bg-amber-900/60 text-amber-300 border-amber-800",
  afternoon: "bg-teal-900/60 text-teal-300 border-teal-800",
};

function RegimeCard({ regime, label, indexLabel }: { regime: Regime; label: string; indexLabel: string }) {
  const TrendIcon = regime.trend === "up" ? TrendingUp : regime.trend === "down" ? TrendingDown : Minus;
  const trendColor = regime.trend === "up" ? "text-emerald-400" : regime.trend === "down" ? "text-red-400" : "text-gray-400";
  const trendLabel = regime.trend === "up" ? "상승" : regime.trend === "down" ? "하락" : "횡보";
  const volColor = { low: "text-blue-400", normal: "text-gray-400", high: "text-orange-400" }[regime.volatility] ?? "text-gray-400";
  const volLabel = { low: "낮음", normal: "보통", high: "높음" }[regime.volatility] ?? "-";

  return (
    <div className={`bg-gray-800/40 rounded-xl p-4 space-y-3 border ${regime.tradeable ? "border-gray-700" : "border-orange-900"}`}>
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-gray-300">{label}</span>
        <div className="flex items-center gap-1 text-xs text-gray-500">
          <Clock size={10} />
          {SESSION_LABEL[regime.session] ?? regime.session}
          {regime.reason && regime.reason !== "" && (
            <span className="ml-1 text-gray-600">({regime.reason})</span>
          )}
        </div>
      </div>

      {!regime.tradeable && (
        <div className="flex items-center gap-1.5 bg-orange-900/30 border border-orange-800 rounded-lg px-2 py-1.5 text-xs text-orange-300">
          <AlertTriangle size={11} /> {regime.reason || "거래 시간 외"}
        </div>
      )}

      <div className="grid grid-cols-3 gap-2">
        <div className="bg-gray-900/60 rounded-lg p-2 text-center">
          <TrendIcon size={14} className={`mx-auto mb-0.5 ${trendColor}`} />
          <p className={`text-xs font-bold ${trendColor}`}>{trendLabel}</p>
          <p className="text-xs text-gray-600">{regime.trend_strength.toFixed(0)}</p>
        </div>
        <div className="bg-gray-900/60 rounded-lg p-2 text-center">
          <Activity size={14} className="mx-auto mb-0.5 text-indigo-400" />
          <p className={`text-xs font-bold ${regime.index_change_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>
            {regime.index_change_pct >= 0 ? "+" : ""}{regime.index_change_pct.toFixed(2)}%
          </p>
          <p className="text-xs text-gray-600">{indexLabel}</p>
        </div>
        <div className="bg-gray-900/60 rounded-lg p-2 text-center">
          <Zap size={14} className={`mx-auto mb-0.5 ${volColor}`} />
          <p className={`text-xs font-bold ${volColor}`}>{volLabel}</p>
          <p className="text-xs text-gray-600">변동성</p>
        </div>
      </div>

      <div className="flex flex-wrap gap-1">
        {regime.preferred_strategies.length > 0 ? (
          regime.preferred_strategies.map((s) => (
            <span key={s} className={`px-1.5 py-0.5 rounded text-xs font-medium border ${STRATEGY_COLOR[s] ?? "bg-gray-800 text-gray-400 border-gray-700"}`}>
              {STRATEGY_LABEL[s] ?? s}
            </span>
          ))
        ) : (
          <span className="text-xs text-gray-600">전략 없음</span>
        )}
      </div>
    </div>
  );
}

export default function MarketRegimePanel() {
  const [data, setData]       = useState<RegimeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState(false);

  const loadRegime = useCallback(async () => {
    try {
      const res = await window.fetch(`${BASE}/api/command/regime`, { cache: "no-store" });
      if (!res.ok) throw new Error();
      setData(await res.json());
      setError(false);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadRegime();
    const id = setInterval(loadRegime, 60_000);
    return () => clearInterval(id);
  }, [loadRegime]);

  if (loading) return <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 animate-pulse h-40" />;
  if (error || !data) return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 flex items-center gap-2 text-gray-600 text-sm">
      <AlertTriangle size={14} /> 장세 분석 불가
    </div>
  );

  const domestic = data.domestic;
  const overseas = data.overseas;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-3">
      <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">장세 분석</h2>
      <div className="grid grid-cols-2 gap-3">
        {domestic && <RegimeCard regime={domestic} label="🇰🇷 국내" indexLabel="KOSPI" />}
        {overseas && <RegimeCard regime={overseas} label="🇺🇸 미국" indexLabel="S&P" />}
      </div>
    </div>
  );
}
