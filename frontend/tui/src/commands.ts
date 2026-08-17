/**
 * TUI 命令目录（P3）：结构化命令表（不复制后端命令表，只放 TUI 需要的
 * 本地命令 + /control 会话控制映射）。会话控制（stop/btw/goal）一律走
 * POST /control + conversation scope；/stop 是守护进程管理命令不绑这里。
 */
import type { ControlCommand } from "./protocol/types.js";

export interface TuiCommand {
  name: string;
  usage: string;
  description: string;
  /** control 命令映射（走 /control） */
  control?: ControlCommand;
  /** 本地处理器 */
  local?: "help" | "clear" | "exit" | "theme";
}

export const COMMANDS: TuiCommand[] = [
  { name: "help", usage: "/help", description: "显示命令帮助", local: "help" },
  { name: "clear", usage: "/clear", description: "清屏", local: "clear" },
  { name: "exit", usage: "/exit", description: "退出 TUI", local: "exit" },
  { name: "theme", usage: "/theme [dark|light]", description: "切换主题", local: "theme" },
  { name: "stop", usage: "/stop", description: "停止当前任务（/control）", control: "stop" },
  { name: "btw", usage: "/btw <内容>", description: "给当前任务改向（/control）", control: "btw" },
  { name: "goal", usage: "/goal <内容>", description: "设置目标（/control）", control: "goal" },
];

/** 解析输入：是命令吗？返回命令名 + 参数 */
export function parseCommand(raw: string): { name: string; args: string } | null {
  const text = raw.trim();
  if (!text.startsWith("/")) return null;
  const parts = text.slice(1).split(/\s+/);
  const name = (parts[0] ?? "").toLowerCase();
  if (!name) return null;
  return { name, args: text.slice(1).replace(/^\S+\s*/, "") };
}

export function commandByName(name: string): TuiCommand | undefined {
  return COMMANDS.find((c) => c.name === name);
}

export function helpText(): string {
  const lines = COMMANDS.map((c) => `  ${c.usage.padEnd(28)} ${c.description}`);
  return ["TUI 命令：", ...lines, "（/control 会话控制：stop/btw/goal）"].join("\n");
}
