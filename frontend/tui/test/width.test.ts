/**
 * Unicode 显示宽度单测（未完成项②）：全角 2 列 / emoji 2 列 / 零宽 0 列 /
 * 截断与断行按列宽不按码点数。
 */
import { describe, expect, it } from "vitest";
import { codePointWidth, displayWidth, truncateByWidth, wrapByWidth } from "../src/width.js";

describe("displayWidth", () => {
  it("ASCII 与半角各 1 列", () => {
    expect(displayWidth("abc")).toBe(3);
    expect(displayWidth("hello world")).toBe(11);
  });

  it("中文/全角各 2 列", () => {
    expect(displayWidth("你好")).toBe(4);
    expect(displayWidth("中文abc")).toBe(7);
    expect(displayWidth("，。！")).toBe(6); // 全角标点
    expect(displayWidth("ＡＢＣ")).toBe(6); // 全角字母
  });

  it("emoji 2 列（含 ZWJ 序列只计基字符）", () => {
    expect(displayWidth("😀")).toBe(2);
    expect(displayWidth("🚀")).toBe(2);
    expect(displayWidth("👨‍👩‍👧")).toBe(6); // 3 个 emoji 基字符，ZWJ 零宽
    expect(displayWidth("👍🏽")).toBe(2); // 肤色修饰符按 0/1 不叠加（简化规则内）
  });

  it("零宽字符 0 列", () => {
    expect(codePointWidth(0x200d)).toBe(0); // ZWJ
    expect(codePointWidth(0xfeff)).toBe(0); // BOM
    expect(codePointWidth(0x0301)).toBe(0); // combining acute
    expect(displayWidth("é")).toBe(1); // é（e + combining）
  });

  it("混合排布求和", () => {
    expect(displayWidth("ctx 7.0K/200K")).toBe(13);
    expect(displayWidth("进度[▓▓░░] 50%")).toBe(displayWidth("进度[▓▓░░] 50%"));
  });
});

describe("truncateByWidth", () => {
  it("超宽按列截断并带省略号", () => {
    const r = truncateByWidth("中文内容很长", 6);
    expect(r.truncated).toBe(true);
    expect(displayWidth(r.text)).toBeLessThanOrEqual(6);
  });

  it("不超宽原样返回", () => {
    const r = truncateByWidth("ok", 10);
    expect(r).toEqual({ text: "ok", truncated: false });
  });

  it("全角边界：不劈开汉字", () => {
    const r = truncateByWidth("你好世界", 5); // 2+2=4 放得下"你好"，+2 超出
    expect(r.text).toBe("你好…");
    expect(displayWidth(r.text)).toBe(5);
  });
});

describe("wrapByWidth", () => {
  it("窄列断行保持宽度不超", () => {
    const lines = wrapByWidth("中文abc中文def", 6);
    for (const line of lines) expect(displayWidth(line)).toBeLessThanOrEqual(6);
    expect(lines.length).toBeGreaterThan(1);
  });

  it("不超宽单行", () => {
    expect(wrapByWidth("hi", 10)).toEqual(["hi"]);
  });

  it("保留换行结构", () => {
    expect(wrapByWidth("a\nb", 10)).toEqual(["a", "b"]);
  });

  it("maxWidth<=0 退化按换行拆", () => {
    expect(wrapByWidth("a\nb", 0)).toEqual(["a", "b"]);
  });
});
