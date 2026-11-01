# MEGA 统一架构：业务空间 v1.2、Cache v2 与 Issue 闭环

更新日期：2026-11-01。**源码候选设计与集成导航，不是发布通过票。**

- 设计研判基线：`d071df60eda3f90d6b6da6e61dd193a80adbbecc`。
- 本页实际核对源码：冻结 `integration/mega-wave-2@a1e23366c9ab57bb16041cf1431498b0488caf71`。
- W16 从该 SHA 开工，维护授权文档、原生成器的常量 JSON，以及总控 W16-fix 明确批准的10项元数据待整改 fixture 基线；没有修改服务实现、默认开关、权限或模型调用。
- 当前派工/状态唯一入口：[MEGA_DISPATCH_PROGRESS](../tasks/MEGA_DISPATCH_PROGRESS.md)；验收标准与后续封包：[MEGA_ACCEPTANCE](../tasks/MEGA_ACCEPTANCE.md)。
- 下列架构约定来自本次派工包 `interface_contracts.json`；代码入口用于定位，不把“存在一个符号”算作真实接通。

## 1. 一个概念一份事实

| 对象 | 权威与边界 | 不得替代它的东西 |
| --- | --- | --- |
| Owner/space/actor | 当前可信宿主、原空间目录与当前授权 | 请求里的 owner/path、复制 DTO、Markdown、展示 allowed_actions |
| 任务与执行 | 原 SessionTask、TaskRun、AgentRun 和所有 attempt 的稳定关系 | latest 推断、后台组织树、模型声称完成 |
| 工作流 | 原 WorkflowRunRecord/run_store 与冻结 scope/context/revision | 第二个调度器、updated_at 冒充 CAS、重试重跑已成功 script |
| 授权与副作用 | 原 grant、路径、审批首决、幂等与投递账 | 可见按钮、文案、日志、断线后新建操作 |
| 上下文与缓存 | canonical raw/base、分源 typed snapshot 和原 Compact 边界 | 只读投影反写权限、离线命中率冒充真实模型用量 |
| 常量 | 实际读取处的源码/正式配置 | `constants_catalog.json` 的展示数据 |
| 验收结论 | 精确 SHA/tree、命令退出码、JUnit、原生/真实调用证据 | 历史分片 PASS、collect 数量、源码包存在、作者自签双审 |

## 2. 同一领域，多入口而非多套业务

```text
原生认证/CLI/自然语言/TUI                 Plugin 原安装/manifest/grant
              |                                      |
    gateway_parts/business_api                 plugin_host/business_actions
              |                                      |
              +------ 同一冻结 ActionSpec/schema -----+
                                  |
                  当前 actor/Owner/space/绑定再核验
                                  |
                BusinessServices -> 原领域服务/原账本
                                  |
                typed result / 精确错误 / 原操作查询
```

设计定义 102 个领域动作；`business.events.stream` 是另一个运输专用 ActionSpec，不计入 102；媒体上传/下载是两条独立 HTTP 运输。定义数量不表示 102 个动作已在当前宿主 ready。

实际入口：`gateway_parts/business_api.py::execute_business` 在领域调用前校验 ActionSpec、宿主身份与 `ready`；`plugin_host/business_actions.py::GatewayBusinessDomain` 复验原 AuthorizedCall，不伪造原生身份。默认 `business_spaces_enabled=false` 保持；只有最终同源验收后沿原配置机制启用，不新增按模型开关。

媒体沿 `gateway_parts/business_artifacts_http.py` 的原认证、CSRF、Owner/空间范围、上传预算、原文件服务和版本引用；文件名只是展示，不作为宿主路径，也不自动接受或执行上传内容。未知结果沿原 operation 查询，不能换新 ID 盲重传。

## 3. 冻结服务端口导航

本表是输入合同的定位投影；实现与消费者必须在最终候选上组合验证。

| 端口 | owner | 定义位置 | 合同消费者 |
| --- | --- | --- | --- |
| SPACE_SERVICE | W1 | `agent_py_agent/agent/conversation/business_spaces.py::BusinessSpaceService` | W2, W3, W6, W13, W14 |
| BIND_TASK_CONTEXT | W1 | `agent_py_agent/agent/conversation/business_context.py::resolve_business_context` | W2, W3, W13 |
| TASK_CONTEXT_PERSIST | W2 | `agent_py_agent/agent/conversation/session_tasks.py::SessionTaskStore.create` | W3, W13 |
| BACKGROUND_SERVICE | W2 | `agent_py_agent/agent/conversation/background_agents.py::BackgroundAgentService` | W3, W12, W13, W14 |
| TRUSTED_COMPLETION_BRIDGE | W6 | `agent_py_agent/agent/capability/task_learning_bridge.py::enqueue_completed_task_learning` | W2, W5 |
| WORKFLOW_SCOPE_SERVICE | W3 | `agent_py_agent/agent/workflows/business_scope.py::WorkflowScopeService` | W1, W13, W14 |
| TASK_LOCATOR | W4 | `agent_py_agent/agent/conversation/task_locator.py::resolve_task_location` | W2, W4, W14 |
| MOUNTED_CAPABILITIES | W6 | `agent_py_agent/agent/capability/business_capabilities.py::resolve_business_capabilities` | W2, W3, W5, W13 |
| DELIVERY_RECOVERY | W3 | `agent_py_agent/agent/conversation/background_delivery.py::FrozenOwnerDelivery` | W2 |
| OBSERVATION | W14 | `agent_py_agent/agent/conversation/business_observability.py::BusinessObservationService` | W12, W13 |
| BUSINESS_API | W13 | `agent_py_agent/agent/gateway_parts/business_api.py::dispatch_business_action` | W12, W14 |
| BUSINESS_STREAM | W14 | `agent_py_agent/agent/gateway_parts/business_event_stream.py::stream_business_events` | W12, W13 |

## 4. 后台主身份与执行树分开

W2 负责持久后台主身份、任务入队、会话索引和原唤醒链；W14 观察原 SessionTask/TaskRun/AgentRun/全部 attempt，保留 `manages`、`executes`、`child_of` 三种不同关系。管理者不是执行父节点，显示树不是身份来源。

消息页必须沿原角色历史/精确子代理 reader、逐消息授权与脱敏；不能退回 root-thread 聊天当作子代理历史。cursor 绑定 Owner、主体、过滤器、绑定、权限修订、代际与有效期；撤权、保留截断、来源更换必须重新裁决或明确失效。缺失费用/用量为 unknown/null，不伪造零。

## 5. Workflow 与投递恢复

W3 复用正式 `ScriptWorkContext`、`ScriptExecutionResult`、`WorkflowRunRecord`；三范围与 execution definition、scope revision、run revision 分别保存。输入的完整 stdin 与环境变量字节预算分开；异步节点按原任务身份关联，不新建平行队列。

#22 的组合闭环必须证明 W2 已 canonical 持久完成后，W3 的恢复仅补交付而不重跑 script。交付未知保留原责任与幂等键，不靠 completed 文案消除未知副作用。

## 6. 能力学习与挂载优先

W5 维护分源事实与完成事件桥，W6 维护 mounted_first、工具投影及可信 Skill 学习。#25 必须经过 W5 候选、W2 canonical 持久完成、W6 可信入队；`save=False` 的原语义、来源批准链、Owner/CAS/配额门不变。工具展示缓存不授予工具执行权。

## 7. Cache v2 的真实边界

- 每个来源是完整 typed snapshot：A→B→A、clear 与 unknown 均由对应来源当前事实解释；不能将空快照与未覆盖混为一谈。
- raw/base 固定 body 是原始权威；投影只读。稳定 system 与已经可见的前缀不能在原位置改写，真实 Compact 边界另行处理。
- text/off 的字节、完整工具批次、模型逻辑请求数和正常工作节奏必须保持；不删工具结果、不增请求、不填充前缀刷命中。
- W7 归一真实 provider 用量并隔离离线账本；真实缓存 H、TTFT、吞吐来自正常业务请求证据，不从字符数换算 token 节省。
- 旧 probe 的 159906 是 Unicode 字符、0 次真实 provider 调用，不能写成线上收益。

## 8. 观察流与来源订阅

W14 的 `stream_business_events` 提供快照/增量/heartbeat/gap/cursor_expired，原生 UTF-8 NDJSON 与 typed EventView 消费方式不同。流的有界队列、原生 HTTP 寿命、Plugin 的原限额/撤权/关闭路径必须分别保留；断开观察不取消业务，也不占 ask worker 或开启 LLM。

订阅位于原空间 catalog，来源由可信 authorizer/resolver 核验；远端推送沿原 Token/HMAC/nonce/IP 与原 receipts。只有原任务耐久接纳后才推进来源 cursor。ACK 丢失/重试/路由编辑仍绑定同一业务事件和原操作，不退回普通 ask。

**尚须最终接线复验**：`BusinessServices.projection`、当前 observation actor/catalog、精确子代理 readers、订阅宿主 hooks、原生/Plugin 两种 stream 消费、后台真实提交服务和普通 task 的 workflow_ref 合同。W14 的历史 focused 不能替代 W13+W14 的宿主组合证据。

## 9. 诊断、治理与跨平台

W4 原任务定位/子代理健康，W8 治理与限流维护，W9 provider/MCP 诊断，W10 原生沙箱组合，W11 配置/管理员口令，W12 命令/TUI 共守原领域。#35 的诊断必须被 W13 实际消费；#36 必须同时满足 runtime 三项与 provider 连接上限恢复。

存储配额以 100MB–100GB、默认 1GB 的真实规模核验；<=128 实时快照与 >128 增量/后台分片对账分开测量。ENOSPC/EIO/重启/CAS 冲突分别一次注入一个现实故障，不能用极小配额或多个永久故障堆出虚假生产 BLOCK。

## 10. 常量目录只读

唯一生成器 `scripts/build_constants_catalog.py` 只做 AST 静态扫描，不 import 产品，不把定义搬到中央注册表。运行时 `settings/constants_catalog.py::{load_catalog,search_constants,entry_with_line}` 只读查找，行号按源码即时定位，JSON 不存漂移行号。

W16 已按原生成器生成 1286 项并 `--check` 一致。修订前原只读守卫发现10个上游定义缺单位/中文说明，不是源码值不一致或已证生产漏洞。总控审阅证据后于2026-11-01通过 `plan-w16-fix.md` 明确批准仅这10项进入一次性待整改基线；fixture中保留原因，旧失败完整归档，修订后原11节点全部通过。源处说明/量纲仍未整改，不把接受欠账说成修复。不能自行继续扩名单、手改目录、改生成器/断言，或复活双层注释格式门；合规/删除后原守卫仍要求移除对应欠账。

## 11. 集成、最终候选与双审分阶段

Wave3 并行阶段只提交 W16 授权改动与局部事实。W15 与 W16 均被总控收单并冻结同一 candidate 后，才运行最终矩阵、按累计代码变更阈值的全仓、严格门、完整源码封包和双审。总控独占 integration 分支与冻结引用，W16 不自行合并、推送、部署、发消息或关闭 Issue。

完整 candidate 的 SHA/tree、源码文件及归档 hash、diff、逐片交接、平台/真实模型证据、NOT_RUN 在仓外 manifest 中计算并绑定；manifest 不进入被 hash 的源码包。两位独立审核席分别为 Opus 5.5 max 与 GPT-6 Pro，审核同一 SHA/hash；作者不自签、不拼接不同候选的 PASS。非 PASS 交付 findings/workstreams/taskbooks 三件返工文件，先按 owner 重划互斥写集。

## 12. 来源与历史

- 输入：本轮 `workstreams.json`、`interface_contracts.json`、`sources/business_acceptance.json`、17 份任务书及 `next_round_audit_prompt.md`。
- Git 实测/局部门：仓外 `/private/tmp/my-agent-w16/integration_inventory.json`、`constants_gate_findings.json`、`focused.xml`、最终 `review.json`；它们是对应观察点的证据，不由文档文字授予任何运行权。
- 旧三套业务/缓存/Issue 活跃清单由本轮统一台账取代；旧文档正文保留为历史记录，不继承它们的已完成/未完成判定。
