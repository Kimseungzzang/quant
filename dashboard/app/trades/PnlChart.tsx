"use client";

import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ReferenceLine, ResponsiveContainer,
} from "recharts";
import type { PnlChart } from "@/lib/api";

interface Props { data: PnlChart[] }

export default function PnlChart({ data }: Props) {
  const maxAbs = Math.max(...data.map((d) => Math.abs(d.cumulativePnl)), 1);

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 h-64">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
          <XAxis
            dataKey="date"
            tick={{ fill: "#6B7280", fontSize: 10 }}
            tickFormatter={(v: string) => v.slice(5)}
          />
          <YAxis
            domain={[-maxAbs * 1.1, maxAbs * 1.1]}
            tick={{ fill: "#6B7280", fontSize: 10 }}
            tickFormatter={(v: number) =>
              Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(0)}K` : String(v)
            }
          />
          <Tooltip
            contentStyle={{ background: "#111827", border: "1px solid #374151", borderRadius: 8 }}
            labelStyle={{ color: "#9CA3AF" }}
            formatter={(v) => [`₩${Number(v).toLocaleString()}`, "누적 손익"]}
          />
          <ReferenceLine y={0} stroke="#4B5563" strokeDasharray="4 2" />
          <Line
            type="monotone"
            dataKey="cumulativePnl"
            stroke="#6366F1"
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4, fill: "#6366F1" }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
