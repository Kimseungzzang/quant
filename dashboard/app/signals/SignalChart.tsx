"use client";

import { useEffect, useState, useCallback } from "react";
import {
  ComposedChart, Line, ReferenceLine, ReferenceArea, XAxis, YAxis,
  CartesianGrid, Tooltip, ResponsiveContainer, Area,
} from "recharts";
import { RefreshCw } from "lucide-react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8084";

interface Candle {
  t: string;
  o: number | null;
  h: number | null;
  l: number | null;
  c: number | null;
  v: number | null;
  ema5: number | null;
  ema20: number | null;
}

interface StockSignal {
  price: number;
  resistance: number | null;
  ema5: number | null;
  ema20: number | null;
  rsi: number | null;
  candles: Candle[];
  updated_at: string;
}

type SignalMap = Record<string, StockSignal>;

function fmtTime(iso: string) {
  try {
    if (!iso || /^\d+$/.test(iso)) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso.slice(11, 16);
    return d.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
  } catch { return iso.slice(11, 16) || ""; }
}

function ConditionBadge({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${ok ? "bg-emerald-900 text-emerald-300" : "bg-gray-800 text-gray-500"}`}>
      {label}
    </span>
  );
}

function StockChart({ code, signal }: { code: string; signal: StockSignal }) {
  const { price, resistance, ema5, ema20, rsi, candles } = signal;

  const chartData = candles.map((c) => ({
    t: fmtTime(c.t),
    rawTime: c.t,
    price: c.c,
    ema5: c.ema5,
    ema20: c.ema20,
  })).filter((d) => d.t);

  // 진입 조건 평가
  const brokOut   = resistance != null && price > resistance * 1.003;
  const nearEma20 = ema20 != null && Math.abs(price - ema20) / ema20 <= 0.02;
  const nearEma5  = ema5  != null && Math.abs(price - ema5)  / ema5  <= 0.02;
  const uptrend   = ema5 != null && ema20 != null && ema5 > ema20;
  const rsiOk     = rsi != null && rsi >= 35 && rsi <= 70;

  const breakoutReady = brokOut;
  const pullbackReady = uptrend && (nearEma20 || nearEma5) && rsiOk;

  const yMin = chartData.length
    ? Math.min(...chartData.map(d => d.price ?? Infinity)) * 0.998
    : undefined;
  const yMax = chartData.length
    ? Math.max(...chartData.map(d => d.price ?? -Infinity)) * 1.002
    : undefined;

  const pctToResistance = resistance && resistance > 0
    ? ((price - resistance) / resistance * 100).toFixed(2)
    : null;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 space-y-3">
      {/* 헤더 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="text-sm font-bold text-gray-100">{code}</span>
          <span className="text-lg font-mono font-bold text-white">
            {price.toLocaleString()}
          </span>
        </div>
        <div className="flex gap-1.5 flex-wrap justify-end">
          <ConditionBadge ok={breakoutReady} label="저항선 돌파" />
          <ConditionBadge ok={pullbackReady} label="눌림목" />
          <ConditionBadge ok={uptrend} label="상승추세" />
          <ConditionBadge ok={rsiOk} label={`RSI ${rsi?.toFixed(0) ?? "?"}`} />
        </div>
      </div>

      {/* 가격 차트 */}
      {chartData.length > 1 ? (
        <ResponsiveContainer width="100%" height={180}>
          <ComposedChart data={chartData} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
            <XAxis dataKey="t" tick={{ fontSize: 10, fill: "#6b7280" }} interval="preserveStartEnd" />
            <YAxis domain={yMin != null && yMax != null ? [yMin, yMax] : ["auto", "auto"]} tick={{ fontSize: 10, fill: "#6b7280" }} width={60}
              tickFormatter={v => v?.toLocaleString()} />
            <Tooltip
              contentStyle={{ backgroundColor: "#111827", border: "1px solid #374151", fontSize: 11 }}
              formatter={(v) => [Number(v).toLocaleString(), ""]}
            />
            {/* 눌림목 구간: EMA20 ±2% 밴드 */}
            {ema20 && (
              <ReferenceArea
                y1={ema20 * 0.98}
                y2={ema20 * 1.02}
                fill="#10b981"
                fillOpacity={nearEma20 ? 0.18 : 0.07}
                stroke="#10b981"
                strokeOpacity={0.3}
                strokeDasharray="4 2"
                label={{ value: "눌림목 구간", fill: "#10b981", fontSize: 9, position: "insideTopLeft" }}
              />
            )}
            {/* EMA5 ±2% 밴드 */}
            {ema5 && (
              <ReferenceArea
                y1={ema5 * 0.98}
                y2={ema5 * 1.02}
                fill="#f59e0b"
                fillOpacity={nearEma5 ? 0.15 : 0.05}
                stroke="none"
              />
            )}
            <Area type="monotone" dataKey="price" stroke="#6366f1" fill="#6366f115" dot={false} strokeWidth={1.5} />
            <Line type="monotone" dataKey="ema5"  stroke="#f59e0b" dot={false} strokeWidth={1} strokeDasharray="4 2" />
            <Line type="monotone" dataKey="ema20" stroke="#10b981" dot={false} strokeWidth={1} strokeDasharray="4 2" />
            {resistance && (
              <ReferenceLine y={resistance} stroke="#ef4444" strokeDasharray="6 3" strokeWidth={1.5}
                label={{ value: `저항 ${resistance.toLocaleString()}`, fill: "#ef4444", fontSize: 10, position: "insideTopRight" }} />
            )}
          </ComposedChart>
        </ResponsiveContainer>
      ) : (
        <div className="h-44 flex items-center justify-center text-xs text-gray-600">
          분봉 데이터 수집 중... (1분봉 완성 후 표시)
        </div>
      )}

      {/* 범례 */}
      <div className="flex gap-3 text-xs text-gray-500">
        <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-indigo-400 inline-block"/>가격</span>
        <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-amber-400 inline-block" style={{borderTop:"1px dashed"}}/>EMA5</span>
        <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-emerald-400 inline-block" style={{borderTop:"1px dashed"}}/>EMA20</span>
        <span className="flex items-center gap-1"><span className="w-3 h-1.5 bg-emerald-400/20 border border-emerald-400/40 inline-block rounded-sm"/>눌림목 구간</span>
        <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-red-400 inline-block" style={{borderTop:"1px dashed"}}/>저항선</span>
      </div>

      {/* 지표 요약 */}
      <div className="grid grid-cols-4 gap-2 text-xs">
        <div className="bg-gray-800 rounded p-2">
          <p className="text-gray-500 mb-0.5">EMA5</p>
          <p className="font-mono text-amber-400">{ema5?.toLocaleString() ?? "-"}</p>
        </div>
        <div className="bg-gray-800 rounded p-2">
          <p className="text-gray-500 mb-0.5">EMA20</p>
          <p className="font-mono text-emerald-400">{ema20?.toLocaleString() ?? "-"}</p>
        </div>
        <div className="bg-gray-800 rounded p-2">
          <p className="text-gray-500 mb-0.5">저항선</p>
          <p className={`font-mono ${brokOut ? "text-emerald-400" : "text-red-400"}`}>
            {resistance?.toLocaleString() ?? "-"}
            {pctToResistance != null && (
              <span className="text-gray-500 ml-1">({pctToResistance}%)</span>
            )}
          </p>
        </div>
        <div className="bg-gray-800 rounded p-2">
          <p className="text-gray-500 mb-0.5">RSI</p>
          <p className={`font-mono ${rsiOk ? "text-indigo-400" : rsi && rsi < 35 ? "text-blue-400" : "text-red-400"}`}>
            {rsi?.toFixed(1) ?? "-"}
          </p>
        </div>
      </div>
    </div>
  );
}

export default function SignalChart() {
  const [signals, setSignals] = useState<SignalMap>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);

  const fetch_ = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API}/api/command/signals`, { cache: "no-store" });
      if (res.ok) {
        const data: SignalMap = await res.json();
        setSignals(data);
        setLastUpdate(new Date());
        if (!selected && Object.keys(data).length > 0) {
          setSelected(Object.keys(data)[0]);
        }
      }
    } catch { /* ignore */ } finally {
      setLoading(false);
    }
  }, [selected]);

  useEffect(() => {
    fetch_();
    const id = setInterval(fetch_, 5000);
    return () => clearInterval(id);
  }, [fetch_]);

  const codes = Object.keys(signals);

  if (codes.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-64 text-gray-500 gap-3">
        <p className="text-sm">매매 시작 후 실시간 신호가 여기 표시됩니다.</p>
        <button onClick={fetch_} className="text-xs text-indigo-400 hover:text-indigo-300 flex items-center gap-1">
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} />새로고침
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* 종목 탭 */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="flex gap-1 flex-wrap">
          {codes.map(code => {
            const s = signals[code];
            const brokOut   = s.resistance != null && s.price > s.resistance * 1.003;
            const uptrend   = s.ema5 != null && s.ema20 != null && s.ema5 > s.ema20;
            const nearEma   = s.ema20 != null && Math.abs(s.price - s.ema20) / s.ema20 <= 0.02;
            const rsiOk     = s.rsi != null && s.rsi >= 35 && s.rsi <= 70;
            const hasSignal = brokOut || (uptrend && nearEma && rsiOk);
            return (
              <button
                key={code}
                onClick={() => setSelected(code)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors relative ${
                  selected === code
                    ? "bg-indigo-600 text-white"
                    : "bg-gray-800 text-gray-400 hover:text-gray-200"
                }`}
              >
                {code}
                {hasSignal && (
                  <span className="absolute -top-1 -right-1 w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
                )}
              </button>
            );
          })}
        </div>
        <div className="ml-auto flex items-center gap-2 text-xs text-gray-500">
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
          <span>5초 갱신</span>
          {lastUpdate && <span>{lastUpdate.toLocaleTimeString("ko-KR")}</span>}
        </div>
      </div>

      {/* 선택 종목 차트 */}
      {selected && signals[selected] && (
        <StockChart code={selected} signal={signals[selected]} />
      )}

      {/* 전체 종목 요약 */}
      <div className="grid grid-cols-2 gap-2">
        {codes.filter(c => c !== selected).map(code => {
          const s = signals[code];
          const brokOut   = s.resistance != null && s.price > s.resistance * 1.003;
          const uptrend   = s.ema5 != null && s.ema20 != null && s.ema5 > s.ema20;
          const nearEma   = s.ema20 != null && Math.abs(s.price - s.ema20) / s.ema20 <= 0.02;
          const rsiOk     = s.rsi != null && s.rsi >= 35 && s.rsi <= 70;
          const breakoutReady = brokOut;
          const pullbackReady = uptrend && nearEma && rsiOk;
          return (
            <button
              key={code}
              onClick={() => setSelected(code)}
              className="bg-gray-900 border border-gray-800 rounded-lg p-3 text-left hover:border-gray-600 transition-colors"
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-bold text-gray-300">{code}</span>
                <span className="text-xs font-mono text-white">{s.price.toLocaleString()}</span>
              </div>
              <div className="flex gap-1">
                {breakoutReady && <span className="text-xs text-emerald-400">● 저항선 돌파</span>}
                {pullbackReady && <span className="text-xs text-amber-400">● 눌림목</span>}
                {!breakoutReady && !pullbackReady && <span className="text-xs text-gray-600">대기 중</span>}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
