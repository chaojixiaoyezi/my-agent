import type { ConfigSchema } from "../types/config";
import { mockConfigSchema, mockCurrentValues } from "../data/mockConfig";
import { frontendRuntimeConfig } from "../data/runtimeConfig";

// ---------------------------------------------------------------------------
// Mock API layer
// ---------------------------------------------------------------------------
// 设计目标：
// 1. 把 mock 数据与 UI 组件解耦，后续替换真实 API 时只需修改此文件
// 2. 所有接口返回 Promise，模拟真实网络的异步特性
// 3. 保留延迟和错误注入钩子，方便测试 loading / error 状态
// ---------------------------------------------------------------------------

const MOCK_DELAY_MS = 300;

async function delay(ms = MOCK_DELAY_MS): Promise<void> {
  return new Promise((res) => setTimeout(res, ms));
}

// ---------------------------------------------------------------------------
// Config APIs
// ---------------------------------------------------------------------------

export async function getConfigSchema(): Promise<ConfigSchema> {
  await delay(150);
  return Promise.resolve(mockConfigSchema);
}

export async function getCurrentConfig(): Promise<Record<string, unknown>> {
  await delay(200);
  return Promise.resolve({ ...mockCurrentValues });
}

export async function validateConfig(
  values: Record<string, unknown>
): Promise<{ valid: boolean; errors: Record<string, string> }> {
  await delay(250);
  const errors: Record<string, string> = {};

  for (const cat of mockConfigSchema.categories) {
    for (const f of cat.fields) {
      const v = values[f.key];

      if (f.minLength !== undefined && typeof v === "string" && v.length < f.minLength) {
        errors[f.key] = `最少 ${f.minLength} 个字符`;
      }
      if (f.maxLength !== undefined && typeof v === "string" && v.length > f.maxLength) {
        errors[f.key] = `最多 ${f.maxLength} 个字符`;
      }
      if (f.min !== undefined && typeof v === "number" && v < f.min) {
        errors[f.key] = `最小值 ${f.min}`;
      }
      if (f.max !== undefined && typeof v === "number" && v > f.max) {
        errors[f.key] = `最大值 ${f.max}`;
      }
      if (f.pattern && typeof v === "string") {
        const re = new RegExp(f.pattern);
        if (!re.test(v)) {
          errors[f.key] = f.patternMessage || "格式不正确";
        }
      }
    }
  }

  return Promise.resolve({ valid: Object.keys(errors).length === 0, errors });
}

export async function exportYaml(values: Record<string, unknown>): Promise<string> {
  await delay(200);
  const lines: string[] = ["# my-agent 配置导出", "---", ""];
  for (const cat of mockConfigSchema.categories) {
    lines.push(`# ${cat.label}`);
    for (const f of cat.fields) {
      const v = values[f.key];
      if (v === null || v === undefined) {
        lines.push(`${f.key}: null`);
      } else if (typeof v === "boolean") {
        lines.push(`${f.key}: ${v}`);
      } else if (typeof v === "number") {
        lines.push(`${f.key}: ${v}`);
      } else if (typeof v === "string") {
        if (v.includes("\n") || v.includes(":") || v.includes("#")) {
          lines.push(`${f.key}: |`);
          for (const line of v.split("\n")) {
            lines.push(`  ${line}`);
          }
        } else {
          lines.push(`${f.key}: "${v}"`);
        }
      } else if (Array.isArray(v)) {
        if (v.length === 0) {
          lines.push(`${f.key}: []`);
        } else {
          lines.push(`${f.key}:`);
          for (const item of v) {
            if (typeof item === "string") {
              lines.push(`  - "${item}"`);
            } else {
              lines.push(`  - ${item}`);
            }
          }
        }
      }
    }
    lines.push("");
  }
  return Promise.resolve(lines.join("\n"));
}

export async function saveConfig(
  _values: Record<string, unknown>
): Promise<{ success: boolean; message: string }> {
  void _values;
  await delay(400);
  // mock: always succeed
  return Promise.resolve({ success: true, message: "配置已保存" });
}

// ---------------------------------------------------------------------------
// Status / Dashboard APIs
// ---------------------------------------------------------------------------

export type SystemStatus = {
  gateway: { running: boolean; pid?: number; uptime: string };
  daemon: { running: boolean; lastRun?: string };
  model: { backend: string; name: string; latencyMs: number };
  subagents: { active: number; pending: number; awaitingAcceptance: number };
  memory: { total: number; today: number };
  requests: { pending: number; processing: number; failed: number };
  notifications: { unread: number; warn: number; info: number };
  errors24h: number;
};

export async function getStatus(): Promise<SystemStatus> {
  await delay(250);
  return Promise.resolve({
    gateway: { running: true, pid: 12345, uptime: "1h 12m" },
    daemon: { running: false, lastRun: "2h 前" },
    model: { backend: "Anthropic 兼容", name: "MiniMax-M2.7", latencyMs: 1200 },
    subagents: { active: 5, pending: 3, awaitingAcceptance: 2 },
    memory: { total: 12450, today: 156 },
    requests: { pending: 2, processing: 1, failed: 0 },
    notifications: { unread: 2, warn: 1, info: 1 },
    errors24h: 0,
  });
}

export async function restartGateway(): Promise<{ success: boolean; message: string }> {
  await delay(800);
  return Promise.resolve({ success: true, message: "Gateway 重启指令已下发（mock）" });
}

export async function runSmokeTest(): Promise<{ success: boolean; message: string; details?: string }> {
  await delay(1200);
  return Promise.resolve({ success: true, message: "Smoke Test 通过（mock）", details: "全部 12 项检查通过" });
}

// ---------------------------------------------------------------------------
// Subagent APIs
// ---------------------------------------------------------------------------

export type DispatchLoopReport = {
  rounds_count: number;
  total_records: number;
  final_pending_count: number;
  stopped_by_limit: boolean;
  stopped_by_no_progress: boolean;
  rounds: { round: number; record_count: number; ok: boolean }[];
  max_rounds: number;
  tool_calls_in_window: number;
  tool_budget_max: number;
};

export type SubagentNode = {
  run_id: string;
  agent_name: string;
  role: string;
  status: string;
  current_step?: string;
  blockers?: string[];
  output_refs?: string[];
  artifact_refs?: string[];
  debug_trace_refs?: string[];
  qa_status?: string;
  children?: SubagentNode[];
  depth?: number;
  report?: DispatchLoopReport;
};

const mockReports: Record<string, DispatchLoopReport> = {
  run_child_001: {
    rounds_count: 3,
    total_records: 3,
    final_pending_count: 0,
    stopped_by_limit: false,
    stopped_by_no_progress: false,
    max_rounds: 5,
    tool_calls_in_window: 8,
    tool_budget_max: frontendRuntimeConfig.tools.budget.max_calls,
    rounds: [
      { round: 1, record_count: 2, ok: true },
      { round: 2, record_count: 1, ok: true },
      { round: 3, record_count: 0, ok: true },
    ],
  },
  run_child_002: {
    rounds_count: 2,
    total_records: 1,
    final_pending_count: 0,
    stopped_by_limit: false,
    stopped_by_no_progress: false,
    max_rounds: 5,
    tool_calls_in_window: 3,
    tool_budget_max: frontendRuntimeConfig.tools.budget.max_calls,
    rounds: [
      { round: 1, record_count: 1, ok: true },
      { round: 2, record_count: 0, ok: true },
    ],
  },
  run_child_003: {
    rounds_count: 4,
    total_records: 2,
    final_pending_count: 1,
    stopped_by_limit: true,
    stopped_by_no_progress: false,
    max_rounds: 4,
    tool_calls_in_window: 12,
    tool_budget_max: frontendRuntimeConfig.tools.budget.max_calls,
    rounds: [
      { round: 1, record_count: 1, ok: true },
      { round: 2, record_count: 0, ok: false },
      { round: 3, record_count: 1, ok: true },
      { round: 4, record_count: 0, ok: false },
    ],
  },
  run_child_004: {
    rounds_count: 4,
    total_records: 5,
    final_pending_count: 0,
    stopped_by_limit: true,
    stopped_by_no_progress: false,
    max_rounds: 4,
    tool_calls_in_window: 18,
    tool_budget_max: frontendRuntimeConfig.tools.budget.max_calls,
    rounds: [
      { round: 1, record_count: 2, ok: true },
      { round: 2, record_count: 1, ok: true },
      { round: 3, record_count: 2, ok: true },
      { round: 4, record_count: 0, ok: true },
    ],
  },
};

export const mockTree: SubagentNode = {
  run_id: "run_root_001",
  agent_name: "coordinator-001",
  role: "coordinator",
  status: "RUNNING",
  current_step: "调度子任务",
  qa_status: "PENDING",
  depth: 0,
  children: [
    {
      run_id: "run_child_001",
      agent_name: "worker-001",
      role: "worker",
      status: "RUNNING",
      current_step: "执行工具调用: write_file",
      output_refs: ["data/subagents/run_001/output.json"],
      artifact_refs: ["data/subagents/run_001/artifacts/patch.diff"],
      debug_trace_refs: ["data/subagents/run_001/debug_trace.jsonl"],
      qa_status: "PENDING",
      depth: 1,
      report: mockReports["run_child_001"],
      children: [
        {
          run_id: "run_grandchild_001",
          agent_name: "tester-001",
          role: "tester",
          status: "DONE",
          qa_status: "DONE",
          depth: 2,
          children: [],
        },
      ],
    },
    {
      run_id: "run_child_002",
      agent_name: "worker-002",
      role: "worker",
      status: "AWAITING_ACCEPTANCE",
      output_refs: ["data/subagents/run_002/output.json"],
      qa_status: "DONE",
      depth: 1,
      report: mockReports["run_child_002"],
      children: [
        {
          run_id: "run_grandchild_002",
          agent_name: "writer-001",
          role: "writer",
          status: "AWAITING_ACCEPTANCE",
          qa_status: "PENDING",
          depth: 2,
          children: [],
        },
      ],
    },
    {
      run_id: "run_child_003",
      agent_name: "worker-003",
      role: "worker",
      status: "BLOCKED",
      current_step: "工具调用超时",
      blockers: [`controlled_exec 超过 ${frontendRuntimeConfig.tools.limits.shell_timeout}s 超时`],
      depth: 1,
      report: mockReports["run_child_003"],
      children: [],
    },
    {
      run_id: "run_child_004",
      agent_name: "researcher-001",
      role: "researcher",
      status: "RUNNING",
      depth: 1,
      report: mockReports["run_child_004"],
      children: [
        {
          run_id: "run_grandchild_003",
          agent_name: "bug_finder-001",
          role: "bug_finder",
          status: "PENDING",
          qa_status: "PENDING",
          depth: 2,
          children: [],
        },
      ],
    },
  ],
};

export async function getSubagentRuns(): Promise<SubagentNode> {
  await delay(300);
  return Promise.resolve(mockTree);
}

// ---------------------------------------------------------------------------
// Memory APIs (placeholder)
// ---------------------------------------------------------------------------

export async function getMemoryStats(): Promise<{
  total: number;
  today: number;
  categories: Record<string, number>;
}> {
  await delay(200);
  return Promise.resolve({
    total: 12450,
    today: 156,
    categories: { tool: 4200, thought: 3800, error: 1200, user: 3250 },
  });
}

// ---------------------------------------------------------------------------
// Logs APIs (placeholder)
// ---------------------------------------------------------------------------

export async function getRecentLogs(): Promise<
  { time: string; level: string; msg: string }[]
> {
  await delay(200);
  return Promise.resolve([
    { time: "10:15", level: "info", msg: "子代理 worker-001 完成 write_file 工具调用" },
    { time: "10:12", level: "info", msg: "Gateway worker-1 完成 run_001" },
    { time: "10:08", level: "info", msg: "子代理 coordinator-001 创建 child 任务" },
    { time: "09:55", level: "warn", msg: "工具调用超时：run_command 超过 30s" },
    { time: "09:30", level: "info", msg: "Gateway 启动成功，监听端口 8420" },
  ]);
}
