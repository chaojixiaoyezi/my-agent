# COMPLETED

本文件不再保存历史流水。当前完成项以 git 历史和模块 `02-progress.md` 为准。

最近收口重点：

- 会话运行时 式当前 turn 引导已经接入：`/btw` 绑定精确当前执行，与 `/stop`、完成共用迁移锁和 active CAS；
  辅助回执无权消费 active-turn input。输入在模型成功接收后幂等写入唯一 thread transcript，compact 续轮用
  typed carrier 保留，不再复制成 task guidance/history。再次发布与 1.10 产物复验前不把 `/btw` 列为稳定完成。
- 前台 request 归档与 durable task 后台接管之间的窄窗不再等同“任务切换”：linked request 的 retired
  状态会继续核对同 thread 当前 TaskRun 并发布幂等 wake；mismatch 和不可读状态仍 fail-closed。该候选
  已有同任务交接接受与真实切换拒绝回归，待 1.10 发布复验。
- 普通用户正文统一收口到模型表达：除显式控制命令外，聊天、派工回执、进度和最终回复都由 LLM 根据
  结构化事实撰写；统一出口只按机器可判定的空正文、真实工具调用和 bracket/XML/native 内部协议拒绝，
  不再用中英文正则猜“完成”、ETA 或大小语义，也不使用固定“正在运行”兜底。任务是否完成只认 typed
  runtime event；开放进度和未聚合子代理不能提前关根任务。
- 1.10 MiniMax M2.7 两个 Feishu-scoped 合成用户最终实测通过：A 精确 5 子任务、`/btw` 后完成，B 精确
  4 子任务、`/stop` 后取消且重启不复活；迟到投递为 0，A/B 记忆口令隔离，最终 `failures=[]`。该证据
  是服务器侧真实 owner/channel/conversation 链，不等同真实 Feishu 客户端入站；37.3/50.7 秒首次自然
  回执延迟仍按产品事实页列为性能缺口。
- 普通任务的派工/等待/完成正文继续由 LLM 生成；回执短轮已剥离旧工具历史，最终完成新增不可变
  `delivery_snapshot`。与快照一致的原模型摘要直接保留；有冲突时才带 draft 进入修订短轮。系统不会
  回退到“任务正在处理”等模板；内部协议、真实工具调用或空正文会重试一次，仍不合格则抑制正文。
  ETA、完成与大小由结构化 facts 约束表达，但用户文字不反向裁决任务状态。
- 普通任务不依赖用户输入 `/goal` 才能自主派工。当前工作树已加入前台安全 quantum：精确绑定的
  Gateway/chat task 到安全点后以同一 task/workspace 转入耐久后台续作，释放普通聊天入口；后台可按任务
  结构继续自做或创建子代理，停止/终态任务不会被该续跑策略复活。该部分已随 `047e24f7` 部署并在 1.10
  观察到两个长任务按 quantum 让出、一个任务自主派 3 个子代理、另一个任务完成 47 项测试；并行聊天的
  第二执行器缺口与尚未部署候选仍以产品事实页为准。
- 后台 claim 的 0 心跳语义、90 秒默认 TTL、同进程域死 owner 立即接管和跨 Pod fail-safe 已闭环；
  Gateway SIGTERM/SIGINT 也进入 typed stop/drain/forensics 主链。真实 1.10 发布复验仍以产品事实页为准。

- 当前 owner home 路径成为唯一默认运行路径。
- 文件大小硬门已改成报告提示。
- 多个旧转发层和历史路径模块已删除。
- owner-scoped 前后台 shell 已改为 bwrap fail-closed，并由 worker/K8s 复用真实 readiness 自检。
- 默认一键安装已进入透明容器 CLI；工作树与 wheel/tar 发布干净度使用同一结构化检查器。
- P0 收敛已完成本地验收：产品事实页、根目录 pytest、配置同步、Ruff、真实 blocker/advisory 报告语义、MCP effect 硬门、sandbox fail-closed 与未跟踪运行数据检查均已闭环；发布状态仍以 `docs/PRODUCT_FACTS.md` 为准。
- P1 主链收敛已完成本地验收：完整 import 矩阵进入 CI，生产 wheel 剥离测试/offline harness，默认 gateway/正式入口/显式插件链收口，真实 embedding 工具检索、POSIX PTY、stdio LSP 与 OpenAI native tools 已接主链；真实生态与规模承诺仍按 `docs/PRODUCT_FACTS.md` 的部分可用/实验性边界描述。
- P2 scale 主链接线已在当前工作树完成：显式 fail-closed profile、PG/ASGI/RLS、Redis 共享准入、OTLP、独立在线迁移 Job、真实 Agent worker 和 continuous-monitor proof 机制均已接线并做本机真依赖 smoke；十万用户、目标集群灰度和 24 小时真实异构来源仍未证明。
- 普通 Feishu 对话与工作主链已收口：真实 chat/topic 多轮 transcript、跨会话隔离、同会话顺序执行、结构化任务选择/提升/完成、内置默认 prompt、USER 自主画像与 SOUL/AGENTS 卡片确认、首条消息不被密码 onboarding 吞掉、长任务异步可恢复回送均已落地；scale worker 复用同一执行链。
- 普通会话累计上下文已接入 owner/thread scope：复用现有 compact 阈值、token 估算和模型后端生成 thread summary，raw transcript 保留；旧聊天进入 owner-local `session_search` 索引。`/verbose off|on|full` 及 typed 工具进度复用持久化回送链，不重提任务。
- 多 IM 投递底座已在当前工作树收敛：普通最终回复、后台主动消息和显式 `send_message` 共用 `DeliveryService`；收件上下文与回复信封分离，adapter/capabilities/target validator 统一注册，第二个 fake IM 契约无需修改投递主流程即可接入。生产第二平台与正式部署复验仍按产品事实页标注。
- 普通会话即时控制已在当前工作树收敛：CLI/Feishu 共用 `/status`、`/btw` 和 `/stop`。`/btw` 作为当前
  active turn 的真实 UserTurn 在下一安全点进入模型并幂等写回同一 thread；`/stop` 中断当前 turn 与子代理
  但保留 transcript、workspace 和 memory。旧永久注入与 `/btw-clear` 已移除；真实 1.10 复验仍按产品事实页标注。
- 前台、后台和 compact 后续轮共用同一 owner/thread 的 summary + raw tail；task link、workspace、progress、
  wake 和子代理树只提供结构化运行事实，不再形成 task-scoped transcript、平行聊天历史或第二种 compact。
- 完成回复投影已保留结构化用户摘要：模型自然结束时写出的最终说明，以及显式验收提交里的测试结果、
  主要功能和限制，都会经过统一清洗后进入回复信封与会话历史；验收权威仍是结构化产物/工具事实，
  内部协议和宿主绝对路径不会外泄。
- root 部署的 owner 路径边界已收紧：宿主 home 危险根豁免只给无 owner scope 的本地管理员，远程 Feishu owner 保留 `/root` 拒绝边界，同时继续以精确 owner home 白名单访问自己的数据；Linux root 场景已加入确定性回归。
- 普通聊天和工作共用同一持续 thread；结构化任务工具只在真实工作开始时建立 workspace/进度记录，
  text-tool envelope 统一净化，自动监督 material-delta 去空转，子代理命令日志不进入普通 transcript。
- 运行故障事实已在当前工作树加固：shell 管道失败不能假绿，UNKNOWN_ERROR 保留原始报码和脱敏输入形状，
  Gateway 意外 watch 返回非零、清理 drain 不完整另记失败；persona replace/remove 使用精确 entry ID，
  临时密码/OTP 不进入 durable memory。
- Tool Gateway 已补齐已知门禁报码到统一错误 taxonomy：命令解析、owner/path 隔离、工具协议、限流熔断、
  审批绑定、幂等和管线配置不再因漏注册降级为 `UNKNOWN_ERROR`；防漏测试按真实强制管线及其直接子门扫描，
  动态 artifact-ref finding 先归一为稳定协议码。专项回归已通过，1.10 真机复验待当前长任务自然收口。
- 工具调用运行身份不再把后台主代理的临时轮次误认成子代理：只有线程级 subagent context 或显式
  `task_local` scope 才查询子代理 canonical ledger；主代理保留持久 `root_task_id`，真实子代理 load error
  仍原样进入结构化审计。`606fe20a` 已部署 1.10，两个全新 Feishu-scoped owner 的后台工具事件零假错。
- `raise_collaboration` 已接入工具 envelope 的持久 `root_task_id`，后台主代理无需让模型补传任务身份；
  协作域报码保留为 `reported_error_code`，运行时另用已注册的 parameter/arguments/execution 控制码，
  工具输出 artifact 与 index 也同时保存两层错误事实，不再把真实原因压成单独的 `UNKNOWN_ERROR`。
- 后台最终回复不再使用唤醒轮开始时间伪装成消息完成时间；会话消息仍按 append-only 顺序读取，thread
  活跃时间只允许向前，避免长任务完成后反而把会话排序回旧时间。
- 派工自然回执的模型可见 facts 已从 runner/recorded 等内部生命周期名投影为 planned/ready/started/
  failed-to-start 用户语义；运行权威账本不变，模型仍按真实数字自由组织回复，不靠关键词拦截或固定句子。
- 后台完成通知已在当前工作树分层：成功兄弟完成信号短窗合并，部分成功只内部整合，最终/失败/阻塞/
  需决策才写普通会话并外呼；后台续跑严格复用原 task link 的 goal/workspace/index 标题，不再被定时提示覆盖。
- 真机复验发现并修复 observation/wake 双写竞态：完成事件改为 wake-first 单入口，内部 wait/自动续推的
  运行中占位正文不再污染普通聊天；wake 失败仍保留 observation 兜底。
- 真机复验发现父任务 goal 仍可被 wait reason 覆盖；现已把 task identity 不变量下沉到 store 的原子
  bind 入口，保留首次 goal/workspace/created_at 并拒绝跨 thread 重绑。
- 真机双用户长任务复验暴露的批量派工与完成投递缺口已在当前工作树收口：同一模型轮里的多次
  `create_subagents` 按序全部执行并合并为一份事实回执，依赖前序结果的编排调用仍延后且返回专用错误码；
  模型臆造的 owner-home 交付路径在未获用户显式授权时归回当前任务 `output/`；后台自动续跑只有形成
  结构化最终交付时才进入普通会话，内部监督、等待和占位正文保持内部可见。
- 真机复验继续暴露“结构化完成与 findings delta 同轮出现”时完成信号被 delta 遮住；当前工作树改为
  结构化完成优先，并且 IM 回复信封只携带已投影的人话，不再把内部完成块、结论账标记或宿主路径交给
  adapter。这样既不恢复内部碎碎念，也不会在产物已验收后静默吞掉最终通知。
