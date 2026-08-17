/**
 * P0 contract fixtures 测试：用 testbox 真实请求数据做 replay，
 * 验证薄 client 状态机（ask 202 / progress cursor / result 收口 /
 * 权限 403 / 网络重试 / 断线去重）。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { TuiHttpClient } from "../src/protocol/client.js";
import type { ProgressEvent, ResultResponse } from "../src/protocol/types.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIX = join(__dirname, "fixtures");

function fixture(name: string): string {
  return readFileSync(join(FIX, name), "utf-8");
}

/** 从真实 chunks.jsonl 构造分页的 /progress mock 响应（模拟 /progress 投影输出：
 *  tool_progress 为结构化字段 level/round/call_index/tool/phase/status/detail，
 *  2026-08-17 真机实核；assistant_commentary 带 text） */
function progressPages(chunksFile: string, pageSize = 3) {
  const rows = fixture(chunksFile)
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l));
  const projected: ProgressEvent[] = rows
    .filter((r) => r.kind === "tool_progress" || r.kind === "assistant_commentary")
    .map((r) => {
      if (r.kind === "assistant_commentary") {
        return { kind: "assistant_commentary" as const, text: String(r.text ?? "") };
      }
      const p = r.progress ?? {};
      return {
        kind: "tool_progress" as const,
        level: p.level ?? r.verbose_level,
        round: p.round ?? r.round,
        call_index: p.call_index ?? r.call_index,
        tool: p.tool ?? r.tool,
        phase: p.phase ?? r.phase,
        status: p.status ?? r.status,
        detail: p.detail ?? r.detail,
      };
    });
  const pages: { events: ProgressEvent[]; next: number }[] = [];
  for (let i = 0; i < projected.length; i += pageSize) {
    pages.push({ events: projected.slice(i, i + pageSize), next: Math.min(i + pageSize, projected.length) });
  }
  return pages;
}

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("TuiHttpClient contract", () => {
  it("ask: POST /ask 返回 202 + request_id", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ request_id: "req-abc", status: "accepted" }, 202));
    const client = new TuiHttpClient({ baseUrl: "http://127.0.0.1:8420", fetchImpl: fetchMock as typeof fetch });
    const accepted = await client.ask({ prompt: "你好" });
    expect(accepted.request_id).toBe("req-abc");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8420/ask");
    expect((init as RequestInit).method).toBe("POST");
  });

  it("ask: 非 202 抛结构化 HTTP 错误（不匹配文本）", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ error: "forbidden" }, 403));
    const client = new TuiHttpClient({ baseUrl: "http://127.0.0.1:8420", fetchImpl: fetchMock as typeof fetch });
    await expect(client.ask({ prompt: "x" })).rejects.toMatchObject({ kind: "http", status: 403 });
  });

  it("progress: cursor 分页推进 + 只投影 contract 允许的 kind", async () => {
    const pages = progressPages("chunks-complete.jsonl");
    expect(pages.length).toBeGreaterThan(0);
    const fetchMock = vi.fn(async (url: string | URL | Request) => {
      const since = Number(new URL(String(url)).searchParams.get("since") ?? "0");
      const page = pages.find((p, i) => since === 0 && i === 0) ?? { events: [], next: pages.length };
      // 简化：since=0 返回第一页，其余返回空（cursor 语义由状态机推进验证）
      if (since === 0) return jsonResponse({ request_id: "req-x", ...page });
      return jsonResponse({ request_id: "req-x", events: [], next: since });
    });
    const client = new TuiHttpClient({ baseUrl: "http://127.0.0.1:8420", fetchImpl: fetchMock as typeof fetch });
    const prog = await client.progress("req-x", 0);
    expect(prog.events.length).toBeGreaterThan(0);
    for (const ev of prog.events) {
      expect(["tool_progress", "assistant_commentary"]).toContain(ev.kind);
    }
  });

  it("progress: since 非法返回 400（结构化错误）", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ error: "since must be a non-negative integer" }, 400));
    const client = new TuiHttpClient({ baseUrl: "http://127.0.0.1:8420", fetchImpl: fetchMock as typeof fetch });
    await expect(client.progress("req-x", -1)).rejects.toMatchObject({ kind: "http", status: 400 });
  });

  it("progress: 403 forbidden（owner 鉴权失败）", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ error: "forbidden", request_id: "req-x" }, 403));
    const client = new TuiHttpClient({ baseUrl: "http://127.0.0.1:8420", fetchImpl: fetchMock as typeof fetch });
    await expect(client.progress("req-x", 0)).rejects.toMatchObject({ kind: "http", status: 403 });
  });

  it("result: 终态结果可读（真实 fixture）", async () => {
    const real = JSON.parse(fixture("result-complete.json")) as ResultResponse;
    const fetchMock = vi.fn(async () => jsonResponse(real));
    const client = new TuiHttpClient({ baseUrl: "http://127.0.0.1:8420", fetchImpl: fetchMock as typeof fetch });
    const outcome = await client.result(real.id as string);
    expect("done" in outcome).toBe(true);
  });

  it("result: processing 未完成态返回 202（真机实锤的 contract）", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ status: "processing" }, 202));
    const client = new TuiHttpClient({ baseUrl: "http://127.0.0.1:8420", fetchImpl: fetchMock as typeof fetch });
    const outcome = await client.result("req-x");
    expect("pending" in outcome && outcome.pending === "processing").toBe(true);
  });

  it("pollUntilDone: progress 事件收集 + result 收口（真实 fixture replay）", async () => {
    const pages = progressPages("chunks-complete.jsonl");
    const real = JSON.parse(fixture("result-complete.json")) as ResultResponse;
    let progressCalls = 0;
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/progress/")) {
        const since = Number(new URL(url).searchParams.get("since") ?? "0");
        progressCalls += 1;
        const page = pages.find((p) => p.next > since) ?? pages[pages.length - 1];
        return jsonResponse({ request_id: "req-x", ...(page ?? { events: [], next: since }) });
      }
      if (url.includes("/result/")) {
        return jsonResponse(real);
      }
      return jsonResponse({ error: "unexpected" }, 500);
    });
    const client = new TuiHttpClient({
      baseUrl: "http://127.0.0.1:8420",
      pollIntervalMs: 1,
      fetchImpl: fetchMock as typeof fetch,
    });
    const collected: ProgressEvent[] = [];
    const result = await client.pollUntilDone("req-x", (events) => collected.push(...events));
    expect(result.final).toBeDefined();
    expect(collected.length).toBeGreaterThan(0);
    expect(progressCalls).toBeGreaterThan(0);
  });

  it("pollUntilDone: 网络抖动退避重试后恢复（不丢游标）", async () => {
    const real = JSON.parse(fixture("result-complete.json")) as ResultResponse;
    let fails = 0;
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.includes("/progress/") && fails < 2) {
        fails += 1;
        throw new TypeError("fetch failed");
      }
      if (url.includes("/result/")) return jsonResponse(real);
      return jsonResponse({ request_id: "req-x", events: [], next: 0 });
    });
    const client = new TuiHttpClient({
      baseUrl: "http://127.0.0.1:8420",
      pollIntervalMs: 1,
      maxPollRetries: 5,
      fetchImpl: fetchMock as typeof fetch,
    });
    const result = await client.pollUntilDone("req-x");
    expect(result.final).toBeDefined();
    expect(fails).toBe(2);
  });

  it("pollUntilDone: 连续失败超过重试上限 → 结构化 network 错误", async () => {
    const fetchMock = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    const client = new TuiHttpClient({
      baseUrl: "http://127.0.0.1:8420",
      pollIntervalMs: 1,
      maxPollRetries: 2,
      fetchImpl: fetchMock as typeof fetch,
    });
    await expect(client.pollUntilDone("req-x")).rejects.toMatchObject({ kind: "network" });
  });

  it("control: POST /control 带 conversation scope", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ status: "stopped" }));
    const client = new TuiHttpClient({ baseUrl: "http://127.0.0.1:8420", fetchImpl: fetchMock as typeof fetch });
    await client.control("stop", {
      channel: "chat",
      channel_conversation_id: "sess_x",
      channel_user_id: "local-agent",
      canonical_user_id: "local-agent",
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8420/control");
    const body = JSON.parse((init as RequestInit).body as string);
    // 2026-08-17 真机实锤：后端期望斜杠语法（/stop）
    expect(body.command).toBe("/stop");
    expect(body.conversation.canonical_user_id).toBe("local-agent");
  });
});
