# L01 小任务收敛与模型耗时交接

## 基本信息

- workstream：L01，小任务范围与原模型账耗时诊断。
- worktree：本期集成工作树，起点 `84a63ea87`。
- owner：latency_runtime_audit；由集成者统一提交、真实验收、部署与打包。
- date：2026-10-13。
- 状态：本地实现及 focused tests 已通过；冻结 wheel 的 Linux 原生 TUI 与官方 MiniMax 单次输入验收已完成，最终字节码安装包仍由集成者做 smoke。

## 本线目标

普通问题在现有事实足够时直接回答，执行任务不扩大成无关审计；把模型耗时保留在原调用账内，允许不读取业务正文也能区分主模型、辅助调用和决策开销。

## 实际完成

- `prompts/default.md` 将数字证据要求限定到数据统计、聚合、测量、对账及产物统计；普通解释的示例数字和简单问答不因此强制调用工具。
- `agent/settings/config.py` 的执行提示明确授权范围与适度验证：已有可靠上下文直答，相关检查通过、无新改动或疑点时交付；保留真正执行任务的完整交付与如实报告。
- 默认 `system_prompt` 与随包 YAML 同源同步；不修改用户已有配置、人格、模型、推理档位或工具权限。
- 原 `ModelCallLedger` 按物理调用时钟累计耗时，沿原主/辅助/决策用途桶保存；明细裁剪、迟到 HTTP、重放不丢失或重复计时。
- `store_usage` 在原累计快照增量中保留六个整型字段；`model_metrics` 在原线程数字显示副本投影当前 request/run 耗时，不新建日志或账本。

## 改动文件与事实源

- `agent/contracts/model_call_latency.py`：原记录计时的纯数字投影，无 I/O、计时器、预算或权限变化。
- `agent/contracts/model_call_ledger.py`：原累计与用途桶沿新旧记录差值更新计时。
- `agent/conversation/store_usage.py`：原加法字段与快照增量纳入计时。
- `agent/conversation/model_metrics.py`：当前请求耗时白名单及原持久显示入口。
- `agent/settings/config.py`、`config/agent_config.yaml`、`prompts/default.md`：同源默认与软引导。
- `tests/test_model_latency_accounting.py`：覆盖原账、裁剪、持久增量、重放、迟到、未知、真实零、匿名显示与旧快照。
- `tests/test_model_call_ledger.py`：现有精确摘要验收同步检查新增计时字段。

以上路径均相对 `agent_py_agent/`。

## 显示字段接线

原模型摘要及各 `purpose_breakdown` 桶新增：

| 字段 | 含义 |
| --- | --- |
| `model_latency_ms` | 已登记总时长的物理调用毫秒之和 |
| `first_token_wait_ms` | 已观察到首 token 的等首包毫秒之和 |
| `post_first_token_ms` | 总时长和首包都已记录且顺序合法的首包后毫秒之和 |
| `timed_model_call_count` | 总时长已计时的调用数，含真实零值 |
| `first_token_timed_call_count` | 已观察首包时钟的调用数 |
| `split_latency_call_count` | 两阶段均可拆分的调用数 |

`thread.model_metrics` 沿原白名单保存同名字段加 `request_` 前缀，并附：

- `request_main_latency_ms`、`request_auxiliary_latency_ms`、`request_decision_latency_ms`。
- `request_physical_model_attempt_count`、`request_logical_model_turn_count`。

`/status` 可显示“模型累计 X 秒；已计时 N/M 次；等首包 Y 秒、首包后 Z 秒”，并分主/辅助/决策。旧显示没有字段时显示未知，不能把缺字段当真实零。不同目的与并行调用时长相加是诊断累计，**不是回合墙钟**；拆分覆盖不全时不可宣称两段覆盖全部等待。工具、审批、排队没有独立时钟事实时不编造零耗时，也不把首包前时间直接命名为某一个阶段。

## 测试命令和结果

在仓库已有 Python 环境执行 focused pytest，覆盖以下文件：

```text
test_model_latency_accounting.py
test_model_call_ledger.py
test_model_call_ledger_partitions.py
test_model_call_ledger_open_calls.py
test_tui_model_metrics.py
test_store_usage_open_world.py
test_prompting_builder.py
test_runner_prompts.py
test_response_decision_native_tool_use.py
```

最终将该组与 `test_config_layers.py`、`test_config_validation.py` 合并执行：286 passed，4.46 秒。本切片 Ruff 与 `git diff --check` 通过。整树 strict code-size 当时有其它切片两项 hard，已告知集成者；报告只写 `/tmp/my-agent-l01-code-size.md`，未修改仓库尺寸报告。不能用本切片通过代替最终整树 gate。

## 影响与真实验证边界

- 原 `PromptBuilder.prepare_render_input` 每次读取动态 prompt 文件（`prompting_parts/builder.py` 的 `read_prompt_files/_read_prompt_file/_dynamic_prompt_text`），线程人格冻结只保存 agents/soul/user/diagnostics，不冻结内置动态文件。
- 正常主会话沿内置 prompt 配置，在新进程使用更新资源后，既有会话也能取得范围指导。用户自行排除内置 `prompt_files` 时不强加它；用户旧显式 `system_prompt` 完整保留，未实施文案匹配迁移。
- 普通会话执行与子代理分别引用原共用执行提示常量；不按自然语言关键词分类任务或强制设小任务工具轮数。
- 本线未读取生产正文、个人 AGENTS 或密钥，未发送真实 IM、切换模型、重启线上、推送或部署。
- 测试使用原生命周期和合成时钟、原 ConversationStore。提示改善只是模型指导，不能以确定性测试宣称真实 LLM 必然变快。

## Linux 原生 TUI 与官方模型验收

集成者冻结新 wheel 后，使用包内 CPython 3.11 在 2 核 Linux 测试机运行实际 Gateway + TUI；两个 case 各有全新隔离 home/config，顺序使用同一空闲端口。官方 `MiniMax-M2.7`、`anthropic_compatible`、官方 endpoint 已核对；凭据只在测试机内部读取，未输出、复制到仓库或提供给模型。测试配置关闭隔离环境的记忆策展、自学习与推理档位自动探测；未改变线上参数、模型、用户人格或权限。

每个 case 在输入前空闲超过 10 秒；只发送一次任务 prompt，此后观察真实 TUI、原请求终态、原模型账、原工具完成事件和模型生成文件，不帮助模型创建产物。

| case | 单次输入到终态 | 输入回显 | 主调用 / 冷启动探测 | 模型累计 | 结果 |
| --- | --- | --- | --- | --- | --- |
| 两句话解释 HTTP 401/403 | 8.62 秒 | 38.0 毫秒 | 1 / 1 | 6.062 秒 | `done`，零工具，正确两句话 |
| 小文件写入 42 并读回 | 11.52 秒 | 34.3 毫秒 | 3 / 1 | 8.289 秒 | `done`，`write_file/read_file` 均成功，文件精确为 42 |

两次模型和 HTTP 重试均为零；decision 和辅助桶均零。额外一次为原有 `probe:tool_capability` 冷启动探测，不是问答多跑一次主模型。首包和首包后拆分覆盖问答主调用 1 次、文件主调用 1/3 次，因此其数字不能宣称覆盖全部等待。

匿名进程采样仅追踪自己的 Gateway、TUI 及其 `/proc/stat` 亲缘元数据，未读取其它进程命令行、环境或会话正文。两 case 均为空闲/工作 2 个测试进程、退出 TUI 后 1 个、停止自己 Gateway 后 0 个；稳定空闲合计 RSS 约 181 MiB、CPU 约 1–2% 单核；工作短窗口峰值分别 50.46% 与 38.50% 单核。这个小任务样本没有遗留进程证据，不能代替对真实并发子代理、adapter 和后台任务的归属核查。

证据保留在测试机隔离根目录的 `tui-acceptance/{question,file}/evidence/`：`summary.json`、`samples.json`、`process-samples.json`、`tool-facts.json` 均为匿名事实；`screen.txt/terminal.raw` 仅含合成任务，权限 0600。实际根路径由集成者持有，不写个人路径入仓库。未验边界：最终 `.pyc` 安装包、Windows SSH 客户端、线上旧 owner 或历史长会话、真实 IM 和云文档；本轮没有旧版本同参数对照，不能宣称普遍提速倍数。

## 需要主线协调与建议下一步

集成者完成最终字节码安装包 smoke 与严格 gate，再提交打包；上线后以匿名原模型账观察用户实际慢请求，区分主调用、冷启动探测、决策、审批或工具等待。可并行审计匿名计时和执行权限，但真实模型与部署由一人统一，不能为求快降低用户档位、放宽权限或假设工具/审批时间为零。
