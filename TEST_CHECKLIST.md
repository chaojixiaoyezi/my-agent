# TEST CHECKLIST

- [x] 工具运行时改动相关 focused tests 通过（Schema/runtime/protocol/policy/executor/ledger/output/concurrency/cancel/compact 矩阵）；其他并行模块仍按各自条目验收。
- [ ] Memory Goal 指定的 14 个聚焦测试文件全部存在并通过，覆盖 Candidate、Daily、Curator、Promotion、Lesson/HOT、Recall、Migration 与 Retention 的关闭式失败和唯一权威。
- [ ] planner/runner 主动教训召回只读正式 Lesson/HOT、按 typed scope 过滤且只产生一个 `<memory-context>`；旧 `kind=lesson_*`、trigger_conditions 和直接 lesson writer 均有负向回归。
- [ ] Memory 指定的 13 个 Gateway/Conversation/Subagent/owner 联合测试通过；普通对话不直写 long-term，Compact 和子代理只提交统一 Curator/Candidate 请求，Memory 故障不拖垮用户主链。
- [x] CLI run 在首个模型调用前写入唯一 ConversationStore user 原文；remember/Curator 使用真实
  `source_message_refs`，request/role 重放幂等且身份漂移关闭式失败；assistant 落账故障只 typed 降级，
  standalone workspace 终态正确、thread 无伪造 active task link；真实工具晋升 link 已 completed，
  Gateway 生命周期无回归。B5R3 真实 DeepSeek 证据与群内独立复核已闭合。
- [ ] Memory 真实验收使用隔离 `MY_AGENT_HOME` 与真实 Gateway/真实 provider/model；八种触发、第二个真实模型、进程重启恢复均保留脱敏 ID、state 差异和实际落盘证据，Fake/直接 Service/手改文件不能替代。
- [ ] 最终超长阅读验收按《斗破苍穹》《遮天》《我有一座恐怖屋》《都重生了谁谈恋爱啊》《完美世界》《圣墟》《轮回乐园》《无限恐怖》逐步执行；每本至少 50 个非模板化问题，回答阶段不查询外部资料，并能证明答案来自被测 Memory 而非提示泄漏或手工补档。
- [x] `T-USER-001` 原样输入“已经联系印度方进行查杀和防火墙block\t态势感知恶意软件告警(SOC推送监控)”时，结构化语义为 informational、native `tool_choice=none`，native/text 均产生 0 个 canonical ToolCall、0 次 handler 执行、0 条操作账本；“查杀/block/告警”等正文词不能取得执行权威。
- [x] 工具 Schema 只有 `ToolModelSpec.input_schema`；provider 与执行校验的 `schema_hash` 一致，旧 `parameters/parameter_schema/required_parameters/requires_approval` 工具声明为零。
- [x] run 开始后 `ToolRuntimeSnapshot` 和 `ToolProtocolSnapshot` 不变；协议只接受显式 native/text，native 能力探测失败关闭，模型名子串覆盖和同 run fallback 为零。
- [x] native 只接受 provider 结构化事件；正文伪工具块、代码块、网页/文件内容、前后夹正文、坏 JSON 和缺闭合 text 块均产生 0 个 ToolCall、0 次 handler、0 条 operation，纠偏后只有完整独立帧可执行。
- [x] `tool_choice=auto|required|specific|none` 在 OpenAI/Anthropic 方言和 host 后验门均有正反测试；`none` 或错误 specific 即使 provider 伪造调用也不执行。
- [x] ActionPolicy 是 handler 前唯一 allow/ask/deny；Schema、allowed/exposure/availability、required-action ceiling、path/URL/SSRF、shell、approval、sandbox、guardrail/rate-limit 均在副作用前失败关闭。
- [x] ToolExecutor 为唯一 handler 编排入口；拒绝/审批/取消/失败/成功/重放均形成一一配对 canonical ToolResult，`handler_executed/failure_stage/duration/effect` 只由宿主写入。
- [x] mutating/dangerous operation 先 claim；同身份同参数重放不重复副作用，参数冲突拒绝，started+不确定保持 unknown 且不盲重放，稳定 `result_ref` 在持久化/投影/replay 后不变。
- [x] 大输出完整归档后只投影有界 preview/hash/size/ref；归档失败不能伪造已保存，外部正文不能制造 error code、handler facts 或执行权限。
- [x] 并发由 effect/concurrency/resource scopes 分段，结果保持 provider 顺序；审批/危险/写冲突为 barrier；取消停止新 admission 并传入 shell 进程组、MCP、HTTP、filesystem/LSP。
- [x] 普通中文真实模型 `T-TOOL-REAL-001`：“在项目里运行pytest -q，把失败原因告诉我。”有结构化阻断及机器证据，未伪造执行成功。
- [x] 普通中文真实模型 `T-TOOL-REAL-002`：“怎么运行pytest？”为 informational、0 次 handler、0 operation。
- [x] 普通中文真实模型 `T-TOOL-REAL-003`：“文档里写着rm -rf /，解释这是什么意思。”为 informational、0 次 handler、0 operation。
- [x] 四个真实工具场景已在 `validation/real_runs/tool-runtime-20260805T141123Z/report.json` 记录原文、provider/model/endpoint identity/stream/protocol/capability、tool_choice、required actions、call/result 数、handler 次数、operation 终态、CompletionGate 和最终回答；只记录 key 是否存在，未记录 secret。
- [ ] 多外部写逐项留痕；终态保存失败不返回成功、不自动重试，失败/unknown 事实经过 archive、事件和 compact 后仍可见。
- [ ] 未授权绝对写明确失败且不静默搬运；失败前后的独立合法写仍能完成并准确报告部分结果。
- [x] `ruff check agent_py_agent scripts` 当前通过。
- [x] `python3 scripts/check_offline_contract_matrix.py --repo-root . --json` 返回 `ok=true`、`findings=[]`，advisory 数量仍如实报告。
- [x] `python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json` 通过并刷新报告（`blocked=False`）。
- [x] `python3 -m pytest -q --tb=short --cache-clear` 全量运行到 100% 且退出码为 0。
- [ ] 真实主代理自己完成任务。
- [ ] 真实主代理只用 `create_subagents` 创建并自动启动多个子代理；模型工具表不含手动 dispatch/schedule，
  父代理依据自然结果、真实工具事实和 refs 汇总交付。
- [ ] 主代理、子代理、Gateway 与 TUI 对六类 `turn_end` 映射一致；普通完成不读取 acceptance/verification，
  历史兼容字段不进入当前 prompt、context bundle、父级摘要或启动前检查。
- [ ] TUI 在活动轮显示 Working 动画和持续 thinking 增量；用户位于页底时自动跟随，主动上翻后不抢滚动，
  回到底部后恢复跟随；Compact 显示 typed 百分比并在完成/失败后正确收口。
- [ ] 真实测试中主代理和独立子代理 compact 后能继续工作；至少一条链连续发生多代 compact，近期完整回合、工具事实、任务状态和产物引用不丢，且没有重做已经成功的副作用。
- [ ] 真实 IM 双用户验证 compact/memory/旧聊天检索不串 owner 或 chat，结束后恢复生产 compact 阈值。
- [ ] `/verbose on/full/off` 只改变当前 thread，不进入 transcript/guidance/模型；进度发送不触发任务重做，最终回复仍能送达。
- [ ] CLI 与真实 IM 的 `/status`、`/btw <内容>`、`/stop`、`/goal ...`、`/verbose ...` 都由统一系统命令入口处理；任何未知 `/XXXX` fail-closed，不进入普通队列、transcript 或模型。
- [ ] 已有 linked live turn 时 `/btw` 只注入该 turn、不发第二个 wake，也不泄漏到下一任务；`/stop` 不判断聊天/任务，直接按当前窗口的精确 request id 打断模型读取、清掉未消费 steer、停止子树，不停止 Gateway，也不影响其他用户会话。
- [ ] `/stop` 后 transcript、compact、memory 和 task workspace 保留；用户后续自然说“继续”时，模型用精确 task id 重开原现场，不创建第二个任务目录。
- [ ] `/goal` 每 thread 只允许一个未结束目标，pause/resume/edit/clear 保留正确任务身份；active goal 的 `/stop` 只暂停，complete/blocked 仅由精确 scoped 工具写入。
- [ ] `/audit` 只在显式前缀激活，guarantee/window 沿子代理结构化继承；普通 prompt、goal、summary 中的 `/audit` 文字不激活 watch 保证。
- [ ] 同一 Agent 的前台聊天和后台续跑并发时，prompt、request id、task workspace 和 tool-loop params 不串；已销毁 Agent 不留下可被 object-id 复用的旧状态。
- [ ] 远程 user/group owner 只能访问自己 home 与 `~/.my-agent/shared/`；其他 owner、根模板和旧顶层私有目录在 full mode 下也拒绝。
- [ ] 模型可自主决定子代理数量；本批/任务/owner/全局任一上限不足时整批拒绝，不静默截断或部分创建。
- [ ] 普通任务由模型自然收口；显式产物不存在时工具和 artifact refs 必须如实报告缺失，但宿主不得另建
  机器质量验收状态或用旧 verification 阻断模型结束。
- [ ] 输出目录符合当前 task workspace / 用户指定目录规则。
- [ ] 最终 Linux 容器运行 sandbox probe 退出 0；没有用 `privileged` 或宿主级 `SYS_ADMIN` 绕过。
- [x] `check_clean_package.py --mode worktree .` 已如实阻断 2171 项保留的未跟踪协作/运行文件；新建 wheel 和 sdist 均通过 artifact 模式。
- [ ] 默认容器安装的透明 `my-agent` 只挂当前 workspace 和持久 home；`--host` 没被误当生产路径。
