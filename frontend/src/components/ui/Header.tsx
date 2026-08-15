import { useLocation, useNavigate } from "react-router-dom";
import { Search, Bell, Menu } from "lucide-react";
import { useConfigStore } from "../../stores/configStore";
import { useState, useRef } from "react";

const pageTitles: Record<string, string> = {
  "/": "Dashboard",
  "/config": "Config",
  "/subagents": "Subagents",
  "/memory": "Memory",
  "/tools": "Tools",
  "/logs": "Logs",
  "/templates": "Templates",
  "/settings": "Settings",
  "/settings/dispatch": "Settings",
  "/settings/model": "Settings",
  "/settings/tools": "Settings",
  "/settings/memory": "Settings",
  "/settings/gateway": "Settings",
  "/settings/subagents": "Settings",
  "/settings/security": "Settings",
  "/settings/adapters": "Settings",
};

export function Header({ onMenuClick }: { onMenuClick: () => void }) {
  const location = useLocation();
  const navigate = useNavigate();
  const title = pageTitles[location.pathname] || "my-agent";
  const setSearchQuery = useConfigStore((s) => s.setSearchQuery);
  const [searchValue, setSearchValue] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const handleSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      inputRef.current?.focus();
      return;
    }
    if (e.key === "Enter" && searchValue.trim()) {
      setSearchQuery(searchValue.trim());
      navigate("/config");
      setSearchValue("");
    }
  };

  return (
    <header className="h-14 px-6 flex items-center justify-between bg-card border-b border-border sticky top-0 z-10">
      <div className="flex items-center gap-3">
        <button
          onClick={onMenuClick}
          className="md:hidden text-ink-secondary hover:text-ink p-1 -ml-1"
          aria-label="打开菜单"
        >
          <Menu size={18} />
        </button>
        <h1 className="text-ink font-semibold text-[15px]">{title}</h1>
      </div>

      <div className="flex items-center gap-3">
        <div className="relative hidden sm:block">
          <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-tertiary" />
          <input
            ref={inputRef}
            type="text"
            value={searchValue}
            onChange={(e) => setSearchValue(e.target.value)}
            onKeyDown={handleSearchKeyDown}
            placeholder="全局搜索..."
            className="pl-8 pr-3 py-1.5 text-sm bg-surface border border-border rounded-lg text-ink placeholder:text-ink-tertiary focus:outline-none focus:border-accent-blue/40 focus:shadow-focus transition-all duration-150"
            style={{ width: 220 }}
          />
        </div>

        <button aria-label="通知" className="relative w-8 h-8 rounded-lg flex items-center justify-center text-ink-secondary hover:bg-black/[0.04] transition-colors duration-150">
          <Bell size={16} />
          <span className="absolute top-1.5 right-1.5 w-2 h-2 rounded-full bg-accent-red"></span>
        </button>

        <div className="w-7 h-7 rounded-full bg-accent-blue/10 text-accent-blue flex items-center justify-center text-xs font-semibold">
          A
        </div>
      </div>
    </header>
  );
}
