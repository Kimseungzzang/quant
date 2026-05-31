import { api } from "@/lib/api";
import PositionsClient from "./PositionsClient";

export default async function PositionsPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string }>;
}) {
  const { mode = "paper" } = await searchParams;
  const positions = await api.trades.positions(mode).catch(() => []);

  return (
    <div className="space-y-5 max-w-6xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">포지션</h1>
        <nav className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 text-sm">
          {["paper", "live"].map((m) => (
            <a
              key={m}
              href={`/positions?mode=${m}`}
              className={`px-3 py-1 rounded-md transition-colors ${
                mode === m ? "bg-indigo-600 text-white" : "text-gray-400 hover:text-gray-100"
              }`}
            >
              {m === "paper" ? "모의" : "실전"}
            </a>
          ))}
        </nav>
      </div>

      <PositionsClient initial={positions} mode={mode} />
    </div>
  );
}
