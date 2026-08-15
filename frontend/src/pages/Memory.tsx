import { useState } from "react";
import { Calendar, Search } from "lucide-react";
import { cn } from "../lib/utils";

const mockEvents = [
  {
    id: "evt_001",
    timestamp: "2026-05-12T10:15:30Z",
    type: "tool_call",
    task_id: "task_001",
    run_id: "run_001",
    summary: "write_file: src/utils/helper.py",
    size_chars: 1200,
  },
  {
    id: "evt_002",
    timestamp: "2026-05-12T10:14:15Z",
    type: "memory_write",
    task_id: "task_001",
    run_id: "run_001",
    summary: "自动保存记忆: worker-001 执行结果",
    size_chars: 800,
  },
  {
    id: "evt_003",
    timestamp: "2026-05-12T10:10:00Z",
    type: "subagent_spawn",
    task_id: "task_001",
    run_id: "run_root_001",
    summary: "创建子代理 worker-001",
    size_chars: 300,
  },
  {
    id: "evt_004",
    timestamp: "2026-05-12T09:55:00Z",
    type: "tool_call",
    task_id: "task_002",
    run_id: "run_002",
    summary: "read_file: docs/README.md",
    size_chars: 4500,
  },
  {
    id: "evt_005",
    timestamp: "2026-05-12T09:30:00Z",
    type: "gateway_request",
    task_id: null,
    run_id: null,
    summary: "Gateway 启动，端口 8420",
    size_chars: 200,
  },
];

const typeLabels: Record<string, string> = {
  tool_call: "工具调用",
  memory_write: "记忆写入",
  subagent_spawn: "子代理创建",
  gateway_request: "Gateway 请求",
};

const typeColors: Record<string, string> = {
  tool_call: "bg-accent-blue/10 text-accent-blue",
  memory_write: "bg-accent-green/10 text-accent-green",
  subagent_spawn: "bg-accent-orange/10 text-accent-orange",
  gateway_request: "bg-accent-purple/10 text-accent-purple",
};

const mockDates = [
  { date: "2026-05-12", event_count: 156, task_count: 8, compacted: true },
  { date: "2026-05-11", event_count: 203, task_count: 12, compacted: true },
  { date: "2026-05-10", event_count: 178, task_count: 10, compacted: true },
  { date: "2026-05-09", event_count: 145, task_count: 7, compacted: false },
  { date: "2026-05-08", event_count: 189, task_count: 9, compacted: true },
];

export default function Memory() {
  const [selectedDate, setSelectedDate] = useState("2026-05-12");
  const [search, setSearch] = useState("");

  const filteredEvents = mockEvents.filter(
    (e) =>
      !search ||
      e.summary.toLowerCase().includes(search.toLowerCase()) ||
      e.type.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-ink">记忆浏览</h2>
        <div className="relative">
          <Search
            size={14}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-tertiary"
          />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索事件..."
            className="pl-9 pr-3 py-1.5 text-sm bg-card border border-border rounded-xl text-ink placeholder:text-ink-tertiary focus:outline-none focus:border-accent-blue/40 focus:shadow-focus transition-all"
            style={{ width: 220 }}
          />
        </div>
      </div>

      <div className="grid grid-cols-4 gap-3">
        {mockDates.map((d) => (
          <button
            key={d.date}
            onClick={() => setSelectedDate(d.date)}
            className={cn(
              "text-left rounded-2xl border p-3 transition-all duration-150",
              selectedDate === d.date
                ? "bg-accent-blue/[0.03] border-accent-blue/20"
                : "bg-card border-border hover:border-border-hover"
            )}
          >
            <div className="flex items-center gap-2 mb-1">
              <Calendar size={13} className="text-ink-tertiary" />
              <span className="text-xs font-medium text-ink">{d.date}</span>
              <span
                className={cn(
                  "w-1.5 h-1.5 rounded-full ml-auto",
                  d.compacted ? "bg-accent-green" : "bg-accent-orange"
                )}
              />
            </div>
            <div className="text-[11px] text-ink-secondary">
              {d.event_count} 事件 · {d.task_count} 任务
            </div>
          </button>
        ))}
      </div>

      <div className="bg-card rounded-2xl border border-border shadow-card">
        <div className="px-4 py-2.5 border-b border-border flex items-center gap-2">
          <span className="text-xs font-medium text-ink">{selectedDate} 事件列表</span>
          <span className="text-[11px] text-ink-tertiary">共 {filteredEvents.length} 条</span>
        </div>
        <div className="divide-y divide-border">
          {filteredEvents.map((e) => (
            <div
              key={e.id}
              className="px-4 py-3 hover:bg-black/[0.01] transition-colors"
            >
              <div className="flex items-center gap-2.5">
                <span
                  className={cn(
                    "px-1.5 py-0.5 rounded-md text-[10px] font-medium",
                    typeColors[e.type] || typeColors.tool_call
                  )}
                >
                  {typeLabels[e.type] || e.type}
                </span>
                <span className="text-xs text-ink-secondary flex-1">
                  {e.summary}
                </span>
                <span className="text-[11px] text-ink-tertiary">
                  {e.size_chars} 字符
                </span>
              </div>
              <div className="flex items-center gap-3 mt-1 text-[11px] text-ink-tertiary">
                {e.task_id && <span>任务: {e.task_id}</span>}
                {e.run_id && <span>Run: {e.run_id}</span>}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
