// LLM: v8 文件入口，握手声明观察/收紧版本 1；事件只回执，审核只返回裁决，不执行收到的命令。
// 模块用途: 无 npm 依赖的单只读工具和 v8 方法起点；不读宿主状态，隔离必须由 B7 宿主施加。
"use strict";

const fs = require("fs");
const path = require("path");
const readline = require("readline");
const declaration = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "declaration.json"), "utf8"));
const MAX_LINE = 131072;

// LLM: 附加字段和错误类型必须拒绝；Array.from 用码点而非 UTF-16 单元计数，保持与 Python 模板一致。
// 函数用途: 执行唯一的纯文本工具，返回 MCP 业务结果，不产生文件和网络副作用。
function callTool(params) {
  const args = params.arguments;
  let value;
  let failed = true;
  if (params.name !== "count_text") {
    value = { code: "UNKNOWN_TOOL", message: "工具不存在。" };
  } else if (!args || typeof args !== "object" || Array.isArray(args) || Object.keys(args).length !== 1 ||
             typeof args.text !== "string" || Array.from(args.text).length > 4096) {
    value = { code: "INVALID_ARGUMENTS", message: "需要不超过 4096 字符的 text。" };
  } else {
    value = { characters: Array.from(args.text).length };
    failed = false;
  }
  return { content: [{ type: "text", text: JSON.stringify(value) }], isError: failed };
}

// LLM: 不回显本地异常和用户输入；协议错误不冒充工具调用成功。
// 函数用途: 构造 JSON-RPC 错误帧。
function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

// LLM: 目录与包描述同源，两个 experimental 位须与方法同步；通知不回复，握手不授权限。
// 函数用途: 路由标准 MCP 和 v8 的观察/收紧方法，不生成宿主操作或执行输入。
function handle(request) {
  if (!request || typeof request !== "object" || Array.isArray(request) || request.jsonrpc !== "2.0" ||
      typeof request.method !== "string") {
    return rpcError(null, -32600, "请求格式无效。");
  }
  if (!("id" in request)) return null;
  const params = request.params && typeof request.params === "object" ? request.params : {};
  let result;
  if (request.method === "initialize") {
    result = { protocolVersion: "2024-11-05", capabilities: { tools: {}, experimental: {
                 "my-agent/events": { versions: ["1"] }, "my-agent/tool-gate": { versions: ["1"] } } },
               serverInfo: { name: declaration.plugin_id, version: declaration.version } };
  } else if (request.method === "tools/list") {
    result = { tools: declaration.tools.map(toolDescription) };
  } else if (request.method === "tools/call") {
    result = callTool(params);
  } else if (request.method === "my-agent/events.observe") {
    result = {};
  } else if (request.method === "my-agent/tool-gate.review") {
    result = reviewGate(params);
  } else if (request.method === "ping") {
    result = {};
  } else {
    return rpcError(request.id, -32601, "不支持此方法。");
  }
  return { jsonrpc: "2.0", id: request.id, result };
}

// LLM: 只核清单中的精确工具和结构化 command；截断只能更严——先按看到的片段判：片段已 deny 保持；
//   片段本来就 ask 时保留它自己的原因码、消息补半句"参数还被截断了"；只有片段可放行才升到
//   ask + ARGUMENTS_TRUNCATED（宿主只在 arguments: full 时给标记，且必须是布尔 true）；参数缺失宁严按 ask。
//   绝不改参数、不执行命令、不放宽宿主。
// 函数用途: 演示 rm -rf 字面模式加确认、参数里的空字节直接拒绝；不是完整 shell 分析器，不替代宿主审批与沙箱。
function reviewGate(params) {
  const gate = declaration.tool_gates.find(item => item.id === params.gate_id);
  const call = params.call && typeof params.call === "object" ? params.call : {};
  const decision = reviewVisible(call, gate);
  if (call.arguments_truncated !== true || decision.verdict === "deny") return decision;
  if (decision.verdict === "ask") {
    const message = decision.message ? `${decision.message}；参数还被截断了` : "参数还被截断了";
    return { ...decision, message };
  }
  return { verdict: "ask", reason_code: "ARGUMENTS_TRUNCATED", message: "参数被截断，看不全命令内容，先确认一次" };
}

// LLM: 只看得到的片段做原判定：门不匹配一律 deny，参数缺失宁严按 ask；截断合并由 reviewGate 统一处理。
//   参数级 deny 示例（与 rm 规则无关，任何工具参数都适用）：命令字符串里出现 NUL 空字节这类畸形内容时直接拒绝——
//   它不是"要求确认"的量级，下游 shell 与解析器对它的处理各不相同，看到就必须拒绝；截断也不能把它降成 ask
//   （reviewGate 对 deny 早返回，不参与截断合并）。
// 函数用途: 对看到的调用片段给出 allow_as_is / ask / deny 三种裁决之一。
function reviewVisible(call, gate) {
  if (!gate || !gate.tools.includes(call.tool)) return { verdict: "deny", reason_code: "OUT_OF_SCOPE" };
  const args = call.arguments;
  if (!args || typeof args !== "object" || Array.isArray(args) || typeof args.command !== "string") {
    return { verdict: "ask", reason_code: "ARGUMENTS_UNAVAILABLE" };
  }
  if (args.command.includes("\u0000")) return { verdict: "deny", reason_code: "MALFORMED_ARGUMENTS" };
  if (/\brm\s+-rf\b/u.test(args.command)) {
    return { verdict: "ask", reason_code: "RM_RF", message: "要删除整个目录，先确认一次" };
  }
  return { verdict: "allow_as_is", reason_code: "NO_MATCH" };
}

// LLM: inputSchema 必须与清单 input_schema 规范化后相等；不要给 MCP 目录另写一份描述。
// 函数用途: 投影单条工具声明。
function toolDescription(tool) {
  return { name: tool.name, description: tool.description, inputSchema: tool.input_schema };
}

// LLM: 超长帧自然关闭输入并用非零退出码结束，不拉起后台进程；诊断只走 stderr。
// 函数用途: 读取并处理一行 JSON，响应只写 stdout，解析失败后仍可继续调用。
function processLine(line) {
  if (line.length > MAX_LINE) {
    process.stderr.write("插件请求超过读取上限。\n");
    process.exitCode = 2;
    lines.close();
    process.stdin.destroy();
    return;
  }
  let request;
  try {
    request = JSON.parse(line);
  } catch {
    process.stdout.write(JSON.stringify(rpcError(null, -32700, "请求不是有效 JSON。")) + "\n");
    return;
  }
  const response = handle(request);
  if (response !== null) process.stdout.write(JSON.stringify(response) + "\n");
}

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
lines.on("line", processLine);
