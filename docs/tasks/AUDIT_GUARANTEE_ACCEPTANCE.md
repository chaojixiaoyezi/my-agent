# `/audit` 通用底座验收

本文件只记录当前产品契约和当前轮正式验收。早期 R7/R14 的固定 judge、分片 watch-lane
和直接模型 harness 已删除，历史数字不再代表当前产品入口。当前“一路一逻辑来源工作者”
只是来源消费权限边界，复用统一 Delegate Kernel，不恢复旧的专用调度路线。

## 1. 产品边界

`/audit` 是统一 Agent runtime 上的命名后台任务，不是固定工作流。

模型根据用户原始目标、当前上下文、可用工具和结构化运行事实自主决定：

- 怎样分析；
- 除来源工作者外是否继续委派；
- 调查、复核和汇总需要多少子代理以及各自角色；
- 是否复核或跨源查询；
- 何时、以什么方式汇报。

程序只强制执行：

- 工具 Schema、权限、owner 隔离和 sandbox；
- 完整记录边界；
- 游标、幂等、原子提交和持久队列；
- 租约、取消、恢复、并发、超时和资源上限；
- `ack_id`、`source_ref`、原文哈希、结论账、覆盖账和最终对账。

程序不得根据自然语言判断业务结果，不固定来源类型、评分阈值、调查子代理数量、角色、层级或执行顺序，
也不为 Audit 增加第二套 Agent loop、Memory、Compact、工具入口或子代理体系。唯一强制拓扑是：
每个实际绑定的 `watch_id` 有且只有一个专属逻辑来源工作者；数量由 watch 清单自动得出。

## 2. 数据与结论契约

1. 每条完整记录先进入 owner-scoped durable spool，落盘成功后才允许提交来源游标。
2. HTTP 游标含糊、不前进或分页结构不合法时 fail-closed；文件尾部未完成片段跨轮、跨重启保留。
3. 批次由等待时间、记录数或累计字节数任一条件触发，但不得从中间截断一条记录。
4. 普通记录完整交给 Agent。只有单条记录本身超过安全上下文时才创建明确标记的缩减视图；
   完整原文仍留盘并可由 `source_ref` 重新读取。
5. 默认结论结构为 `hit|clear|unsure`、总分、理由和原始引用。用户有自定义评分维度时，
   原样保留维度名称、范围、分数和理由；程序只校验结构、数值范围和记录对应关系。
6. `watch_stream(action=verdict)` 必须携带本批真实 `ack_id`。只有结论账原子写入成功后才签收；
   未提交、重复、跨批或未知 `ack_id` 不推进 ACK。
7. 每条记录可查回原文、来源位置、采集时间、字节数、哈希、模型、任务、结论、评分、理由、
   复核与汇报状态。
8. `coverage.audit_receipt` 的入队、已判、待判、丢弃必须可对账；覆盖闭合不等于模型判断准确。

## 3. Agent 与控制契约

- `worker_key = audit_id + watch_id`；每个 watch 幂等确保一个逻辑来源工作者。
- 来源工作者经统一 `create_subagents`/runner/Compact/取消/恢复主链运行，不使用专用 Agent loop。
- 来源工作者只能 pull 自己绑定的 watch，并只能对该 watch 提交首次 verdict。
- 来源工作者的 `read_file` 只能命中自己的 run 工作目录或当前来源显式文档；
  `read_artifact` 只能命中自己当前 run 的登记输出。兄弟来源、同 owner 其他资料均在 handler 前拒绝。
- 主 Agent、Audit 协调者和调查子代理不得 pull Audit 原始队列，也不得提交首次 verdict；
  它们可以看统计、inspect 指定原文、追加 review、创建调查和汇总。
- 工具执行入口必须校验结构化任务属性与当前 `run_id + attempt_id` 租约；只写 Prompt 不算隔离。
- 同一 `worker_key` 同时只能有一个有效租约；过期后可由新 attempt/run 接替，旧运行迟到提交被拒。
- runner 进程退出、整个独立宿主冻结，以及“模型/工具调用卡住但 durable heartbeat 仍新鲜”
  必须分别记录。冻结宿主只有在其全部活跃运行都是已停滞来源工作者时才允许进程组终止；
  模型/工具卡住则由有界 runner 工作片先封禁 attempt 并请求中断传输。活进程接替只允许精确
  限权的来源工作者使用，普通子代理仍保留防误杀保护。
- 来源工作者接替沿同一任务、watch、spool 和 verdict 账继续；累计 `runner_attempts` 不得把长期
  Audit 永久卡死。恢复原因、次数、最后废弃 attempt 和当前恢复状态必须可观测。
- Audit finding 账兼作耐久 outbox；finding 落盘后、协调事件发布前崩溃时，恢复轮必须沿同一
  `finding_id + revision` 补发一次统一会话事件，不能重复通知或要求重新判读。
- 容量或启动失败时，watch 保持采集并明确显示 `waiting_for_worker`/`degraded`，不能假装正常，
  也不能让主 Agent静默代消费。
- 统一监督器在宿主层恢复停滞 run 和补齐缺失来源工作者，不周期性唤醒主 Agent；只有 finding、
  故障/阻塞、来源工作者终态或用户指令等结构化事件才进入主 Agent。
- 最终边界事务提交后，收割者必须直接触发既有来源终态投影；窗口已结束且 ACK 已清账时，不依赖
  用户先发 `/status`，也不等待周期监督器才能把来源工作者标为 `DONE + VERIFIED`。仍有积压或
  新鲜 RUNNING attempt 时不得抢先完成，周期监督器继续作为失败恢复兜底。
- `/audit help` 只显示当前正式语法，不进入模型。
- `/audit <名称> prepare <内容>` 在准确 Audit 工作区完成讨论、样例阅读、探针和候选修改；
  名称区分大小写，下一条普通消息不继承 Audit 范围。
- prepare 的 `pending_prompt` 与当前生效要求分开；只有 prepare turn 可用的结构化发布入口能更新
  生效要求，测试失败或证据引用无效时保持旧配置。
- `/audit <时长> <名称> <任务>` 创建或启动命名 Audit；已 prepare 的名称复用稳定 `audit_id`，
  启动文字不能覆盖已发布要求。
- `/audit <名称> status` 显示准确名称、状态、生效要求、待处理内容、时长和来源概况，不显示内部
  ID、版本或绝对路径。
- `/audit <名称> clear` 精确停止对应 Audit，保留原始账、结论账和准备工作区。
- `/stop` 只中断当前前台生成、工具调用和本轮前台子任务，不停止命名 Audit/Goal，
  不等待模型确认，也不额外发送普通聊天回复。
- `/status` 只显示当前 owner 可见的命名任务、状态、运行时间和必要覆盖事实。
- Audit 运行时，普通聊天和其他任务继续使用同一会话能力，但不能继承 Audit 的原始记录或子代理状态。

## 4. 当前实现落点

| 模块 | 当前职责 |
|---|---|
| `conversation/control_commands.py`、`named_work.py` | typed 命令解析、命名任务身份和精确 clear |
| `gateway_parts/control_service.py`、`adapter/manager.py` | 前台即时中断、迟到回复丢弃、IM `/stop` 静默 |
| `ingestion/sources.py`、`puller.py` | HTTP/file 来源、完整记录、分页反馈与 fail-closed |
| `ingestion/harvester.py` | durable spool、稳定引用、inflight/ACK、结论账、覆盖账、审计读取 |
| `ingestion/source_worker.py` | `worker_key`、幂等创建、来源工具权限、租约和恢复适配 |
| `ingestion/watch_tool.py`、`watch_payloads.py` | 统一工具入口、批次投影、结构化 verdict/inspect/status |
| `ingestion/audit_state.py` | exact task 的来源、覆盖、积压和完成事实（不调度模型） |
| `ingestion/source_worker.py` | 来源工作者幂等创建、租约、宿主巡检、接替和 finding outbox |
| 统一 Agent runtime / Delegate Kernel | 会话、Memory、Compact、创建、启动、取消和故障恢复 |

已删除的无效路线包括：Audit 专用 judge、固定分片 watch-lane、固定来源类型/业务步骤，
以及绕过正式入口直接调用模型的旧验收脚本。当前按 watch 建立逻辑工作者不复活这些路线。

## 5. 正式验收顺序

### A. 本地确定性门禁

- Schema、权限、owner 隔离和 sandbox；
- HTTP 游标、快照、文件、多行片段、超大单条；
- ACK 幂等、重复提交、跨批提交、崩溃恢复；
- 原文、哈希、引用、结论账、覆盖账和汇报回执对账；
- 多命名 Audit/Goal、`/status`、`/stop`、精确 clear；
- 普通会话与后台 Audit 并发、统一 Compact。
- 1/5/7/9/13 路 watch 与逻辑来源工作者数量相等；
- 主 Agent/协调者 pull 或首次 verdict fail-closed；
- 工作者跨 watch、旧租约迟到提交和重复创建 fail-closed；
- 来源工作者读取未授权 owner 文件或兄弟 run artifact 时 fail-closed，且工具 handler 未执行；
- 容量不足、启动失败、runner 崩溃、宿主冻结、模型/工具调用卡死、租约过期、服务重启和接替
  可观测且不丢账；健康同宿主任务不得被停滞兄弟误杀；
- 卡死/崩溃后旧 attempt 不能继续调用来源工具，未 ACK 批次由新 attempt 原样重投；
- 正常工作片轮换与无进展卡死必须分账，前者不能增加连续 stall 计数；
- 普通子代理只有 PID 仍活着时不会因来源工作者的特殊接替规则被误杀；
- 多次正常工作片轮换不会被普通任务的累计 attempt 上限永久停止。

### B. CLI 10 分钟

- 五个 HTTP 来源和五个持续增长文件，共十路；每路 1 条/秒，共 6,000 条；
- 每路 3–4 条隐藏真样本；
- 本地模型只做基础通路，MiniMax-M2.7 做语义、长链和极端验证；
- 独立对账完整覆盖、重复、漏账、召回、误报、排空延迟和资源使用。

### C. CLI 60 分钟

- 五个 HTTP 来源和五个持续增长文件，共十路；每路 5 条/秒，共 180,000 条；
- 记录采集窗口、判读排空时间、最大积压、模型调用量、重启恢复与资源峰值；
- 输入结束时允许存在诚实 pending，但最终必须闭合或明确失败，不能静默丢弃。

### D. 24 小时连续运行与故障

- 多个命名 Audit 和多个来源连续运行满 24 小时，覆盖速率变化、突发、空闲、HTTP 错误和文件轮转；
- 覆盖来源工作者退出、模型调用卡住、Gateway 重启、MiniMax 临时不可用、本地模型接管与安全切回；
- 同时验证多轮 Compact、主 Agent 持续聊天和普通任务；
- 记录每路采集/已判/pending、最大积压与延迟、排空时间、模型调用与 token、恢复次数、重复漏账、
  finding/通知数量以及 CPU、内存和磁盘增长。

### E. 真实飞书双用户

- 两个真实用户从正式 Feishu 入站创建和管理命名 Audit；
- 同时聊天、布置其他任务、查询状态、前台 `/stop`、精确 clear；
- 检查 owner 产物、Persona、Memory、Skill、工具权限和原始记录不可互读；
- 检查子代理工具过程、内部协议和迟到回复不泄露到用户聊天。

## 6. 通过标准

只有同时具备以下证据才能宣布本轮完成：

- 正式入口而非测试旁路；
- 每条输入与 `source_ref`、原文哈希、结论账一一对应；
- 游标、ACK、重启与取消无重复、无静默漏账；
- 多用户和普通聊天隔离成立；
- 模型质量、吞吐、积压和排空延迟如实报告；
- 全量本地门禁通过；
- 同一干净 wheel 部署到 192.0.2.10 的唯一正式 Gateway/Feishu 服务；
- 文档、提交和远端 `main` 与已部署制品一致。

在上述 CLI 10 分钟、CLI 60 分钟、24 小时连续运行和真实飞书双用户验收全部完成前，
本文件不得写“生产级通过”。
