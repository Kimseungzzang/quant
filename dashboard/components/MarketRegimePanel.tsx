"use client";

import { useState, useEffect, useCallback } from "react";
import { TrendingUp, TrendingDown, Minus, Activity, Clock, Zap, AlertTriangle } from "lucide-react";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8080";

interface Regime {
  trend: "up" | "down" | "sideways";
  trend_strength: number;
  volatility: "low" | "normal" | "high";
  session: "pre" | "opening" | "morning" | "midday" | "afternoon" | "closing";
  index_change_pct: number;
  preferred_strategies: string[];
  tradeable: boolean;
  reason: string;
}

const SESSION_LABEL: Record<string, string> = {
  pre:       "장 전",
  opening:   "개장",
  morning:   "오전 본장",
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

export default function MarketRegimePanel() {
  const [regime, setRegime]     = useState<Regime | null>(null);
  const [loading, setLoading]   = useState(true);
  const [error, setError]       = useState(false);

  const loadRegime = useCallback(async () => {
    try {
      const res = await window.fetch(`${BASE}/api/command/regime`, { cache: "no-store" });
      if (!res.ok) throw new Error();
      setRegime(await res.json());
      setError(false);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadRegime();
    const id = setInterval(loadRegime, 60_000); // 1분마다 갱신
    return () => clearInterval(id);
  }, [loadRegime]);

  if (loading) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 animate-pulse h-40" />
    );
  }

  if (error || !regime) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 flex items-center gap-2 text-gray-600 text-sm">
        <AlertTriangle size={14} /> 장세 분석 불가
      </div>
    );
  }

  const TrendIcon = regime.trend === "up"
    ? TrendingUp
    : regime.trend === "down"
    ? TrendingDown
    : Minus;

  const trendColor = regime.trend === "up"
    ? "text-emerald-400"
    : regime.trend === "down"
    ? "text-red-400"
    : "text-gray-400";

  const trendLabel = regime.trend === "up" ? "상승" : regime.trend === "down" ? "하락" : "횡보";

  const volColor = {
    low:    "text-blue-400",
    normal: "text-gray-400",
    high:   "text-orange-400",
  }[regime.volatility];

  const volLabel = { low: "낮음", normal: "보통", high: "높음" }[regime.volatility];

  return (
    <div className={`bg-gray-900 border rounded-xl p-5 space-y-4 ${
      regime.tradeable ? "border-gray-800" : "border-orange-900"
    }`}>
      {/* 헤더 */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">장세 분석</h2>
        <div className="flex items-center gap-1.5 text-xs text-gray-500">
          <Clock size={11} />
          {SESSION_LABEL[regime.session]}
        </div>
      </div>

      {/* 매매 불가 경고 */}
      {!regime.tradeable && (
        <div className="flex items-center gap-2 bg-orange-900/30 border border-orange-800 rounded-lg px-3 py-2 text-xs text-orange-300">
          <AlertTriangle size={12} />
          {regime.reason || "매매 불가 장세"}
        </div>
      )}

      {/* 핵심 지표 */}
      <div className="grid grid-cols-3 gap-3">
        {/* 추세 */}
        <div className="bg-gray-800/60 rounded-lg p-3 text-center">
          <TrendIcon size={18} className={`mx-auto mb-1 ${trendColor}`} />
          <p className={`text-sm font-bold ${trendColor}`}>{trendLabel}</p>
          <p className="text-xs text-gray-600 mt-0.5">강도 {regime.trend_strength.toFixed(0)}</p>
        </div>

        {/* KOSPI */}
        <div className="bg-gray-800/60 rounded-lg p-3 text-center">
          <Activity size={18} className="mx-auto mb-1 text-indigo-400" />
          <p className={`text-sm font-bold ${regime.index_change_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>
            {regime.index_change_pct >= 0 ? "+" : ""}{regime.index_change_pct.toFixed(2)}%
          </p>
          <p className="text-xs text-gray-600 mt-0.5">KOSPI</p>
        </div>

        {/* 변동성 */}
        <div className="bg-gray-800/60 rounded-lg p-3 text-center">
          <Zap size={18} className={`mx-auto mb-1 ${volColor}`} />
          <p className={`text-sm font-bold ${volColor}`}>{volLabel}</p>
          <p className="text-xs text-gray-600 mt-0.5">변동성</p>
        </div>
      </div>

      {/* 추천 전략 */}
      <div>
        <p className="text-xs text-gray-500 mb-2">추천 전략</p>
        {regime.preferred_strategies.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {regime.preferred_strategies.map((s) => (
              <span
                key={s}
                className={`px-2 py-1 rounded-md text-xs font-medium border ${
                  STRATEGY_COLOR[s] ?? "bg-gray-800 text-gray-400 border-gray-700"
                }`}
              >
                {STRATEGY_LABEL[s] ?? s}
              </span>
            ))}
          </div>
        ) : (
          <span className="text-xs text-gray-600">없음 — 관망 구간</span>
        )}
      </div>
    </div>
  );
}
