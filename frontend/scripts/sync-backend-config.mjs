import { readFileSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const frontendRoot = resolve(scriptDir, "..");
const repoRoot = resolve(frontendRoot, "..");
const outputPath = resolve(frontendRoot, "config/backend-config-catalog.json");

const sources = [
  ["agent_config", "agent_config.yaml", "agent_py_agent/config/agent_config.yaml"],
  ["capability_config", "capability_config.yaml", "agent_py_agent/config/capability_config.yaml"],
  ["log_analysis_config", "log_analysis_config.yaml", "agent_py_agent/config/log_analysis_config.yaml"],
].map(([id, label, path]) => ({ id, label, path: resolve(repoRoot, path) }));

const categoryMeta = {
  basic: ["基础信息", "Agent 名称、工作区、启动行为等基础配置", 1],
  model: ["模型配置", "模型后端、API、采样和流式参数", 2],
  tools: ["工具配置", "工具调用、读写、网络、目录和检索限制", 3],
  memory: ["记忆配置", "记忆、归档、compact、resume 和本地事实源", 4],
  subagent: ["子代理配置", "子代理数量、模板、工作流、层级和验收策略", 5],
  gateway: ["Gateway / 调度", "Gateway、dispatch、runner、daemon 和 lease", 6],
  adapters: ["适配器", "飞书、QQ、外部通道和会话目录", 7],
  security: ["安全 / 审计", "权限、审计、通知、并发锁和 watchdog", 8],
  cli: ["CLI / 展示", "命令行默认 limit、预览和折叠配置", 9],
  capability: ["能力路由", "skill、tool、MCP、授权和能力上抛配置", 10],
  log_analysis: ["日志分析", "安全日志分析模块独立配置", 11],
};

const choiceMap = {
  model_backend: ["echo", "openai_compatible", "anthropic_compatible"],
  memory_rule_routing_mode: ["off", "soft", "strict"],
  memory_resume_auto_context_mode: ["off", "trigger", "always"],
  scheduler_mode: ["auto", "manual", "off"],
  runner_concurrency: ["auto"],
  runner_start_rate: ["auto"],
  runner_timeout_seconds: ["off", "auto"],
  runner_failure_policy: ["auto", "off", "none", "disabled"],
  daemon_max_runners: ["auto"],
  daemon_reviewer: ["parent-daemon"],
  log_level: ["debug", "info", "warning", "error", "critical"],
  subagent_workflow_mode: ["auto", "manual", "off"],
  tool_catalog_mode: ["compact", "full", "retrieval_only", "off"],
  response_mode: ["recommend", "dry_run", "execute"],
  local_store_backend: ["jsonl", "sqlite", "duckdb", "parquet"],
  capability_level: ["L0", "L1", "L2", "L3", "L4", "L5"],
};

const unitMap = {
  max_tokens: "tokens",
  request_timeout: "秒",
  tool_http_timeout: "秒",
  tool_shell_timeout: "秒",
  tool_agent_budget_window_seconds: "秒",
  tool_artifact_read_budget_window_seconds: "秒",
  gateway_heartbeat_interval: "秒",
  gateway_stale_seconds: "秒",
  gateway_stop_timeout: "秒",
  gateway_request_timeout: "秒",
  gateway_processing_timeout_seconds: "秒",
  lease_heartbeat_interval_seconds: "秒",
  lease_stale_without_heartbeat_seconds: "秒",
  subagent_run_timeout: "秒",
  subagent_heartbeat_timeout: "秒",
  subagent_due_check_interval: "秒",
  dynamic_timeout_min: "秒",
  dynamic_timeout_max: "秒",
  dynamic_timeout_safety_margin: "倍",
  memory_hook_retention_days: "天",
  cli_audit_cleanup_days: "天",
};

function parseSimpleYaml(text) {
  const entries = [];
  const lines = text.split(/\r?\n/);
  let comments = [];
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (trimmed.startsWith("#")) {
      comments.push(trimmed.replace(/^#\s?/, "").trim());
      continue;
    }
    const match = /^([A-Za-z_][A-Za-z0-9_]*):(?:\s*(.*))?$/.exec(line);
    if (!match) {
      comments = [];
      continue;
    }
    const key = match[1];
    const raw = (match[2] ?? "").trim();
    let value;
    if (raw === "") {
      const list = [];
      let cursor = index + 1;
      while (cursor < lines.length) {
        const listMatch = /^\s*-\s*(.*)$/.exec(lines[cursor]);
        if (!listMatch) break;
        list.push(parseScalar(listMatch[1].trim()));
        cursor += 1;
      }
      value = list.length ? list : "";
      if (list.length) index = cursor - 1;
    } else {
      value = parseScalar(raw);
    }
    entries.push({ key, value, comments: comments.slice(-8) });
    comments = [];
  }
  return entries;
}

function parseScalar(raw) {
  const text = stripInlineComment(raw.trim());
  if (text === "[]") return [];
  if (text === "{}") return {};
  if (text === "true") return true;
  if (text === "false") return false;
  if (text === "null") return null;
  if (/^-?\d+$/.test(text)) return Number.parseInt(text, 10);
  if (/^-?\d+\.\d+$/.test(text)) return Number.parseFloat(text);
  if ((text.startsWith('"') && text.endsWith('"')) || (text.startsWith("'") && text.endsWith("'"))) {
    return text.slice(1, -1);
  }
  if (text.startsWith("[") && text.endsWith("]")) {
    const inner = text.slice(1, -1).trim();
    return inner ? inner.split(",").map((item) => parseScalar(item.trim())) : [];
  }
  return text;
}

function stripInlineComment(raw) {
  let quote = "";
  for (let index = 0; index < raw.length; index += 1) {
    const char = raw[index];
    if ((char === '"' || char === "'") && raw[index - 1] !== "\\") {
      quote = quote === char ? "" : quote || char;
    }
    if (char === "#" && !quote && /\s/.test(raw[index - 1] || "")) {
      return raw.slice(0, index).trim();
    }
  }
  return raw;
}

function inferCategory(sourceId, key) {
  if (sourceId === "capability_config") return "capability";
  if (sourceId === "log_analysis_config") return "log_analysis";
  if (key.startsWith("memory_") || key.startsWith("local_store_") || key === "auto_save_memory") return "memory";
  if (
    key.startsWith("subagent_") ||
    key.startsWith("task_max_") ||
    key.startsWith("acceptance_") ||
    key.startsWith("max_auto_") ||
    key === "enable_subagents"
  ) return "subagent";
  if (
    key.startsWith("gateway_") ||
    key.startsWith("dispatch_") ||
    key.startsWith("daemon_") ||
    key.startsWith("runner_") ||
    key.startsWith("dynamic_timeout_") ||
    key.startsWith("scheduler_") ||
    key.startsWith("lease_")
  ) return "gateway";
  if (key.startsWith("tool_") || key === "enable_tools" || key === "max_tool_rounds" || key === "stream_enabled") return "tools";
  if (key.startsWith("chat_") || key.startsWith("cli_")) return "cli";
  if (key.startsWith("adapter_") || key.startsWith("feishu_") || key.startsWith("qq_") || key.startsWith("session_")) return "adapters";
  if (
    key.startsWith("auth_") ||
    key.startsWith("audit_") ||
    key.startsWith("notification_") ||
    key.startsWith("concurrency_") ||
    key.startsWith("watchdog_") ||
    key.startsWith("user_") ||
    key === "admin_user_id"
  ) return "security";
  if (
    [
      "model_backend",
      "api_base",
      "api_key",
      "api_key_env",
      "model_name",
      "request_timeout",
      "max_tokens",
      "temperature",
      "anthropic_version",
      "model_speed_profile_path",
      "auto_bench_model_on_first_use",
    ].includes(key)
  ) return "model";
  return "basic";
}

function inferType(key, value) {
  if (key.includes("api_key") || key.endsWith("_secret") || key.endsWith("_token") || key.endsWith("_encrypt_key")) return "secret";
  if (typeof value === "boolean") return "boolean";
  if (typeof value === "number") return "number";
  if (Array.isArray(value)) return "string_list";
  if (choiceMap[key]) return "choice";
  if (key.endsWith("_path") || key.endsWith("_dir") || key.endsWith("_dirs") || key.endsWith("_workspace") || key.includes("root")) return "path";
  if (key.includes("prompt") || key.includes("instruction")) return "text";
  return "string";
}

function fieldMeta(source, entry, order) {
  const category = inferCategory(source.id, entry.key);
  const type = inferType(entry.key, entry.value);
  const risk = riskForField(entry.key, category, type);
  const field = {
    key: entry.key,
    label: `${entry.key}（${entry.key.replace(/_/g, " ")}）`,
    description: entry.comments.join(" ") || `${source.label} 中的 ${entry.key} 配置项。`,
    type,
    defaultValue: entry.value,
    currentValue: entry.value,
    category,
    categoryLabel: categoryMeta[category][0],
    advanced: isAdvanced(entry.key, category),
    restartRequired: restartRequired(entry.key),
    riskLevel: risk.level,
    riskWarning: risk.warning,
    requiresUnlock: requiresUnlock(entry.key, category),
    source: source.label,
    order,
  };
  if (choiceMap[entry.key]) field.choices = choiceChoices(entry.key, entry.value);
  if (unitMap[entry.key]) field.unit = unitMap[entry.key];
  const range = rangeForField(entry.key);
  if (range) {
    field.min = range[0];
    field.max = range[1];
  }
  return field;
}

function isAdvanced(key, category) {
  return (
    category === "capability" ||
    category === "log_analysis" ||
    key.includes("debug") ||
    key.includes("timeout") ||
    key.includes("limit") ||
    key.includes("max_") ||
    key.includes("path") ||
    key.includes("dir") ||
    key.includes("workspace")
  );
}

function restartRequired(key) {
  return key.includes("workspace") || key.includes("root") || key.includes("backend") || key === "gateway_port" || key === "extensions_dir";
}

function requiresUnlock(key, category) {
  if (category === "subagent") return !["enable_subagents", "subagent_board_limit"].includes(key);
  if (category === "capability") return true;
  if (category === "log_analysis") return key.endsWith("_enabled") || key.includes("execution") || key.includes("dispatch");
  return false;
}

function riskForField(key, category, type) {
  if (type === "secret") return { level: "high", warning: "密钥或凭证字段，保存前确认不要写入真实公开仓库。" };
  if (key === "api_base" || key === "workspace_root" || key === "extensions_dir") {
    return { level: "high", warning: "会改变运行边界或加载来源，建议只在明确知道影响时修改。" };
  }
  if (requiresUnlock(key, category)) {
    return { level: "medium", warning: "默认保护字段，修改后可能影响子代理、能力路由或安全日志分析行为。" };
  }
  if (category === "tools" && (key.includes("write") || key.includes("shell") || key.includes("web"))) {
    return { level: "medium", warning: "放宽工具边界会影响模型调用成本和执行风险。" };
  }
  return { level: "low", warning: "" };
}

function rangeForField(key) {
  if (key.includes("port")) return [0, 65535];
  if (key.includes("temperature")) return [0, 2];
  if (key.includes("timeout") || key.endsWith("_seconds")) return [0, 3600];
  if (key.includes("chars")) return [0, 2_000_000];
  if (key.includes("tokens")) return [0, 200_000];
  if (key.includes("limit") || key.includes("max_") || key.includes("count")) return [0, 1_000_000];
  if (key.includes("days")) return [0, 3650];
  return null;
}

function choiceChoices(key, value) {
  return Array.from(new Set([...(choiceMap[key] || []), String(value)]));
}

function buildCatalog() {
  const fields = [];
  const sourceSummaries = [];
  for (const source of sources) {
    const entries = parseSimpleYaml(readFileSync(source.path, "utf8"));
    sourceSummaries.push({ id: source.id, path: relative(repoRoot, source.path), fieldCount: entries.length });
    entries.forEach((entry, index) => fields.push(fieldMeta(source, entry, index + 1)));
  }
  const categories = Object.entries(categoryMeta)
    .map(([key, [label, description, order]]) => ({
      key,
      label,
      description,
      order,
      fields: fields.filter((field) => field.category === key),
    }))
    .filter((category) => category.fields.length > 0);
  return {
    version: "backend-config-catalog-v1",
    generatedFrom: sourceSummaries,
    schema: {
      version: "backend-config-schema-v1",
      categories,
      meta: {
        lastUpdated: "generated-from-backend-config",
        source: "agent_config.yaml + capability_config.yaml + log_analysis_config.yaml",
        editable: true,
        exportFormats: ["yaml", "json"],
      },
    },
  };
}

const catalog = buildCatalog();
const rendered = `${JSON.stringify(catalog, null, 2)}\n`;

if (process.argv.includes("--check")) {
  const current = readFileSync(outputPath, "utf8");
  if (current !== rendered) {
    console.error("backend-config-catalog.json is stale. Run npm run sync:config.");
    process.exit(1);
  }
  console.log(`Config catalog is in sync (${catalog.schema.categories.reduce((sum, cat) => sum + cat.fields.length, 0)} fields).`);
} else {
  writeFileSync(outputPath, rendered, "utf8");
  console.log(`Wrote ${relative(repoRoot, outputPath)} with ${catalog.schema.categories.reduce((sum, cat) => sum + cat.fields.length, 0)} fields.`);
}
