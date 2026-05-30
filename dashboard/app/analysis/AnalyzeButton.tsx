"use client";

import { useState, useEffect, useRef } from "react";
import { Play, Loader2, CheckCircle, XCircle } from "lucide-react";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8080";
const LS_KEY = (market: string, horizon: string) => `analyze_run_id_${market}_${horizon}`;

const PERIOD_OPTIONS = [
  { label: "7일",  value: 7 },
  { label: "14일", value: 14 },
  { label: "30일", value: 30 },
  { label: "60일", value: 60 },
  { label: "90일", value: 90 },
];

interface Progress {
  done: number;
  total: number;
  current: string;
  status: "running" | "completed" | "failed" | "unknown";
  pct: number;
}

export default function AnalyzeButton({ market, horizon }: { market: string; horizon: string }) {
  const [lookbackDays, setLookbackDays] = useState(30);
  const [loading, setLoading]           = useState(false);
  const [progress, setProgress]         = useState<Progress | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 마운트 시 localStorage에 저장된 runId가 있으면 폴링 재개
  useEffect(() => {
    const saved = localStorage.getItem(LS_KEY(market, horizon));
    if (saved) {
      const runId = parseInt(saved, 10);
      if (!isNaN(runId)) {
        setProgress({ done: 0, total: 0, current: "", status: "running", pct: 0 });
        startPolling(runId);
      }
    }
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [market, horizon]);

  function startPolling(runId: number) {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const res = await fetch(`${BASE}/api/command/analyze/${runId}/progress`);
        if (!res.ok) return;
        const p: Progress = await res.json();
        setProgress(p);
        if (p.status === "completed" || p.status === "failed") {
          clearInterval(pollRef.current!);
          pollRef.current = null;
          localStorage.removeItem(LS_KEY(market, horizon));
          if (p.status === "completed") {
            setTimeout(() => window.location.reload(), 1200);
          }
        }
      } catch { /* ignore */ }
    }, 2000);
  }

  async function handleClick() {
    setLoading(true);
    setProgress(null);
    try {
      const res = await fetch(`${BASE}/api/command/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ market, horizon, topN: 10, lookbackDays }),
      });
      if (res.status === 409) {
        // 이미 실행 중 — localStorage에 run_id 없으면 확인 불가능하지만 UI 표시는 함
        setProgress({ done: 0, total: 0, current: "", status: "running", pct: 0 });
        return;
      }
      if (!res.ok) throw new Error();
      const data = await res.json();
      localStorage.setItem(LS_KEY(market, horizon), String(data.run_id));
      setProgress({ done: 0, total: 0, current: "", status: "running", pct: 0 });
      startPolling(data.run_id);
    } catch {
      setProgress({ done: 0, total: 0, current: "오류 발생", status: "failed", pct: 0 });
    } finally {
      setLoading(false);
    }
  }

  const busy = loading || progress?.status === "running";

  return (
    <div className="flex flex-col gap-3 items-end">
      <div className="flex items-center gap-2">
        {/* 기간 선택 */}
        <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1">
          {PERIOD_OPTIONS.map(({ label, value }) => (
            <button
              key={value}
              onClick={() => !busy && setLookbackDays(value)}
              className={`px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${
                lookbackDays === value
                  ? "bg-indigo-600 text-white"
                  : busy ? "text-gray-600 cursor-not-allowed"
                  : "text-gray-400 hover:text-gray-200"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {/* 실행 버튼 */}
        <button
          onClick={handleClick}
          disabled={busy}
          className="flex items-center gap-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm px-4 py-2 rounded-lg transition-colors whitespace-nowrap"
        >
          {busy
            ? <Loader2 size={14} className="animate-spin" />
            : <Play size={14} />}
          {busy ? "분석 중..." : "분석 실행"}
        </button>
      </div>

      {/* 프로그레스 바 */}
      {progress && (
        <div className="w-full min-w-[340px] bg-gray-900 border border-gray-800 rounded-xl p-4 space-y-2">
          <div className="flex items-center justify-between text-xs">
            <div className="flex items-center gap-2">
              {progress.status === "completed" && <CheckCircle size={13} className="text-emerald-400" />}
              {progress.status === "failed"    && <XCircle    size={13} className="text-red-400" />}
              {progress.status === "running"   && <Loader2    size={13} className="animate-spin text-indigo-400" />}
              <span className={
                progress.status === "completed" ? "text-emerald-400"
                : progress.status === "failed"  ? "text-red-400"
                : "text-indigo-400"
              }>
                {progress.status === "completed" ? `완료 — ${progress.done}개 분석됨`
                 : progress.status === "failed"   ? "분석 실패"
                 : progress.total > 0
                   ? `${progress.done} / ${progress.total} 종목`
                   : "유니버스 로딩 중..."}
              </span>
            </div>
            <span className="text-gray-500 font-mono">{progress.pct}%</span>
          </div>

          <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-500 ${
                progress.status === "completed" ? "bg-emerald-500"
                : progress.status === "failed"  ? "bg-red-500"
                : "bg-indigo-500"
              }`}
              style={{ width: `${progress.pct}%` }}
            />
          </div>

          {progress.current && progress.status === "running" && (
            <p className="text-xs text-gray-500 truncate">
              분석 중: <span className="text-gray-300 font-mono">{progress.current}</span>
            </p>
          )}
        </div>
      )}
    </div>
  );
}
