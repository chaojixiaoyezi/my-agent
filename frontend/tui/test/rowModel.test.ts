/**
 * 行模型单测（未完成项① 虚拟滚动）：行展开=估算=渲染三一致、
 * 贴底视口切片、中英混排按显示宽度断行。
 */
import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../src/state/session.js";
import { flattenMessages, messageToRows, toolLineToText, viewportSlice } from "../src/rowModel.js";
import { displayWidth } from "../src/width.js";

const W = 40;

describe("messageToRows", () => {
  it("user 消息：首行 ❯ 前缀，超宽按列 wrap", () => {
    const rows = messageToRows({ role: "user", text: "中文内容".repeat(10) }, W);
    expect(rows[0].kind).toBe("user");
    expect(rows[0].text.startsWith("❯ ")).toBe(true);
    expect(rows.length).toBeGreaterThan(1);
    for (const r of rows) expect(displayWidth(r.text)).toBeLessThanOrEqual(W);
  });

  it("短 user 消息单行", () => {
    const rows = messageToRows({ role: "user", text: "hi" }, W);
    expect(rows).toHaveLength(1);
    expect(rows[0].text).toBe("❯ hi");
  });

  it("工具行：单行 + detail 按显示宽度截 80（不劈中文）", () => {
    const line = { key: "1#1", round: 1, callIndex: 1, tool: "ls", status: "开始", detail: "中".repeat(100) };
    const text = toolLineToText(line);
    expect(text.startsWith("[工具] round=1#1 ls 开始")).toBe(true);
    expect(text.endsWith("…")).toBe(true);
    expect(displayWidth(text)).toBeLessThanOrEqual(displayWidth("[工具] round=1#1 ls 开始: ") + 81);
  });

  it("commentary 文本按宽度 wrap", () => {
    const rows = messageToRows({ role: "assistant", text: "", toolLines: ["备注".repeat(30)] }, W);
    expect(rows.every((r) => r.kind === "commentary")).toBe(true);
    expect(rows.length).toBeGreaterThan(1);
  });

  it("markdown 块展开：heading 前缀/code 边框/list bullet/quote/hr", () => {
    const msg: ChatMessage = {
      role: "assistant",
      text: "# 标题\n\n- 列表项\n\n> 引用\n\n---\n\n```\ncode\n```\n\n正文文本",
    };
    const rows = messageToRows(msg, W);
    const kinds = rows.map((r) => r.kind);
    expect(kinds).toContain("heading");
    expect(kinds).toContain("list");
    expect(kinds).toContain("quote");
    expect(kinds).toContain("hr");
    expect(kinds).toContain("code");
    expect(kinds).toContain("text");
    const heading = rows.find((r) => r.kind === "heading");
    expect(heading?.text.startsWith("# ")).toBe(true);
    const list = rows.find((r) => r.kind === "list");
    expect(list?.text.startsWith("• ")).toBe(true);
    const quote = rows.find((r) => r.kind === "quote");
    expect(quote?.text.startsWith("│ ")).toBe(true);
    const codeRows = rows.filter((r) => r.kind === "code");
    expect(codeRows.length).toBeGreaterThanOrEqual(3); // 顶/内容/底
    expect(codeRows[0].text.startsWith("┌─")).toBe(true);
    expect(codeRows[codeRows.length - 1].text.startsWith("└─")).toBe(true);
  });

  it("空助手消息渲染占位空行", () => {
    const rows = messageToRows({ role: "assistant", text: "" }, W);
    expect(rows.length).toBeGreaterThanOrEqual(1);
  });
});

describe("flattenMessages", () => {
  it("扁平化：每条消息后追加间隔空行（2026-08-18 复读根修：行数=渲染行数）", () => {
    const messages: ChatMessage[] = [
      { role: "user", text: "a" },
      { role: "assistant", text: "b" },
    ];
    const flat = flattenMessages(messages, W);
    // 2 内容行 + 2 间隔空行（间距是行模型的一部分，不再是渲染 margin）
    expect(flat).toHaveLength(4);
    expect(flat[0].row.text).toBe("❯ a");
    expect(flat[1].row.kind).toBe("empty");
    expect(flat[2].row.text).toBe("b");
    expect(flat[3].row.kind).toBe("empty");
    expect(flat[0].msgEnd).toBe(true); // 单行消息内容行即末行
    expect(flat[1].msgEnd).toBe(true); // 间隔空行带 msgEnd
    expect(flat[3].msgEnd).toBe(true);
  });

  it("多行消息：内容末行 + 间隔空行各带 msgEnd", () => {
    const flat = flattenMessages([{ role: "user", text: "长".repeat(50) }], W);
    const ends = flat.filter((f) => f.msgEnd);
    expect(ends).toHaveLength(2); // 内容末行 + 间隔空行
    expect(flat[flat.length - 1].row.kind).toBe("empty");
    expect(flat[flat.length - 1].msgEnd).toBe(true);
  });
});

describe("viewportSlice", () => {
  it("贴底窗口：只返回最后 N 行", () => {
    const flat = flattenMessages(
      Array.from({ length: 20 }, (_, i) => ({ role: "user" as const, text: `msg${i}` })),
      W,
    );
    // 每条消息 = 内容行 + 间隔空行 → 40 行；窗口 6 = [空行, ●msg18, 空行, ●msg19, 空行?]
    const view = viewportSlice(flat, 6);
    expect(view).toHaveLength(6);
    expect(view[view.length - 2].row.text).toBe("❯ msg19");
    expect(view[view.length - 1].row.kind).toBe("empty");
  });

  it("消息不足时全量（含间隔空行）", () => {
    const flat = flattenMessages([{ role: "user", text: "x" }], W);
    expect(viewportSlice(flat, 10)).toHaveLength(2);
  });

  it("viewportRows<=0 退化全量（含间隔空行）", () => {
    const flat = flattenMessages([{ role: "user", text: "x" }], W);
    expect(viewportSlice(flat, 0)).toHaveLength(2);
  });
});

describe("表格对齐渲染（2026-08-18 free-code 风格）", () => {
  it("markdown 表格 → 对齐行（表头 + 分隔线 + 数据行）", () => {
    const msg: ChatMessage = {
      role: "assistant",
      text: "| 语言 | 行数 |\n|------|------|\n| Go | 150 |\n| Python | 30 |",
    };
    const rows = messageToRows(msg, W);
    const tables = rows.filter((r) => r.kind === "table");
    expect(tables.length).toBeGreaterThanOrEqual(4);
    // 表头行包含两列内容
    expect(tables[0].text).toContain("语言");
    expect(tables[0].text).toContain("行数");
    // 第二行是分隔线（├ ┼ ┤）
    expect(tables[1].text.startsWith("├")).toBe(true);
    expect(tables[1].text).toContain("┼");
    // 数据行
    expect(tables[2].text).toContain("Go");
    expect(tables[3].text).toContain("Python");
    // 对齐：表头与数据行每列宽度一致
    const colWidths = (t: string) => t.split("│").map((s) => displayWidth(s));
    expect(colWidths(tables[0].text)[1]).toBe(colWidths(tables[2].text)[1]);
  });

  it("表格行按列对齐（中文列宽不劈）", () => {
    const msg: ChatMessage = {
      role: "assistant",
      text: "| 文件 | 作用 |\n|------|------|\n| parse.go | 解析、导航 |\n| bs4.go | 入口 |",
    };
    const rows = messageToRows(msg, W).filter((r) => r.kind === "table");
    for (const r of rows) {
      expect(displayWidth(r.text)).toBeLessThanOrEqual(W);
    }
    expect(rows.length).toBeGreaterThanOrEqual(4);
  });
});
