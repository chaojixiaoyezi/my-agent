# 作者合同：声明不等于权限

以产品 `plugin_manifest.py`、`plugin_events/declarations.py`、`docs/design/PLUGIN_EVENT_HOOKS.md`
为 v8 权威；工具审批仍看 `plugin_runtime.py` / `tooling/mcp_registration.py`，不凭包自述授予权限。

## 清单与目录

- 两模板都是 v8：在 v6 的 `entry/files/platforms` 上增加 `events/tool_gates/permissions`。
  Python 用 `interpreter=python3, command=src/server.py`，Node 用 `interpreter=node, command=src/server.js`，`args=[]`。
  解释器只能写程序名而非本机绝对路径；仅标准库脚本可 `platforms=["any"]`。
  `files` 输入只写 `path/executable`，入口必须在其中；`schema_version/sha256` 由文件构建器生成。
  v8 不带 `entry_module/entry_wheel/wheels`；v1–v5 wheel 不支持订阅。Go 可用 executable 入口和真实平台。
- 只有工具和面板时 v6 已足够；旧 wheel v2–v5 的扩展不等于 v8 事件。v8 至少一条事件或收紧订阅，
  允许没有工具/面板的纯订阅包；第一期不能同时声明 `host_api`，返回 `events_with_host_api_unsupported`。
- `actions[].target` 引用本包工具，`default_action` 引用已声明动作；动作参数必须与 schema 一致。
- 所有语言走 MCP stdio；`tools/list` 使用 `inputSchema`，清单使用 `input_schema`，规范化后必须相等。
  模板读取同一份随包 `declaration.json`，不要维护第二份目录常量。

## effect 和审批

| 真实行为 | `requested_effect` |
| --- | --- |
| 纯计算、只读查询 | `read_only` |
| 改文件、保存状态等明确授权修改 | `mutating` |
| 删除、外部程序、不可逆或高风险动作 | `dangerous` |

具体风险需要按业务重新核对。`requested_effect` 不是执行器的最终 effect，更不是免审批承诺。
当前插件代理调用 `build_proxy_tool` 不下调默认 effect，因此仍为 `dangerous`；审批取宿主 `ToolRuntimePolicy` 的默认规则。
包的工具字段不接受 `approval`，普通 MCP 配置的审批声明也不能直接移植到 plugin_package。
只能保持或收紧默认，不能通过包声明、MCP 自述或修改用户配置降低安全边界。
v8 两语言启用都要求用户本人确认入口/文件/解释器、订阅正文/参数范围、网络及强制沙箱，工具审批不能替代它。

## 观察与工具收紧

| `events[].type` | 合法 `content` |
| --- | --- |
| `prompt_submitted` | `none` 或 `text`（提示文字，统一脱敏后最多 4000 字） |
| `turn_started` / `turn_ended` / `tool_call_started` / `tool_call_finished` / `command_executed` | 只能 `none`，仅结构化事实，无工具参数/输出或命令原文 |

最多六类、类型不重复；不得自造 `events[].tools`。事件是合并投递，不是完整审计日志。
工具参数只能走收紧门：每包最多四门，`id` 唯一、小写字母数字连字符（1–32 位）。
`tools` 最多十六个精确工具名，不支持通配；`effects` 只能 `mutating/dangerous`，两者至少一个非空。
`arguments: full` 必须 `tools` 非空且 `effects: []`；按效果收紧只能 `none`，只给 `args_hash`。
完整参数先去掉 `__` 宿主内部键、统一脱敏再截到 4000 字；收到的数据不是新的宿主能力。

### 参数截断标记 `arguments_truncated`

只有声明 `arguments: full` 的门才会收到这个字段，它是宿主给的结构化事实，不要自己猜：

| `call.arguments_truncated` | 含义 |
| --- | --- |
| `true` | 收到的 `arguments` 被截到 4000 字上限，命令/参数**看不全**，后面还有内容没给你 |
| `false` | 参数完整，`arguments` 是宿主脱敏后的全部内容 |
| 字段不存在 | 该门不是 `arguments: full`（例如 `none` 只给 `args_hash`），本来就没有完整参数 |

**推荐做法**：依赖完整参数才能判断的门，看到 `arguments_truncated` 是 `true` 时**必须回 `ask`**（原因码写成
`ARGUMENTS_TRUNCATED` 一类，消息说明"参数被截断、看不全"，让用户确认）。不要拿残片去匹配危险模式后放行——
别人可以故意在 4000 字之后再写危险内容，把你的门当成白名单绕过；也不要假装截断部分不存在。
字段缺失或 `false` 时照旧按完整参数判断（`false` 就是完整，不必额外保守）。
两个语言模板的 `review_gate`/`reviewGate` 都演示了这个早返回分支，可直接抄。

`initialize` 回复必须声明相应 `capabilities.experimental`：
```json
{"my-agent/events":{"versions":["1"]},"my-agent/tool-gate":{"versions":["1"]}}
```
`my-agent/events.observe` 接 `{ "events": [...] }`，JSON-RPC `result` 只回 `{}`，不把正文写日志或主动推回宿主。
`my-agent/tool-gate.review` 接 `{ "gate_id": "guard-rm", "call": { "tool": "run_command", "arguments": {...} } }`。
只回 `{ "verdict": "ask", "reason_code": "RM_RF", "message": "要删除整个目录，先确认一次" }` 一类结果。
`verdict` 只允许 `allow_as_is/ask/deny`，原因码大写字母数字下划线（1–40 位）；message 可选、宿主净化后最多 80 字。
不能回复新参数或接管调用；宿主合并只取更严决定，`allow_as_is` 不撤销原有 `ask/deny`。
漏握手位、超时、错误或不合规回复时收紧按 `ask`，观察能力缺失则不投递，不能宣称已生效。

## 观察事件字段表（别猜字段名）

写订阅事件的插件时按本表读字段，不要凭印象猜名字。**以实现为准**：公共字段来源
`agent_py_agent/agent/plugin_events/protocol.py`，每类事件的 `facts` 来源
`agent_py_agent/agent/plugin_events/points.py` 的 `_EVENT_FACT_FIELDS`；**B4 定稿后 3a 会再核一次**，
本表与那两处不一致时以代码为准。

### 一条事件的公共字段（9 个）

| 字段 | 含义 |
| --- | --- |
| `event_id` | 本条事件的宿主编号，同一插件内唯一，用来去重与对账 |
| `type` | 事件类型，只能是下表六类之一 |
| `seq` | 该插件视角的单调递增序号；插件可据此判断有没有漏收 |
| `occurred_at` | 事件发生时间（Unix 秒，浮点） |
| `dropped_before` | 这类事件自上次送达后被合并掉几条（只留最新），`0` 表示没丢 |
| `channel` | 渠道名（`tui` / `feishu` / …），空串表示没有渠道信息 |
| `thread_ref` | 会话编号的哈希（**不是原编号**），空串表示无会话 |
| `actor` | 谁发起的：`main`（主模型）/ `subagent`（子代理）/ `decision`（决策模型自动执行）；非法值归为 `main` |
| `facts` | 该类事件自己的结构化字段，见下表；**永远不含工具参数原文与输出** |

### 六类事件各自的 `facts` 字段

| `type` | `facts` 字段 | 每个字段的含义 |
| --- | --- | --- |
| `prompt_submitted` | `request_id`、`chars`、`has_attachments` | 请求编号；提示正文的字符数；这一轮是否带附件 |
| `turn_started` | `request_id`、`model_name` | 请求编号；本轮使用的模型名 |
| `turn_ended` | `request_id`、`status`、`duration_ms`、`tool_calls`、`error_code` | 请求编号；本轮结束状态；耗时毫秒；本轮工具调用次数；结束时的错误码（无错为空串） |
| `tool_call_started` | `call_id`、`tool`、`effect`、`args_hash` | 调用编号；工具名；该工具的 effect；**参数的哈希**（不是参数本身） |
| `tool_call_finished` | `call_id`、`tool`、`ok`、`error_code`、`failure_stage`、`duration_ms`、`handler_executed` | 调用编号；工具名；是否成功；错误码；失败阶段；耗时毫秒；处理器是否真的执行过 |
| `command_executed` | `command`、`operation_id`、`state` | 斜杠命令名（只给命令名，**不含参数**）；操作编号；执行状态 |

### 正文（`content`）

**只有 `prompt_submitted` 可能带正文**，而且三件事都要成立：清单里该事件声明 `content: "text"`、
用户在确认码里同意过、这一轮确实有提示文字。满足时正文经统一脱敏、截到 **4000 字**后才给。
其余五类**一律没有正文**；工具调用只给 `args_hash`，命令只给命令名，工具输出从不外发。
不需要提示文字时把该订阅改成 `content: "none"`，确认码里要用户同意的范围也随之变小。

「向前兼容」：宿主以后可能加新的事件类型或新的 `facts` 字段。**不认识的 `type` 要跳过、不认识的字段要忽略**，
不能让它们在插件里抛异常或当成错误——只处理你声明过、且本表列出的部分。

## 文件、SDK 与授权

只处理传入文本的模板不需要 SDK，也不声明工作区扩展。增加文件功能时：

- 先读 `docs/design/PLUGIN_WORKSPACE_CONTEXT.md` 和 `PLUGIN_WORKSPACE_WRITE.md` 的现行合同。
- Python 先核 SDK 当前接口（`plugins/sdk`；`my-agent-plugin-api`），v8 的依赖必须显式随文件交付，
  不能传 wheel 给 v8 文件构建器；旧 wheel 工程才用 `build_plugin_api.py` 和 `--wheel`，不猜版本或下载依赖。
- Node 参考 `plugins/hello-node/src/workspace_read.js`，跑 `plugins/sdk/conformance/workspace_read_check.json`；
  不声称普通字符串前缀检查是安全路径校验。非 Python 写入暂缺跨语言一致性用例，不直接复制读取检查当写入检查。
- 权限上下文只取本次可信 `_meta`，缺失或非法须拒绝；不要缓存旧轮授权。宿主只读 API 也须明确声明和授权。
- 文件协作合同不是 OS 沙箱；新增风险遵守用户授权、宿主工具审批和目录边界，不能由插件自行扩权。

## 安装启用边界

**B7 前 v8 只能保存安装包，生产启用一律 `plugin_events_disabled`，即使正确码也拒绝。**
B7 启用必须同时守总开关、仅 `local/main`、强制沙箱（不可用拒绝）、默认断网和收窄读；
插件读不到宿主配置、会话、记忆，只能拿到声明且用户确认过的数据。B9 没有实现或验证这些宿主接线。
`permissions.network` 是真布尔、默认 false；联网必须明确声明 true，并进入用户确认码；不能把断网降成普通建议。
安装与启用只由用户在产品界面操作。模型看不到管理工具；也不能经 shell、内部 API、安装表或自动化代操作。
交付统一提示“请用 /plugins install <路径> 安装，启用时按界面提示输确认码”，不要替用户获取或输入确认码。
