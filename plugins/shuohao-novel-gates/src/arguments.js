"use strict";
// LLM: 插件与 tools/list 共用同包声明；这里只处理此包使用的 JSON Schema 子集，不修改宿主通用合同。
// 模块用途: 直接 MCP 调用也拒绝未知参数和写命令，不依赖调用方是否先做 schema 校验。
const { ToolError } = require("./errors.js");

// LLM: 此包仅字符串与路径数组；有限数组项数避免一次调用无界读取，机器只看声明字段。
// 函数用途: 判断一个参数是否符合声明的类型、长度与枚举。
function matches(value, schema) {
  if (schema.type === "array") {
    return Array.isArray(value) && value.length <= schema.maxItems && value.every((item) => matches(item, schema.items));
  }
  return typeof value === "string" && value.length >= (schema.minLength || 0)
    && value.length <= (schema.maxLength || 4096) && !value.includes("\0")
    && (!schema.enum || schema.enum.includes(value));
}

// LLM: 不采纳 arguments._meta，读取授权由服务入口单独恢复；所有字段都必须出现在工具 schema。
// 函数用途: 核对工具参数，默认 operation 由包装层按阶段确定。
function validateArguments(args, tool) {
  const schema = tool.input_schema;
  if (!args || typeof args !== "object" || Array.isArray(args)) {
    throw new ToolError("INVALID_ARGUMENTS", "参数必须是对象。");
  }
  const valid = Object.keys(args).every((key) => Object.hasOwn(schema.properties, key) && matches(args[key], schema.properties[key]));
  if (!valid || !schema.required.every((key) => Object.hasOwn(args, key))) {
    throw new ToolError("INVALID_ARGUMENTS", "参数不符合只读工具声明。");
  }
  return args;
}
module.exports = { validateArguments };
