/**
 * markdown 渲染测试（完整版收尾）：块解析 + 内联样式。
 */
import { describe, expect, it } from "vitest";
import { inlineSegments, markdownToLines } from "../src/markdown.js";

describe("markdownToLines", () => {
  it("标题/列表/代码块/分隔线/引用", () => {
    const blocks = markdownToLines("# 标题\n\n- 列表项\n\n```go\nfmt.Println(\"hi\")\n```\n\n---\n\n> 引用");
    const kinds = blocks.map((b) => b.kind);
    expect(kinds).toContain("heading");
    expect(kinds).toContain("list");
    expect(kinds).toContain("code");
    expect(kinds).toContain("hr");
    expect(kinds).toContain("quote");
    const heading = blocks.find((b) => b.kind === "heading");
    expect(heading?.text).toContain("标题");
    expect(heading?.level).toBe(1);
    const code = blocks.find((b) => b.kind === "code");
    expect(code?.text).toContain("fmt.Println");
  });

  it("相邻段落合并", () => {
    const blocks = markdownToLines("第一段\n\n第二段");
    expect(blocks.filter((b) => b.kind === "text")).toHaveLength(2);
  });
});

describe("inlineSegments", () => {
  it("粗体/代码/链接分段", () => {
    const segs = inlineSegments("这是 **粗体** 和 `代码` 与 [链接](https://x)");
    expect(segs.some((s) => s.bold && s.text === "粗体")).toBe(true);
    expect(segs.some((s) => s.code && s.text === "代码")).toBe(true);
    expect(segs.some((s) => s.link && s.text === "链接")).toBe(true);
  });

  it("无样式文本原样", () => {
    expect(inlineSegments("普通文本")).toEqual([{ text: "普通文本" }]);
  });
});
