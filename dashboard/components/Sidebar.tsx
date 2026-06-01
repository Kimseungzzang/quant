"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { TrendingUp, History, FlaskConical, LayoutDashboard, BriefcaseBusiness, FileText, Activity } from "lucide-react";

const nav = [
  { href: "/",          label: "대시보드",   icon: LayoutDashboard },
  { href: "/analysis",  label: "추천 종목",  icon: TrendingUp },
  { href: "/signals",   label: "실시간 신호", icon: Activity },
  { href: "/positions", label: "포지션",     icon: BriefcaseBusiness },
  { href: "/trades",    label: "매매 이력",  icon: History },
  { href: "/report",    label: "리포트",     icon: FileText },
  { href: "/backtest",  label: "백테스트",   icon: FlaskConical },
];

export default function Sidebar() {
  const path = usePathname();

  return (
    <aside className="w-48 shrink-0 bg-gray-900 border-r border-gray-800 flex flex-col py-6 px-3 gap-1">
      <div className="px-3 mb-6">
        <span className="text-xs font-bold tracking-widest text-indigo-400 uppercase">Quant</span>
      </div>
      {nav.map(({ href, label, icon: Icon }) => {
        const active = path === href;
        return (
          <Link
            key={href}
            href={href}
            className={`flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors ${
              active
                ? "bg-indigo-600 text-white"
                : "text-gray-400 hover:bg-gray-800 hover:text-gray-100"
            }`}
          >
            <Icon size={16} />
            {label}
          </Link>
        );
      })}
    </aside>
  );
}
