# 智能程度（推理强度）

状态：已合入 main `7a15c9c91` 并双机部署（运行时 `step12k-e5b2f8bc`，2026-09-26），隔离真实验收通过（结果见第 7 节与 TESTS.md 顶部）。本文是“智能程度”的唯一模块设计；`DESIGN_LEDGER.md` 只保留摘要和链接。

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

## 3. 档位和控制方式

- **档位**（用户层）：`auto`（不发任何参数，服务商默认）、`off`（关闭思考）、`low`、`medium`、`high`、`max`。
- **控制方式**（模型层，`reasoning_control`）：
  - `effort`：OpenAI 兼容接口写 `reasoning_effort: <档位>`，Anthropic 兼容接口写 `output_config.effort`；`off` 写 `thinking: disabled`。
  - `budget`：Anthropic 兼容接口写 `thinking: {type: enabled, budget_tokens: N}`。N 取值：low 2048、medium 6144、high 12288、max 为上限，再夹到 `[1024, max_tokens-1024]`。OpenAI 兼容接口只写 `thinking: enabled`。`off` 写 `thinking: disabled`。
  - `none`：不发任何字段。回执如实说明“不支持调节”。
  - `auto`（默认）：只对实测确认的供应商给默认值——`api.deepseek.com` 的 OpenAI 兼容接口取 `effort`，Anthropic 兼容接口取 `budget`；其余一律 `none`。这张表只是优化，可在 `/model` 编辑里显式声明覆盖（例如把 MiniMax M3 声明为 `budget`）。OpenAI Responses 协议暂未接字段换算，一律 `none`。
- **优先级**：强制调用工具（原有逻辑）要求关闭思考时，优先于任何档位。DeepSeek 历史里缺 `reasoning_content` 而被迫关思考时，同样不再带档位。

## 4. 档位从哪里来

档位是会话线程的属性（`ConversationThread.reasoning_effort`，空串表示没设置）：

1. 主会话：`/effort <档位>` 写入当前线程，`/effort default` 清除；没有设置时用全局默认 `model_reasoning_effort`（YAML，默认 `auto`）。
2. 子代理：`create_subagents` 的 `effort` 参数（整批或逐项）。省略时冻结父级本轮的实际档位（线程设置，否则全局默认）。写进 `host_reasoning_effort.v1`（宿主属性，伪造值会先被移除），物化子线程时写进子线程；恢复和重放不改。孙代理同理，继承直接父级。`effort` 也计入派工去重身份，同目标、不同档位的对比不会被合并。
3. 每次模型请求时，按本轮参数里的 `agent_thread_id` / `conversation_thread_id` 现读线程。读取失败时回落全局默认，绝不阻断请求。

真实请求（`tool_model_generation._provider_request_options`）和两处载荷投影（网关自动选模 `gateway_model_adoption._payload`、子代理首轮选模）都调用 `settings/reasoning_effort.request_reasoning_options`。投影与真实发送的载荷逐字比对因此保持一致。压缩摘要、Curator 等后台辅助调用不带档位，用服务商默认。

## 5. 用户入口

- `/effort`：显示本会话档位（及来源：本会话设置 / 全局默认），以及在当前会话模型上的实际效果。
- `/effort auto|off|low|medium|high|max`：设置本会话档位；`/effort default` 清除；`/effort help` 显示用法。回执总是说明当前模型会怎样生效，模型不支持时明确说“本设置暂不改变请求”。
- `/model` 编辑模型：新增“思考控制”单选（自动 / 按档位 / 按预算 / 不支持），存为档案字段 `reasoning_control`（只有显式声明且不是 auto 才写键，旧档案逐字节不变）；`manage_models` 工具同一字段。
- 本地（非 Gateway）模式与 `/model` 等一样提示改用 `chat --gateway`。

## 6. 边界与后续

- 不按模型名、回复正文或模型自述判断能力；已知表只按接口域名加协议。
- 暂不在 TUI 底栏显示档位（`/effort` 可查看）；Responses 协议的 `reasoning.effort` 待接；Anthropic 官方模型开启思考时要求温度为 1，若显式声明 `budget` 又填了温度，可能被拒，届时再按协议处理。
- 仓库默认 `model_reasoning_effort: auto`，不改变任何现有请求。
- 关闭思考或调低档位会降低正确率：真实验收里 DeepSeek `off` 两次有一次答错；MiniMax M3 默认不思考时答错，声明 `budget` 并 `/effort high` 后答对。

## 7. 测试与验收

- 单测 `test_reasoning_effort.py`：控制方式解析，档位与强制工具选择的优先级，两种协议的真实组包（fake 传输），DeepSeek 历史缺思考正文时去掉档位，已声明方式的未知域名也能真正关思考，线程档位与默认，公用函数与自动选模投影一致，子代理显式 / 继承 / 非法值 / 去重身份 / 线程初始化，`/effort` 回执，档案字段校验与解析，TUI 表单，配置规范化。
- 真实验收（2026-09-26，隔离 home + 回环 8432，运行时 `step12k-e5b2f8bc`，详见 TESTS.md 顶部）：
  - DeepSeek 官方 OpenAI 兼容：出站请求按档位带 `reasoning_effort` 或 `thinking: disabled`，auto 不带字段；主模型输出 token 均值 max 约为 low 的 1.9 倍，off 最低，单题波动大。
  - DeepSeek 官方 Anthropic 兼容与声明为 `budget` 的 MiniMax M3：以助手原生内容块为证，off / 默认不思考时没有思考块，开启后有；M3 开启后从答错变为答对。
  - MiniMax M2.7：回执如实说明不支持调节，请求照常完成。
  - 子代理：一次创建 low、max、不设三个子代理，子线程档位分别是 low、max、继承的 medium，逐条出站请求与各自档位一致；输出 token 为 low < medium < max。
