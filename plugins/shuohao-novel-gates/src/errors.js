"use strict";
// LLM: 包内业务异常只返回稳定 code 与固定中文说明，不回显文件正文或本地栈；由 MCP 入口转 isError。
// 模块用途: 统一只读工具的失败分类，避免把拒绝或读失败当成门通过。

// LLM: 供参数门、读取门与服务入口共用；不写文件、不启动进程。
// 类用途: 保存可供宿主和模型判断的错误码。
class ToolError extends Error {
  // LLM: 只保存调用方提供的分类与用户说明；入口不得把任意异常文本交给这里。
  // 函数用途: 创建一次可公开的工具失败。
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}
module.exports = { ToolError };
