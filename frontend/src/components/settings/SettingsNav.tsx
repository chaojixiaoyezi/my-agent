import { NavLink } from "react-router-dom";
import {
  LayoutGrid,
  Activity,
  Brain,
  Wrench,
  Database,
  Server,
  Users,
  Shield,
  Puzzle,
} from "lucide-react";
import { cn } from "../../lib/utils";

const navItems = [
  { to: "/settings", label: "概览", icon: LayoutGrid, end: true },
  { to: "/settings/dispatch", label: "调度", icon: Activity },
  { to: "/settings/model", label: "模型", icon: Brain },
  { to: "/settings/tools", label: "工具", icon: Wrench },
  { to: "/settings/memory", label: "记忆", icon: Database },
  { to: "/settings/gateway", label: "网关", icon: Server },
  { to: "/settings/subagents", label: "子代理", icon: Users },
  { to: "/settings/security", label: "安全", icon: Shield },
  { to: "/settings/adapters", label: "适配器", icon: Puzzle },
];

export function SettingsNav() {
  return (
    <nav className="w-44 shrink-0 hidden lg:block">
      <div className="sticky top-6 space-y-0.5">
        {navItems.map((item) => {
          const Icon = item.icon;
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 px-3 py-2 rounded-xl text-xs font-medium transition-all duration-150",
                  isActive
                    ? "bg-accent-blue/10 text-accent-blue"
                    : "text-ink-secondary hover:bg-black/[0.03] hover:text-ink"
                )
              }
            >
              <Icon size={15} strokeWidth={1.8} />
              {item.label}
            </NavLink>
          );
        })}
      </div>
    </nav>
  );
}
