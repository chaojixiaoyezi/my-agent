"use strict";
// LLM: 独立 Node 18+ MCP stdio 入口；目录与安装包声明同源，逐次授权只取 _meta；固定 ESM 装载完成后串行处理协议帧。
//   标准输出只写 JSON-RPC，未知错误不泄露路径、文档或栈。联测真实 MCP、宿主安装确认与 tools/list 全量一致性。
// 模块用途: 为 outline/art/script/storyboard/cast 提供五个只读工具，不接入写命令、report 或出图服务。
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");
const { EXTENSION, VERSION, ContextError, parseContext } = require("./workspace_read.js");
const { validateArguments } = require("./arguments.js");
const { ToolError } = require("./errors.js");
const { loadModules, runCheck } = require("./gates.js");
const declaration = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "declaration.json"), "utf8"));
const tools = new Map(declaration.tools.map((tool) => [tool.name, tool]));
const stages = { outline_check: "outline", art_check: "art", script_check: "script", storyboard_check: "storyboard", cast_check: "characters" };
const MAX_LINE = 131072;
let initialized = false;

// LLM: 内容只来自结构化字段；门失败是成功计算的业务结果，读取拒绝才 isError，不能翻转成通过。
// 函数用途: 把结果包装为 MCP 工具回执。
function toolResult(value, isError = false) {
  return { content: [{ type: "text", text: JSON.stringify(value) }], isError };
}

// LLM: 所有本地异常固定分类；上下文错误与业务错误分别公开，未知异常不补空结果。
// 函数用途: 生成一次可公开的工具失败。
function failure(error) {
  if (error instanceof ToolError) return toolResult({ code: error.code, message: error.message }, true);
  if (error instanceof ContextError) return toolResult({ code: "INVALID_CONTEXT", message: "宿主读取上下文无效。" }, true);
  return toolResult({ code: "CHECK_FAILED", message: "输入无法完成检查，请核对文件格式与结构。" }, true);
}

// LLM: 工具名固定白名单；arguments 的路径不参与模块选择。缺上下文整体拒绝，不使用安装目录或进程 cwd 替代。
// 函数用途: 核对逐次授权和参数后分派只读计算。
function callTool(params, modules) {
  if (!tools.has(params.name)) return toolResult({ code: "UNKNOWN_TOOL", message: "工具不存在。" }, true);
  try {
    if (!params._meta || !Object.hasOwn(params._meta, EXTENSION)) throw new ToolError("MISSING_CONTEXT", "缺少宿主本次读取上下文。");
    const context = parseContext(params._meta[EXTENSION]);
    const args = validateArguments(params.arguments, tools.get(params.name));
    return toolResult(runCheck(modules, stages[params.name], context, args));
  } catch (error) {
    return failure(error);
  }
}

// LLM: 协议错误不伪装成工具结果，不解析错误自然语言做路由。
// 函数用途: 返回 JSON-RPC 错误。
function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

// LLM: initialize 只声明读取扩展 v1，不请求写上下文；通知不回复，目录与描述完全同源。
// 函数用途: 处理一条 MCP 协议请求。
function handle(request, modules) {
  if (!request || request.jsonrpc !== "2.0" || typeof request.method !== "string") return rpcError(null, -32600, "请求格式无效。");
  if (!Object.hasOwn(request, "id")) return null;
  const { id, method } = request;
  if (method === "initialize") {
    initialized = true;
    return { jsonrpc: "2.0", id, result: { protocolVersion: "2024-11-05",
      capabilities: { tools: {}, experimental: { [EXTENSION]: { versions: [VERSION] } } },
      serverInfo: { name: declaration.plugin_id, version: declaration.version } } };
  }
  if (!initialized) return rpcError(id, -32000, "请先初始化连接。");
  if (method === "ping") return { jsonrpc: "2.0", id, result: {} };
  if (method === "tools/list") return { jsonrpc: "2.0", id, result: { tools: declaration.tools.map((tool) => (
    { name: tool.name, description: tool.description, inputSchema: tool.input_schema })) } };
  if (method === "tools/call") return { jsonrpc: "2.0", id, result: callTool(request.params || {}, modules) };
  return rpcError(id, -32601, "不支持此方法。");
}

// LLM: 每条消息至多 MAX_LINE 个字符；stdin EOF 自然退出，无后台工作。JSON 坏帧不影响后续合法请求。
// 函数用途: 串行读取协议帧并把响应写到 stdout。
async function main() {
  const modules = await loadModules();
  const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of lines) {
    if (line.length > MAX_LINE) throw new ToolError("FRAME_TOO_LARGE", "插件请求超过上限。");
    let request;
    try { request = JSON.parse(line); } catch { process.stdout.write(JSON.stringify(rpcError(null, -32700, "请求不是有效 JSON。")) + "\n"); continue; }
    const response = handle(request, modules);
    if (response) process.stdout.write(JSON.stringify(response) + "\n");
  }
}
main().catch(() => { process.stderr.write("插件启动或协议处理失败。\n"); process.exitCode = 2; });
