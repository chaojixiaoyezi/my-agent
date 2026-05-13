import { NavLink } from "react-router-dom";
import {
  LayoutDashboard,
  Settings,
  Users,
  Database,
  Wrench,
  FileText,
  BookOpen,
  Shield,
  X,
} from "lucide-react";
import { cn } from "../../lib/utils";

const navItems = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard },
  { to: "/config", label: "Config", icon: Settings },
  { to: "/subagents", label: "Subagents", icon: Users },
  { to: "/memory", label: "Memory", icon: Database },
  { to: "/tools", label: "Tools", icon: Wrench },
  { to: "/logs", label: "Logs", icon: FileText },
  { to: "/templates", label: "Templates", icon: BookOpen },
  { to: "/settings", label: "Settings", icon: Shield },
];

export function Sidebar({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  return (
    <>
      {/* Mobile overlay */}
      {open && (
        <div
          className="fixed inset-0 bg-black/20 z-40 md:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      <aside
        className={cn(
          "h-screen bg-card border-r border-border flex flex-col sticky top-0 z-50 transition-transform duration-300",
          "fixed inset-y-0 left-0 w-56 md:translate-x-0 md:relative",
          open ? "translate-x-0" : "-translate-x-full"
        )}
      >
        <div className="px-5 py-4 flex items-center gap-2.5 border-b border-border">
          <div className="w-7 h-7 rounded-lg bg-accent-blue flex items-center justify-center">
            <span className="text-white text-xs font-bold">M</span>
          </div>
          <span className="text-ink font-semibold text-[15px]">my-agent</span>
          <button
            onClick={onClose}
            className="ml-auto md:hidden text-ink-tertiary hover:text-ink p-1"
            aria-label="关闭菜单"
          >
            <X size={16} />
          </button>
        </div>

        <nav className="flex-1 overflow-y-auto py-3">
          <ul className="space-y-0.5 px-2">
            {navItems.map((item) => {
              const Icon = item.icon;
              return (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    end={item.to === "/"}
                    onClick={onClose}
                    className={({ isActive }) =>
                      cn(
                        "flex items-center gap-3 px-3 py-2 rounded-2xl text-sm font-medium transition-all duration-150 ease-smooth",
                        isActive
                          ? "bg-accent-blue/10 text-accent-blue"
                          : "text-ink-secondary hover:bg-black/[0.03] hover:text-ink"
                      )
                    }
                  >
                    <Icon size={18} strokeWidth={1.8} />
                    {item.label}
                  </NavLink>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="px-4 py-3 border-t border-border text-[11px] text-ink-tertiary">
          v0.3.0 · 本地管理后台
        </div>
      </aside>
    </>
  );
}
