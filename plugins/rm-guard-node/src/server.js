// LLM: v8 收紧样例（Node）：只按结构化 gate_id / 工具名 / command、patch 字段裁决；绝不执行命令、不改参数、不放宽宿主。
//   回复只有 allow_as_is / ask / deny 三种，原因码大写；参数缺失宁严按 ask，不认识的组合不 allow。
//   宿主截断标记 arguments_truncated 为 true 时看不全参数，直接 ask；false 或缺失（旧宿主）照旧判。
// 模块用途: 演示 run_command 的 rm 组合收紧与 apply_patch 删除文件段直接拒绝；隔离与合并由宿主 B5/B7 施加，不使用 npm 依赖。
"use strict";

const fs = require("fs");
const path = require("path");
const readline = require("readline");
const declaration = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "declaration.json"), "utf8"));
const MAX_LINE = 131072;

// LLM: 与 Python 样例同一判定：命令里出现独立 rm 词，且选项里同时具备递归和强制；不是完整 shell 解析器。
//   覆盖 -rf、-fr、-r -f、--recursive --force 及等价混合写法；宁可多问一次，不放过删除整目录。
// 函数用途: 判断命令是否属于"rm 加 -r 和 -f"的删除整目录写法。
function deletesTree(command) {
  if (!/\brm\b/u.test(command)) return false;
  const shorts = [...command.matchAll(/(?<![\w-])-([A-Za-z]+)(?![\w-])/gu)].map(item => item[1].toLowerCase());
  const longs = [...command.matchAll(/(?<![\w-])--([A-Za-z][A-Za-z-]*)(?![\w-])/gu)].map(item => item[1]);
  const recursive = shorts.some(item => item.includes("r")) || longs.includes("recursive");
  const force = shorts.some(item => item.includes("f")) || longs.includes("force");
  return recursive && force;
}

// LLM: 与宿主 apply_patch 的解析同款：行首严格是 `*** Delete File: `（冒号后一个空格）才算删除段，
//   正文行里的同样字样（如加号行）不算；先归一换行再按行切分，不做子串匹配。
// 函数用途: 取出补丁里全部删除段头的路径（去掉行首空白后可能是空串，供调用方区分"删除但没写路径"）。
function deletePathsInPatch(patch) {
  return patch.replace(/\r\n?/gu, "\n").split("\n")
    .filter(line => line.startsWith("*** Delete File: "))
    .map(line => line.slice("*** Delete File: ".length).trim());
}

// LLM: 补丁删除门只读补丁文本的删除段头：有带路径的删除段就 deny；只有空路径删除段按参数不可用 ask。
//   不解析正文、不执行补丁，宁严勿松。
// 函数用途: 裁决一次 apply_patch 补丁的删除征询，返回三种裁决之一。
function reviewDeleteGate(call) {
  const args = call.arguments;
  const patch = args && typeof args === "object" && !Array.isArray(args) ? args.patch : null;
  if (typeof patch !== "string") {
    return { verdict: "ask", reason_code: "ARGUMENTS_UNAVAILABLE", message: "看不到补丁内容，先确认一次" };
  }
  const paths = deletePathsInPatch(patch);
  if (paths.some(item => item.length > 0)) {
    return { verdict: "deny", reason_code: "DELETE_FILE_BLOCKED", message: "补丁要删除文件，拒绝" };
  }
  if (paths.length > 0) {
    return { verdict: "ask", reason_code: "ARGUMENTS_UNAVAILABLE", message: "删除段没有写路径，先确认一次" };
  }
  return { verdict: "allow_as_is", reason_code: "NO_MATCH" };
}

// LLM: 裁决只依据清单声明的门与结构化调用事实；不认识的组合一律 deny 或 ask，绝不 allow。
//   参数缺失时看不到完整命令，按"要求确认"处理（宁严勿松）。
//   截断标记为 true 时先于具体门判定直接 ask，不按看到的片段判（防截断处误 allow 或误 deny）。
// 函数用途: 处理一次收紧征询，返回三种裁决之一。
function reviewGate(params) {
  const gate = declaration.tool_gates.find(item => item.id === params.gate_id);
  const call = params.call && typeof params.call === "object" && !Array.isArray(params.call) ? params.call : {};
  if (!gate || !gate.tools.includes(call.tool)) return { verdict: "deny", reason_code: "OUT_OF_SCOPE" };
  if (call.arguments_truncated === true) {
    return { verdict: "ask", reason_code: "ARGUMENTS_TRUNCATED", message: "参数太长被截断，看不全，先确认一次" };
  }
  if (gate.id === "guard-delete") return reviewDeleteGate(call);
  const args = call.arguments;
  if (!args || typeof args !== "object" || Array.isArray(args) || typeof args.command !== "string") {
    return { verdict: "ask", reason_code: "ARGUMENTS_UNAVAILABLE", message: "看不到完整命令，先确认一次" };
  }
  if (deletesTree(args.command)) {
    return { verdict: "ask", reason_code: "RM_RF", message: "要删除整个目录，先确认一次" };
  }
  return { verdict: "allow_as_is", reason_code: "NO_MATCH" };
}

// LLM: 协议错误只含固定中文说明，不回显输入；JSON-RPC 分类与业务裁决分开。
// 函数用途: 构造 JSON-RPC 错误帧。
function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

// LLM: 握手声明收紧能力位；清单声明了而握手没声明时宿主按 ask 处理（宁严），所以这里必须与方法同步。
//   通知不回复；未知方法返回 -32601；任何请求都不执行、不改参数。
// 函数用途: 路由标准 MCP 与 v8 收紧征询，不产生宿主操作。
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
                 "my-agent/tool-gate": { versions: ["1"] } } },
               serverInfo: { name: declaration.plugin_id, version: declaration.version } };
  } else if (request.method === "ping") {
    result = {};
  } else if (request.method === "tools/list") {
    result = { tools: [] };
  } else if (request.method === "my-agent/tool-gate.review") {
    result = reviewGate(params);
  } else {
    return rpcError(request.id, -32601, "不支持此方法。");
  }
  return { jsonrpc: "2.0", id: request.id, result };
}

// LLM: 超长帧自然关闭输入并用非零退出码结束，不拉起后台进程；诊断只走 stderr。
// 函数用途: 读取并处理一行 JSON，响应只写 stdout，解析失败后仍可继续调用。
function processLine(line) {
  if (line.length > MAX_LINE) {
    process.stderr.write("插件请求超过读取上限。\n");
    process.exitCode = 2;
    lines.close();
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
