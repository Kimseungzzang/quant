"use client";

import { useState, useEffect, useCallback } from "react";
import { Play, Square, Loader2, Activity } from "lucide-react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8080";

interface EngineStatus {
  status: string;
  trading_active: boolean;
  trading_market?: string;
  mode?: string;
}

interface TradeControlProps {
  mode: "paper" | "live";
}

export default function TradeControl({ mode }: TradeControlProps) {
  const [engine, setEngine]   = useState<EngineStatus | null>(null);
  const [market, setMarket]   = useState<"domestic" | "overseas">("domestic");
  const [loading, setLoading] = useState(false);
  const [msg, setMsg]         = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/command/health`, { cache: "no-store" });
      if (res.ok) {
        setEngine(await res.json());
        setUpdatedAt(new Date());
      }
    } catch {
      setEngine(null);
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const id = setInterval(fetchStatus, 2000);
    return () => clearInterval(id);
  }, [fetchStatus]);

  async function handleStart() {
    setLoading(true);
    setMsg(null);
    try {
      const res = await fetch(`${API}/api/command/trade/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ market, mode }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        setMsg(err.detail ?? err.message ?? "시작 실패");
      } else {
        setMsg("매매 시작됨");
        fetchStatus();
      }
    } catch {
      setMsg("서버 연결 실패");
    } finally {
      setLoading(false);
    }
  }

  async function handleStop() {
    setLoading(true);
    setMsg(null);
    try {
      const res = await fetch(`${API}/api/command/trade/stop`, { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        setMsg(err.detail ?? err.message ?? "중단 실패");
      } else {
        setMsg("매매 중단 중...");
        fetchStatus();
      }
    } catch {
      setMsg("서버 연결 실패");
    } finally {
      setLoading(false);
    }
  }

  async function handleModeSwitch() {
    setLoading(true);
    setMsg(null);
    try {
      const res = await fetch(`${API}/api/command/mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        setMsg(err.detail ?? err.message ?? "전환 실패");
      } else {
        setMsg(`엔진을 ${mode === "paper" ? "모의" : "실전"} 모드로 전환했습니다.`);
        fetchStatus();
      }
    } catch {
      setMsg("서버 연결 실패");
    } finally {
      setLoading(false);
    }
  }

  const active = engine?.trading_active ?? false;
  const online = engine?.status === "ok";
  const modeMatched = !engine?.mode || engine.mode === mode;
  const canStart = online && modeMatched;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">매매 제어</h2>
        <div className="flex items-center gap-2 text-xs">
          <span className={`w-2 h-2 rounded-full ${online ? "bg-emerald-400" : "bg-gray-600"}`} />
          <span className={online ? "text-emerald-400" : "text-gray-600"}>
            엔진 {online ? "온라인" : "오프라인"}
          </span>
        </div>
      </div>

      <div className="flex items-center justify-between text-xs text-gray-500">
        <span>상태 2초 갱신</span>
        <span>{updatedAt ? updatedAt.toLocaleTimeString("ko-KR") : "확인 중"}</span>
      </div>

      {/* 상태 배지 */}
      <div className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium ${
        active
          ? "bg-emerald-900/40 text-emerald-300 border border-emerald-800"
          : "bg-gray-800 text-gray-500 border border-gray-700"
      }`}>
        <Activity size={14} className={active ? "animate-pulse" : ""} />
        {active
          ? `매매 실행 중 — ${
              engine?.trading_market === "domestic" ? "🇰🇷 국내" :
              engine?.trading_market === "overseas" ? "🇺🇸 미국" :
              engine?.trading_market === "both"     ? "🇰🇷🇺🇸 국내+미국" :
              engine?.trading_market ?? ""
            }`
          : "대기 중"}
      </div>

      <div className={`flex items-center justify-between px-3 py-2 rounded-lg border text-xs ${
        modeMatched
          ? "bg-gray-800/60 border-gray-700 text-gray-400"
          : "bg-red-950/40 border-red-900 text-red-300"
      }`}>
        <span>화면 {mode === "paper" ? "모의" : "실전"}</span>
        <span>엔진 {engine?.mode === "paper" ? "모의" : engine?.mode === "live" ? "실전" : "확인 중"}</span>
      </div>

      {/* 시장 선택 */}
      {!active && (
        <div className="flex gap-1 bg-gray-800 rounded-lg p-1">
          {(["domestic", "overseas"] as const).map((m) => (
            <button
              key={m}
              onClick={() => setMarket(m)}
              className={`flex-1 py-1.5 rounded-md text-xs font-medium transition-colors ${
                market === m
                  ? "bg-indigo-600 text-white"
                  : "text-gray-400 hover:text-gray-200"
              }`}
            >
              {m === "domestic" ? "국내" : "미국"}
            </button>
          ))}
        </div>
      )}

      {/* 시작 / 중단 버튼 */}
      <div className="flex gap-2">
        {!active ? (
          <button
            onClick={handleStart}
            disabled={loading || !canStart}
            className="flex-1 flex items-center justify-center gap-2 bg-emerald-700 hover:bg-emerald-600 disabled:opacity-40 text-white text-sm py-2 rounded-lg transition-colors"
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
            매매 시작
          </button>
        ) : (
          <button
            onClick={handleStop}
            disabled={loading}
            className="flex-1 flex items-center justify-center gap-2 bg-red-800 hover:bg-red-700 disabled:opacity-40 text-white text-sm py-2 rounded-lg transition-colors"
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <Square size={14} />}
            매매 중단
          </button>
        )}
      </div>

      {msg && (
        <p className="text-xs text-gray-400 text-center">{msg}</p>
      )}
      {!active && online && !modeMatched && (
        <div className="space-y-2">
          <p className="text-xs text-red-300 text-center">
            엔진 모드와 화면 모드가 달라 시작할 수 없습니다.
          </p>
          <button
            onClick={handleModeSwitch}
            disabled={loading}
            className={`w-full text-xs py-2 rounded-lg border transition-colors disabled:opacity-40 ${
              mode === "live"
                ? "border-red-800 text-red-200 bg-red-950/40 hover:bg-red-900/40"
                : "border-indigo-800 text-indigo-200 bg-indigo-950/40 hover:bg-indigo-900/40"
            }`}
          >
            엔진을 {mode === "paper" ? "모의" : "실전"} 모드로 전환
          </button>
        </div>
      )}
    </div>
  );
}
