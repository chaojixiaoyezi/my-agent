/**
 * TUI 入口（P1）：解析参数 → 连 gateway → 渲染 App。
 * 仅回环本机（P0/P1 定位）；gateway base URL 由环境变量/参数传入。
 * P4：my-agent 入口检测 tty+node 后拉起本入口；无 node 回退 Python plain。
 */
import React from "react";
import { render } from "ink";
import { App } from "./App.js";
import { TuiHttpClient } from "./protocol/client.js";

function parseArgs(argv: string[]): { baseUrl: string; sessionId: string; model: string } {
  const args = new Map<string, string>();
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a.startsWith("--") && i + 1 < argv.length) {
      args.set(a.slice(2), argv[i + 1]);
    }
  }
  return {
    baseUrl: args.get("base-url") ?? process.env.MY_AGENT_GATEWAY_URL ?? "http://127.0.0.1:8420",
    sessionId: args.get("session-id") ?? process.env.MY_AGENT_SESSION_ID ?? `tui-${process.pid}-${Date.now()}`,
    model: args.get("model") ?? process.env.MY_AGENT_MODEL ?? "unknown",
  };
}

async function main(): Promise<void> {
  const { baseUrl, sessionId, model } = parseArgs(process.argv.slice(2));
  const client = new TuiHttpClient({ baseUrl });
  // 历史恢复（完整版③）：启动时拉取本会话历史注入（TUI 重启接续）
  let history: { role: "user" | "assistant"; text: string }[] = [];
  try {
    history = await client.history(sessionId);
  } catch {
    history = []; // 历史不可用不阻塞启动
  }
  render(<App client={client} sessionId={sessionId} model={model} history={history} />);
}

void main();
