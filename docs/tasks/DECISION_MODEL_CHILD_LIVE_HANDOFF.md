# 子代理自动选模真实隔离验收交接

状态：本轮真实隔离验收完成。2026-09-22 本地日期（证据时间使用 UTC 2026-09-23）。本线不提交、不部署。
分支为 `codex/decision-model-integration`，子代理自动选模线负责；这次只新增/更新验收文档，没有生产代码修复。

## 范围和事实源

只运行 decision-model-plan 独立 worktree 与既有私有测试 home，使用单个 8431 Gateway 和独立 TUI 会话。
日常 8420 未操作，开始时监听 PID 为 2543。普通生成默认仍是官方 MiniMax-M2.7。
每个会话只给一次普通中文任务，未在 prompt、create 参数或 child thread 中人工指定模型，未代做子任务或修改执行状态。
初始三候选为官方 M2.7、官方 M3、OpenCode DeepSeek-V4-Flash；隔离候选列表经原设置 CAS 明确限定这三项。

私有只读 observer 包裹原 HTTP 发送入口，不改变请求、响应、后端类、探针或选择结果。
证据只含模型、真实 endpoint、payload 字段/大小/hash、工具 schema hash、消息结构、HTTP 状态和结构化 decision outcome；
不保存认证头、凭据、请求/响应消息正文。第二轮起同时记录原 decision 返回的合法选项 ID 与原 apply 产生的 typed advice。
原 canonical thread、原 run 状态和原 model_usage 账仍是采用与完成权威。

私有证据目录：`p4-child/20260923T052157Z/`。对应路径由主线持有，不把个人绝对路径写入仓库。

## 已完成的普通派工样本

| 样本 | 真实结果 | 证据边界 |
| --- | --- | --- |
| 三子代理审查并发队列、恢复协议、订单数据 | 三个 child 均 DONE，均 inherited M2.7，主任务 completed；决策 4 次、输入 58,060 | 含一次三题 child 选择请求，但第一版 observer 未保留非选择答案；不能断言具体 retain 原因，更不能计为异模通过 |
| 两子代理深入审查并发状态机与跨存储原子性 | Jev 第一题选 M3、第二题 retain_original；M3 与 M2.7 child 均 DONE、主任务 completed；决策 4 次、输入 56,886 | 这是三候选范围内真实自动采用 M3；没有用户逐 child 操作 |
| 两子代理设计图算法与精确计数 | Jev 两题均合法 retain_original，两个 child 均 DONE，主任务 completed；决策 4 次、输入 57,005 | 两个 child 实际沿 M2.7 执行，不能计作 DeepSeek 采用 |
| 候选限 DeepSeek，一子代理审查并发恢复 | Jev 合法选 DeepSeek，原探针 HTTP 200；advice 为 retained/selection_changed，child 用 M2.7 DONE，主任务 completed；决策 3 次、输入 36,829 | 4 秒预算；事后目录代次/设置一致，但未记录当时提交分支，不能归因于超时或锁竞争 |
| 新源码基线、原 4 秒预算重复普通审查 | 前置能力推荐 Jev 超时，child 选模 cooldown/connection_backoff、无候选请求；child 沿 M2.7 DONE，主任务 completed | 2 次 Jev HTTP；仅成功一次报告输入 17,611，另一次输入未知；不能用此轮解释第四轮原因 |
| 相同新基线、原 4 秒预算再次普通审查 | Jev 合法选 DeepSeek，宿主自动采用；child 单 attempt、9 工具轮后 DONE，主任务 completed；决策 3 次、输入 36,865 | 仅证明 DeepSeek 受限候选链，不证明三候选自然偏好；没有人工指定模型或增加期限 |

第二轮原创建 operation 为 `op:create_subagents:efa7f958f348681f`，父 thread 为 `thread-8a3720e14a9e40f4`。
M3 child 为 `subagent-1790141720-7b854fab`，原 attempt 为 `attempt-1790141721-f3f96352`：

- 原 Jev outcome 为 `success / may_apply=true`，合法 choice 指向官方 M3 的 profile；原 apply 与准备指纹匹配。
- canonical thread 为 `model_selection_source=automatic / revision=2`，advice 为 `adopted / first_request_validated`，
  first-request 为 `submitted`，创建 operation、child run/thread 和 attempt 连续一致。
- 验证记录为明确窗口 1,000,000、原输入估算 28,281、实际出站输出 cap 16,314；原 native probe 返回真实结构化工具调用。
  原估算不是供应商精确 tokenizer，不以窗口通过替代工具能力通过。
- 原 wire 共捕获 1 次 M3 probe 与 2 次 M3 业务请求，均到
  `https://api.minimaxi.com/anthropic/v1/messages`、HTTP 200。第二次业务请求含原 `tool_result` block。
- child 只有 1 个 attempt；原 usage 为 2 次业务调用、0 retry、输入 41,866、输出 4,408，模型仅 M3。
  最终 DONE、failure_type 为空；并行 M2.7 child 同样 DONE。

已固定证据 `m3-accepted-evidence.json`，SHA-256 为
`966fc3f70599bae494ade67f54477ac8f52a1b878207fb9503fb119f1d0e1fa4`。
该文件仅含上述结构与 hash，不含 provider 消息正文。

## DeepSeek 自动采用证据

第六轮原创建 operation 为 `op:create_subagents:276b53779972a9e2`，父 thread 为 `thread-d29c722614254d03`。
child 为 `subagent-1790143563-f9b62971`，attempt 为 `attempt-1790143564-156a4664`：

- 原合法建议、准备指纹、canonical pending 与同一 child/operation 关联，最终 revision 2 / source automatic /
  adopted / first_request_validated；首次发送标记 submitted，未从旧建议或空历史恢复资格。
- 原 native probe 真实成功；完整请求估算 29,766、实际输出 cap 16,314、明确窗口 1,000,000。
  只读原提交观测为 committed=true，进入提交剩 1.1288 秒、提交后剩 1.1042 秒；仍使用原 4 秒设置。
- 真实 endpoint 为 `https://opencode.ai/zen/go/v1/chat/completions`，1 次探针、10 次业务请求全部 HTTP 200，
  均有原 `x-opencode-session` 头（只记录存在性）。首业务 payload 132,035 字节、19 个工具；
  第二业务 payload 140,436 字节，包含两条 tool 结果及原 assistant reasoning_content，后续工具结果继续累积。
- canonical usage 为 10 次业务调用、0 retry、输入 385,744、输出 25,061、缓存输入 350,720，模型只有 DeepSeek。
  单 attempt 完成 9 个工具轮，最后自然返回最终响应、DONE、failure_type 为空；约 10 分 38 秒，
  比 M3 样本耗时更长，不能由成功推断同等延迟或质量。测试者没有修改任务状态、代执行任务命令或补交产物。

已固定 `deepseek-accepted-evidence.json`，SHA-256 为
`f661dc62a803a8a5b882b32f990212db816d4ea64138a12807b3be42dd98fff8`。
它与 M3 样本使用的共享 runtime 基线不同，分别记录源码 SHA；第六轮启动至停止，记录的六个核心文件 hash 均未变化。

## 测试部署问题与限制

首次私有 observer 遮蔽 Homebrew 的原 sitecustomize，导致 CLI 缺 packaging；恢复原解释器 bootstrap 后启动成功，
失败启动未发送模型请求。普通 `gateway ask` 文件客户端按 user owner 查找服务，未把请求投到 local-main；
改为原 Gateway 图形 TUI 后提交成功。没有通过改状态或另开 Gateway 绕过单实例合同。

第四轮经主线同意，仅在隔离设置把候选范围暂限 DeepSeek，仍允许合法保留继承 M2.7，不指定 child 模型。
这只验证受限候选链，不证明三候选自然偏好或默认策略。原设置 CAS 从 owner revision 9 前进到 10；
仅非秘密 overrides 的规范 JSON hash 从 `c3a0659333ecba1c83845f8143b9dee08a2b7f7c5bc999943570e16d198c98c3`
变为 `9d96ae5e370b1fde5549f4082cd36bbc6486975316b04a7a54c70f742493bee4`，记录为 `deepseek-settings.json`。
首四轮合计 15 次 Jev，原账输入 208,780；包含能力推荐和父后台接续，不等于 15 次 child 选择。

第四轮 OpenCode 原探针到 `https://opencode.ai/zen/go/v1/chat/completions`，带原会话头，收到响应头约 3.12 秒。
未发送 DeepSeek 业务请求。M2.7 child 三次业务调用、0 retry、输入 69,166、输出 5,443；
这是建议合法但安全保留的真实证据，不是异模验收通过。后续私有 observer 增加原提交分支和期限只读观测；
不重放旧 pending，不修改原返回值。第五轮重启前记录新共享 runtime 接缝 SHA，
该新基线与此前已运行样本分别归档，不把不同源码版本当同一对照。

第五轮实际 Jev 超时在私有 HTTP observer 记录为 InterruptedError，约 3.995 秒；
随后 child 原选模 outcome 为 cooldown/connection_backoff，没有待验证建议，也没有 DeepSeek 探针。
M2.7 child 单 attempt、7 次业务调用、0 retry 后 DONE。该轮证明冷却时自动沿原模型执行，未要求用户补资料或重选。
前五轮累计 17 次 Jev HTTP，已报告输入 226,391；另一次超时没有输入用量，不能按零输入解释。

六轮最终有 6 个主任务 completed、10 个 child DONE。本线新增 **20 次 Jev HTTP**，19 次报告输入共 **263,256**，
一次超时输入未知。前序主线已登记 27 次，协调累计为 **47 次**；原 wire 与 canonical decision 账的本线请求数一致。
除 Jev 外，observer 捕获的非探针请求为 M2.7 96 次、M3 2 次、DeepSeek 10 次，工具探针分别 13、1、2 次。
M2.7 数量包含父代理与后台接续，不能按 child 数量折算选模请求；这些均是私有样本请求，不是生产收益或延迟分布。

## 隔离环境与移交

私有 `environment-handoff.json` 保存准确 home/worktree 绝对路径；仓库不登记个人路径或凭据。
`TEST_HOME` 指主线已持有的 `decision-model-live` 私有根，原入口如下，在该 worktree 执行：

```bash
python3 -m agent_py_agent --config "$TEST_HOME/config/gateway-isolated.yaml" gateway start
python3 -m agent_py_agent --config "$TEST_HOME/config/tui-isolated.yaml" chat --gateway --gateway-timeout 600
python3 -m agent_py_agent --config "$TEST_HOME/config/gateway-isolated.yaml" gateway stop --timeout 20
```

Gateway 配置是 private local-main，TUI 是 `providers/local/users/decision-live-validation`；只允许单个 8431。
可复用 profile ID（不含凭据）如下：

| 用途 | profile ID |
| --- | --- |
| Jev 决策 | `57297b28-1558-477c-a516-1d92e8c3fd60` |
| 官方 M2.7 继承默认 | `535f5478-97e7-4e76-a2ff-6b79c4d9dfd6` |
| 官方 M3 | `45910aeb-2fa1-4b2e-afca-4d31a2e756ef` |
| OpenCode DeepSeek-V4-Flash | `6bf0febf-6425-4d03-95f4-6b74e315877b` |

唯一临时 owner 字段 `points.subagent_model.candidate_profile_ids` 已沿原 restore CAS 撤销，owner revision 10→11，
没有倒退 schema 或 generation。恢复后的 overrides 与开跑前逐值一致，规范 hash 同为
`93d529b3b6a9036532f27bea82d1894a4a1fd8e1f54c0d91fa15a56c8caca268`，默认仍是官方 M2.7。
隔离原配置中已有的 enabled/skill_tool apply/subagent_model apply 及其 4 秒设置原样保留；未开启日常配置。
原 CLI 已正常停止隔离 PID 35340，8431 无监听；日常 8420 仍是开跑前 PID 2543。
没有日常配置的前置 hash，故不声称做过其前后字节比较；测试没有操作该配置或日常进程。
状态和恢复事实见 `cleanup.json`、`settings-restored.json`、`usage-total.json`。

## 改动与验证

本轮仓库改动仅本交接及 `DECISION_MODEL_REAL_VALIDATION.md`，同时纳入主线提供的 P5-A/P5-C planning 真实结论。
新基线定向验证：`test_subagent_first_request_selection.py` 与 `test_model_scope_dependencies.py` **36 passed**；
相关 selector/thread/tests Ruff、doc-sync、`git diff --check` 通过。没有远端操作，未把线上 CI 当验收来源。

## Goal 勾选建议

按当前 Goal 字面，P2-B、12、13 都不建议整项提前勾选，可分别确认以下子片：

| 项目 | 本轮可确认 | 尚未覆盖的整项条件 |
| --- | --- | --- |
| P2-B | 文本/native 首请求的同对象创建、原工具探针、完整 payload/cap、目录/权限/设置 CAS、原发送资格及自动采用；M3/DeepSeek 单 owner 实际执行 | Goal 还明确包含模态核验。当前候选验证未单独证明视觉等模态能力或图片 token，首请求测试也没有该负例；不能把 payload ready/native probe 当成模态通过 |
| 12 | 两个已支持适配器的完整请求组包与真实输出 cap、未知保留及 fake 容量边界；真实工具历史没有丢失 | 仍包括 token 计数差异、图片/推理预留、Compact inspect/完整投影和稳定缓存整体。真实样本远未逼近 250K/1M，缓存 usage 不等于受控收益对照 |
| 13 | 普通中文 TUI 自动 M3、受限 DeepSeek 首轮/工具轮/DONE；Jev 超时冷却后继承 M2.7 完成 | P4-B 中文配置、跨模型多 owner、慢网/额度/取消/恢复/插件并行真实故障组合、质量和延迟分布仍待验 |

2026-09-25 更新：P2-B、第 12、13 项均已勾选。模态缺口由媒体集成 `319004926` 以保守规则补上（带图片等非文本内容时不换模型，并有反例测试），见 GOAL 复核说明。

模态边界的准确只读位置是 `subagent/model_selection.py::_candidate_request_input` 与
`tool_request_projection.py::project_tool_loop_request`：后者保留原 IR 图片/推理，但 ready 只证明材料完整和组包成功；
前者目前使用原 payload 估算与 native 探针，没有另一个明确候选模态证明。此处仅登记覆盖缺口，未扩改生产代码。
第四轮 selection_changed 未定因不否定其安全回退或第六轮独立成功，但不能充作期限/锁竞争任一精确故障门的验收。

建议下一步：移交已停止的 8431，让 P4-B 与主会话 Stage C 按上述配置串行做真实验收；
可并行只读核对模态事实来源与本线证据，再由单一 owner 补缺口。第四轮 selection_changed 的精确提交条件仍未知；完整故障矩阵与多 owner 质量不在本轮通过范围。
后续必须保持普通中文一次需求、自动采用或保留、原控制和 canonical 事实源，不把受限候选或手工补产物扩写为默认策略结论。
