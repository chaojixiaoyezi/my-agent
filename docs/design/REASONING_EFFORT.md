# 智能程度（推理强度）

状态：已合入 main `7a15c9c91` 并双机部署（运行时 `step12k-e5b2f8bc`，2026-09-26），隔离真实验收通过（结果见第 7 节与 TESTS.md 顶部）。第 8 节“自动检测是否支持调节”已合入 main（2026-09-27，随 step13t 部署），真实服务商验收待做。本文是“智能程度”的唯一模块设计；`DESIGN_LEDGER.md` 只保留摘要和链接。

C7 八档扩展已在 `worker/sol-effort-levels` 本地实施，尚未集成/部署（验收记录日期 2026-10-02）；真实模型由 3a 集成后核对，不能沿用上述旧版真实结果证明新档已生效。新增合同见第 9 节。

2026-10-02 小输出上限预算修复已在 `worker/sol2-anthropic-budget` 本地实施，基于集成线 `b35796a60`，待集成/真实入口核对。它修复的是 C7 之前已有的空预算区间，不新增档位、开关或协议字段，合同见第 10 节。

2026-10-02 C7 计量与说法修正已在 `worker/ds2-effort-probe-metering` 本地实施，基于集成线 `claude/3a-step16z`（b5be542e8），待集成。两处修正：探测调用计入用量账（独立用途标签 `probe:tool_capability`），自动检测加输出上限并把回执说法改成由结构化事实算出，合同见第 11 节。

## 1. 背景

用户要求：能设置模型的智能程度，包括给子代理单独设置，并实测。

原状：`/effort [low|medium|high|max|auto]` 只是空壳。它读后端的 `supported_effort_levels`，但没有任何后端声明这个属性，所以回执永远是“未改变任何模型参数”。请求体里只有强制调用工具时才会发 `thinking: disabled`，从不发推理强度或思考预算。

## 2. 实测：各服务商到底认什么（2026-09-26）

用产品后端组包，只在进程内给请求体合入待测字段，不改代码和配置。题目：1 到 2000 中既是 3 的倍数、各位数字之和又能被 7 整除的整数个数（答案 64）。统计推理 token 或输出 token，以及思考正文长度：

| 接口 | 默认 | 关闭思考 | 调强度 | 结论 |
|---|---|---|---|---|
| DeepSeek 官方 · OpenAI 兼容（v4-flash） | 推理 1925 | 生效（0 思考） | `reasoning_effort: low` → 919；`max` → 1887 | 档位生效 |
| DeepSeek 官方 · Anthropic 兼容 | 输出 1249 | 生效（0 思考） | `output_config.effort`、`reasoning.effort`、`thinking.budget_tokens` 都没有减少 | 只能开 / 关 |
| OpenCode Go 中转（deepseek-v4-flash） | 输出约 1100–1200 | 不生效 | `reasoning_effort: low` 反而 2234 / 2664 | 不支持 |
| MiniMax M2.7（Anthropic 兼容） | 输出 5620 | 不生效（仍思考） | 预算 1024 仍输出 4714；`output_config.effort` 不生效 | 总是深度思考 |
| MiniMax M3（Anthropic 兼容） | 默认不思考 | —— | `thinking.enabled` 后才思考；预算不被严格执行 | 只能开 / 关 |

结论：同一个参数在不同服务商上的效果差别很大，“请求被接受”不等于“生效”。所以控制方式必须按模型如实登记，不能按协议一刀切。

### 2026-09-30 复测（生产目录每个对话模型，产品后端，隔离副本）

同一道题、同一判定口径（`reasoning_probe_judge`），每个档位 3 次（部分同接口的其它模型只跑 1 次看是否被拒），按轮交替。
请求走 `selected_model_config` + `get_backend` + `provider_runtime_scope`，档位经产品的 `reasoning_request_values` 换算；
只记实际发出的推理字段、token 数、思考块字数和答案对不对，不存正文。证据 `~/.my-agent/decision-evidence/reasoning-effort-audit-20260930/`。

| 接口 · 模型 | 字段被拒？ | 关闭思考 | low → max（中位） | 结论 |
|---|---|---|---|---|
| DeepSeek 官方 OpenAI 兼容 · v4-flash | 没有 | 生效（0 思考） | 推理 1129 → 1424，默认 1308，两组重叠 | 这道题上差别小；v4-pro 单次 1522 → 10292 |
| DeepSeek 官方 Anthropic 兼容 · v4-flash | 没有（effort、budget 都接受） | 生效 | effort 输出 1163 → 1524，budget 1099 → 1439，重叠 | 开 / 关确定；v4-pro 的 effort 单次 1493 → 10038 |
| opencode Go · deepseek-v4-flash | 没有 | 不生效（仍在思考） | 输出 1711 → 1372，默认 955 | 不支持调节 |
| opencode Go · deepseek-v4.1-flash | 没有 | 生效（0 推理、0 思考） | 推理 1068 → 1280，默认 1260 | 能关思考，档位作用弱 |
| MiniMax 官方 · M2.7（minimaxi.com 与 minimax.cn） | 没有 | 不生效 | 输出 6135 → 5133，默认 5986 | 总是深度思考 |
| MiniMax 官方 · M3 | 没有 | —— | budget 开启思考：有思考块，12 次答对 11，但量不随档位；effort 不开思考，多次只回 2 个 token 答错 | 只能开 / 关，用 budget |
| ChatGPT 订阅 · gpt-6-luna（Responses） | 没有（low…max） | 目录没有 none / minimal，不发字段 | 未声明档位时 max 实发 high：312 → 429（分不开）；声明后 max：312 → 580 | 声明档位后判定“支持” |
| ChatGPT 订阅 · gpt-6.1-sol / gpt-6-astra / gpt-6-sol / gpt-5.6-sol | 没有 | 同上 | 各 1 次，low → 声明后 max：103→207、105→211、146→211、283→345 | 同上 |
| 本地 qwen（127.0.0.1:8901）、step7 relay（127.0.0.1:18881） | —— | —— | —— | 拒绝连接，不可达 |

与 09-26 相比：DeepSeek 官方 v4-flash 在这道题上 low 和 max 的差距变小（当时推理 919 → 1887）；Anthropic 兼容接口的关闭思考与 opencode
v4.1-flash 的关闭思考现在都生效。结论不变的：opencode v4-flash 与 MiniMax M2.7 不支持调节；MiniMax M3 只能开 / 关。

**据此的处理**：
- 已知表不加条目。同一个中转（opencode）、同一个服务商（MiniMax）下不同模型表现不同，域名级默认值会误伤，改按模型档案声明
  （`reasoning_control`、`reasoning_levels`，由集成者经 `/model` 写入）。
- 回执如实说明 Responses 实际发送（见第 5 节，分支 `claude/38-effort-receipt`）。
- 顺带发现：ChatGPT 订阅的 WebSocket 传输 49 次里有 2 次“回复完成前断开”（约 23–24 秒，`ConnectionClosedError`，产品没保留关闭码），已交集成者。

## 3. 档位和控制方式

- **档位**（用户层）：`auto`（不发任何参数，服务商默认）、`off`（关闭思考）、`low`、`medium`、`high`、`xhigh`、`max`、`ultra`。
- **控制方式**（模型层，`reasoning_control`）：
  - `effort`：OpenAI 兼容接口写 `reasoning_effort: <档位>`，Anthropic 兼容接口写 `output_config.effort`；`off` 写 `thinking: disabled`。
  - `budget`：Anthropic 兼容接口仅在合法区间非空时写 `thinking: {type: enabled, budget_tokens: N}`。N 取值：low 2048、medium 6144、high 12288、xhigh 24576、max/ultra 为原可用上限，夹到 `[1024, max_tokens-1024]`；空区间不发 thinking，回执说明结构化未发送原因（第 10 节）。OpenAI 兼容接口仍只写 `thinking: enabled`。`off` 写 `thinking: disabled`。
  - `none`：不发任何字段。回执如实说明“不支持调节”。
  - `auto`（默认）：只对实测确认的供应商给默认值——`api.deepseek.com` 的 OpenAI 兼容接口取 `effort`，Anthropic 兼容接口取 `budget`，`chatgpt.com` 的 Responses 接口（ChatGPT 订阅）取 `effort`；其余一律 `none`。这张表只是优化，可在 `/model` 编辑里显式声明覆盖（例如把 MiniMax M3 声明为 `budget`）。
  - **Responses 协议（09-30 接入）**：`effort` 写 `reasoning: {effort: <服务商档位>}`。服务商档位按模型档案的 `reasoning_levels`（服务商声明的可用档位，
    ChatGPT 订阅模型添加时从目录 `supported_reasoning_levels` 自动带上，其它服务商可在 `/model` 编辑的「高级」里填）对应：
    low/medium/high 取同名；`xhigh` 依次取 xhigh → high，`max` 依次取 max → xhigh → high，`ultra` 依次取 ultra → max → xhigh → high 中第一个存在的；`off`（或强制工具要求关闭思考）只在声明了 none 或 minimal 时发送，
    否则不发字段、交服务商默认。未声明档位时只发通用的 low/medium/high（`max` 发 high），不发可能被拒的取值。
    09-30 真实核对 gpt-6-luna：`low` 推理 token 0、未声明时 `max`→high 为 24、声明后 `max`→max 为 59，服务商均接受。
- **优先级**：强制调用工具（原有逻辑）要求关闭思考时，优先于任何档位。DeepSeek 历史里缺 `reasoning_content` 而被迫关思考时，同样不再带档位。

## 4. 档位从哪里来

档位是会话线程的属性（`ConversationThread.reasoning_effort`，空串表示没设置）：

1. 主会话：`/effort <档位>` 写入当前线程，`/effort default` 清除；没有设置时用全局默认 `model_reasoning_effort`（YAML，默认 `auto`）。
2. 子代理：`create_subagents` 的 `effort` 参数（整批或逐项）。省略时冻结父级本轮的实际档位（线程设置，否则全局默认）。写进 `host_reasoning_effort.v1`（宿主属性，伪造值会先被移除），物化子线程时写进子线程；恢复和重放不改。孙代理同理，继承直接父级。`effort` 也计入派工去重身份，同目标、不同档位的对比不会被合并。
3. 每次模型请求时，按本轮参数里的 `agent_thread_id` / `conversation_thread_id` 现读线程。读取失败时回落全局默认，绝不阻断请求。

真实请求（`tool_model_generation._provider_request_options`）和两处载荷投影（网关自动选模 `gateway_model_adoption._payload`、子代理首轮选模）都调用 `settings/reasoning_effort.request_reasoning_options`。投影与真实发送的载荷逐字比对因此保持一致。压缩摘要、Curator 等后台辅助调用不带档位，用服务商默认。

## 5. 用户入口

- `/effort`：显示本会话档位（及来源：本会话设置 / 全局默认），以及在当前会话模型上的实际效果；末尾一行列出可选档位和怎么改
  （2026-09-30 起，`reasoning_control.describe_level_choices`，IM 没有菜单靠它知道能怎么改）。
- TUI 里单独 `/effort`（2026-09-30 起）：打开本地档位菜单 `cli/chat_parts/tui_effort_menu.py`，上下键选查看 / auto / off / low /
  medium / high / xhigh / max / ultra / default / probe，回车后以 `/effort <值>` 经控制出站箱发给 Gateway，Esc 不发；带参数的文字形式照旧直接发送。
  起因：用户只看到“最高（全局默认）”，从补全选中 `/effort` 回车会立刻提交，没机会输入档位。
- 回执与发送同一换算（2026-09-30 起，`reasoning_control.describe_config_reasoning_effect`，`/effort` 与参数中心共用）：
  Responses 模型上，没有声明 none / minimal 时 `/effort off` 实际不发字段，回执写“本设置不改变请求”；没有声明更高档位时
  `/effort max` 实际发 high，回执写“实际发送 high”；声明了就写“发送 max”。此前回执一律写“请求时关闭思考”“最高”，
  与 ChatGPT 订阅的实际请求不符（订阅目录没有任何模型声明 none / minimal，旧档案没有 reasoning_levels）。
- `/effort auto|off|low|medium|high|xhigh|max|ultra`：设置本会话档位；`/effort default` 清除；`/effort help` 显示用法。回执总是说明当前模型会怎样生效，模型不支持时明确说“本设置暂不改变请求”。
- `/effort probe`：在后台检测当前模型是否真的支持按档位调节；`/effort revert <编号>` 撤销检测写入的档案修改（见第 8 节）。
- `/model` 编辑模型：新增“思考控制”单选（自动 / 按档位 / 按预算 / 不支持），存为档案字段 `reasoning_control`（只有显式声明且不是 auto 才写键，旧档案逐字节不变）；`manage_models` 工具同一字段。
- 本地（非 Gateway）模式与 `/model` 等一样提示改用 `chat --gateway`。

## 6. 边界与后续

- 不按模型名、回复正文或模型自述判断能力；已知表只按接口域名加协议。
- 暂不在 TUI 底栏显示档位（`/effort` 可查看）；Responses 协议已接 `reasoning.effort`（见第 3 节）；Anthropic 官方模型开启思考时要求温度为 1，若显式声明 `budget` 又填了温度，可能被拒，届时再按协议处理。
- 仓库默认 `model_reasoning_effort: auto`，不改变任何现有请求。
- 关闭思考或调低档位会降低正确率：真实验收里 DeepSeek `off` 两次有一次答错；MiniMax M3 默认不思考时答错，声明 `budget` 并 `/effort high` 后答对。

## 7. 测试与验收

- 单测 `test_reasoning_effort.py`：控制方式解析，档位与强制工具选择的优先级，两种协议的真实组包（fake 传输），DeepSeek 历史缺思考正文时去掉档位，已声明方式的未知域名也能真正关思考，线程档位与默认，公用函数与自动选模投影一致，子代理显式 / 继承 / 非法值 / 去重身份 / 线程初始化，`/effort` 回执，档案字段校验与解析，TUI 表单，配置规范化。
- 真实验收（2026-09-26，隔离 home + 回环 8432，运行时 `step12k-e5b2f8bc`，详见 TESTS.md 顶部）：
  - DeepSeek 官方 OpenAI 兼容：出站请求按档位带 `reasoning_effort` 或 `thinking: disabled`，auto 不带字段；主模型输出 token 均值 max 约为 low 的 1.9 倍，off 最低，单题波动大。
  - DeepSeek 官方 Anthropic 兼容与声明为 `budget` 的 MiniMax M3：以助手原生内容块为证，off / 默认不思考时没有思考块，开启后有；M3 开启后从答错变为答对。
  - MiniMax M2.7：回执如实说明不支持调节，请求照常完成。
  - 子代理：一次创建 low、max、不设三个子代理，子线程档位分别是 low、max、继承的 medium，逐条出站请求与各自档位一致；输出 token 为 low < medium < max。

## 8. 自动检测是否支持调节（2026-09-27，分支 `claude/be-effort-probe`）

### 背景

my-agent 为了确认 opencode.ai 是否支持 `reasoning_effort`，先后 3 次把用户的 API key 写进 `web_fetch` 的请求头。根本原因是产品没有“用宿主保存的凭据检测模型能力”的入口，模型只能自己去试。

### 入口（TUI 与飞书相同，都走 Gateway 的 `/effort`）

- **自动**：用户把 `/effort` 设成 auto 以外的档位时，如果同时满足下面五条，宿主在后台检测一次，结果按模型档案缓存：
  1. 开关 `reasoning_control_auto_probe` 开着（默认开）；
  2. 档案没有显式声明思考控制方式（auto）；
  3. 解析结果是 none（不在已核对名单里）；
  4. 协议能按档位发字段（OpenAI / Anthropic 兼容；Responses 写了也不生效，不检测）；
  5. 这个模型还没有有效的检测记录。

  显式声明过 none 的档案尊重用户声明，不自动检测。
- **手动**：`/effort probe`，已有结果也重新检测，不受开关和显式声明限制。
- **查看**：`/effort` 显示进度（“已完成 n/9 次请求”）或结论。检测在后台跑，因为控制命令要在 30 秒内返回（TUI 的等待上限），9 次请求可能要几分钟。
- **撤销**：`/effort revert <编号>`。
- 结果不另发主动消息（主动推送只有飞书支持，且要走会话落账的整套投递），检测开始时的回执会提示“完成后发 /effort 查看结果”。
  检测结束时把结论作为宿主提示排进发起检测的会话（来源 `reasoning_probe`，同一会话只留最新一条），同一会话下一条前台回复顶部显示一次
  （飞书正文前“【提示】”、TUI 灰色系统行），`/effort` 查看过或重新开始检测时清掉（分支 `claude/be-host-notices`，见 [宿主提示](HOST_NOTICES.md)）。

### 方法

- 同一道需要几步推理的短题：与第 2 节实测同一题，答案 64，只要求写出数字，答案部分很短。
- 分三组各发 3 次，按轮交替发送，减少时段漂移：“低”（`reasoning_effort: low`）、“最高”（`max`）、不带字段。
- 后端按档案配置构建，只把控制方式临时当作 effort，这样低、最高两组才真的带字段；单次等待放宽到 180 秒；输出上限沿用正式请求的值。
- 每次只记档位、token 数、HTTP 状态码和异常类型；回复正文和异常消息都不保存（可能回显密钥）。
- 会话头按独立的检测身份生成，不借用任何会话。

### 判定（`settings/reasoning_probe_judge.py`，纯函数，只读 usage 的结构化字段）

- **计量**：优先比较推理 token（`completion_tokens_details.reasoning_tokens`，Responses 为 `output_tokens_details`）。只有所有成功样本都没有推理 token、但都有输出 token 时，才退回比较输出 token，回执写明“按输出 token 判定”（集成者 2026-09-27 同意此回退，以回执写明为前提）。第 2 节里中转接口的实测看的就是输出 token。
- **支持**：三条同时满足：
  - “最高”组中位数至少是“低”组的 1.5 倍；
  - 至少多 200 个 token；
  - 两组完全不重叠，即“最高”组最少的一次也比“低”组最多的一次多。

  同分布下三对三完全不重叠的概率只有 1/20，噪声很难凑出“支持”。
- **不支持**：
  - 带字段的两组全被 400/422 拒绝、不带字段的一组全部成功（服务商拒绝了这个字段）；
  - 所有样本都是 0（模型不产生推理 token）；
  - 不满足上面三条。
- **无法判定**：
  - 任何一组成功样本不足 3 个（有请求失败）；
  - 用量里没有 token 统计；
  - 检测过程出错。
- **标定**（2026-09-26 回放，见 `test_reasoning_probe.py`）：
  - DeepSeek 官方 OpenAI 兼容接口：推理 token 低 919、最高 1887、不带 1925，判支持；
  - OpenCode 中转：输出 token 低 2234 / 2664、不带约 1100–1200，判不支持；
  - MiniMax M2.7：判不支持；
  - 完整主代理上下文里的 DeepSeek：最高均值约为低的 1.9 倍，但两组重叠，判不支持。这正是检测不用主代理上下文、而用独立短题的原因。

### 结果

- 记录在该用户模型档案旁的 `.reasoning-probes.json`，每个档案一条，带模型指纹（协议 + 接口地址 + 模型名的哈希）。
  - 模型换了，旧结论就失效。
  - running 记录超过 1 小时（比如 Gateway 中途重启）视为中断，可以重新检测。
  - 同一档案同时只跑一个检测。
- **确认支持**时，经参数中心写 `reasoning_control: effort`：
  - **私有档案**：`parameter_changes.set_profile_field` 在档案文件锁内读改写并经 `validate_model` 校验，记在该用户档案旁的 `.changes.jsonl`（与配置账本同一格式、同一脱敏与回滚规则）。下一轮对话起生效，`/effort revert <编号>` 撤销，只能撤销本人账本里的记录。
  - **部署默认模型**：只有管理员触发时才用 `set_parameter` 写全局配置 `model_reasoning_control: effort`，重启 Gateway 后生效，`/settings revert <编号>` 撤销。普通用户触发时只记录结果，提示请管理员写入。
  - **管理员共享的模型**：只记录结果。
  - **已经按档位发送的档案**（显式 effort，或已知表解析为 effort）不重复写。
- **不支持或无法判定**：只记录，并如实告诉用户原因和中位数。

### 边界

- 09-30 起 Responses 协议也按档位发送（第 3 节），所以同样可以检测；未声明档位时“最高”按 high 发送参与比较。判定门槛不变（最高档至少比低档多 200 个推理 token 且 1.5 倍、两组不重叠），题目很简单时即使支持也可能判“不支持”，ChatGPT 订阅由已知表直接按档位发送，不依赖检测。

- 凭据只在正式后端内部使用，不进入模型上下文、工具参数、检测记录或回执。测试断言假密钥不出现在回执、检测记录和账本里。
- 每次检测发 9 次请求；每次请求显式带输出上限 `PROBE_MAX_OUTPUT_TOKENS`（40000，与档案输出上限取小后才是真实发送值），
  回执按结构化事实写“每次输出上限 N token，最多约 9×N token”，不再写死“1～5 分钟/少量 token”（2026-10-02 起，
  见第 11 节）。实测 MiniMax-M2.7 上 9 次检测约 23 分钟、单次输出最多 32867 token，长输出模型可能更久。
- 标定只有 2026-09-26 的单次实测与完整主代理上下文的波动范围，没有独立短题的多次采样。真实服务商上的误判率要靠真实验收确认。

## 9. C7：用户可选 xhigh / ultra（已实施，指定台账日期 2026-10-01）

**解决问题**：模型档案已声明 xhigh/ultra，但用户菜单与命令白名单只有六档，无法直接选择。
用户层固定八档 `auto/off/low/medium/high/xhigh/max/ultra`；服务商层 `reasoning_levels` 仍允许新的合法字符串声明，不反过来限制成这八档。

唯一换算表在 `agent/backends/reasoning_control.py::_REASONING_LEVEL_RULES`，同时定义中文标签、两个 effort 方言的候选顺序与预算：

| 用户档位 | Responses 的候选顺序 | OpenAI Chat / Anthropic effort 的候选顺序 | budget 原始值 |
|---|---|---|---|
| xhigh（超高） | xhigh → high | high | 24576 |
| max（最高，原档） | max → xhigh → high | max → high | 原上限 |
| ultra（极限） | ultra → max → xhigh → high | max → high | 与 max 相同 |

- 有声明时只取候选与声明的第一个交集，交集为空不发 effort 字段；不按型号、展示文案、模型回复猜能力。
- 无声明时保留原协议通用范围：Responses 为 low/medium/high（新档回落 high），Chat/Messages 为 low/medium/high/max。
  新用户档位不能因此原样发到不支持的新接口；Chat/Messages 的 xhigh/ultra 仍落到原已能发送的 high/max。
- Anthropic 的原始预算表不变；2026-10-02 第 10 节修复了继承的空区间缺陷：先检查 `max_tokens-1024 >= 1024`，成立才取
  `max(1024, min(原始预算, max_tokens-1024))`，否则不发 thinking。max_tokens 仍是本次载荷沿原规则计算的上限，不为预算抬高。
  OpenAI budget 仍只有 thinking.enabled；预算与 none 控制不发送服务商档位字符串。
- auto 不加字段，off/强制工具选择优先，DeepSeek 缺 reasoning_content 的原关闭逻辑保持。
- 菜单、TUI/IM 共用命令、配置枚举与派工顶层/逐项 schema 都沿唯一档位定义；线程和 child 属性保存用户档位，不能在继承时提前降档。
  真实发送、Gateway 自动采用和 child 首轮候选投影按候选后端自身声明换算，不能沿用父模型的实际发出值。
- `/effort` 和 user_config 的“实际效果”与发送使用同一个候选筛选，降档写“实际发送 high/max”等；无对应值写“不改变请求”。
  budget 配置回执按工厂同源的常规输出上限说明预算数值或未发送原因；没有上限的纯档位说明只写条件规则，不能冒充每个请求已发出的最终数值。配置 set 的重启语义不变。
- Responses 仍无自动采用所需的完整容量投影，保留原 `provider_request_surface_unknown`；本项只扩档位，不冒用 Chat 投影扩大选模范围。

组件覆盖包括声明/无声明/无交集、三种控制方式、出站字段与回执、八档菜单、配置保存、两处选模投影与首业务请求。
三个独立变异各自被拦且逐字节还原，扩展回归中的既有失败保留在 TESTS。真实模型及实际 TUI/IM 客户端未验证，由 3a 集成后核对；本线不触碰生产配置或启停 Gateway。

## 10. Anthropic 小输出上限的预算边界（2026-10-02）

**解决问题**：工厂按窗口统一派生输出上限，窗口 4096 可得到 max_tokens=1024。旧实现把预算上界强行抬到 1024，
发出与输出上限相等的预算；max_tokens=2047 时也不满足正文至少预留 1024 的合同。该缺陷早于 C7 八档扩展。

唯一裁决是 `backends/reasoning_control._anthropic_budget_decision`，只读档位预算和本次输出上限，返回冻结的
`budget_tokens/reason_code`，不写配置、不发请求。`reasoning_payload_fields` 与预算回执共用该裁决：

| 本次 max_tokens | thinking 出站字段 | 宿主未发送原因 |
|---|---|---|
| 1024、2047 | 完全省略 | `reasoning_budget_interval_empty` |
| 2048 | enabled，budget_tokens=1024 | 无 |
| 2049 | enabled，budget_tokens=1025 | 无 |

- 判断整个区间是否为空，不针对窗口、型号或某个档位加专项分支；正常大上限与原始预算表不变。
- 不抬高 max_tokens，不发送 reason_code；省略 thinking 是没有额外设置，不保证服务商默认已关闭思考。
- `/effort` 与参数中心共用 `describe_config_reasoning_effect`，常规输出上限来自工厂同源的 `effective_max_output_tokens`，
  不使用配置里未经窗口夹取的原始 max_tokens。无上限的 `describe_reasoning_effect` 只解释条件规则。
- 具体请求仍按本次上限裁决；短 JSON、强制工具与其它请求局部覆盖不能用常规回执代替。off/强制关闭优先级与其它协议不变。
- `test_anthropic_reasoning_budget.py` 经真实工厂与后端 builder 截获非流/流式替身出站，核对完整投影；
  真实控制服务的 chat/feishu 路由和参数中心用隔离 home 测回执。所有实际 HTTP 入口默认拒绝，夹具遗漏会本地失败。
- 三项变异分别破坏空区间判据、2048 包含边界和回执上限来源，均被拦截并恢复；命令与结果见 TESTS.md 顶部。
  这些是组件证据，真实供应商、实际 TUI/IM 客户端及生产 Gateway 未验证，未部署。

## 11. C7 计量与说法修正（2026-10-02，分支 `worker/ds2-effort-probe-metering`）

**解决问题**：①每个模型第一次被用时宿主先发一次 `probe_tool_capability`（工具能力探测，按模型缓存），
该调用不进 `model_usage`，用量账不完整；②`/effort` 自动检测回执写“约需 1～5 分钟、会额外消耗少量 token”，
实测 MiniMax-M2.7 上 9 次检测约 23 分钟、单次输出最多 32867 token、合计约 10 万输出 token，说法低估且成本无上界。

### 探测调用计入用量账

- 探测每次真实 `generate` 都记账（重试也算）：开始记 `ModelCallStartedParams(is_probe=True,
  metadata={"purpose": "probe:tool_capability"})`，成功用供应商 usage 收尾、失败记 failed；按模型缓存的行为不变，
  命中缓存不重复记账。
- 用途独立成桶：`model_call_ledger._PURPOSE_BUCKETS` 与 `store_usage._PURPOSES` 同步加 `probe:tool_capability`，
  和业务调用分开统计；探测无宿主绑定 scope（直调、测试替身）时不记账。
- 记账范围经 `probe_accounting_scope`（ContextVar）从 `select_tool_protocol` 的三个调用点
  （loop_support、model_selection、gateway_model_adoption）透传到后端，附 request_id/run_id。

### 自动检测成本上界

- 新常数 `reasoning_probe.PROBE_MAX_OUTPUT_TOKENS = 40000`（进常数目录 constants_catalog.json，
  名字带 `_TOKENS` 单位后缀、上方有中文说明）；每次检测请求经 `ProviderRequestOptions(max_output_tokens=...)`
  显式带上限，与档案输出上限取小后才是真实发送值。base/anthropic/openai_chat 适配器 `generate()` 透传该字段。
- 回执改为按结构化事实生成：“每次输出上限 40000 token，最多约 360000 token；长输出模型可能更久”，删除写死的
  “1～5 分钟/少量 token”。不改变“自动检测是否默认触发”（开关与五条触发条件原样）。
- 判定（`reasoning_probe_judge`，纯函数只读 usage 结构化字段）所需的最低输出差仅 200 token，上限 40000 远高于此，
  截断不会压平 low/max 差异；对应测试用替身构造截断到上限的输出验证判定仍返回“支持”。

### 测试与门禁

- 新增 `test_probe_tool_capability_metering.py`（用途标签/次数/scope、缓存命中不重复记账、无 scope 直调不记账、
  失败记 failed）；`test_reasoning_probe.py` 追加检测请求带上限+回执按事实、截断输出下判定仍 supported。
- 5 个独立变异（purpose 分支删除、不带输出上限、回执写死旧文案、去掉 ledger.started、store_usage 桶集合不同步）
  全部被拦截并恢复；命令与结果见 TESTS.md 顶部。
- 真实 Gateway 交互、真实服务商上的检测耗时与用量账未验证，由 3a 集成后核对。
