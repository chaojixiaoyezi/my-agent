import { useState } from "react";
import { Search } from "lucide-react";
import { cn } from "../lib/utils";

const mockLogs = [
  {
    timestamp: "2026-05-12T10:15:30Z",
    level: "info",
    source: "gateway",
    message: "Request worker-1 completed run_001",
    request_id: "req_001",
    run_id: "run_001",
  },
  {
    timestamp: "2026-05-12T10:14:20Z",
    level: "info",
    source: "subagent",
    message: "子代理 worker-001 进入 AWAITING_ACCEPTANCE 状态",
    request_id: "req_001",
    run_id: "run_001",
  },
  {
    timestamp: "2026-05-12T10:12:00Z",
    level: "warn",
    source: "tools",
    message: "tool_write_inline_max_chars 接近上限，建议增大",
    request_id: null,
    run_id: null,
  },
  {
    timestamp: "2026-05-12T10:10:00Z",
    level: "info",
    source: "subagent",
    message: "创建子代理 researcher-001",
    request_id: null,
    run_id: "run_child_004",
  },
  {
    timestamp: "2026-05-12T09:55:00Z",
    level: "error",
    source: "tools",
    message: "run_command 超时: sleep 60 超过 30s 限制",
    request_id: null,
    run_id: "run_003",
  },
  {
    timestamp: "2026-05-12T09:30:00Z",
    level: "info",
    source: "gateway",
    message: "Gateway 启动成功，监听端口 8420",
    request_id: null,
    run_id: null,
  },
  {
    timestamp: "2026-05-12T09:15:00Z",
    level: "info",
    source: "audit",
    message: "配置变更: tool_write_inline_max_chars 12000 -> 25000",
    request_id: null,
    run_id: null,
  },
  {
    timestamp: "2026-05-12T09:00:00Z",
    level: "debug",
    source: "subagent",
    message: "debug_trace_level=3, 记录 refs-only 阶段日志",
    request_id: null,
    run_id: "run_root_001",
  },
];

const levelColors: Record<string, string> = {
  debug: "bg-ink-tertiary/10 text-ink-tertiary",
  info: "bg-accent-blue/10 text-accent-blue",
  warn: "bg-accent-orange/10 text-accent-orange",
  error: "bg-accent-red/10 text-accent-red",
};

const levelLabels: Record<string, string> = {
  debug: "DEBUG",
  info: "INFO",
  warn: "WARN",
  error: "ERROR",
};

export default function Logs() {
  const [search, setSearch] = useState("");
  const [levelFilter, setLevelFilter] = useState<string | null>(null);
  const [sourceFilter, setSourceFilter] = useState<string | null>(null);

  const filtered = mockLogs.filter((l) => {
    const matchesSearch =
      !search ||
      l.message.toLowerCase().includes(search.toLowerCase()) ||
      (l.request_id?.toLowerCase().includes(search.toLowerCase()) ?? false);
    const matchesLevel = !levelFilter || l.level === levelFilter;
    const matchesSource = !sourceFilter || l.source === sourceFilter;
    return matchesSearch && matchesLevel && matchesSource;
  });

  const levels = ["debug", "info", "warn", "error"];
  const sources = ["gateway", "subagent", "tools", "audit"];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-ink">日志</h2>
        <div className="flex items-center gap-2">
          <div className="relative">
            <Search
              size={14}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-tertiary"
            />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="搜索日志..."
              className="pl-9 pr-3 py-1.5 text-sm bg-card border border-border rounded-xl text-ink placeholder:text-ink-tertiary focus:outline-none focus:border-accent-blue/40 focus:shadow-focus transition-all"
              style={{ width: 220 }}
            />
          </div>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <div className="flex items-center gap-1">
          <span className="text-[11px] text-ink-tertiary mr-1">级别:</span>
          <button
            onClick={() => setLevelFilter(null)}
            className={cn(
              "px-2.5 py-1 rounded-lg text-[11px] font-medium transition-all",
              levelFilter === null
                ? "bg-ink text-white"
                : "bg-card border border-border text-ink-secondary hover:border-border-hover"
            )}
          >
            全部
          </button>
          {levels.map((lv) => (
            <button
              key={lv}
              onClick={() => setLevelFilter(levelFilter === lv ? null : lv)}
              className={cn(
                "px-2.5 py-1 rounded-lg text-[11px] font-medium transition-all",
                levelFilter === lv
                  ? levelColors[lv].replace("/10", "").replace("text-", "bg-").replace("ink-tertiary", "ink") + " text-white"
                  : "bg-card border border-border text-ink-secondary hover:border-border-hover"
              )}
            >
              {levelLabels[lv]}
            </button>
          ))}
        </div>

        <div className="w-px h-4 bg-border"></div>

        <div className="flex items-center gap-1">
          <span className="text-[11px] text-ink-tertiary mr-1">来源:</span>
          <button
            onClick={() => setSourceFilter(null)}
            className={cn(
              "px-2.5 py-1 rounded-lg text-[11px] font-medium transition-all",
              sourceFilter === null
                ? "bg-ink text-white"
                : "bg-card border border-border text-ink-secondary hover:border-border-hover"
            )}
          >
            全部
          </button>
          {sources.map((src) => (
            <button
              key={src}
              onClick={() => setSourceFilter(sourceFilter === src ? null : src)}
              className={cn(
                "px-2.5 py-1 rounded-lg text-[11px] font-medium transition-all capitalize",
                sourceFilter === src
                  ? "bg-accent-blue text-white"
                  : "bg-card border border-border text-ink-secondary hover:border-border-hover"
              )}
            >
              {src}
            </button>
          ))}
        </div>
      </div>

      <div className="bg-card rounded-2xl border border-border shadow-card overflow-hidden">
        <div className="divide-y divide-border">
          {filtered.map((l, i) => (
            <div
              key={i}
              className="px-4 py-2.5 hover:bg-black/[0.01] transition-colors"
            >
              <div className="flex items-center gap-2.5">
                <span
                  className={cn(
                    "px-1.5 py-0.5 rounded-md text-[10px] font-medium",
                    levelColors[l.level]
                  )}
                >
                  {levelLabels[l.level]}
                </span>
                <span className="text-[11px] text-ink-tertiary w-16">
                  {l.source}
                </span>
                <span className="text-[11px] text-ink-tertiary w-14">
                  {new Date(l.timestamp).toLocaleTimeString("zh-CN", {
                    hour: "2-digit",
                    minute: "2-digit",
                    second: "2-digit",
                  })}
                </span>
                <span className="text-xs text-ink-secondary flex-1">
                  {l.message}
                </span>
              </div>
              {(l.request_id || l.run_id) && (
                <div className="flex gap-3 mt-0.5 ml-[108px]">
                  {l.request_id && (
                    <span className="text-[10px] text-ink-tertiary">
                      req: {l.request_id}
                    </span>
                  )}
                  {l.run_id && (
                    <span className="text-[10px] text-ink-tertiary">
                      run: {l.run_id}
                    </span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
        {filtered.length === 0 && (
          <div className="px-4 py-8 text-center text-sm text-ink-tertiary">
            没有找到匹配日志
          </div>
        )}
      </div>
    </div>
  );
}
