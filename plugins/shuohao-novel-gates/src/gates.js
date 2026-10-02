"use strict";
// LLM: 固定导入五个原样上游 ESM，机器读取 gateReport/validate 返回数组，不解析 CLI 自然语言。
//   不调用 CLI main、logGates、loadRecipes 或任何写入口；storyboard 因此等价于 --no-log，不产生 .gates.jsonl。
// 模块用途: 把各阶段只读校验、门报告和已有门日志统计转换成 MCP 结构化回执。
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { readJson, readText } = require("./workspace_files.js");
const { ToolError } = require("./errors.js");
const { checkCast } = require("./cast.js");
const STAGES = ["outline", "art", "script", "storyboard", "characters"];

// LLM: 模块名只取固定内部阶段集合，绝不来自工具路径或文档内容；导入不会进入各模块的 CLI main。
// 函数用途: 装入原上游只读计算函数，供服务启动时调用。
async function loadModules() {
  const entries = await Promise.all(STAGES.map(async (stage) => {
    const source = path.join(__dirname, "..", "upstream", "skills", `novel-${stage}`, "scripts", `novel-${stage}.mjs`);
    return [stage, await import(pathToFileURL(source).href)];
  }));
  return Object.fromEntries(entries);
}

// LLM: 不用上游 loadCtx 的直接 fs 读取；每份参考资料与每张卡片单独过宿主权限门。
//   空数组 shots 等于没给：不挂载配方库，shot-recipe 门走跳过。
// 函数用途: 为剧本、分镜等跨阶段对账构造内存参考对象。
function references(context, args, modules) {
  const result = {};
  for (const key of ["outline", "cast", "art", "script"]) {
    if (args[key]) result[key] = readJson(context, args[key]);
  }
  if (args.shots && args.shots.length > 0) {
    const cards = args.shots.map((file) => modules.storyboard.parseCardFields(readText(context, file))).filter(Boolean);
    result.recipes = new Map(cards.map((card) => [card.id, card]));
  }
  return result;
}

// LLM: 缺依赖的门必须按结构化依赖标 skipped；不得解析上游 detail 里的“跳过”文字或把原 ok=true 当完整验过。
//   按数据内容跳过的门也一样：cast 没名字（no-names）、大纲没有 props 字段（prop-cap）、没挂配方库（shot-recipe）。
// 函数用途: 给未实际检查的门标出原因，保留稳定门编号。
function skippedGates(stage, refs, names, document) {
  const skipped = new Map();
  if (stage === "outline" && !Array.isArray(document?.props)) skipped.set("prop-cap", "大纲没有 props 字段");
  if (stage === "art" && (!refs.cast || names.length === 0)) skipped.set("no-names", "未提供 cast 或 cast 没有名字");
  if (stage === "script" && !refs.outline) skipped.set("beats-claimed", "未提供 outline");
  if (stage === "script" && !refs.outline) skipped.set("refs-characters", "未提供 outline");
  if (stage === "script" && !refs.art) skipped.set("refs-scenes", "未提供 art");
  if (stage === "storyboard" && !refs.recipes) skipped.set("shot-recipe", "未挂载镜头配方库");
  if (stage === "storyboard" && !refs.outline && !refs.cast) skipped.set("prompt-no-names", "未提供 outline/cast");
  return skipped;
}

// LLM: 原始 ok 只作 upstream_ok 留存；公开 passed/status 不把跳过算通过。只处理原函数返回字段。
// 函数用途: 归一化门编号、判定与统计，不改变上游检查规则。
function summarizeGates(raw, skipped) {
  const gates = raw.map((gate) => ({
    id: gate.id, label: gate.label, detail: gate.detail, upstream_ok: gate.ok,
    passed: skipped.has(gate.id) ? null : gate.ok,
    status: skipped.has(gate.id) ? "skipped" : gate.ok ? "passed" : "failed",
    reason: skipped.get(gate.id) || "",
  }));
  const counts = { total: gates.length, passed: 0, failed: 0, skipped: 0 };
  for (const gate of gates) counts[gate.status] += 1;
  return { gates, counts, complete: counts.skipped === 0 };
}

// LLM: 原验证函数只消费内存对象；cast 的顶层检查在 checkCast，保持 CLI 校验语义。
// 函数用途: 根据阶段获取结构违规列表。
function problemsOf(module, stage, document, options) {
  const validators = { outline: "validateOutline", art: "validateArt", script: "validateScript", storyboard: "validateStoryboard" };
  return module[validators[stage]](document, options);
}

// LLM: stats 是读取已有 JSONL 门日志，不是分镜资产统计；坏行沿上游 CLI 口径忽略，同时明确返回忽略数。
// 函数用途: 返回门运行次数、失败门排名与未响过的门，全程不追加日志。
function logStats(module, context, args) {
  const lines = readText(context, args.path).split(/\r?\n/).filter(Boolean);
  const entries = [];
  let ignored = 0;
  for (const line of lines) {
    try { entries.push(JSON.parse(line)); } catch { ignored += 1; }
  }
  const ids = module.gateReport({ episodes: [] }, {}).map((gate) => gate.id);
  return { stage: "storyboard", operation: "stats", passed: null, ignored_lines: ignored,
    stats: module.summarizeGateLog(entries, ids) };
}

// LLM: 所有文件已按本次上下文读取；只调用内存函数，无子进程、网络、日志或其它写副作用。
// 函数用途: 执行某阶段的只读工具，区分统计、结构校验和质量门。
function runCheck(modules, stage, context, args) {
  const operation = args.operation || (stage === "characters" ? "validate" : "checkup");
  const module = modules[stage];
  if (operation === "stats") return logStats(module, context, args);
  if (stage === "storyboard" && !args.script) throw new ToolError("INVALID_ARGUMENTS", "分镜校验必须提供 script。");
  const document = readJson(context, args.path);
  if (stage === "characters") {
    const cast = checkCast(module, document, readText(context, args.book), args);
    return { stage, operation, ...cast, passed: cast.problems.length === 0, complete: true, gates: [],
      counts: { total: 0, passed: 0, failed: 0, skipped: 0, problems: cast.problems.length } };
  }
  const refs = references(context, args, modules);
  const names = stage === "art" && refs.cast ? module.castNamesOf(refs.cast) : [];
  const options = stage === "outline" ? args.stage || "full" : stage === "art" ? (names.length ? names : null) : refs;
  const raw = stage === "outline" ? module.gateReport(document) : module.gateReport(document, options);
  const result = summarizeGates(raw, skippedGates(stage, refs, names, document));
  const problems = operation === "validate" ? problemsOf(module, stage, document, options) : [];
  return { stage, operation, ...result, problems, passed: result.counts.failed === 0 && problems.length === 0 };
}
module.exports = { loadModules, runCheck };
