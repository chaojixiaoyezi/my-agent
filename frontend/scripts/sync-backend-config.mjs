// LLM: YAML 是键和值、说明的来源，生效时机直接读取后端登记表；生成只读投影，不碰用户配置或启动服务。
// 模块用途: 重生前端参数目录，避免前后端各维护一份马上生效的开关名单。
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const frontendRoot = resolve(scriptDir, "..");
const repoRoot = resolve(frontendRoot, "..");
const outputPath = resolve(frontendRoot, "config/backend-config-catalog.json");

// LLM: 使用仓库根下的后端 parameter_registry 读 effect；仅随包默认与元数据，不读取 owner 或凭据。
//   PYTHON 可指定项目解释器，默认 python3（Windows 为 python）；失败即终止生成，不回退写死时机。
// 函数用途: 给生成器提供后端的重启事实，名单只留在后端现读入口。
function backendRestartRequirements() {
  const script = [
    "import json",
    "from agent_py_agent.agent.settings.parameter_registry import parameter_registry",
    "from agent_py_agent.agent.settings.user_config_capability import EFFECT_GATEWAY_RESTART",
    "print(json.dumps({key: spec.effect == EFFECT_GATEWAY_RESTART for key, spec in parameter_registry().items()}))",
  ].join("\n");
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  return JSON.parse(execFileSync(python, ["-c", script], {
    cwd: repoRoot, encoding: "utf8", env: { ...process.env, PYTHONPATH: repoRoot, PYTHONDONTWRITEBYTECODE: "1" },
  }));
}

const restartRequirements = backendRestartRequirements();

const sources = [
  ["agent_config", "agent_config.yaml", "agent_py_agent/config/agent_config.yaml"],
  ["capability_config", "capability_config.yaml", "agent_py_agent/config/capability_config.yaml"],
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
};

const choiceMap = {
  model_backend: ["echo", "openai_compatible", "anthropic_compatible"],
  memory_rule_routing_mode: ["off", "soft", "strict"],
  memory_resume_auto_context_mode: ["off", "trigger", "always"],
  runner_concurrency: ["auto"],
  runner_start_rate: ["auto"],
  runner_timeout_seconds: ["off", "auto"],
  daemon_max_runners: ["auto"],
  daemon_reviewer: ["parent-daemon"],
  log_level: ["debug", "info", "warning", "error", "critical"],
  tool_catalog_mode: ["compact", "full", "retrieval_only", "off"],
  response_mode: ["recommend", "dry_run", "execute"],
  local_store_backend: ["jsonl", "sqlite", "duckdb", "parquet"],
  capability_level: ["L0", "L1", "L2", "L3", "L4", "L5"],
};

// 按键名给出的明确取值范围，优先于 rangeForField 的名字猜测：后端没有上限、名字又会被猜成 [0, 200000] 的数字键放这里。
// 压缩触发线的绝对上限要能填到 1M 以上窗口里的几十万（后端只要求 >= 0，0 表示不封顶）。
const rangeMap = {
  memory_compact_auto_trigger_max_tokens: [0, 10_000_000],
};

const unitMap = {
  max_tokens: "tokens",
  memory_compact_auto_trigger_max_tokens: "tokens",
  request_timeout: "秒",
  tool_http_timeout: "秒",
  tool_shell_timeout: "秒",
  gateway_stale_seconds: "秒",
  gateway_stop_timeout: "秒",
  gateway_request_timeout: "秒",
  gateway_processing_timeout_seconds: "秒",
  lease_stale_without_heartbeat_seconds: "秒",
  subagent_run_timeout: "秒",
  subagent_heartbeat_timeout: "秒",
  dynamic_timeout_min: "秒",
  dynamic_timeout_max: "秒",
  cli_audit_cleanup_days: "天",
};

// LLM: 键的描述取该键正上方连续 `#` 注释（与后端 parameter_registry._descriptions_from_lines 同一规则：空行或任何
//   非注释行都会中断注释块，注释块只归紧挨着的下一个键；第一个键前以空行结尾的文件头注释不属于任何键，被丢弃）。
//   没有上方注释时取该行行尾注释（引号里的 # 不算，见 trailingComment），与后端 yaml_trailing_comment 同一规则。
//   后端是权威（改归属规则要两边一起核对，并逐字段比对重新生成的目录）；同名键只保留第一次出现。
// 函数用途: 按项目的极简 YAML 子集读出每个键的值和上方的中文注释，供生成设置页目录。
function parseSimpleYaml(text) {
  const entries = [];
  const lines = text.split(/\r?\n/);
  let comments = [];
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const trimmed = line.trim();
    if (!trimmed) {
      comments = [];
      continue;
    }
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
    const above = comments.join(" ");
    const fallback = trailingComment(line);
    entries.push({ key, value, comments: above ? comments : (fallback ? [fallback] : []) });
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

// LLM: 与后端 config_io.yaml_trailing_comment 同一规则：取引号外的第一个 `#`（前面有空白）之后的文本，作为该行说明；
//   没有行尾注释返回空串。只读，不参与值解析（值解析用 stripInlineComment 去掉同一段）。
// 函数用途: 取一行 YAML 的行尾注释文字，供键上方没有注释块时兜底使用。
function trailingComment(line) {
  let quote = "";
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if ((char === '"' || char === "'") && line[index - 1] !== "\\") {
      quote = quote === char ? "" : quote || char;
    }
    if (char === "#" && !quote && /\s/.test(line[index - 1] || "")) {
      return line.slice(index + 1).trim();
    }
  }
  return "";
}

function inferCategory(sourceId, key) {
  if (sourceId === "capability_config") return "capability";
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
    field.max = typeof entry.value === "number" ? Math.max(range[1], entry.value) : range[1];
  }
  return field;
}

function isAdvanced(key, category) {
  return (
    category === "capability" ||
    key.includes("debug") ||
    key.includes("timeout") ||
    key.includes("limit") ||
    key.includes("max_") ||
    key.includes("path") ||
    key.includes("dir") ||
    key.includes("workspace")
  );
}

// LLM: 只消费后端登记的结构化 effect，不按键名或中文说明猜；未登记的 YAML 键视为合同错误，不能静默生目录。
// 函数用途: 返回某项是否需要重启，现读开关显示马上生效，其余项保持重启。
function restartRequired(key) {
  if (!Object.hasOwn(restartRequirements, key)) throw new Error(`Backend parameter is not registered: ${key}`);
  return restartRequirements[key];
}

function requiresUnlock(key, category) {
  if (category === "subagent") return !["enable_subagents", "subagent_board_limit"].includes(key);
  if (category === "capability") return true;
  return false;
}

// LLM: 风险按结构化键名与类别声明，不从中文说明判断；记忆档案改变内容发送目的地，需与后端边界登记同样标为高风险。
// 函数用途: 给用户配置目录标注敏感字段风险，不授予模型修改权限。
function riskForField(key, category, type) {
  if (key === "memory_curator_model_profile") {
    return { level: "high", warning: "决定把记忆内容发给哪个服务商；仅用户经 /settings 修改，模型不能改。" };
  }
  if (type === "secret") return { level: "high", warning: "密钥或凭证字段，保存前确认不要写入真实公开仓库。" };
  if (key === "api_base" || key === "workspace_root" || key === "extensions_dir") {
    return { level: "high", warning: "会改变运行边界或加载来源，建议只在明确知道影响时修改。" };
  }
  if (requiresUnlock(key, category)) {
    return { level: "medium", warning: "默认保护字段，修改后可能影响子代理或能力路由行为。" };
  }
  if (category === "tools" && (key.includes("write") || key.includes("shell") || key.includes("web"))) {
    return { level: "medium", warning: "放宽工具边界会影响模型调用成本和执行风险。" };
  }
  return { level: "low", warning: "" };
}

function rangeForField(key) {
  if (rangeMap[key]) return rangeMap[key];
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
        source: "agent_config.yaml + capability_config.yaml",
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
