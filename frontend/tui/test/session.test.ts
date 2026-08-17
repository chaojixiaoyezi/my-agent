/**
 * 会话状态机测试：提交/排队/进度合并/done/error。
 */
import { describe, expect, it } from "vitest";
import {
  createInitialSession,
  mergeToolLines,
  progressEventToLine,
  sessionReducer,
} from "../src/state/session.js";

describe("sessionReducer", () => {
  it("提交进入 polling（工具行事件不被丢的真机修复）", () => {
    const s = sessionReducer(createInitialSession(), { type: "submit", prompt: "hi", sessionId: "s" });
    expect(s.phase).toBe("polling");
    expect(s.messages.at(-1)?.role).toBe("user");
  });

  it("polling 中再提交排队，不重复建 user 消息", () => {
    let s = sessionReducer(createInitialSession(), { type: "submit", prompt: "a", sessionId: "s" });
    s = sessionReducer(s, { type: "submit", prompt: "b", sessionId: "s" });
    expect(s.queuedPrompt).toBe("b");
    expect(s.messages.filter((m) => m.role === "user")).toHaveLength(1);
  });

  it("progress 事件追加工具行（同工具合并单行）", () => {
    let s = sessionReducer(createInitialSession(), { type: "submit", prompt: "a", sessionId: "s" });
    s = sessionReducer(s, {
      type: "progress",
      events: [
        { kind: "tool_progress", round: 1, call_index: 1, tool: "run_command", status: "开始", detail: "ls" },
      ],
    });
    const assistant = s.messages.at(-1);
    expect(assistant?.role).toBe("assistant");
    expect(assistant?.toolLines).toHaveLength(1);
    // 完成态覆盖开始态（不新增行）
    s = sessionReducer(s, {
      type: "progress",
      events: [
        { kind: "tool_progress", round: 1, call_index: 1, tool: "run_command", status: "完成", detail: "ls" },
      ],
    });
    const lines = s.messages.at(-1)?.toolLines ?? [];
    expect(lines).toHaveLength(1);
    expect((lines[0] as { status: string }).status).toBe("完成");
  });

  it("done 追加 assistant 文本", () => {
    let s = sessionReducer(createInitialSession(), { type: "submit", prompt: "a", sessionId: "s" });
    s = sessionReducer(s, { type: "done", result: { ok: true, response: "答复" } });
    expect(s.messages.at(-1)?.role).toBe("assistant");
    expect(s.messages.at(-1)?.text).toBe("答复");
  });

  it("error 回到 idle + 记录错误", () => {
    let s = sessionReducer(createInitialSession(), { type: "submit", prompt: "a", sessionId: "s" });
    s = sessionReducer(s, { type: "error", message: "boom" });
    expect(s.phase).toBe("idle");
    expect(s.error).toBe("boom");
  });
});

describe("progressEventToLine / mergeToolLines", () => {
  it("结构化 tool_progress → ToolLine", () => {
    const line = progressEventToLine({
      kind: "tool_progress",
      round: 2,
      call_index: 3,
      tool: "read_file",
      status: "开始",
      detail: "/tmp/x",
    }) as { key: string; tool: string };
    expect(line.key).toBe("2#3");
    expect(line.tool).toBe("read_file");
  });

  it("commentary 保持字符串", () => {
    const line = progressEventToLine({ kind: "assistant_commentary", text: "思考中…" });
    expect(line).toBe("思考中…");
  });

  it("mergeToolLines 同 key 覆盖", () => {
    const merged = mergeToolLines([], [
      { key: "1#1", round: 1, callIndex: 1, tool: "t", status: "开始", detail: "" },
      { key: "1#1", round: 1, callIndex: 1, tool: "t", status: "完成", detail: "" },
    ]);
    expect(merged).toHaveLength(1);
  });
});
