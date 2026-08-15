import { useState } from "react";
import {
  ChevronRight,
  ChevronDown,
  CheckCircle2,
  AlertTriangle,
  Clock,
  Loader2,
  Pause,
  FileText,
  Wrench,
  Search,
  Activity,
  Ban,
} from "lucide-react";
import { cn } from "../lib/utils";
import type { SubagentNode, DispatchLoopReport } from "../api/mockApi";
import { mockTree } from "../api/mockApi";

const roleColors: Record<string, string> = {
  coordinator: "bg-accent-purple/10 text-accent-purple border-accent-purple/20",
  worker: "bg-accent-blue/10 text-accent-blue border-accent-blue/20",
  tester: "bg-accent-green/10 text-accent-green border-accent-green/20",
  writer: "bg-accent-cyan/10 text-accent-cyan border-accent-cyan/20",
  researcher: "bg-accent-orange/10 text-accent-orange border-accent-orange/20",
  bug_finder: "bg-accent-red/10 text-accent-red border-accent-red/20",
  acceptor: "bg-accent-green/10 text-accent-green border-accent-green/20",
};

function StatusIcon({ status }: { status: string }) {
  if (status === "RUNNING")
    return <Loader2 size={13} className="text-accent-blue animate-spin" />;
  if (status === "DONE")
    return <CheckCircle2 size={13} className="text-accent-green" />;
  if (status === "BLOCKED")
    return <AlertTriangle size={13} className="text-accent-red" />;
  if (status === "AWAITING_ACCEPTANCE")
    return <Pause size={13} className="text-accent-orange" />;
  return <Clock size={13} className="text-ink-tertiary" />;
}

function ReportCard({ report }: { report: DispatchLoopReport }) {
  const roundPct = Math.min(100, (report.rounds_count / report.max_rounds) * 100);
  const toolPct = Math.min(100, (report.tool_calls_in_window / report.tool_budget_max) * 100);

  const stopReason = report.stopped_by_limit
    ? "达到轮数上限"
    : report.stopped_by_no_progress
    ? "无进展停止"
    : "正常完成";

  const stopColor = report.stopped_by_limit
    ? "text-accent-orange"
    : report.stopped_by_no_progress
    ? "text-accent-red"
    : "text-accent-green";

  return (
    <div className="rounded-xl bg-surface border border-border p-3 space-y-3">
      <div className="flex items-center gap-1.5 text-[11px] font-medium text-ink-secondary">
        <Activity size={12} />
        调度报告
      </div>

      {/* Rounds progress */}
      <div className="space-y-1">
        <div className="flex items-center justify-between text-[11px]">
          <span className="text-ink-tertiary">思考轮数</span>
          <span className={cn("font-medium", report.stopped_by_limit ? "text-accent-orange" : "text-ink")}>
            {report.rounds_count} / {report.max_rounds}
          </span>
        </div>
        <div className="h-1.5 rounded-full bg-black/[0.06] overflow-hidden">
          <div
            className={cn(
              "h-full rounded-full transition-all",
              report.stopped_by_limit ? "bg-accent-orange" : "bg-accent-blue"
            )}
            style={{ width: `${roundPct}%` }}
          />
        </div>
      </div>

      {/* Tool budget progress */}
      <div className="space-y-1">
        <div className="flex items-center justify-between text-[11px]">
          <span className="text-ink-tertiary">工具调用</span>
          <span className="text-ink font-medium">
            {report.tool_calls_in_window} / {report.tool_budget_max}
          </span>
        </div>
        <div className="h-1.5 rounded-full bg-black/[0.06] overflow-hidden">
          <div
            className="h-full rounded-full bg-accent-green transition-all"
            style={{ width: `${toolPct}%` }}
          />
        </div>
      </div>

      {/* Stop reason */}
      <div className={cn("flex items-center gap-1.5 text-[11px] font-medium", stopColor)}>
        {report.stopped_by_limit ? <Ban size={12} /> : report.stopped_by_no_progress ? <AlertTriangle size={12} /> : <CheckCircle2 size={12} />}
        {stopReason}
      </div>

      {/* Rounds detail */}
      {report.rounds.length > 0 && (
        <div className="space-y-1">
          <span className="text-[10px] text-ink-tertiary">每轮详情</span>
          <div className="grid grid-cols-3 gap-1">
            {report.rounds.map((r) => (
              <div
                key={r.round}
                className={cn(
                  "px-2 py-1 rounded-lg text-[10px] text-center border",
                  r.ok
                    ? "bg-accent-green/[0.04] border-accent-green/10 text-accent-green"
                    : "bg-accent-red/[0.04] border-accent-red/10 text-accent-red"
                )}
              >
                <div className="font-medium">第 {r.round} 轮</div>
                <div className="opacity-80">{r.record_count} 记录</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function SubagentTreeNode({
  node,
  defaultExpanded = false,
}: {
  node: SubagentNode;
  defaultExpanded?: boolean;
}) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [selected, setSelected] = useState(false);
  const hasChildren = (node.children?.length ?? 0) > 0;

  return (
    <div>
      <div
        className={cn(
          "flex items-center gap-2 py-2 px-3 rounded-xl cursor-pointer transition-all duration-150 hover:bg-black/[0.02]",
          selected && "bg-accent-blue/[0.03]"
        )}
        style={{ paddingLeft: 12 + (node.depth ?? 0) * 24 }}
        onClick={() => setSelected(!selected)}
      >
        <button
          onClick={(e) => {
            e.stopPropagation();
            setExpanded(!expanded);
          }}
          className={cn(
            "w-5 h-5 rounded-md flex items-center justify-center text-ink-tertiary hover:text-ink hover:bg-black/[0.04] transition-colors",
            !hasChildren && "invisible"
          )}
          aria-label={expanded ? "折叠" : "展开"}
        >
          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>

        <StatusIcon status={node.status} />

        <span className="text-sm font-medium text-ink">{node.agent_name}</span>

        <span
          className={cn(
            "px-1.5 py-0.5 rounded-md text-[10px] font-medium border",
            roleColors[node.role] || roleColors.worker
          )}
        >
          {node.role}
        </span>

        <span className="text-[11px] text-ink-tertiary">{node.status}</span>
      </div>

      {selected && (
        <div
          className="rounded-xl bg-surface border border-border p-3 mx-3 mb-2 space-y-2"
          style={{ marginLeft: 12 + (node.depth ?? 0) * 24 + 28 }}
        >
          {node.current_step && (
            <div className="text-xs text-ink-secondary">
              <span className="text-ink-tertiary">当前步骤：</span> {node.current_step}
            </div>
          )}
          {node.blockers && node.blockers.length > 0 && (
            <div className="text-xs text-accent-red flex items-start gap-1">
              <AlertTriangle size={12} className="mt-0.5 shrink-0" />
              {node.blockers.join(", ")}
            </div>
          )}
          {node.output_refs && node.output_refs.length > 0 && (
            <div className="space-y-0.5">
              <span className="text-[11px] text-ink-tertiary">Output refs</span>
              {node.output_refs.map((r) => (
                <div
                  key={r}
                  className="flex items-center gap-1 text-[11px] text-accent-blue"
                >
                  <FileText size={10} />
                  {r}
                </div>
              ))}
            </div>
          )}
          {node.artifact_refs && node.artifact_refs.length > 0 && (
            <div className="space-y-0.5">
              <span className="text-[11px] text-ink-tertiary">Artifact refs</span>
              {node.artifact_refs.map((r) => (
                <div
                  key={r}
                  className="flex items-center gap-1 text-[11px] text-accent-blue"
                >
                  <Wrench size={10} />
                  {r}
                </div>
              ))}
            </div>
          )}
          {node.qa_status && (
            <div className="text-xs text-ink-secondary">
              <span className="text-ink-tertiary">QA 状态：</span> {node.qa_status}
            </div>
          )}
          {node.report && <ReportCard report={node.report} />}
        </div>
      )}

      {expanded &&
        node.children?.map((child) => (
          <SubagentTreeNode key={child.run_id} node={child} />
        ))}
    </div>
  );
}

export default function Subagents() {
  const [filter, setFilter] = useState("");

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-ink">子代理层级树</h2>
        <div className="relative">
          <Search
            size={14}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-tertiary"
          />
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="过滤子代理..."
            className="pl-9 pr-3 py-1.5 text-sm bg-card border border-border rounded-xl text-ink placeholder:text-ink-tertiary focus:outline-none focus:border-accent-blue/40 focus:shadow-focus transition-all"
            style={{ width: 220 }}
          />
        </div>
      </div>

      <div className="bg-card rounded-2xl border border-border shadow-card p-3">
        <SubagentTreeNode node={mockTree} defaultExpanded={true} />
      </div>
    </div>
  );
}
