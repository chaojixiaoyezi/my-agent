"use strict";
// LLM: 独立 stdio MCP 服务，只用 Node 标准库；标准输出只写协议帧。工具目录来自同包 declaration.json，与宿主装包时
//   核对的声明同源。工作区权限只从 tools/call 的 _meta（my-agent/workspace-read-context v1）逐次取得，arguments 不能冒充；
//   缺上下文或上下文无效时拒绝，不退回插件自己的目录。改动须同步 declaration.json 与 plugins/hello-node/README.md。
// 模块用途: hello-node 示例插件的入口：打招呼，以及按宿主权限读取工作区文本。

const fs = require("fs");
const path = require("path");
const readline = require("readline");
const { EXTENSION, VERSION, ContextError, check, parseContext, realpathLoose } = require("./workspace_read.js");

const MAX_LINE = 131072;
const declaration = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "declaration.json"), "utf8"));
const toolNames = new Set(declaration.tools.map((tool) => tool.name));
let initialized = false;

// LLM: code 是给宿主与模型看的稳定分类，message 是中文说明；不回显本地异常正文。
// 类用途: 表示一次工具调用的业务失败。
class ToolError extends Error {
  // 函数用途: 保存失败分类与说明。
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

// 函数用途: 把结构化结果编码成 MCP 文本内容。
function toolResult(value, isError) {
  return { content: [{ type: "text", text: JSON.stringify(value) }], isError };
}

// 函数用途: 返回问候语，顺带说明运行它的 Node 版本与平台（便于确认宿主确实用了回执里的解释器）。
function hello(args) {
  const name = typeof args.name === "string" && args.name ? args.name : "朋友";
  return { text: `你好，${name}！这条回复来自 Node.js ${process.version}（${process.platform}-${process.arch}）。` };
}

// LLM: 先按宿主上下文裁决，再打开裁决给出的真实路径（最后一段不跟随链接）；Node 没有 openat，所以打开后复核
//   "重新解析的真实路径仍是原目标，且就是刚打开的那个文件"，检查与打开之间被换成链接时拒绝。只读，不写任何文件。
// 函数用途: 读取一个 UTF-8 文本文件的开头部分。
function readText(context, args) {
  const maxBytes = args.max_bytes === undefined ? 4096 : args.max_bytes;
  if (typeof args.path !== "string" || !args.path || !Number.isInteger(maxBytes) || maxBytes < 1 || maxBytes > 65536) {
    throw new ToolError("INVALID_ARGUMENTS", "参数无效。");
  }
  const decision = check(context, args.path);
  if (!decision.allowed) {
    throw new ToolError(decision.code, "宿主本次下发的工作区读取权限不允许读取这个路径。");
  }
  let fd;
  try {
    fd = fs.openSync(decision.target, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  } catch {
    throw new ToolError("READ_FAILED", "文件不存在、是链接或无法打开。");
  }
  try {
    const opened = fs.fstatSync(fd, { bigint: true });
    if (!opened.isFile()) {
      throw new ToolError("NOT_A_FILE", "目标不是普通文件。");
    }
    let current = null;
    try {
      current = realpathLoose(decision.target) === decision.target ? fs.statSync(decision.target, { bigint: true }) : null;
    } catch {
      current = null;
    }
    if (!current || current.dev !== opened.dev || current.ino !== opened.ino) {
      throw new ToolError("PATH_CHANGED", "文件在检查后发生了变化，请重试。");
    }
    const buffer = Buffer.alloc(maxBytes);
    const size = fs.readSync(fd, buffer, 0, maxBytes, 0);
    let text;
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(buffer.subarray(0, size), { stream: true });
    } catch {
      throw new ToolError("NOT_UTF8", "文件不是 UTF-8 文本。");
    }
    return { path: args.path, bytes: size, truncated: opened.size > BigInt(size), text };
  } finally {
    fs.closeSync(fd);
  }
}

// LLM: 业务错误作为 isError 工具结果返回，不影响后续请求；未知异常只给固定分类。
// 函数用途: 分派一次 tools/call。
function callTool(params) {
  if (typeof params.name !== "string" || !toolNames.has(params.name)) {
    return toolResult({ code: "UNKNOWN_TOOL", message: "工具不存在。" }, true);
  }
  const args = params.arguments !== null && typeof params.arguments === "object" ? params.arguments : {};
  try {
    if (params.name === "hello") {
      return toolResult(hello(args), false);
    }
    const meta = params._meta;
    if (meta === null || typeof meta !== "object" || !(EXTENSION in meta)) {
      throw new ToolError("MISSING_CONTEXT", "缺少宿主逐次工作区读取上下文。");
    }
    return toolResult(readText(parseContext(meta[EXTENSION]), args), false);
  } catch (error) {
    if (error instanceof ToolError) {
      return toolResult({ code: error.code, message: error.message }, true);
    }
    if (error instanceof ContextError) {
      return toolResult({ code: "INVALID_CONTEXT", message: "宿主读取上下文无效。" }, true);
    }
    return toolResult({ code: "READ_FAILED", message: "读取失败。" }, true);
  }
}

// 函数用途: 生成 JSON-RPC 错误响应。
function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

// LLM: initialize 声明支持工作区读取扩展 v1，宿主据此在每次调用的 _meta 里附带上下文；声明本身不授予任何权限。
// 函数用途: 处理一条 JSON-RPC 请求，通知（无 id）不回复。
function handle(request) {
  if (request === null || typeof request !== "object" || request.jsonrpc !== "2.0" || typeof request.method !== "string") {
    return rpcError(null, -32600, "请求格式无效。");
  }
  if (!("id" in request)) {
    return null;
  }
  const { id, method } = request;
  const params = request.params !== null && typeof request.params === "object" ? request.params : {};
  if (method === "initialize") {
    initialized = true;
    return { jsonrpc: "2.0", id, result: {
      protocolVersion: "2024-11-05",
      serverInfo: { name: declaration.plugin_id, version: declaration.version },
      capabilities: { tools: {}, experimental: { [EXTENSION]: { versions: [VERSION] } } },
    } };
  }
  if (!initialized) {
    return rpcError(id, -32000, "请先初始化连接。");
  }
  if (method === "ping") {
    return { jsonrpc: "2.0", id, result: {} };
  }
  if (method === "tools/list") {
    return { jsonrpc: "2.0", id, result: { tools: declaration.tools.map((tool) => (
      { name: tool.name, description: tool.description, inputSchema: tool.input_schema })) } };
  }
  if (method === "tools/call") {
    return { jsonrpc: "2.0", id, result: callTool(params) };
  }
  return rpcError(id, -32601, "不支持此方法。");
}

// LLM: 超长行直接退出，交由宿主记录真实退出；stdin 关闭即结束进程。
// 函数用途: 逐行读取标准输入并写回响应。
function main() {
  const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  lines.on("line", (line) => {
    if (line.length > MAX_LINE) {
      process.stderr.write("插件请求超过读取上限。\n");
      process.exit(2);
    }
    let request;
    try {
      request = JSON.parse(line);
    } catch {
      process.stdout.write(JSON.stringify(rpcError(null, -32700, "请求不是有效 JSON。")) + "\n");
      return;
    }
    const response = handle(request);
    if (response !== null) {
      process.stdout.write(JSON.stringify(response) + "\n");
    }
  });
}

main();
