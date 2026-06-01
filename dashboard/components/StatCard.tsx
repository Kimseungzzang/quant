import { ReactNode } from "react";

interface StatCardProps {
  label: string;
  value: string | number;
  sub?: string;
  color?: "default" | "green" | "red" | "indigo";
  action?: ReactNode;
}

const colorMap = {
  default: "text-gray-100",
  green:   "text-emerald-400",
  red:     "text-red-400",
  indigo:  "text-indigo-400",
};

export default function StatCard({ label, value, sub, color = "default", action }: StatCardProps) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <div className="flex items-center justify-between mb-1">
        <p className="text-xs text-gray-500 uppercase tracking-wide">{label}</p>
        {action}
      </div>
      <p className={`text-2xl font-bold ${colorMap[color]}`}>{value}</p>
      {sub && <p className="text-xs text-gray-500 mt-1">{sub}</p>}
    </div>
  );
}
