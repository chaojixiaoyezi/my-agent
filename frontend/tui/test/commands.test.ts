/**
 * 命令目录测试：解析 + 命令表（stop/btw/goal 走 /control，不绑 /stop）。
 */
import { describe, expect, it } from "vitest";
import { commandByName, helpText, parseCommand } from "../src/commands.js";

describe("parseCommand", () => {
  it("普通消息不是命令", () => {
    expect(parseCommand("你好")).toBeNull();
  });

  it("/ 命令解析", () => {
    const cmd = parseCommand("/stop");
    expect(cmd?.name).toBe("stop");
  });

  it("带参数命令", () => {
    const cmd = parseCommand("/btw 换个思路");
    expect(cmd?.name).toBe("btw");
    expect(cmd?.args).toBe("换个思路");
  });
});

describe("commandByName", () => {
  it("stop/btw/goal 映射 /control（不绑守护进程 /stop）", () => {
    expect(commandByName("stop")?.control).toBe("stop");
    expect(commandByName("btw")?.control).toBe("btw");
    expect(commandByName("goal")?.control).toBe("goal");
  });

  it("help/clear/exit 本地处理", () => {
    expect(commandByName("help")?.local).toBe("help");
    expect(commandByName("clear")?.local).toBe("clear");
    expect(commandByName("exit")?.local).toBe("exit");
  });

  it("未知命令", () => {
    expect(commandByName("nope")).toBeUndefined();
  });
});

describe("helpText", () => {
  it("包含全部命令", () => {
    const text = helpText();
    expect(text).toContain("/stop");
    expect(text).toContain("/btw");
    expect(text).toContain("/goal");
    expect(text).toContain("/theme");
  });
});
