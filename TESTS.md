# TESTS

## Compact 中断的不可见候选边界

- transcript、active-turn archive 与 native IR 都必须在慢摘要前后、候选改写前后、checkpoint 前后和
  generation CAS 前读取同一个 typed stop/cancellation callback；不能只取消外层 HTTP 流。
- 在 CAS 前停止时，断言 generation/cursor/失败计数不变，IR/tool-context 恢复原值，并只发布
  `superseded/candidate_discarded`；checkpoint 已写可以留下不可达候选，但 thread 不得引用它。
- 在 CAS 已成功后不回滚已提交代次。摘要、checkpoint 或 CAS 的真实异常仍记失败，不能把用户停止混入熔断。
- R154 本机 MiniMax-M2.7 真 TUI `ma-r154-local-compact-cancel` 在 active-turn 摘要 20% 时 Esc，观察到
  request/task interrupted、generation 4、failure 0，并在同一会话继续原工作。child/grandchild 仍需自然
  真机样本；focused 只覆盖竞态边界，不替代这两条最终验收。

## Computer Use 真 TUI 最小闭环

- 默认配置、普通 owner、WorkspaceOnly 与远程 owner 先走合同测试，必须证明不会启动 Computer Use MCP。
- 真机只在 local/main + Full Access + `my-agent[computer-use]` 环境启用；同一轮记录唯一 Gateway PID、MCP
  子进程数、MiniMax-M2.7 生效配置和准确 tmux 名称。
- 测试窗口使用隔离 Xvfb/xterm。普通中文 prompt 触发窗口枚举、OCR、激活、滚动、ASCII 输入、按键与后置 OCR，
  不允许测试者替被测 Agent 调工具或补目标应用状态。
- 截图/OCR、输入、点击和按键等 dangerous 动作必须走真实审批；滚动/切窗按 mutating 进入统一 operation 账；
  完成只认后置 OCR/应用状态，不认上游“Successfully”文案。
- 合同测试同时证明：普通 `catalog_category=mcp` 仍被默认渐进披露，而官方 `computer_use` 分类在初始
  provider Schema 中直接可见；目录分类不能降低 dangerous effect 或绕过 exact approval。
- 另做一次慢 OCR `/stop`，确认 typed cancellation 和 MCP/Gateway 存活；没有环境时明确记为环境缺口。

2026-09-01 R123 已在 `.10` 完成：`ma-r123-110-u376-computer-use` 真实调用窗口激活、截图、OCR、滚轮、
点击、ASCII 输入、按键、等待和后置 OCR；从屏幕读到 `SCROLL-R122-927`，目标窗口回显
`COMPUTER_USE_PASS R122`。`ma-r123-110-u377-computer-stop` 用确定性的 120 秒 `wait_milliseconds`
覆盖与慢 OCR 相同的 MCP client cancellation 通道，Running 时 `/stop` 立即变为“已中断”，随后 Gateway/MCP
均仍为单进程且 pending/processing 为 0。普通 owner `ma-r123-110-u378-computer-isolation` 没有任何
Computer Use 工具。热 OCR 太快，未用碰运气式时序冒充“慢 OCR 正好被撞中”。

## 2026-09-01 R121 Goal/思考与底层恢复边界（本地与真 TUI 通过）

- TUI focused 覆盖：Goal 无 active task 时仍投影；Goal 排在 child 前；`↓/Enter` 展开 exact objective 且不
  切换代理；`Ctrl+G` 收起；选中项始终滚入八行视窗；malformed Goal 丢弃；authoritative empty 才清除旧行。
- 控制入口覆盖：`/goal` 先写 durable outbox、再显示恰好一个用户样式命令；该显示不进入 prompt queue、
  active-turn steering 或第二次 dispatch。空输入时 Enter 只展开选中的 Goal。
- thinking 覆盖：live 块持续展开；completed/failed/interrupted 默认只显示 `Ctrl+O 展开` 摘要；detailed
  transcript 显示完整灰色正文，切换不改变 canonical block。
- 子代理插话覆盖：活跃轮原地收取；等待代理只预留一个 successor 并只启动一个 runner；并发/HTTP 重放不
  重复消息或 attempt；已消费 guidance 形成 exact reply obligation；终态 child 仍拒绝。
- provider/工具覆盖：typed stream idle timeout 进入配置化外层退避；工具归档只接受 exact applied approval，
  handler metadata 不能伪造批准；普通 task 不再注册/消费旧自动续跑，显式 active Goal continuation 保留。
- Goal 正文裁决新增回归：active Goal 的无工具正式回复会以 `thread_goal_progress` 进入 owner channel 与 canonical
  messages，但不额外登记下一轮；child 活跃时没有 guidance 的 wake 仍不轮询，用户显式 guidance 触发的真实
  Goal turn 则保留正式回复。会话运行时 对照路径为 `会话运行时-rs/ext/goal/src/runtime.rs::continue_if_idle`。
- Goal 工作区新增正反回归：exact active Goal 的 `task_path` 为空时，下一前台 turn 不产生 load error、继续
  投影同一 ThreadGoal 并能实际完成 `_run_gateway_ask`；sticky task 已配置非空路径而路径后来消失时，仍产生
  `gateway.conversation.workspace_task` load error。整个 `test_gateway_chat_conversation_context.py` 已通过。
- 本地三组所有受影响 focused pytest、整个 Gateway 会话上下文测试文件、触及产品文件 PyCompile、Ruff、
  doc sync、strict code-size、diff 与 clean-package 均通过。累计变更 4,769 行，低于 10k，未跑全仓 pytest。
- 最终 wheel 真机仍使用 `tmux attach -t ma-r121-110-u374-goal-thinking`：同一 active Goal 的后续消息成功，
  5 秒 thinking 自动折叠，assistant final 直接显示；raw canonical history 精确保存本轮 user/final。随后
  `Ctrl+O` 往返详细模式、`↓/Enter/Ctrl+G` 往返 Goal detail 均通过。唯一 Gateway、PID、模型和 wheel hash
  见 `STATUS.md`。

## 2026-08-31 R116/R117 provider 校准与后台 final canonical history

- 本地新增/更新 provider observation 回归，覆盖稳定指纹、动态消息不致失效、Compact generation CAS、缺失
  usage 清理、主/child exact thread 写入、保守 ratio floor 与 mismatch 回退。background final 回归使用真实
  `DeliveryService` 且没有 TUI adapter，要求 channel=`tui` 返回 `not_applicable` 仍只提交一条 final；相同 wake
  重试不重复，下一 `_gateway_conversation_history` 能读回正文。
- R116 真 TUI：
  - `tmux attach -t ma-r116-110-u323-background-context-calibration`：8 名 child 全 DONE、主 compact 0；立即
    追问 usage 为 input 8,061 / cache-read 32,418。
  - `tmux attach -t ma-r116-110-u324-four-wake-regression`：4 名 child 全 DONE，主上下文自然越 90%，只发生
    一次真实 Compact generation 0→1 并继续最终回复。
- R117 首次 `ma-r117-110-u325-background-final-continuity` 不计通过：Gateway 使用
  `/root/my-agent-src/.venv/bin/python`，其 editable finder 导入 schema v7，遮蔽已安装 wheel；raw messages
  仍缺 final。这一负样本要求部署除进程/端口/cwd 外还核对 Python executable、editable metadata、module source
  与 schema。
- R117b 真通过：`tmux attach -t ma-r117b-110-u326-final-history-proof`。两个 child 分别交付 marker
  `DNS-B731`/`SSE-C942` 的 421/629 行报告，main 写出 357 行 `final_history_proof.md` 并真实 Compact 一次。
  在发送追问前读取 raw thread，确认 schema v8、message_count=11、6 条 background commentary、恰好 1 条
  background final、notice 1 条；追问随后准确复述文件、marker 与行数。
- 最终提交 `efa90d1` wheel SHA-256 为
  `ce6b35facef781ac8f3c7f67cb38d7cddd5d9aac878ba7826ad06b135f5cff8e`，安装到独立
  `/root/.my-agent/runtime-venv-efa90d1`；离线核对 module 位于该 venv、schema v8、`tui=True`。顺序停止旧
  PID 4092080 并确认端口释放后，唯一 Gateway
  `ma-gateway-efa90d1-110-runtime-venv` / PID 4099186 在 8420 运行，MiniMax-M2.7。
- 默认 `command -v my-agent` 为 `/usr/local/bin/my-agent`；部署前它仍链接旧
  `/root/my-agent-src/.venv/bin/my-agent`。备份原 symlink 后已原子切到新 runtime venv，`readlink -f` 与
  `my-agent --help` 均验证成功；Gateway 和 CLI/TUI 不再使用两套 Python 环境。
- 部署后 TUI `ma-efa90d1-110-u327-deploy-final-smoke` 约 3 秒出首屏并自动派 2 名 child；报告实测
  245/198 行、marker 分别 12/14 次，main 整合 290 行。追问前 canonical messages 已有 4 条 background
  commentary + 1 条 `root_subagents_terminal` final、notice=1；随后禁止工具的追问准确召回全部事实。

## 2026-08-30 R114u/R114v Compact 公平让出与中性 service cwd

- 真实旧失败：`.10` `r114r-u305` 在一个后台 active turn 连续推进 13 代 Compact；旧代码在单 scheduler slice
  完成第 8 代后抛普通 RuntimeError，claim 被假记 failed，下一轮又从未消费 wake 继续。上下文最终没有丢，
  但日志、Working、policy failure 和恢复判断被污染。
- 新回归 `test_background_compact_slice_yields_after_eight_progressful_generations` 精确要求前 8 次每次 generation
  单调前进，第 8 次后只发 `_BackgroundCompactSliceYield`；
  `test_background_scheduler_treats_compact_slice_yield_as_clean_continuation` 要求 claim=`finished`、源 wake 保持
  未处理、无 policy failure。普通异常/无 generation 进展仍按原合同失败，不能借控制信号吞 bug。
- `test_gateway_commands.py` 同时锁定 systemd `WorkingDirectory` 与 launchd cwd 都使用
  `<MY_AGENT_HOME>/service-cwd`，而非测试/安装时 checkout；启动参数和 config 路径不变。
- 本轮全部变更测试文件已统一定向 pytest 通过。wheel
  `41f752dae9894e57ef5a38fe331a399598f99ff1cdc1616dad56ffffd11138a4` 部署后，唯一 Gateway
  `ma-gateway-r114u-110-wheel` 从 `/root`/site-packages 启动。重启前 GORM 任务有 7 名 live child；日志显示
  `running_reclaimed=7, orphans_revived=7`，原 roster 无重复并继续收敛。fresh 自然跨 8 代样本仍在长任务矩阵
  观察，不能只用单测或旧失败冒充真机通过。

## 2026-08-30 R114t pending cwd 与首批派工路径

- 真实失败基线：`.10` `ma-r114r-110-u305-sequential-long` 首轮 prompt 明示 owner home，首批 7 个 child
  goal 全部复制 `/root/.my-agent/owners/.../r114r-u305/bbb`；晋升后的 main `working_dir` 已是 canonical
  `tasks/.../gwreq-...`，child 因此得到 `WRITE_FORBIDDEN` 并反复申请能力。该样本证明安全门通过、放置语义失败。
- 本地回归覆盖 pending Workspace Context 不再含 owner 绝对根、只说明相对路径与首个工作工具固定 cwd；
  pending Main Context Bundle 的模型短投影不泄露 `primary_workspace_root/my_agent_home/context_bundle_json`
  的绝对宿主路径，但 canonical bundle payload 仍保留原值供内部恢复；`gwreq-*` 被识别为机器 ID。
- 命令：
  `python3 -m pytest agent_py_agent/tests/test_prompting_builder.py agent_py_agent/tests/test_main_context_bundle_contract.py agent_py_agent/tests/test_home_layout.py -q --tb=short`
  ，结果 94 passed；相关 Ruff 与 PyCompile 通过。
- 真 TUI gate：fresh owner 只输入一次多 child 长任务，首个模型 prompt 不得出现 owner 绝对根；首次
  `create_subagents` 的 goal 使用相对路径或 canonical task root，所有 child 的 cwd/写根/产物同根，路径拒绝
  与无意义 capability request 为 0；任务目录名不得直接等于 `gwreq-*`。
- 真 TUI 结果：`.10` `ma-r114t-110-u313-cwd-subagents-wheel` 的 6 名 child 首批 goal 使用相对路径，main 与
  child 均落同一可读 task root；owner-root 泄漏、`WRITE_FORBIDDEN` 和路径型 capability request 为 0。
  `bbb/` 真实生成 engine/enemies/levels/mario/ui/index.html，后续同 thread 再派 child 补 README；手动 Compact
  generation 1 后仍准确召回原路径和关卡事实。

## 2026-08-30 R114s Persona 写入限频 owner 隔离

- 真实失败样本：`.10` u306/u307 共用唯一 Gateway 并行运行。u306 模型先后发出三个合法
  `update_persona`，前两个写入成功，第三个“界面颜色偏好：深绿色”被拒；runtime operation 的
  `unknown_reason=effect_outcome_unknown:PERSONA_UPDATE_RATE_LIMITED`。u307 的写入发生在同一 30 秒窗口，
  证明旧进程级计数把不同 owner 串到同一个配额，并非模型没调用或文件路径越界。
- focused 新增两层覆盖：owner-a 连续三次写满额度后 owner-b 第一次仍成功；owner-a 第四次被拒且
  `effect_outcome=not_started`。同一结果再经过 canonical tool runtime，必须保留专用 code、failed 状态和
  明确未执行，不得升级为 UNKNOWN/DIRTY。错误 taxonomy 另断言可退避重试。
- 本地执行：

  ```bash
  python3 -m pytest agent_py_agent/tests/test_persona_tool.py \
    agent_py_agent/tests/test_agent_contracts.py::test_error_taxonomy_uses_explicit_codes_only \
    agent_py_agent/tests/test_recovery_code_policy.py \
    agent_py_agent/tests/test_tool_operation_idempotency.py::test_failed_result_is_replayed_exactly \
    -q --tb=short
  ```

  结果 38 项通过，相关 Ruff 通过。`.10` 唯一 Gateway 的
  `ma-r114s-110-u310-persona-a` / `u311-persona-b` 各完成三次真实写入，runtime.db 六条
  `update_persona` operation 全部 `SUCCEEDED`；两份 `USER.md` 各有且只有自己的三条事实。新的
  `u310-persona-recall` / `u311-persona-recall` 不调用读取工具，分别准确召回三项且无交叉，真 TUI gate 通过。

## 2026-08-30 R114r 跨工作片 active-turn Compact

- 真实失败样本：`.10` `ma-r114o-110-u299-pdf-skill` 在同一 request 累计 73 个主代理工具调用，archive index
  约 220k 字符、external outputs 约 420k bytes；`/context` 曾为约 104.3k/128k 且 `compact 0`，后续工作片
  又无 generation 地回落到约 66.9k。该样本用来区分“终态 fold”与“当前 turn 被暗中缩短”。
- 新增合同覆盖：真实 checkpoint/CAS 后只隐藏已提交的 exact source call，保留近期调用；手工追加但未被
  thread 指针引用的下一代孤儿候选不能隐藏任何记录；完整 archive 仍恢复全部 tool rounds 与 executed tools，
  model projection 不再包含旧正文。
- 前台 Gateway 与后台 main 各覆盖一次“provider overflow、transcript 暂无可压消息、active-turn archive
  推进 generation、同一 request 原地重试”的路径；第二次 `RunParams.carried_archive_tool_calls` 仍是完整列表，
  防止 Compact 重置一次性工具、预算或副作用事实。
- 当前 5 条新增定向用例与 PyCompile/Ruff 已通过。真 TUI gate：部署 `.10` 唯一 Gateway 后，用 fresh
  MiniMax-M2.7 长多子代理任务自然跨越多个工作片；必须看到 Compact 动画与 `compact 0 -> 1`，上下文降到
  恢复目标附近，Todo/child/插话/工具事实不重放，后续模型账继续有 cache-read，最终回复和产物完整。
- 真 TUI 结果：`ma-r114t-110-u314-architecture-research-long` 的 8 名 child 全部 DONE，main 跨 5 个后台工作
  片完成 367 行总报告并直接 final。Compact ledger 三代分别是 transcript `25,430→10,157`、live-tool
  `98,767→890`、live-tool `17,509→12,932`，屏幕精确显示 `compact 3`。八份分报告、直属终态、Todo、final
  都保留；其中 终端交互 只有 85 行且明确标记未找到代码，是被测任务内容缺口，不由测试者补写。
- provider usage 账合计 107 次物理调用：普通 input 487,739、cache-write 802,831、cache-read 3,172,988、
  output 76,826。只按用户给的输入价格计算，普通/缓存价 5/0.1 与 5/1 时分别比全按普通输入省约 69.66% 和
  56.87%；这些数字来自 `conversations/model_usage/*.jsonl`，不是 TUI Context 估算。

## 2026-08-30 R114q 后台记忆工具连续性

- 真实失败样本：`.10` `ma-r114o-110-u300-memory-a` 的首轮已在当前 owner 的 `USER.md` 写入“青玉7309”、
  “先结论后证据”和“偏好深绿色”；后台整合轮却因快照没有 `remember/update_persona`，尝试用 shell 读取受保护
  人格文件后错误汇报未保存。
- 定向合同：`test_subagent_lifecycle_continuation_keeps_owner_memory_tools` 断言 child 生命周期唤醒仍保留两项
  owner 记忆工具；整链用例再实例化真实 `SimpleAgent`，断言 policy、registry 冻结快照和 provider-visible
  schema 都包含 `skill_search/remember/update_persona`。后续用 fresh TUI 在等待 child 时插入一条新偏好，
  再由同一 owner 的新会话回读验证；若仍发生协议越界，runtime event 必须给出 exact 工具快照证据。
- 真 TUI 结果：`ma-r114t-110-u316-managed-service-skill-memory` 自然调用 Skill、创建 4 名 child，受管 Python
  服务实际监听 `0.0.0.0:18086`，根路径 HTTP 200；独立 tester 两分钟后写出复查报告。main 后台整合轮将三项
  长期偏好写入当前 owner `USER.md`，其它 owner 和 SOUL 未改；最终回复直接显示、Working 撤下。

## 2026-08-30 R114o TUI 首帧 readiness 竞态

- focused：`python3 -m pytest agent_py_agent/tests/test_tui_preflight.py agent_py_agent/tests/test_tui_runtime.py agent_py_agent/tests/test_tui_threading.py -q --tb=short`，45 条通过。
- 新增 deferred-loop 用例：模拟 Gateway 瞬时就绪但 UI loop 尚未消费回调，断言后台线程
  不直接启动 worker/刷新；loop drain 后再同一帧清除 connection block、启动 worker 并
  invalidate 一次。
- 真 TUI gate：唯一 Gateway 已经运行时新开多个 scoped owner TUI，不输入任何键，
  3–4 秒内必须自动显示 welcome/输入框，不得停在“正在启动交互界面”。
- 真 TUI 结果：`ma-r114o-110-u296-*` 至 `u303-*` 共 8 个 owner 同时 fresh 启动，完全
  不按键等待 5 秒后 8/8 自动显示 welcome 和输入框，真实验收通过。

## 2026-08-30 R114n Compact 恢复余量

- 真实失败样本来自 `ma-r114l-110-u295-compact-research-long`：八名 child 全部 DONE，主代理自然整合并 final，
  但约 119.4k→100.1k 后很快又到 115.4k，再压到 94.7k，最终共 `compact 3`。终态正确，成本和等待体验不佳。
- 本地新增/更新覆盖：默认恢复目标 60%；128k/90%/11.52k 尾部得到 76.8k；较低触发线仍以
  trigger-minus-tail 为上界；配置缺省、合法、非法和 25--80 边界；已完成前缀吃掉恢复余量时跳过 live summary，
  仍由 transcript Compact 处理；显式 80% fixture 继续覆盖 live 候选成功和过大摘要回滚。
- 定向命令：

  ```bash
  python3 -m pytest agent_py_agent/tests/test_memory_config.py \
    agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py \
    agent_py_agent/tests/test_gateway_conversation_compact.py \
    agent_py_agent/tests/test_background_main_agent_runtime.py -q --tb=short
  ```

  结果全部通过（既有 2 项 xfail 保留）。下一 wheel 必须用 MiniMax-M2.7 fresh 长 TUI 证明：首次 Compact 后
  provider-visible 输入不高于约 60%，同一普通大报告不会立刻生成下一代，任务/子代理/最终回复仍完整。
- R114t u314 真 TUI 的三代 checkpoint 分别替换 transcript 与两段不同 active-turn source；每代 source
  projection 都低于 recovery target，后续增长来自新的报告读取和整合输出，而非同一 90% 贴线候选反复提交。
  该样本通过恢复余量合同，但仍保留“最终新正文把只读 Context 推回 113k”的观察，不能把最终显示值当作
  Compact 后 wire input。

## 2026-08-30 R114m search_text 默认正则

- 对照 会话运行时 的 `rg`、终端交互 `GrepTool` 与 任务运行时 `grep`，`search_text` 在省略 `literal` 时改为
  ripgrep 正则；`literal=true` 仍提供精确普通文本搜索。focused 同时验证默认 alternation 命中、显式 literal
  空结果、rg/Python 两种 no-match envelope 和 schema 默认值。
- 本地执行：

  ```bash
  python3 -m pytest agent_py_agent/tests/test_tools/test_filesystem_tools.py \
    agent_py_agent/tests/test_core_tools_precise_schema.py -q --tb=short
  ruff check agent_py_agent/agent/tooling/_filesystem_search.py \
    agent_py_agent/agent/tooling/_filesystem_search_models.py \
    agent_py_agent/tests/test_tools/test_filesystem_tools.py \
    agent_py_agent/tests/test_core_tools_precise_schema.py
  ```

  58 项全部通过；下一 wheel 用真实 MiniMax-M2.7 TUI 复现原 `A|B` 场景。

## 2026-08-30 R114l 后台主代理 Compact 原地续跑

- 新增长 assistant tool-turn 回归：六轮工具调用各带 8,000 字符思考正文，完整 live handoff 生成后必须删除
  被覆盖的 assistant/tool/result 整轮，落同一 ConversationThread generation，并使完整 provider preflight
  回到 recovery target 以下；默认无摘要的 pair window 行为和 UserTurn 保留测试继续通过。
- 新增后台 main overflow 回归：首轮返回 `context_overflow` 且已经完成一个工具；外层必须强制 transcript
  Compact、把 context generation 从 0 刷到 1、携带该工具 archive 与相同 continuation injection，在同一
  scheduler slice 第二次调用成功。没有 generation 或结构化进展时仍有界失败，不能靠下一次 policy 空转。
- 本地执行：

  ```bash
  python3 -m pytest agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py -q --tb=short
  python3 -m pytest agent_py_agent/tests/test_background_main_agent_runtime.py -q --tb=short
  ruff check agent_py_agent/agent/conversation/runtime.py \
    agent_py_agent/agent/agent_core/tool_ir_history.py \
    agent_py_agent/agent/agent_core/tool_ir_compact.py \
    agent_py_agent/agent/agent_core/_tool_loop_service.py \
    agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py \
    agent_py_agent/tests/test_background_main_agent_runtime.py
  ```

  两个 focused 文件全部通过，相关 PyCompile、Ruff 与 `git diff --check` 通过。
- `.10` wheel SHA256 为 `e6f8402c9d094185061f48c89c8670054a058d9faa010b7713b05c9573876556`；单一
  Gateway `ma-gateway-r114l-110-wheel` 已验证只有一个 8420 listener、一个 Gateway 进程且模型配置仍为
  MiniMax-M2.7。fresh 长任务 TUI `ma-r114l-110-u295-compact-research-long` 已真实创建 8 名 child；其中
  任务运行时 child 已将约 124,049 token 压至约 53,711 token、generation 0→1，并保留 23 对工具调用的
  handoff 摘要和报告产物；终端交互 child 也自然 `compact 1`。最终八名 child 全部完成，主代理写出八份分报告
  和 21,876-byte 横向报告，final 直接显示且 Working 撤下。主链验收通过；主代理短时间 `compact 3` 已转入
  R114n 恢复余量修复，不把“终态正确”冒充“压缩成本合理”。

## 2026-08-30 R114 Gateway 重启、冷通知与 child 插话持久回放

- 本地 focused 同时覆盖：多页 owner 发现连续翻页、已死 in-process runner 精确回收、
  旧 active attempt 终态对账、child guidance 只在 provider 真正接收后写入 child thread，以及
  重连 TUI 没有本地 pending map 时仍能按 exact message id 回放普通用户行。
- `.10` 始终只有 `ma-gateway-r114g-110-wheel` 一个 8420 listener，MiniMax-M2.7。顺序重启后
  u285/u286/u287 仍保留原 run 名册；u287 的 7 个失联 runner 在后台精确 reclaim/revive，
  不到 25 秒八行全部恢复为正在等待模型/运行，没有重复创建 child。
- 已完成 u283 在 Gateway 重启后是 cold owner，旧轮询返回 0 notice，屏幕停在最后一个
  Thought/Tool；新读路只解析该 authenticated owner 已存在的会话库，不初始化 Agent。
  部署后原 TUI `ma-r114e-110-u283-mario-long` 不用 resume 即补出完整最终汇报。
- fresh `ma-r114g-110-u287-child-steer-replay` 一次原样八项目调研 prompt 创建 8 名 child。
  进入 researcher-7 后仅输入普通中文补充要求，界面先显示排队，provider 下一安全点
  消费后变成正常 user block。完全 `/exit` 再精确 resume 同一 session，以及后续再重启
  Gateway，进入同一 child 都能在原时间位置看到该消息，模型也真实改为至少读两个核心源文件。
- 部署验收另外抓到一个操作问题：在 `/root/my-agent-src` 里启动 `python -m` 会让旧源码
  压过已安装 wheel。真正验收必须从 `/root/.my-agent/service-cwd` 启动，并核对
  `module.__file__` 位于 venv `site-packages`；仅有 `pip install` 成功不能当作部署成功。
- u288 重启同时取得了“runtime 已 done、task 仍 RUNNING”的真实竞态样本，旧恢复把首批 6 名 child
  重开为 generation 2/3。新增参数化回归把完成、失败、取消三种 natural terminal event 与旧 task
  RUNNING/session-running 组合起来，断言监督只补 task/result/session/父级通知，不 abandon、不创建下一代、
  不调用 auto-start；既有 dead-running 测试继续证明 recovery-generated cancelled 会 requeue。
- R114j 真 TUI 复验要求：fresh 多 child 长任务运行中只重启唯一 Gateway，记录重启前后每个 run 的
  `current_attempt_generation/runner_attempts/status`；已自然终态 child 必须原值不动，真正运行中 child 才允许
  按原 run 恢复。该项完成前不把本轮升级为发布通过。
- R114j 已取得上述正证：u288 九名终态 child 在再次重启后 generation 不变；u289/u290 的 live child 发生
  原 run 恢复。新 R114k focused 构造同 owner 的两条真实工具索引和一条 foreign request，断言 recovery 首次
  provider 参数只携带 exact request 的 `task_progress/create_subagents`，顺序、Todo id 与 covers 保留；伪造
  marker request id 不匹配时恢复为空。startup requeue 另断言 typed marker 携带 dead attempt id。
- R114k 发布后的真 TUI 必须在主代理已经建立 Todo 并派出多 child 后重启唯一 Gateway；恢复后 canonical Todo
  总数和 id 集合不得变化，不得出现第二次同义 `task_progress create` 或取消/重派已有 child。运行中的 child
  可以按原 run 换代，终态 child 仍不得复活；模型应直接继续等待、整合或执行剩余工作。

## 2026-08-30 R109 child typed lifecycle 与首次详情视口

- 对照 会话运行时 `core/src/agent/status.rs`、`tui/src/app/agent_status_feed.rs`、`tui/src/multi_agents.rs`，状态只由
  `TurnStarted/Complete/Aborted/Error` 等 typed 事件推进；对照 终端交互 `REPL.tsx` 与 `Spinner.tsx`，动画只
  跟真实 loading/running 事实，不从自然语言猜状态。
- 本地回归覆盖：`queued/starting/waiting_first_event/running/waiting_descendants/terminal` 标签、终态撤下
  Working、goal 成为 child 首个 user block、普通/modal 两个视口首次从顶部打开、主/子页面回切恢复独立
  锚点。命令为 `python3 -m pytest agent_py_agent/tests/test_tui_view.py
  agent_py_agent/tests/test_tui_agent_navigation.py agent_py_agent/tests/test_tui_prompt_toolkit_pipe.py
  agent_py_agent/tests/test_tui_renderer.py -q --tb=short`，结果 87 passed。
- 真 TUI 使用 `.10` 唯一 Gateway、MiniMax-M2.7 与原样八项目调研提示；旧客户端
  `ma-r109-110-u272-subagent-phases` 已取得 8 个排队到运行、3 个终态、5 个继续运行的阶段证据。首次详情
  失败样本证明后端 goal/69 条事件均完整，问题仅在首次物理视口贴尾。修复发布后必须换 fresh TUI，直接
  Enter child 即看到完整提示词开头；不能用 `Ctrl+Home` 人工补救冒充修复通过。
- 修复后关闭旧客户端但保留 canonical session/后台任务，只同步两个 TUI 客户端文件，不重启 Gateway；
  fresh resume 客户端 `ma-a166c7d-110-u272-child-first-view` 首次 Enter 即显示完整 轻量运行时 goal 与开头 thinking。
  两次 PageDown 后 `Ctrl+G` 返回再 Enter，视口保持原位置；child 在观察期间终态后 footer 改为“已结束，只读”。
  最后 8/8 child 终态，轻量运行时/deepseek-harness 各 `compact 1`，main 从等待自动恢复、写出 8 份分报告和 1 份
  `横向对比报告.md`（16,651 bytes），自然 final 直接显示，终态无 Working。测试机始终只有 PID `3699956`
  一个 Gateway 和一个 8420 listener。

## 2026-08-30 R108 收尾回归与真 TUI Compact

- 唯一全仓诊断跑完后得到 11 个 failure；之后只按来源定向复测，没有再跑全仓。
  精确 11 项复测结果为 `10 passed, 1 xfailed`；xfail 是用户明确要求延后的 `AUDIT-02`。
- 相邻回归文件覆盖 error taxonomy、Gateway 鉴权/会话控制、observation route、subagent
  bundle/kernel/protocol、ToolExecutor 和 managed background，共 176 项全过。
- 静态/发布门：Ruff PASS，`DOC_SYNC_PASS`，strict code-size `hard=0`，import-boundary
  `findings=0`，`git diff --check` PASS，temp-index clean-package PASS。
- 真 TUI：唯一 `ma-gateway-r108-110` + MiniMax-M2.7；客户端
  `ma-matrix-r108-110-u271-compact-errors` 中先完成普通回合，再输入 `/compact`。界面显示
  `正在压缩上下文` 进度条，结果为 `generation 1` / `11,595 → 9,186 tokens`，旧历史仍在；
  最后 `/exit` 正常关闭 TUI，8420 仍只有 PID `3694064` 一个 Gateway listener。

## 2026-08-30 R106/R107 单 Gateway 真实 TUI 矩阵

所有输入均从 tmux 中的真实 TUI 发送普通中文；直接读取 JSON/文件/进程只用于核对模型汇报，不能替代产品
验收。唯一 Gateway `ma-gateway-r106-110` 使用 MiniMax-M2.7；u256--u269 分属独立 owner：

1. `/sessions` 只列当前 owner 并给精确 resume；WorkspaceOnly Shell 成功/失败都说明隔离视图。
2. `list_agents` 只读 schema、Todo 与 4 child 精确联动、Goal create/get/update/complete。
3. Schedule/Watch 完成 create/pause/resume/history/pull/delete/close；删除保留 typed tombstone，watch 保留 closed
   审计记录，但 `current_run/next_run` 和后台 harvester 均不得继续活跃。
4. capability child 从只读启动，申请 exact 单文件写权，下一 context snapshot 自行写出报告；详情页可查看
   prompt、thinking、工具、final，`Ctrl+O` 不切 root，折叠后 `Ctrl+G` 返回。
5. Skill 只选择并读取 2 个真实 `SKILL.md`；Memory 分开验证 USER 偏好与 formal personal fact，同 owner 新会话
   召回、另一 owner 同时返回空；普通 Memory 不修改 SOUL，也不要求确认。
6. `search_text` 验精确/不存在/大小写/大量结果 offset，配合 `read_artifact` 与分块读取核对首中尾文件。
7. 长任务至少触发三次真实 Compact，要求每次显示进度动画、generation 单调增加、上下文回落后仍沿同一
   task/owner 继续，最终客观核对 25 份输入和 400 行汇总。
8. 递归 child 必须让 coordinator 创建至少两名孙代理；每个 `task_workspace_dir`、产物与状态路径都在同一
   根任务下，父级直接读取声明产物，Gateway 不得再出现 `outside its owner task`。R106 取得失败样本；R107
   fresh TUI 已验证 coordinator 与两名 depth-2 child 共用同一 canonical task root，父级可读三份产物且日志
   零路径拒绝。

资源采样必须同时列 exact 本轮 TUI PSS、全部 TUI PSS、Gateway PSS、runner inflight 与系统 available；结束
后再采一遍空闲高水位。`.7` 只有恢复 SSH 后才能做 8GiB 同负载对照，不能拿历史值代替。

本轮结果：R106 u256--u269 的 14 个场景均已取得终态或明确失败证据；长上下文用例真实完成 25 份输入、
422 行总报告和 7 次 Compact。R107 `ma-matrix-r107-110-u270-nested-root` 另取得递归修复正证：Python/Go/
coordinator summary 分别为 363/331/122 行，三个 agent state 的 `task_workspace_dir` 完全一致。R107 切换前
远端 stage 上 workspace/write-boundary focused 全过；真实 TUI 仍是产品通过证据，focused 不替代它。

## 2026-08-30 R105 多用户 TUI 功能与资源矩阵

所有产品结论均从真实 TUI 普通中文输入取得；focused tests 只作开发护栏。单机保持一个 Gateway，每个用例
使用不同 owner，启动前公开 tmux 名称。当前 `.10` 会话为 `ma-matrix-r105-110-u246-*` 至 `u255-*`：

1. Skill：搜索索引、按需读取 2 个正文、生成审计报告；不逐个测试可增删 Skill 内容。
2. 权限：自己工作区写入、外部 HTTPS、不可见宿主路径和宿主负证据；成功与失败都核对结构化 scope。
3. Memory/工作区：三个唯一事实写入后精确搜索；另一个 owner 不可见；任务目录保持 input/work/output 规划。
4. 子代理控制：4 child 运行时给 exact child 插话、取消另一个、其余继续；父代理立即收到用户消息，不等待
   全体 child。取消 child 后父代理可为未覆盖职责另派新 child，但旧 attempt 必须保持 CANCELLED。
5. PTY/进程：真实审批、交互 Python、后台 HTTP 启停、端口残留核对；工具账本另覆盖大文件分块、搜索、
   补丁、故意错误恢复、网络和 managed background。
6. 会话与 Compact：`/status`、`/context`、`/effort`、多阶段连续任务；R106 另以 fresh TUI 验 `/sessions` 和
   WorkspaceOnly 成功回执，不能用本地测试替代。

本地候选定向命令：

```bash
python3 -m pytest agent_py_agent/tests/test_sandbox.py -q --tb=short
python3 -m pytest agent_py_agent/tests/test_chat_control_runtime.py agent_py_agent/tests/test_tui_input.py -q --tb=short
```

前者 26 passed，后者 72 项通过。累计改动超过 10,000 行，本轮收尾允许且只执行一次全仓 pytest；在此之前
继续 focused，避免重复浪费。

## 2026-08-29 exact child 停止与远程工具清单真 TUI 验收

本轮必须同时覆盖“用户看到什么”和“底层最后写成什么”，不能只用 HTTP 200 或模型口头说明判定通过：

1. 在同一 Gateway 创建至少三名直属 child，分别用详情页 Esc 和主代理自然语言触发
   `cancel_subagents`；核对 exact run/attempt 被 signal，兄弟状态不变。
2. 在停止受理后继续观察 canonical store，确认 `accepted` 最终进入 `CANCELLED`，旧 runner snapshot 与
   heartbeat 不能把终态写回 RUNNING；重复 stop 必须复用同一受理事实。
3. 让普通远程 user 用自然语言明确“只调用一次 `list_tools`”；核对工具结果、模型列举、provider Schema
   都来自同一 `ToolRuntimeSnapshot`。清单不能为空，也不能出现 root-only、内部或当前环境 unavailable 工具。
4. 每次真机只保留一个 Gateway，并同时核对 TUI 顶栏、provider usage 和 listener，而不是用不存在的
   `/health` JSON 合同猜服务是否正常。

真机记录：r56 TUI `ma-fullcov-r56-stop-ack-retest`，child
`subagent-1787994403-6708c63b`，accepted 约 6.06 秒、canonical terminal 约 9.62 秒；r57 TUI
`ma-fullcov-r57-list-tools`，request `gwreq-1787995416-c5e8b37d4bfe41cbbf03eda6bf63eedd`，一次
`list_tools` 返回 30 visible/30 executable。r57 两次物理 MiniMax 调用约 112 秒，慢点在模型请求，不在
清单 handler；该样本有 cache creation 28,990、cache read 18,667、output 500。

定向回归：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_gateway_agent_control_service.py \
  agent_py_agent/tests/test_orchestration_cancel_subagents_tool.py \
  agent_py_agent/tests/test_runner_session_pool.py \
  agent_py_agent/tests/test_chat_client_context.py \
  agent_py_agent/tests/test_tool_manifest_contract.py \
  agent_py_agent/tests/test_tool_runtime_scope.py \
  agent_py_agent/tests/test_tool_progressive_disclosure.py \
  -q --tb=short
```

结果为 74 passed；测试机单独运行 manifest/runtime-scope 为 12 passed。真实 IM、fresh capability 审批链、
active Audit、Windows PTY、macOS 右键与浏览器依赖缺失场景继续记未覆盖，不能由上述 focused 冒充通过。

## 2026-08-29 单 Gateway 双用户隔离与 Shell 可见范围

多用户回归不能只检查文件工具。至少两个不同 owner 的真实 TUI 必须同时连接同一个 Gateway，分别验证：
自己的文件写/读/改、自己的 `run_command` 写/读、文件工具跨 owner 读写在 handler 前拒绝、跨 owner
`working_dir` 在 handler 前拒绝，以及命令文本内嵌另一个 owner 的绝对路径时由 OS 沙箱隐藏且没有宿主副作用。
最后一种不能解析任意 shell 文本猜权限；它必须返回 `owner_workspace_only`、
`external_host_paths_hidden=true`、`host_path_absence_proven=false`，防模型把沙箱内 `ENOENT` 说成宿主路径不存在。

`run_command` 已返回最终退出码，模型工具说明禁止追加 `; echo $?` 或其他恒成功后缀遮蔽前序失败。测试仍需
保留这个反例：若供应商自行遮蔽，底座只能诚实记录最终 shell 退出码，不能从 stdout 文本反推副作用；下一
fresh 回合应使用未遮蔽命令得到 `COMMAND_FAILED`，operation ledger 为 failed，目标文件不存在。

```bash
python3 -m pytest \
  agent_py_agent/tests/test_sandbox.py \
  agent_py_agent/tests/test_path_access_owner_scope.py \
  agent_py_agent/tests/test_main_owner_scope_default.py \
  agent_py_agent/tests/test_remote_owner_workspace_scope.py \
  -q --tb=short
```

本地与 `.7` 同组均为 49 项通过。真实 TUI 为 `ma-fullcov-r48-u10-isolation`、
`ma-fullcov-r48-u11-isolation`，唯一 Gateway `ma-gateway-fullcov-r38` 使用 MiniMax-M2.7；最终 request
`gwreq-1787985268-fd590c551b054c53afb1947caee696a0` 返回 `COMMAND_FAILED/return_code=1`，模型逐项
说明三项 sandbox scope，宿主核对没有 `cross-shell-u10-r49b.txt`。

## 2026-08-28 旧验收链与无副作用 package import 退役

开发护栏覆盖两组事实：fresh runtime.db 不再创建 `tasks.status`、`task_runs.current_contract_id`、
`acceptance_contracts` 或 `validator_operations`，repository 也不再暴露 `closeout_task_run`；同时任意先导入
`conversation.agent_thread` 都不能因 `agent_core/__init__.py` 提前装载 subagent runtime 而循环失败。存量库
旧列/表不做破坏性 DROP，仍需由真实升级样本证明新代码完全不读写它们。

```bash
python3 -m pytest \
  agent_py_agent/tests/test_runtime_db_main_chain.py \
  agent_py_agent/tests/test_runtime_db_delivery.py \
  agent_py_agent/tests/test_runtime_db.py \
  agent_py_agent/tests/test_runtime_db_operations.py \
  agent_py_agent/tests/test_runtime_db_recover_stale.py \
  agent_py_agent/tests/test_subagent_lifecycle_service.py \
  agent_py_agent/tests/test_agent/test_subagent_lifecycle.py \
  agent_py_agent/tests/test_conversation_agent_activity.py \
  agent_py_agent/tests/test_conversation_store.py \
  agent_py_agent/tests/test_gateway_agent_control_service.py \
  agent_py_agent/tests/test_subagent_persistence_service.py \
  agent_py_agent/tests/test_subagent_runtime_compact.py \
  -q --tb=short
```

这些 pytest/静态检查只算开发护栏，不算产品验收。最终最低标准是：部署到测试机唯一 Gateway，模型固定
MiniMax-M2.7，以提前公开名称的 fresh tmux 启动真实 TUI，通过普通中文提示完成一次工具/子代理/自然终稿
链路，并确认没有第二套 acceptance 硬门、循环导入或退出异常。

### 2026-08-29 零调用旧判官清理补充

8 份历史 Gateway coverage 只用来找候选，不能把 Feishu、WeCom、Windows、迁移或管理 CLI 的 TUI 0%
直接判死。删除必须再满足：全仓生产 import/动态注册检索为零、只有模块自己的测试引用、现行 typed 主链已有
唯一替代。按此标准删除旧 `acceptance_contract`、`target_coverage_ledger`、`subagent_outputs`、
`recovery_batches`、`progress_fingerprint` 和旧 `dispatch/progress_payload`，并同步删除只验证这些孤立实现的测试。

验证顺序：先跑受影响的 contract/dispatch/import focused tests，再跑一次全仓 pytest；随后执行 Ruff、文档同步、
strict code-size、diff 与 clean-package。最终仍必须在 `.7` 的唯一 Gateway、MiniMax-M2.7、提前公开 tmux 名称的
真实 TUI 长任务中验证八路 child、Compact、主代理整合和自然终稿；coverage 只证明执行到哪里，不替代 TUI。

## 2026-08-28 WorkspaceOnly/Full Access 与 Compact 恢复余量

权限回归必须覆盖：默认本地/远程 owner home、显式 local/main Full、远程 Full 降权、owner 策略只收窄、
控制面只读、Full 父级 child 恢复 owner wall、子代理保留明确项目根、request-local Shell/PTY/网络 handler，
以及 WorkspaceOnly 外部 cwd 拒绝。文件墙与网络开关分离。

Compact 回归必须覆盖：live/transcript 都低于同一 recovery target；每次尝试有独立 operation id；live 失败后
后备进度仍显示；child transcript 收到 start/progress/completed；手动 `/compact` 入 outbox 后保持活动块，
真实回执才收口。普通无摘要窗口必须保留最新工具对；完整替代摘要已覆盖本轮工具历史时，必须允许回收
最后一对巨型回执，并证明 checkpoint/generation 成功、provider IR 无孤儿且不再立即触发 overflow。摘要
transport 异常必须恢复原 IR 并累加失败熔断；供应商正常完成但 text 为空时，live/transcript 都必须只消费
这一次模型调用，用 typed IR/raw transcript + operation evidence 生成有界机械续接摘要并推进有效 generation。
候选完成计量后仍高于 recovery target 时，必须恢复原 IR、保持 failure count 不变并发布
`superseded/candidate_discarded`；TUI 要静默撤下该块，不得冻结红色失败，随后同代 transcript operation 必须
仍能启动。真正的摘要 transport、checkpoint 与 CAS 异常继续发布 `failed`。live 摘要还必须通过
`compact-live-handoff.v1` 固定结构校验：缺字段、只有“下一步”短句，或包含供应商私有工具协议/XML 调用时，
只能退回 typed IR 的 `compact-mechanical-fallback.v1`；伪工具正文不得执行、不得进入下一轮续接。明确只读、
不要修改或不要落盘的研究任务必须在对话中交付，不能被通用“研究要写报告”软提示反向诱导创建文件。

```bash
python3 -m pytest \
  agent_py_agent/tests/test_main_owner_scope_default.py \
  agent_py_agent/tests/test_remote_owner_workspace_scope.py \
  agent_py_agent/tests/test_path_access_owner_scope.py \
  agent_py_agent/tests/test_credential_file_denylist.py \
  agent_py_agent/tests/test_chat_client_context.py \
  agent_py_agent/tests/test_gateway_chat_conversation_context.py \
  agent_py_agent/tests/test_runtime_gate_ledger.py \
  agent_py_agent/tests/test_sandbox.py \
  agent_py_agent/tests/test_attempt_sandbox.py \
  agent_py_agent/tests/test_tool_gateway_contract.py \
  -q --tb=short

python3 -m pytest \
  agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py \
  agent_py_agent/tests/test_compact_semantic_summary.py \
  agent_py_agent/tests/test_gateway_conversation_compact.py \
  agent_py_agent/tests/test_gateway_streaming.py \
  agent_py_agent/tests/test_tui_runtime.py \
  agent_py_agent/tests/test_tui_input.py \
  agent_py_agent/tests/test_background_notice_display.py \
  agent_py_agent/tests/test_subagent_runtime_compact.py \
  -q --tb=short
```

2026-08-29 新增两类真实反例：MiniMax-M2.7 在 live Compact 中返回了
`<minimax:tool_call><invoke name="write_file">...`，以及只返回“数据已核对，写入报告”的半句动作。focused
回归要求两者都使用 typed fallback，保留当前任务、用户只读限制、真实 call id/result，并排除伪路径和伪工具
标签。真实 TUI 复验还要核对 Compact 后不重读同一批文件、不创建被禁止的报告、最终正文直接可见。

上述两组本地均通过；本轮权限与 Compact/TUI 两组 focused 共 536 项通过、9 项按平台跳过。生产与测试改动
少于 10,000 行，按项目约定
不跑全仓 pytest。真机只使用 `.7` 唯一 Gateway 与 MiniMax-M2.7，启动 fresh TUI 前必须先报告 tmux 名称和
观察命令。当前真机官方超级玛丽任务证明：启动 cwd 不越过 owner home、5/5 child 自然完成、主 Compact
`26,496 → 8,912` 且 child generation 达到 2、最终正文直显；最后一对巨型回执回收另由 exact focused
回归锁定，避免用旁路修改被测产物来伪造通过。

`f6d58c0` 发布后，fresh `.7` TUI `ma-f6d58c0-compact-superseded-r3` 再次运行官方超级玛丽提示词：8/8 child
自然 DONE、Todo 10/10、最终正文直显。live 候选在 20% 后以 superseded 静默退出、没有红色失败；后续真实
Compact 提交 generation 1，canonical failure count 0、source tool pairs 8，且模型在 Context 回落后仍保留
完整任务、child 文件和验证下一步。单 Gateway、MiniMax-M2.7、owner home 启动边界均再次通过。

## 2026-08-28 后台权威工作片 Compact、notice 与有界 handoff

回归必须证明三条独立合同：

- `save=False + conversation_transcript_authoritative=true` 可以提交 exact ConversationThread 的 live-tool
  Compact；普通 no-save 辅助轮仍不能推进 generation。
- child wake、observation 和 due policy 的可交付 report 共用唯一 notice 出口；当前至少用 due policy 端到端
  断言真实 notice 文件、reason 和 assistant 内容，底层 helper 继续覆盖过滤与故障 fail-open。
- carried 大 write 正文只留下路径/模式/hash/preview；工具索引超限时保留最新动作，不再 oldest-first 截掉尾部。
- live-tool Compact 从慢摘要调用前创建与 transcript Compact 相同 schema 的活动块；非权威临时裁剪必须依次
  发布 `5/20/65/100`，权威 checkpoint/CAS 路径还必须发布 `82/92`。同一 generation 原位更新，TUI 中文
  进度条显示真实 stage，结束后收起；回调失败不得改变 Compact 结果。

```bash
python3 -m pytest \
  agent_py_agent/tests/test_memory_config.py \
  agent_py_agent/tests/test_runtime_context_pressure.py \
  agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py \
  agent_py_agent/tests/test_tool_output_externalizer.py \
  agent_py_agent/tests/test_memory_runtime_compact_auto_continuation.py \
  agent_py_agent/tests/test_background_notice_display.py \
  agent_py_agent/tests/test_background_main_agent_runtime.py \
  agent_py_agent/tests/test_tui_renderer.py \
  -q --tb=short
```

上述组合本地已通过（保留既有 xfail）；真机仍必须从 fresh `.10` MiniMax-M2.7 TUI 观察 canonical
`compact_generation >= 1`、Compact 后继续工作以及最终 assistant 正文无需 `Ctrl+O` 直接出现。`.7` 慢模型
样本已经用户授权从 TUI `/stop`，后续 `.7/.10`、会话运行时 与 终端交互 的真实对照统一使用 MiniMax-M2.7。

## 2026-08-28 Todo 标题区分总 roster 与额外 child

Todo 标题中的 child 数只统计没有被当前可见 Todo 项代表的活动 child，不是下方代理面板总数。渲染回归
必须显示“另有 N 个子代理运行中”；一旦 child 通过 exact `progress_item_ids` 映射到可见 Todo，该提示消失，
Todo 自身状态原位变化。标题不从名称、goal 或行位置猜关联。

```bash
python3 -m pytest \
  agent_py_agent/tests/test_tui_renderer.py::test_todo_header_reports_active_child_not_represented_by_visible_items \
  -q --tb=short
```

## 2026-08-28 子代理 Compact 后 exact attempt 仍可调用工具

回归从真实 `.10` 失败链还原一条完整 child 生命周期：先在自己的 ConversationThread 形成历史，provider
返回 typed `context_overflow`，宿主完成一次 Compact，然后同一 child 继续调用 `list_files` 并最终结束。
断言必须同时证明：

- Compact generation 真实推进，后续模型采样仍复用原 exact attempt；
- 工具失败账没有 `TOOL_AUTHORITY_CONTEXT_MISSING`，最终 child 为 `DONE`；
- 只有 authoritative `task_local` overflow 延迟收口，普通主代理、非权威 task-local、最终成功/失败和取消
  仍走原审计终态合同，不能靠重开 attempt 或放宽工具门变绿。

```bash
python3 -m pytest \
  agent_py_agent/tests/test_subagent_runtime_compact.py \
  agent_py_agent/tests/test_run_audit_terminal.py \
  agent_py_agent/tests/test_subagent_runtime_guards.py \
  -q --tb=short
```

新端到端回归先红后绿；提交前还需完成上述组合、Ruff、doc sync、strict code-size、diff 与 clean-package。
真机验收只认 `.10` 唯一 Gateway 的 fresh MiniMax-M2.7 长任务中 child `compact >= 1` 后出现成功工具结果，
不能只看模型最终文字。`c8a2ece` 部署后的 `ma-110-c8a2ece-compact-r1` 已满足该门：工具运行时 child 的
checkpoint 在 `2026-08-28T00:47:42Z` 提交 generation 1；同一
`attempt-1787877717-487f5d43` 于 `00:47:53Z` 又得到 `run_command status=ok/tool_success=true`，随后自然
`DONE`。fresh task 全目录没有 `TOOL_AUTHORITY_CONTEXT_MISSING`。

## 2026-08-28 后台 child 终态整合跨工具轮续接

回归必须覆盖同一结构化 gate 的正反两面：

- `background_main_agent + save=False + TOOL_ROUND_LIMIT_REACHED + subagents_terminal` 使用
  `conversation_task_id` 登记 `due_now=True` 的 ordinary resume；后台 attempt id 不能替代 durable root。
- phase 为 `subagents_active/no_subagents/subagent_state_unknown/缺失` 时零 policy，避免轮询、状态未知时
  猜测和普通回执自我唤醒；task-local child 仍只由自己的 runner 续跑。
- 该判断只读工作片开始时冻结的 typed phase，不在模型运行后再读树，也不解析模型回复。

```bash
python3 -m pytest \
  agent_py_agent/tests/test_run_audit_terminal.py \
  agent_py_agent/tests/test_background_main_agent_runtime.py \
  agent_py_agent/tests/test_tools/test_tool_loop.py \
  -q --tb=short
```

以上组合 focused 已通过，保留既有 xfail/xpass；提交前继续执行 Ruff、doc sync、strict code-size、diff 与
clean-package。真实验收只从 fresh `.10` MiniMax-M2.7 TUI 发一次原样长任务，不能人工补“继续”冒充通过。

## 2026-08-27 Compact 有效代次与终态 progress policy

本轮回归钉住两条底层合同：

- 已结束 provider history 自己达到压缩线时，live-tool IR 不调用摘要、不改 IR、不推进 generation，统一
  preflight 必须返回 context pressure 交给 transcript Compact；live 摘要候选即使已经成对删旧工具，只要
  完整下一次请求仍未低于同一触发线，也必须原样回滚且不发布 Compact。
- canonical task link 进入 completed/interrupted 等不可复活终态时，立即退休 exact task 的所有 enabled
  progress policy；同 thread 其它任务继续启用，scheduler 仍能清理升级前残留 policy。测试后端检查真实
  native messages/tools，而不再从不会发送给 provider 的诊断 prompt 推断上下文或工具能力。

```bash
python3 -m pytest \
  agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py \
  agent_py_agent/tests/test_conversation_store.py \
  agent_py_agent/tests/test_gateway_conversation_compact.py \
  agent_py_agent/tests/test_context_pressure_native_trigger.py \
  agent_py_agent/tests/test_gateway_chat_conversation_context.py \
  agent_py_agent/tests/test_background_main_agent_runtime.py \
  -q --tb=short
```

以上共收集 316 项；代码推送前必须再取得完整退出码，并运行 Ruff、doc sync、strict code-size、diff 与
clean-package。真实验收继续使用 `.10` MiniMax-M2.7 TUI；`.7` 的慢模型长样本已经主动停止，恢复
MiniMax-M2.7 后才允许开启下一轮真实 TUI。

## 2026-08-27 慢流、active covers 与缓存注入边界

本轮回归同时钉住三个底层事实：

- 流式请求只有动态 first-event 与 rolling idle，没有会误杀持续有效 SSE data 的固定总墙钟；首事件预算
  是 request-local，不能修改共享 backend，`first_event/stream_idle/wall_clock` 分账。
- 同一直属父级已有活动 child 占用 exact covers 时，后续批次除非显式 replacement，否则任何 run 落盘前
  整批拒绝；接管关系预检失败零创建，edge 落账失败的新 child 启动前取消。
- Gateway/child 已结束历史通过同一 immutable `ConversationHistorySeed` 进入 canonical messages。完成回合
  保存 exact user/assistant/tool_call/tool_result 信封；下一轮按 committed summary → completed native messages
  → exact current user → current-turn IR 追加，不再把工具历史降级成散文，也不把 current user 同时发两次。
  runtime facts 在第一次出现时固化成 `RuntimeFactsTurn`，后续工具轮只追加，旧 system/tools/messages 前缀
  不重写。共享 task root 的 output 声明不再成为目录锁，越过父 workspace 仍硬拒绝。
- 慢模型只要仍有正文、思考或工具参数 delta，子代理状态会按 15 秒节流刷新“持续响应/思考/生成参数”；
  连接退避显示 typed 尝试次数。状态只保存阶段和计数，不保存模型正文。Compact 摘要也进入统一模型调用账，
  并删除了无法取消、超时后仍在后台耗费额度的第二摘要线程计时器。

```bash
python3 -m pytest \
  agent_py_agent/tests/test_stream_timeout_contract.py \
  agent_py_agent/tests/test_gateway_helpers.py \
  agent_py_agent/tests/test_timeout_gate2_stages.py \
  agent_py_agent/tests/test_timeout_budget_locked.py \
  agent_py_agent/tests/test_tool_model_generation.py \
  agent_py_agent/tests/test_dispatch_progress_seed.py \
  agent_py_agent/tests/test_orchestration_tools.py \
  agent_py_agent/tests/test_orchestration_create_subagents_tool.py \
  agent_py_agent/tests/test_orchestration_create_subagents_idempotency.py \
  agent_py_agent/tests/test_orchestration_create_subagents_items.py \
  agent_py_agent/tests/test_prompting_builder.py \
  agent_py_agent/tests/test_prompting.py \
  agent_py_agent/tests/test_backends_openai_native_tool_use.py \
  agent_py_agent/tests/test_backends_native_tool_use.py \
  agent_py_agent/tests/test_native_tool_use_ir_messages_flow.py \
  agent_py_agent/tests/test_compact_semantic_summary.py \
  agent_py_agent/tests/test_subagent_debug_trace.py \
  agent_py_agent/tests/test_run_task_workspace_writer.py \
  -q --tb=short
```

本轮生产与测试改动约两千行，未达到项目约定的 10,000 行全仓 pytest 触发线。最终提交前仍需重跑上述直接相关
focused 与严格 gate；r65 已保留本地慢模型跨旧 600 秒边界但长期无推进的失败样本。后续真实验收按用户最新
要求统一使用 `.7/.10` MiniMax-M2.7 fresh TUI，不能用 loopback SSE 或短提示冒充长任务通过。

## 2026-08-26 子代理具体工具审批回送所属 TUI

第七层安全修复正确让 `controlled_exec(apply=true)` 在 child capability grant 后仍停在
`APPROVAL_REQUIRED`，但 child 使用的 `BackgroundTranscriptSink` 没有 `request_permission`，因此用户无法
裁决并让同一调用续跑。回归必须钉住：

- 实际后台 sink 发布完整 exact request，owner TUI 决定后原等待者收到同一 permission 决定；
- pending record 只位于 owner ConversationStore，并按 root/run/full request 校验；
- 无显式交互 consumer、租约过期、取消、终态、损坏与 stale 决定全部 fail closed；
- 前台 main、两个 child 以及 main+child 混合到达时共用一个 FIFO，后到项不能覆盖当前 overlay；
- child 标题只作显示，HTTP/local writer 都使用服务端原 request；页面切换不隐藏 root 审批面板。

```bash
python3 -m pytest \
  agent_py_agent/tests/test_gateway_agent_control_service.py \
  agent_py_agent/tests/test_tui_runtime.py \
  agent_py_agent/tests/test_gateway_streaming.py \
  agent_py_agent/tests/test_background_notice_display.py \
  agent_py_agent/tests/test_tool_round_execution.py \
  agent_py_agent/tests/test_authorization_gate.py \
  agent_py_agent/tests/test_tui_view.py \
  agent_py_agent/tests/test_tui_input.py \
  -q --tb=short
```

上述完整相关 focused 已通过，保留 2 个既有 xfail；本轮净改动远低于 10,000 行，按项目约定未追加全仓
pytest。全项目 Ruff、doc sync、strict code-size、diff 与 clean-package 门均通过。

当前 179 项 focused 与严格 gate 全部通过，`d29caab` 已推送并部署 `.7` 唯一 Gateway。fresh
`ma-evidence-r52-child-owner-approval-fresh` 已证明 child Yes、exact Yes always 与 No；面板显示 child，
批准前无副作用，批准后原调用续跑。本轮生产/测试改动远低于 10,000 行，未跑全仓 pytest。

## 2026-08-26 后台命令必须跨 one-shot runner 存活

r52 中 child 的工具审批与启动均成功，但 child DONE 后 HTTP 端口立即关闭。回归必须钉住：

- detached managed host 是 bwrap 的直接父进程，child/root runner 自然退出不杀已批准服务；
- 另一个 Python 进程能从受保护记录水合同一 session，查询 running 并 stop；
- owner store 位于模型沙箱外，scope/store root/PID 出生指纹全部匹配后才可控制；
- running 只能进入 exited/killed，旧缓存不能复活终态；
- agent runner 消失后 host 仍执行日志上限，stop 仍终止完整进程树。

```bash
python3 -m pytest \
  agent_py_agent/tests/test_process_sessions.py \
  agent_py_agent/tests/test_tools/test_shell_background.py \
  agent_py_agent/tests/test_shell_bg_log_cap.py \
  agent_py_agent/tests/test_sandbox.py \
  agent_py_agent/tests/test_shell_orphan_kill.py \
  -q --tb=short
```

扩大后的直接相关回归 140 项通过；全项目 Ruff、doc sync、strict code-size、diff 和 clean-package 严格 gate
全绿。`8f50d19` 已推送并部署 `.7` 唯一 Gateway。fresh `ma-evidence-r53-child-process-session` 已证明：
审批前 8769 关闭；Yes 后 child DONE，服务跨 child/root 终态继续 200；另一进程水合 running，错误 scope
不可见，精确 stop 后 host/命令树消失、端口关闭、记录为 killed。本轮低于 10,000 行，未跑全仓 pytest。

## 2026-08-26 受管后台进程与 PTY start 必须经过精确审批

真机 r43 证明 provider system 能改正“尚未跨机验证”的回复，但模型仍可以在只读要求下
提交 `run_command(run_in_background=true)`。回归不解析该中文要求，而是钉住两个结构化
合同：command effect 与参数 effect 取最高等级；共享主机网络且越过调用生命周期的后台
进程不属于 sandbox 已包住效果。未有 exact approval binding 时必须 `ask`、handler 不执行；
绑定完整匹配后才能 `allow`。

```bash
python3 -m pytest \
  agent_py_agent/tests/test_tool_runtime_unification.py \
  agent_py_agent/tests/test_tool_manifest_contract.py \
  agent_py_agent/tests/test_native_tool_protocol_wiring.py \
  agent_py_agent/tests/test_tool_round_execution.py \
  agent_py_agent/tests/test_gateway_streaming.py \
  agent_py_agent/tests/test_gateway_verbose_progress.py \
  agent_py_agent/tests/test_tui_runtime.py \
  agent_py_agent/tests/test_tui_input.py \
  agent_py_agent/tests/test_tui_renderer.py \
  agent_py_agent/tests/test_tools/test_shell_background.py \
  agent_py_agent/tests/test_pty_sessions.py \
  agent_py_agent/tests/test_tool_input_completion_provenance.py \
  agent_py_agent/tests/test_registry_resilience_contract.py \
  -q --tb=short
```

`f8a4744` 的 166 项与严格 gate 通过；真 TUI r44 已证明 `run_command` 审批前端口关闭、
拒绝后 handler 未执行。但模型随后用 `terminal_session.start` 绕过并真实启动端口，所以
r44 不通过。第六候选必须证明：

- `SandboxPolicy.uncontained_by_parameter` 只能引用同一工具 schema 已声明字段；
- `terminal_session.start` 未批准时 `ask` 且 handler 不执行；
- `list/write/read/resize/close` 复用已批准 PTY，不重复弹窗；
- manifest 完整投影 uncontained mapping，不在 ActionPolicy 按工具名猜。

扩大后共 293 项 focused 全部通过；语法、全项目 Ruff、doc-sync、strict
code-size、diff 与 clean-package 严格 gate 全绿。`467093c` 部署后的 fresh r47 已证明后台
`run_command` 拒绝时 handler 不执行，fresh r48 也证明 PTY start 拒绝时不创建新进程；但 r47 又发现
child 可在 capability grant 后经 `controlled_exec` 绕过用户批准。本轮远低于 10,000 行，不跑全仓 pytest。

### 子代理 capability grant 不得代替用户 exact approval

r47 的真实链路是“直接后台命令被拒绝 → child 创建 capability request → Router/grant 缩小到
`python3` 与任务目录 → `controlled_exec(apply=true)` 直接 Popen 成功”。回归必须证明权限范围和单次
副作用批准是两个正交合同：

- 有效 command/path capability grant 存在时，`apply=true` 仍返回 `APPROVAL_REQUIRED`；
- 未批准时 `handler_executed=false`，marker/进程不能出现；
- `apply=false` 仍可只读预览，不弹副作用审批；
- exact approval binding 完整匹配后，既有成功执行与审计 refs 保持可用；
- `controlled_exec` 当前执行器直接使用 `subprocess.Popen`，所以 runtime policy 必须声明
  `sandbox=none`，不能借 capability grant 冒充 bwrap/OS sandbox。

```bash
python3 -m pytest \
  agent_py_agent/tests/test_subagent_controlled_exec_tool.py \
  agent_py_agent/tests/test_subagent_controlled_exec_gateway.py \
  agent_py_agent/tests/test_subagent_shell_gateway.py \
  agent_py_agent/tests/test_capability_auto_grant.py \
  agent_py_agent/tests/test_tool_runtime_unification.py \
  agent_py_agent/tests/test_gateway_streaming.py \
  agent_py_agent/tests/test_tui_runtime.py \
  agent_py_agent/tests/test_tui_input.py \
  -q --tb=short
```

扩大后的相关 240 项 focused 已通过；语法、全项目 Ruff、doc-sync、strict code-size、diff 与
clean-package 严格 gate 全绿，部署和 fresh child 真 TUI 仍待完成。

## 2026-08-26 模型验证结论与动作不能越过实际边界

真实反例：测试机本机 HTTP 200、监听所有接口，但开发机跨机连接被 firewalld 拒绝；MiniMax-M2.7 在原任务
和明确纠正 follow-up 中都误报“局域网能用”。第三候选虽在可能有副作用的工具说明里加入同一授权段，
真 TUI `ma-evidence-r42-tool-auth` 仍擅自启动 HTTP 服务。回归不模拟 HTTP/防火墙语义，也不建立任务专项
裁判；它证明宿主边界通过供应商真实 system 通道发送，原 user/tool 历史顺序不变，旧 fake 不接收未知参数：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_backends_openai_native_tool_use.py \
  agent_py_agent/tests/test_backends_native_tool_use.py \
  agent_py_agent/tests/test_tool_model_generation.py \
  agent_py_agent/tests/test_prompting_builder.py \
  agent_py_agent/tests/test_context_pressure_native_trigger.py \
  -q --tb=short
```

首版收集 203 项，结果为 196 passed、7 个既有 xfail，且本地严格 gate 通过；但部署后的正确 cwd fresh TUI
仍生成矛盾结论并擅自启动服务，所以明确判失败。第二候选新增跨观察点反例并放到完整输入末尾；同样
203 项为 196 passed、7 个既有 xfail，严格 gate 通过。部署后 `ma-evidence-r41-readonly` 的最终结论已能
明确区分本机已验证与另一机器未验证，但仍擅自启动服务，因此只通过“如实汇报”，未通过“只读行动”。

第四候选不解析用户正文、不关闭工具。OpenAI-compatible payload 必须是 system → 原始 user → native history；
Anthropic-compatible payload 必须使用顶层 system。原生 Schema 投影继续读取 canonical
`ToolRuntimePolicy`：纯 `read_only` 工具保持原说明，command strategy、默认 mutating/dangerous 或参数可升为
副作用的工具追加 `model_guidance.py` 中同一动作授权段。定向回归还必须证明原 ToolModelSpec 与
input_schema 没有被改写，provider-visible token 只统计实际支持的 system 通道：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_native_tool_protocol_wiring.py \
  agent_py_agent/tests/test_prompting_builder.py \
  agent_py_agent/tests/test_backends_tool_schema.py \
  agent_py_agent/tests/test_context_pressure_native_trigger.py \
  -q --tb=short
```

最小 provider/system 定向组通过；加入 backend 能力探针、native IR、root/child Prompt 与 runtime guidance 后
共收集 366 项，结果为 359 passed、7 个既有 xfail。严格 gate 和真 TUI 待办。本轮远低于 10,000 行，不跑
全仓 pytest。真机仍必须重发普通中文要求，只看 payload 单测或测试机 localhost 结果不能冒充跨机验证。

## 2026-08-26 空输入 ↓ 返回当前视口最新消息

真实 TUI `ma-tool-progress-r37-mario` 已先复现：进入 child 后 `Ctrl+Home`，等待新事件出现
`1 new message ↓`；`Ctrl+End` 能回底，普通 `↓` 无动作。回归必须分别证明：输入有字时仍按 ASCII/CJK
视觉折行移动；空输入且 `follow=false` 时第一次 `↓` 只调用当前 main/child 的统一 `end()`，不移动 child
selection；`follow=true` 时下一次 `↓` 仍能选择 child。当前定向命令：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_tui_input.py \
  agent_py_agent/tests/test_tui_view.py \
  agent_py_agent/tests/test_tui_agent_navigation.py \
  agent_py_agent/tests/test_tui_prompt_toolkit_pipe.py \
  -q --tb=short
```

四个最小文件 73 项通过；再加入 renderer/runtime 的相关组合共 145 项通过，并通过语法、全项目 Ruff、
doc-sync、strict code-size、diff 与 clean-package。改动远低于 10,000 行，未跑全仓 pytest。

`814cc3b` 已部署 `.7` 唯一 Gateway。fresh `ma-scroll-r38-resume` 精确恢复既有复杂会话后，主视图真实键序列
`Ctrl+Home → Down` 回底，第二次 `Down` 选择第一名 child；终态 child 视图也以 `Ctrl+Home → Down` 回到
自己的最终回复。随后普通中文 follow-up 立即入屏并触发 MiniMax-M2.7 流式 Thinking，旧 TUI 进程不作为证据。

## 2026-08-26 大工具参数生成期间的脱敏 TUI 进度

目标：证明 Anthropic `input_json_delta` 在完整 tool_use 形成前也能给用户持续的计数反馈，同时绝不泄露
半截 JSON/文件正文，不提前执行工具，也不把临时行冻结为历史。定向命令：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_backends_native_tool_use.py \
  agent_py_agent/tests/test_tool_model_generation.py \
  agent_py_agent/tests/test_gateway_verbose_progress.py \
  agent_py_agent/tests/test_background_notice_display.py \
  agent_py_agent/tests/test_conversation_agent_activity.py \
  agent_py_agent/tests/test_tui_runtime.py \
  agent_py_agent/tests/test_tui_view_model.py \
  agent_py_agent/tests/test_tui_renderer.py \
  -q --tb=short
```

当前 195 项通过。覆盖点：

- 9,000 字符工具参数只外发 `started/streaming/ready` 与累计字符数，真实完整 input 仍只在 stop 后交给工具层；
- 首条、每秒/每 8,192 字符和 ready 合批，展示 callback 失败不改变 provider 响应；
- rich Gateway chunk、后台 main/child ring 和本地 TUI 使用同一公开白名单，未知 `partial_json`、路径和正文
  不能落盘或进入 reducer；普通非 rich 客户端保持旧协议；
- reducer 只维护 `role=tool_input` 易失块，ready/reset/真实 tool/turn terminal 后删除且 stable history 为空；
- renderer 隐藏同阶段笼统 Thinking，显示灰色动画工具名、累计字符数与耗时，缓存键只读取展示字段。

本轮另通过 Ruff 与 strict code-size；改动远少于 10,000 行，遵守约定不跑全仓 pytest。`8c11eab` 已在
`ma-cache-firstturn-r34` 安全终态后快进部署 `.7` 唯一 Gateway。fresh
`ma-tool-progress-r37-mario` 使用用户原始复杂中文任务，真实观察到 `create_subagents` 从
515 chars/5s 增长到 3.0k chars/29s，55s 后完整工具卡创建 5 个 child，临时行自动删除；没有半截 JSON、
提前执行、重复卡片或 stable history 残留。

## 2026-08-26/27 Anthropic-compatible 原生主动缓存与稳定 system 分层

目标：证明主动缓存只改变 provider payload 投影，不污染 canonical messages/tools、不伪造 Compact，并能
通过唯一配置开关兼容不支持 `cache_control` 的端点。

当前 focused：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_backends_base.py \
  agent_py_agent/tests/test_backends_native_tool_use.py \
  agent_py_agent/tests/test_backends_message_adapter.py \
  agent_py_agent/tests/test_native_tool_use_ir_messages_flow.py \
  agent_py_agent/tests/test_backends_incomplete_response.py \
  agent_py_agent/tests/test_prompting_builder.py \
  agent_py_agent/tests/test_config_validation.py \
  agent_py_agent/tests/test_config_normalize.py \
  agent_py_agent/tests/test_native_runtime_guidance_forwarding.py \
  agent_py_agent/tests/test_native_middle_guidance_forwarding.py \
  -q --tb=short
```

覆盖点：

- 普通字符串调用方保留稳定工具尾、首条 prompt 与最新历史的旧 copy-on-write 断点；
- `CacheStructuredPrompt` 的完整诊断字符串必须无损包含 stable system、兼容 user 段、current-user 副本与
  volatile 正文；stable system 只允许系统规则、owner scope、Persona/Prompt Files/Skill 和文本工具目录；
  current user 通过 canonical messages 发送，记忆、Conversation 运行事实、推荐工具、Workspace 和执行事实
  必须在 volatile；
- Anthropic native 必须是 committed summary/已结束消息 → current user → current-turn IR → volatile facts，始终只有一个
  message-level 断点并向最新历史推进；tools/system 断点继续保留；
- OpenAI-compatible native 必须投影成同样的追加顺序，且不生成 Anthropic 专用字段；
- native 首轮即使 IR 历史为空也必须保留 `messages=[]`，不能压成代表 text 请求的 `None`；
- copy-on-write 后输入 messages/tools 与 canonical IR 保持逐字段不变；
- `messages=None` 的普通 text 请求和空 prompt 的旧请求形态不变；
- 配置关闭后 payload 不出现 `cache_control`，YAML 与 dataclass 默认一致；
- 配置关闭后 typed prompt 仍须无损发送全部段落，但 current user 不能因关闭缓存而在 prompt/messages 重复；
- 部署后的真 TUI 必须从 provider usage ledger 观察到先 cache-write、后 cache-read，不能用 Context 或
  Compact 数字替代该证据。

首版 174 项和严格 gate 已通过并以 `69bf2ec` 部署。fresh TUI 首轮权威账本仍为 0 cache-write/read；
二层分叉修复 `df95d27` 的 9 个直接相关测试文件 193 项、本地严格 gate、推送和 `.7` 单 Gateway 部署均
已完成。本轮改动远低于 10,000 行，按约定不跑全仓 pytest。

2026-08-27 stable/volatile 三层复验：

- 修复前 `ma-cache-probe-minimax-r60` 同一组 25 tools/system 的 SHA-256 连续相同，首条 user prompt 仍从
  约 31KB→33KB→45KB；账本出现 24,790 cache-write/0 read，后续只读 14,140 tools 并重写 11,386；
- 修复后透明代理只记录长度、SHA-256 与 marker，system 的宿主块/stable 块以及 tools 连续完全相同，
  动态 user 块按真实会话从 25,079 bytes 增到 25,117 bytes；
- 首个新布局回合 25,192 普通 input；第二轮 5,897 input / 13,967 read / 5,333 write；第三轮
  5,959 input / 19,300 read / 0 write，且模型准确续接前文；
- 当前 prompt/cache/native IR/Compact 组合 140 项通过。长多子代理的 my-agent 本地/MiniMax 与
  会话运行时/终端交互 MiniMax 对照仍属本轮后续，不用该短诊断链冒充长任务验收。

OpenAI-compatible 本地模型真 TUI 首次复验与回归：

- `ma-cache-long-local-r63` 在模型 HTTP 请求前直接终止，Gateway terminal request 的结构化错误为
  `TypeError: OpenAICompatibleBackend.generate() got an unexpected keyword argument 'on_thinking_delta'`；
  这证明后端公开接口不一致，不能归因于模型慢、上下文溢出或缓存失效；
- `OpenAICompatibleBackend` 现在接受统一 observer，流式解析 `delta.reasoning_content`，并保证完整思考
  在第一段正文/工具或流结束时只封口一次；非流式响应仍由安全白名单 assistant block 走统一 fallback；
- 工具调用续轮只在 assistant 同时含真实 tool calls 时回放 `reasoning_content`，普通文本轮不加供应商扩展
  字段；未知响应字段不进入 canonical IR；
- 首批三个直接文件 90 项通过；扩大到 backend、incomplete、message adapter、native、Gateway streaming、
  thinking spinner 与 tool stream boundary 的 10 个文件共 207 项仍全绿。fresh
  `ma-cache-long-local-r64` 已从同一自然语言长提示进入 `create_subagents`；主代理首段权威账本为
  77,244 input / 40,960 cached / 0 write / 2,740 output，缓存占约 53.03%，证明 OpenAI-compatible
  追加前缀真实命中；按输入 5、缓存 0.1/1，相对全普通输入分别节省约 51.97%/42.42%；
- r64 不是成功样本：约 1 小时 41 分内只创建 轻量运行时/工具运行时 两名 child，轻量运行时 遇到本地端点 600 秒流总墙钟
  超时，工具运行时 未自然结束，余下六项未派出；最终从同一 TUI `/stop`，root=`PAUSED`、child 均
  `CANCELLED`。超时和取消调用没有最终 provider usage，所以上述账只作成本下界；任务完成度按失败记录；
- 停止前 main 约 43.2k/262.1k、child 约 67.1k/51.8k，均未到 90% 压缩点，因此 `compact 0` 正确，
  不能把长墙钟时间当作应该 Compact 的证据。
- 恢复 MiniMax 单 Gateway 后，不另起重复长任务，而在已自然终态的 r62 长会话发一条只读证据复核追问；
  4 秒内开始思考，随后列出三处原报告证据不足及 exact 报告路径。该轮 6 次 provider 调用为
  31,378 input / 138,444 cache-read / 47,165 cache-write / 2,609 output，证明重启后模型、历史与缓存均续接；
  屏幕 Context 从 78.3k 到 44.0k/58.1k，`compact 0` 不变，仍是终态工具折叠而非 Compact。

真机协议探针（同一部署代码、同一配置、同一 MiniMax-M2.7 端点，不输出 Key 或 prompt）：

- 脱敏 payload 组装：`cache_enabled=True`，message/tool 各一个 `cache_control`；
- 第一次重复前缀请求：10,551 cache-creation、0 cache-read；
- 第二次：10 cache-creation、10,541 cache-read；
- fresh Agent 主轮前三次：46,269 input、0 cache-creation、0 cache-read，因 native `[]` 被压成 `None`。

部署后二层真 TUI（`ma-cache-firstturn-r34`，同一 `.7` 唯一 Gateway）：

- fresh 首请求 3 次物理调用：7,875 accounted input、1,810 output、25,999 cache-creation、
  12,313 cache-read，模型为 MiniMax-M2.7；
- 同 thread 完成 8 子代理调研后，连续两个普通后续任务的 provider 账分别为
  46,047 cache-creation / 37,234 cache-read 与 21,878 / 46,972；
- thread 权威 `compact_generation=0`、无 checkpoint。TUI 69.1k→46.3k→36.9k 的回落来自既有
  `conversation_terminal_tool_fold.v1`（当前 V2 读取器显式兼容为 cold-fold），不能计成 Compact，也没有破坏 cache-read；
- Gateway `/status` 为 running，测试期间模型配置为 MiniMax-M2.7，Gateway Python 进程精确为 1。

真 TUI 旧版对照样本（`ma-97468f3-longchain-r27`）：

- 一个主代理 + 19 个 child 约 3 小时 59 分，合计 1,156 次模型调用、46,024,438
  accounted input、565,392 output、6,454,431 cache-read、0 cache-creation；
- canonical Compact 主代理 2 次、child 合计 10 次；19 个 child 虽均生命周期 `DONE`，
  但独立产物验收不通过；
- `npx tsc --noEmit` exit 0；`npx vitest run --no-cache --dangerouslyIgnoreUnhandledErrors=false`
  为 68 pass / 8 skip / 1 fail / 1 unhandled error，exit 1；
- 原 Click 测试是 46 个 Python 文件/14,529 行，复刻是 8 个 TypeScript 文件/880 行；
- 独立 Python/TypeScript 对照探针证明 7 条核心行为不一致：选项取值、Group 子命令、未知
  子命令、`--help` 退出码、callback return value、ANSI style、`open_file("-")`。

该真任务的产物失败只作为底座样本，未由测试者修改任何复刻产物文件。同一普通中文
会话运行时 + MiniMax-M2.7 的 r31/r33 因测试适配器丢失 namespace 子工具而无效；修复测试适配器的通用
namespace 双向转换后，`会话运行时-m27-click-r36` 已真实执行 namespaced `spawn_agent/wait_agent/list_agents`。
其 child 首轮又把工具调用吐成普通 `<minimax:tool_call>` 文本，作为 会话运行时+该模型兼容样本单列，不能
反推 my-agent 缓存失败。

## 2026-08-25 子代理业务产物与宿主交接文件隔离

子代理只能看到和写入父级/用户明确声明的业务产物；宿主内部 `final_report/output.json/runner_result` 不得
进入 output contract、task packet、workspace refs 或旧 bundle 的 runner prompt。显式业务文件即使同名为
`final_report.md` 仍必须保留。定向命令：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_runner_prompts.py \
  agent_py_agent/tests/test_subagent_context_bundle.py \
  agent_py_agent/tests/test_subagent_policies.py \
  agent_py_agent/tests/test_subagent_prompt_contract.py \
  agent_py_agent/tests/test_direct_parent_lifecycle.py \
  agent_py_agent/tests/test_subagent_context_bundle_prompting.py \
  -q --tb=short
```

上面 100 项通过；另补执行上下文落盘的 grants/quality 两项定向回归，总计 102 项。覆盖当前 child 不见宿主
closeout、旧 bundle 二次过滤、恢复清单过滤、显式同名业务文件保留，以及 coordinator 仍能收到直属 child
完成信封。全量 Ruff、doc-sync、strict code-size、diff 与 clean-package 均通过；改动远少于 10,000 行，按
约定未跑全仓 pytest。真 TUI 必须使用部署后的 fresh child，检查业务完成后直接自然 final，宿主仍生成
交接投影且 child 不主动写内部报告。

## 2026-08-25 跨回合工具终态折叠与缓存连续性

普通回合的工具历史不能在下一轮无痕消失，也不能为保留历史而把大段原始结果每次重发。每个完成回合只生成
一次有界、脱敏的 `conversation_terminal_tool_fold.v2` metadata，内含固定 hot-tail/cold-fold/deadline；真正
Compact 才摘要并推进 generation。主代理、子代理、配置、上下文计量与摘要消费的定向命令：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_runtime_context_pressure.py \
  agent_py_agent/tests/test_tool_context_microcompact.py \
  agent_py_agent/tests/test_gateway_conversation_compact.py \
  agent_py_agent/tests/test_gateway_chat_conversation_context.py \
  agent_py_agent/tests/test_gateway_conversation_control.py \
  agent_py_agent/tests/test_subagent_runtime_compact.py \
  agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py \
  agent_py_agent/tests/test_conversation_store.py \
  agent_py_agent/tests/test_memory_runtime_basics.py::test_run_no_save_still_persists_thread_model_usage_without_runtime_archive \
  agent_py_agent/tests/test_config_validation.py::test_terminal_tool_fold_defaults_match_shipped_config \
  agent_py_agent/tests/test_config_validation.py::test_terminal_tool_fold_config_is_normalized_and_bounded \
  -q --tb=short
```

`7b14e34` 的 289 项已通过并部署 `.7` 唯一 Gateway。覆盖折叠确定性、字符上限、嵌套敏感参数脱敏、主链同账、公开正文不变、下一轮可见、
main/child 共用、真正 Compact 摘要输入、token 估算和 `compact 0` 不被普通折叠冒充。还覆盖 HEAD 原可复现的
child overflow→Compact→继续回复：会话用量从同 scope 累计快照原子换成逐物理调用增量，第二次成功不再因
event id 异值复用失败，provider cache-read/cache-write 不重复累计。原长真 TUI 两次真实 provider 回执分别
出现 42,107 与 12,987 cache-read token，且后一轮能准确续接前轮两次 Read；手动 Compact generation 1
把 45,639 降到 15,029，并吸收 98 条消息，不能只用本地估算冒充缓存证据。

2026-08-27 V2 热尾与本地搜索空结果补充回归：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_runtime_context_pressure.py \
  agent_py_agent/tests/test_gateway_conversation_compact.py \
  agent_py_agent/tests/test_gateway_chat_conversation_context.py \
  agent_py_agent/tests/test_subagent_runtime_compact.py \
  agent_py_agent/tests/test_tools/test_filesystem_tools.py \
  agent_py_agent/tests/test_tooling_filesystem.py \
  agent_py_agent/tests/test_core_tools_precise_schema.py \
  -k 'terminal_tool_fold or search_text' -q --tb=short
```

当前 32 项通过；另有 20 项最小定向批次通过。覆盖同一 V2 metadata 在 deadline 前选固定 hot-tail、之后选
fixed cold-fold、V1 持久数据恒按 cold 读取、配置 0..86,400 秒边界，以及 `search_text` 的
`local_text_search.v1` path/match-mode/backend/complete/status/hint。完整 no-match 不再和 Python 扫描上限混淆。

本地模型 `127.0.0.1:8901/v1` 的精确 V2 A/B（相同长前缀、相同约 6k 投影）账本：hot 第二轮
`14,998 input / 14,336 cached`，立即 cold 为 `15,072 / 12,288`。普通输入价 5、缓存价 0.1 时成本为
4,743.6 对 15,148.8；缓存价 1 时为 17,646 对 26,208，hot 分别节省约 68.7%/32.7%。这证明即时
经济性和缓存命中，不证明所有端点共享 TTL。`127.0.0.1:4000/v1` 当前要求鉴权且以测试凭证返回
`No connected db`，没有把它计作有效模型样本。
独立 55 秒间隔探针的 follow-up 为 `9,025 input / 8,192 cached / 833 uncached`。后续 endpoint 专属固定
前缀探针中，`8901` 直连和带本机配置鉴权的 `4000` LiteLLM 入口在 immediate、约 70 秒、约 310 秒均从
约 12,64x prompt tokens 命中 12,288 cached tokens。两条地址共用同一 OMLX 后端，因此只证明当前本机缓存
至少五分钟，不证明不同 provider TTL 相同，也不把 310 秒当成失效点。

MiniMax-M2.7 真实 native tool-choice 三例均返回 `stop_reason=tool_use`：未下载 GitHub 仓库选
`web_search`；本地精确类名选 `search_text(query=PromptBuilder,literal=true)`；收到 typed local no-match 后
改选 `web_search(代理运行时 github repository)`。该探针不替代部署后的 fresh TUI/ConversationStore 验收。

`.7` 部署后的原 `ma-hotfold-search-v2-r58` 第二轮曾在模型调用前失败。精确 load report 证明唯一错误是尚未
产生任何事件的 `observations/<thread>.jsonl` 不存在；这类账本本来就是 append-on-first-use。修复后缺失返回
空集合，存在但损坏继续报错。Gateway/context/store 相关 140 项 focused 全通过，另有损坏账本的 8 项保护
回归。原 r58 同 thread 重发和 fresh `ma-hotfold-search-v2-r59` 的工具轮→普通追问均由 MiniMax-M2.7 完成；
r59 两轮 request 均为 done、同一 thread、项目树无 `.chat_history`，首轮 assistant metadata 为
`conversation_terminal_tool_fold.v2` 且 hot/cold/deadline 齐全。
超过该 deadline 后，结构化 selector 返回 `projection=cold_fold`；同一 r59 再次不调用工具，仍准确复述唯一
搜索串与四个 typed 字段。随后唯一 Gateway 重启，TUI 自动重连并继续正常回复。

手动 Compact 的 typed 回执与缓存稳定前缀补充回归：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_gateway_conversation_control.py \
  agent_py_agent/tests/test_chat_control_runtime.py \
  agent_py_agent/tests/test_tui_runtime.py \
  agent_py_agent/tests/test_tui_renderer.py \
  agent_py_agent/tests/test_gateway_conversation_compact.py \
  -q --tb=short
```

当前 190 项通过。覆盖控制回执不解析显示文案、只按 typed generation 发布 Compact 边界、立即撤下已失效的
压缩前 Context 数字、下次真实模型调用再刷新，以及未 Compact 时后一轮 history 必须以前一轮完整 history
为 exact 稳定前缀。该显示刷新不会额外调用模型，也不改变 MiniMax/Anthropic 兼容链的自动缓存协议。

真 TUI 进一步暴露 idle/resume snapshot 会丢失代数；以下回归覆盖没有 Working block 时仍水合 Compact，且
迟到旧 activity frame 不能把同一 session 的 generation 从 3 回退到 1：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_tui_runtime.py \
  agent_py_agent/tests/test_tui_view_model.py \
  agent_py_agent/tests/test_background_notice_display.py \
  agent_py_agent/tests/test_tui_renderer.py \
  -q --tb=short
```

当前 105 项通过。现场服务端 activity 已独立返回 `compact_count=1`，因此该回归锁定的是客户端状态水合，
不复制服务端账本，也不从屏幕文字猜次数。扩展到控制回执、持久 operation receipt、Compact 历史的完整相关
链共 251 项通过。`.7` 原 tmux 真机已验证：恢复时直接水合 `compact 1`；手动 generation 2 后下一真实模型轮
显示约 28.5k 与 `compact 2`；再一轮普通续问命中 17,019 provider cache-read，仅新增 3,254 input。

## 2026-08-25 当前回合 Todo、真实 Window 粘底与 Compact 计数

同一长 conversation 会把完整 task-path 进度账本跨回合保留，但底部 Todo 只能显示当前普通用户回合明确
写入/派工的项；迟到的上一回合 poll/notice 也不能把旧清单刷回来。主/子页面的 control 锚点必须通过
prompt_toolkit `Window.get_vertical_scroll` 落到真实窗口，提交有效消息后才能可靠回到底部。定向命令：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_task_progress_advisory.py \
  agent_py_agent/tests/test_task_progress_tool_dispatch_reconcile.py \
  agent_py_agent/tests/test_dispatch_progress_seed.py \
  agent_py_agent/tests/test_conversation_agent_activity.py \
  agent_py_agent/tests/test_tui_view_model.py \
  agent_py_agent/tests/test_tui_runtime.py \
  agent_py_agent/tests/test_tui_view.py \
  agent_py_agent/tests/test_tui_agent_navigation.py \
  agent_py_agent/tests/test_tui_renderer.py \
  agent_py_agent/tests/test_background_notice_display.py \
  agent_py_agent/tests/test_tool_round_execution.py \
  agent_py_agent/tests/test_gateway_verbose_progress.py \
  agent_py_agent/tests/test_tui_worker_paths.py \
  agent_py_agent/tests/test_background_main_agent_runtime.py -q --tb=short
```

当前 362 项通过（含 2 项既有 xfail）。另以 95 项 Compact/TUI 组合回归确认：状态条次数只来自成功提交的
`ConversationThread.compact_generation`；`68k/128k=53%` 低于配置的 `90%` 压缩点（约 115.2k），此时
`compact 0` 是正确事实，不按屏幕历史长度、临时进度或模型文字推断。`.7` 真机继续显示 46%–53% 与
`compact 0`；两轮追加、PageUp 离底、Enter 回底和终态 Working 收口均在原长 session 通过。

## 2026-08-25 显式 output_files 祖先冲突必须整批返工

真实 r28 批次同时声明 `/internal`、`/internal/core` 和 `/internal/param`，三个 child 均已启动并在 4 分钟内
写出重复 API。新增回归只比较同一次调用里主动提供的结构化路径段，不解析 goal、不扫描实际文件：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_orchestration_create_subagents_items.py \
  agent_py_agent/tests/test_orchestration_create_subagents_items_policy.py \
  agent_py_agent/tests/test_orchestration_tools.py \
  agent_py_agent/tests/test_tool_input_schema.py \
  agent_py_agent/tests/test_tool_input_normalize_gate.py -q --tb=short
```

当前结果 59 passed。覆盖父目录/子目录整批 `not_started`、等价文件路径、exact 冲突对和修复动作，以及 `core`/`corex`、
不同文件、未声明 output 继续允许；最终仍须在同一真 TUI 观察模型收到结构化错误后自行重拆。

## 2026-08-25 角色提示、heredoc 与 tmux 3.3a 复制真失败回归

本轮三个失败都来自同一长 TUI `ma-97468f3-longchain-r27`，不得用项目名写专项分支：leaf 角色提示被丢弃、
Go heredoc 的 `&Context{}` 被误判后台操作符、tmux 3.3a 不支持被 mock 成功的 `load-buffer -w`。定向命令：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_tools/test_shell_background.py \
  agent_py_agent/tests/test_subagent_prompt_contract.py \
  agent_py_agent/tests/test_subagent_role_templates.py \
  agent_py_agent/tests/test_tui_input.py -q --tb=short
```

当前结果 75 passed。角色回归要求 leaf 只见当前 `prompt_zh`、snapshot 可跨重启保留；shell 回归要求忽略
heredoc payload 但仍抓 opener/结束后的真实 `&` 及注释伪 opener；tmux 回归要求普通终端调用公开的
`set-buffer -w`，iTerm2 只用 `load-buffer -`。最终复制仍以用户 attach 后粘贴为准。

## 2026-08-25 DNS 解析瞬断必须先有界重试

真实失败样本是运行 1:56:09、Compact 3 次的 child 因一次 typed `socket.gaierror` 直接终止。普通 JSON 和
流式入口都要先执行现有 2/5/15 秒退避，耗尽后保持 `ProviderTransientError`，供模型轮恢复：

```bash
python3 -m pytest agent_py_agent/tests/test_gateway_helpers.py -q --tb=short
```

新增两个定向断言先在旧实现稳定失败，再证明每个入口 4 次物理 open、3 次 wait。测试 mock 掉真实等待；
不靠异常字符串，不把 malformed URL、认证、额度或代理错误变成可重试。

## 2026-08-25 并行编码派工必须向模型说明互斥写入范围

真实失败样本来自 `.7` 的 `ma-97468f3-longchain-r27`：多个 child 的职责标题看似不同，实际在旧目标目录
同时覆盖同一批 Go 参数文件，230+ 轮后仍互相制造编译错误。回归只验证模型可见合同，不把软纪律升级为
目录锁或自然语言机器裁决：

```bash
python3 -m pytest agent_py_agent/tests/test_orchestration_tools.py -q --tb=short
```

当前结果：13 passed。断言覆盖总工具说明、items 参数和逐项 goal 都明确共同目标目录与互不重叠的文件/
模块范围，同时保留 `output_files` 不是完整写集、权限或机器锁。部署后须在同一长 TUI 的新复刻阶段观察
模型实际生成的 items，不得由测试者人工改写被测派工。

## 2026-08-25 子代理必须实际拿到局部编辑工具

真实失败基线来自 `.7` 唯一 Gateway 的 `ma-97468f3-longchain-r27`：首个 Click→TypeScript worker 已收到
局部修改提示，但真实工具列表没有 `edit_file`；一次只差两个前导空格的 `apply_patch` 失败后，它反复整文件
重写并制造重复声明。回归覆盖直属/递归/角色三条工具快照、父级显式缩窄负例、授权/写围栏/风险分类，
以及 会话运行时 式有界 expected-lines 失败反馈：

```bash
python3 -m pytest -q --tb=short \
  agent_py_agent/tests/test_tooling_filesystem_write.py \
  agent_py_agent/tests/test_tool_failure_error_code_semantics.py \
  agent_py_agent/tests/test_orchestration_tool_constants.py \
  agent_py_agent/tests/test_subagent_role_templates.py \
  agent_py_agent/tests/test_subagent_role_contracts.py \
  agent_py_agent/tests/test_subagent_hierarchy_scheduler.py \
  agent_py_agent/tests/test_subagent_model_normalizers.py \
  agent_py_agent/tests/test_capability_auto_grant.py \
  agent_py_agent/tests/test_orchestration_write_guard.py \
  agent_py_agent/tests/test_capabilities.py
```

当前结果：11 个文件共 188 passed；`test_write_boundary.py` 直接确保 canonical 成员集含
`edit_file`。当前长任务仍使用旧部署，不能用它冒充修复后通过；须等安全点部署后由新 child 的真实工具账
证明 `edit_file` 可见且补丁失配不再诱发整文件覆盖循环。

## 2026-08-25 父级 lifecycle mailbox 不得被兄弟 child 消费

真实失败序列不是“7 个 child 一次性全部结束”，而是 child 逐个结束时，其余 task-local child 仍在模型轮中。
因此回归必须同时验证：共同 `root_task_id` 不等于共同收件箱；运行 child 的安全点看不到、不能注入或 ack
兄弟完成 wake；原 wake 保持 pending，随后 exact conversation parent 仍能按 FIFO 注入并确认。与既有七份
长结果有界排空测试组合执行：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_runtime_guidance.py \
  agent_py_agent/tests/test_background_main_agent_runtime.py \
  -q --tb=short
```

当前结果：通过（含旧代码先红、新候选转绿的定向证据）。部署后仍须在原长 TUI 中先补齐七份调研，再追加
两个普通小任务、第二个多子代理复刻和独立验收，确认同一 session 不再漏交接。

## 2026-08-25 后台进程只能由所属用户会话续接

真实失败样本是 `.7` 长任务启动后台 `cargo check` 后，模型按工具回执寻找
`process_status/list_processes/kill_process`，但三个名字都不存在，只能另跑 shell sleep 轮询。回归要求
`run_command` 的 host scope 随后台记录冻结，`process_session wait` 返回真实退出码和日志，其他 TUI session
看不到同一 id，stop 终止完整进程树，裸工具调用在缺少可信 scope 时 fail-closed：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_process_sessions.py \
  agent_py_agent/tests/test_tools/test_shell_background.py \
  agent_py_agent/tests/test_tooling_shell.py \
  -q --tb=short
```

当前结果已由上方跨 runner 套件扩为 50 passed。部署后在 fresh TUI 的后台服务中检查模型实际调用
`process_session(action=wait)`，不得再出现 `run_command("sleep ...")` 轮询。

子代理还要覆盖默认角色、动态 shell 授权和旧持久任务恢复的工具依赖闭包：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_orchestration_tool_constants.py \
  agent_py_agent/tests/test_capability_auto_grant.py \
  agent_py_agent/tests/test_subagent_capability_request_tool.py \
  agent_py_agent/tests/test_subagent_role_templates.py \
  agent_py_agent/tests/test_subagent_effective_runtime_context.py \
  -q --tb=short
```

与 process session 回归合跑当前 54 passed；owner 显式 `disabled_tools` 仍是最终收窄，依赖闭包不能把禁用工具
偷偷加回。

## 2026-08-25 后台 thinking 传输必须合批且保留完整终态

真实失败样本来自 `.7` 长 TUI `ma-97468f3-longchain-r27`：后台工具仍连续执行，但一个 reasoning block 的
逐 token delta 把 1024 条公开事件环打满，TUI 数分钟停在旧工具后才一次性追上。回归要求首片即时可见、
短时间内的碎片不逐条落事件、到时间/字符阈值后按原顺序合批，最终完整 thinking 仍覆盖全部正文：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_background_notice_display.py \
  agent_py_agent/tests/test_tui_runtime.py \
  -q --tb=short
```

当前结果：47 passed。部署后继续使用同一 session 的普通追加轮，比较 Gateway `event_cursor` 增长、TUI 首次
可见延迟和终态正文；测试者不读取或修改被测产物来推动任务。

## 2026-08-25 完成合批不能漏掉 sibling 结果

真实失败样本为 `.7` 同一长 TUI 的七路调研：7 个 child 全部 `DONE`，root 只读取 5 份并把另两份误报为
未完成。新回归把 7 份长 completion 放进 4k background 总预算，同时把普通 pending prompt limit 设为 5；
每轮只取有界批次，延期成员必须留在 mailbox，不能被安全点降成瘦事件后提前确认：

```bash
python3 -m pytest agent_py_agent/tests/test_background_main_agent_runtime.py \
  -q --tb=short \
  -k 'successful_sibling_completion_wakes_are_coalesced or \
      successful_completion_mailbox_drains_every_sibling_under_prompt_pressure or \
      completion_coalescing_acknowledges_only'
```

当前关键路径结果：4 passed。断言覆盖多个有界模型轮最终读到 7 个 child 的结论前缀和精确报告引用、延期
wake 在下一轮前仍 pending、root link 保持 active、中间回复全部 suppressed、最终 pending 归零且只投递一次。
部署后的自然正向样本继续复用原长 TUI。

## 2026-08-25 批量 create_subagents 不重复要求顶层 goal

真实失败样本来自 `.7` 长 TUI `ma-97468f3-longchain-r27`：七个 `items` 都已有独立 `goal`，provider 仍在
handler 前报 `$.goal: 必填缺失`，模型补发后才创建成功。回归同时覆盖 root 与 descendant 批量入口、省略
顶层 goal 的成功路径、schema 提示和原有显式批次说明兼容：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_orchestration_tools.py \
  agent_py_agent/tests/test_orchestration_create_subagents_items.py \
  agent_py_agent/tests/test_orchestration_create_subagents_tool.py \
  -q --tb=short
```

当前结果：65 passed。部署前还需运行 Ruff、doc-sync、strict code-size、diff 与 clean-package；真实自然正向
样本继续复用同一个长 TUI，不另开 Gateway。

## 2026-08-25 同一长会话的新主任务必须重置 Working 计时

focused 回归命令：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_conversation_agent_activity.py \
  agent_py_agent/tests/test_background_notice_display.py \
  -q --tb=short
```

回归构造同一 thread 中旧 root、当前 `workspace_task_id` root 和更新更晚的 child，并预置一条旧 root 的
易失 main sink。活动投影必须保留当前 root 的阶段内容，同时把 `started_at` 固定到当前 root
`ThreadTaskLink.created_at`；没有 main activity 的 renderer 不允许拿 session-scoped background block 起点
冒充本回合耗时。该测试只验证只读展示，不改变 active link、child 生命周期或任务完成判断。

## 2026-08-25 后台恢复轮不得用当前 task id 读出空 Todo

focused 回归命令：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_task_progress_tool_dispatch_reconcile.py \
  agent_py_agent/tests/test_task_identity.py \
  -q --tb=short
```

真实长 TUI 的 root 在 child 完成唤醒后调用
`task_progress(action=read, run_id=<当前 gwreq task id>)`，旧工具把它当历史账原样读取，返回 0 项；同一时刻
TUI 从 canonical task-path 账本仍显示 18 项。回归必须证明当前 typed task id 会归一到原 task-path 账本，
而明确不同的历史 run id 仍精确读取自己的账，不发生跨任务吸附。该修复对照 会话运行时 session/turn-scoped
`update_plan`，不解析标识前缀或 prompt，也不把 Todo 恢复成机器验收门。

## 2026-08-25 依赖型派工不得伪装成同批串行

focused 回归命令：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_orchestration_tool_specs.py \
  agent_py_agent/tests/test_orchestration_tools.py \
  -q --tb=short
```

当前 15 项通过。回归要求 `create_subagents` 的总说明与 `items` 参数详情都明确：同批 child 会立即并发，
自然语言写“先 A 后 B”不形成顺序；B 依赖 A 的未来修复/产物/结论时，必须等 A 的生命周期完成事件后再
创建 B。该测试只锁模型合同，不解析业务 prompt，也不把测试角色变成机器依赖。

真机复验继续使用 `.7` 唯一 Gateway、MiniMax-M2.7 和公开 tmux：用普通中文要求“先修复，再派独立测试”，
必须先只出现修复 child；修复终态唤醒后才出现测试 child。测试者不修改被测产物、不新增 Gateway。

## 2026-08-25 后台续片操作终态与子代理报告读取

focused 回归命令：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_background_main_agent_runtime.py \
  agent_py_agent/tests/test_direct_parent_lifecycle.py \
  agent_py_agent/tests/test_memory_compact_tool_output_refs.py \
  agent_py_agent/tests/test_tool_output_externalizer.py \
  agent_py_agent/tests/test_tools/test_filesystem_tools.py \
  -q --tb=short
```

共收集 206 项，结果为 204 passed、2 个既有 xfailed。它必须同时证明：大小工具输出的 index 都保留有界
execution/operation；私有 envelope 字段不进入索引；成功派工跨 child wake 后仍核验 succeeded；自然 final
关闭 root task link；foreground request id 与 durable task id 不同、以及旧行尚无新字段时仍只恢复 exact
active turn；精确和可权威解析的 stale-task `final_report.md` 可读，而其它 agent state 不会跟随跳转。改动远低于
10,000 行，按约定不跑全仓 pytest；Ruff、doc-sync、strict code-size、diff 与 clean-package 均通过。

部署后继续复用同一个 durable TUI/tmux 完成长链，不为阶段切换新建会话：多子代理代码级调研 → 两条普通
小追加 → 多子代理跨语言复刻既有项目 → 普通验收追加 → 针对真实入口缺口的多子代理返工 → 最终普通追加。
每次输入前公开同一个 tmux 名称；全部消息使用普通用户能懂的中文，不向被测代理注入技术定位答案，测试者
不修改被测产物。链路要同时观察 completion 报告直读、root Working 收口、Todo、child token/Compact、
后续消息排队和真实入口验证。

## 2026-08-24 child 插话、八槽容量与历史入口

focused 回归覆盖 Gateway owner 控制、guidance exact-turn reserve/submission、无活跃 attempt 拒绝、默认
八个 root slots、显式单批收紧、配置 YAML/dataclass 一致，以及 root/child 常驻历史快捷键。当前命令：

```bash
.venv/bin/python -m pytest -q --tb=short \
  agent_py_agent/tests/test_gateway_agent_control_service.py \
  agent_py_agent/tests/test_runtime_guidance.py \
  agent_py_agent/tests/test_gateway_request_runtime_errors.py \
  agent_py_agent/tests/test_gateway_conversation_control.py \
  agent_py_agent/tests/test_orchestration_create_subagents_tool.py \
  agent_py_agent/tests/test_orchestration_tools.py \
  agent_py_agent/tests/test_orchestration_create_subagents_items.py \
  agent_py_agent/tests/test_orchestration_create_subagents_guardrails.py \
  agent_py_agent/tests/test_settings_config.py \
  agent_py_agent/tests/test_config_normalize.py \
  agent_py_agent/tests/test_tui_agent_navigation.py \
  agent_py_agent/tests/test_tui_renderer.py \
  agent_py_agent/tests/test_tui_stateful.py \
  agent_py_agent/tests/test_tui_input.py \
  agent_py_agent/tests/test_tui_view.py \
  agent_py_agent/tests/test_background_notice_display.py
```

本地 429 项已运行到 100%，保留既有 xfail；Ruff、doc-sync、strict code-size、diff 与 clean-package 也
通过。`.7` 唯一 Gateway、MiniMax-M2.7 的 fresh `ma-6d33228-p3-guidance8-r20` 只输入一次用户原样 Prompt 3，
已验证八名 child 同时运行、运行 child 的普通中文消息 exact receipt 最终 consumed 且继续工作、root/child
`Ctrl+Home` 历史、F6 后滚轮与返回原生复制。测试者没有改调研产物或另开 Gateway，任务继续自行运行。

当前测试文档只保留常用入口。完整文件清单以 `agent_py_agent/tests/` 为准，不再手工维护旧表格。

默认按改动范围运行 focused tests，不重复运行全仓 pytest。只有本轮生产代码和测试代码新增、删除累计
约 10,000 行以上，或用户明确要求时，才追加一次 `python3 -m pytest -q --tb=short`。静态检查、文档同步、
严格代码尺寸、diff 和 clean-package 守卫仍按发布风险执行，不用全仓 pytest 代替。

测试机上的所有真机用例只使用一个真实 Gateway。需要并行时启动多个独立 TUI/会话并统一连接该实例，
不得为 input、render、lifecycle、isolation、replica 或 steer 等用例另开端口和 Gateway。每轮真机测试前先
确认只有一个 Gateway 进程和一个配置端口；故障恢复用例顺序重启或中断这个实例。合同单测中的 fake
Gateway 可以并行，但不能作为“单 Gateway 多客户端”验收的替代证据。

2026-08-22 起，任何真实 TUI 启动、切换或输入之前，测试者必须先在用户可见消息中报告测试对象、测试机、
唯一 tmux session 名称和完整观看命令，例如
`ssh root@192.0.2.7 -t 'tmux attach -t <session>'`；报告之前不得先操作该 TUI。正式功能验收固定使用
`MiniMax-M2.7`，并依次输入下面四个原样任务，不得用玩具 prompt 替代：

1. `你在底下创建一个abc目录，然后在abc目录里写一个web页面的植物大战僵尸游戏，不准自己写，必须多个子代理，要求游戏真实，带20个关卡，关卡设置合理，你只能盯着，并且要求最后是0.0.0.0局域网都能访问`
2. `在自己的任务目录底下创建一个bbb的目录，在里面写一个超级玛丽的游戏，要求不准自己做，必须创建多个子代理，需要复刻原始超级玛丽的所有玩法和前三个关卡，直接网页版可玩`
3. `深度调研轻量运行时,工具运行时,会话运行时,终端交互,deepseek-harness,代理运行时,通道运行时,长期助手架构和完整功能，做代码级别的学习和了解，不准自己去做，只能派子代理去做调研，完整列出每个功能目录如何做的，技术实现，架构方案，优缺点等等。最后你整合进行横向对比。`
4. 先在该 TUI 对应目录下载一个 GitHub stars 超过 10,000、功能代码超过 40,000 行的项目；stars 必须从
   GitHub 当前项目页/API 核验，功能代码行数必须在下载后的固定 commit 上排除依赖、构建产物、测试快照与
   纯文档后本地统计并留证。然后再输入：
   `换一种编程语言，完整复刻这个项目，要求实现所有的功能，所有测试，最后保证你复刻的这个项目能够和原项目一样，毫无缺陷的运行，并且所有功能和原项目一样可用和完善。你自己不允许进行复刻，只能创建子代理进行复刻和代码编写工作，你只能负责最后的测试功能和整合工作，你不允许写功能代码。`

这四轮都只把用户 prompt 输入一次；测试者只观察 TUI、typed 账本、日志、产物和真实服务，不替被测对象
补代码、发技术推动指令或修改产物。单测、fake、renderer snapshot 和静态 gate 只作上线前护栏，不能替代
上述真实 TUI 验收。

TUI 退出/session 生命周期与单 Gateway 降载回归：

```bash
python3 -m pytest agent_py_agent/tests/test_chat_parts.py agent_py_agent/tests/test_background_notice_display.py agent_py_agent/tests/test_conversation_agent_activity.py agent_py_agent/tests/test_subagent_listruns_cache.py agent_py_agent/tests/test_direct_parent_lifecycle.py agent_py_agent/tests/test_background_main_agent_runtime.py -q --tb=short
```

必须证明普通 `/exit` 会停止当前 TUI 的 refresh/notices/worker，并保留 canonical session，输出精确
`my-agent resume <session_id>`；恢复后只读取同 session 的历史且不重新提交 prompt。tmux `Ctrl+B D` 只是
detach，因此 TUI PID 和轮询保持存在，不得把它记为产品退出。活动快照必须经 root/parent/depth 索引选择
exact run id，再从 canonical task 文件复核；索引不是状态权威。健康空闲轮询为 5 秒，前台或后台有任务时
为 1 秒，失败仍按 0.5/1/2/4/8 秒退避。`.7` 真机需同时记录退出前后 TUI PID、唯一 Gateway PID、
session 恢复、`/client/notices` 延迟和 Gateway CPU；不得另启 Gateway，也不得删除运行任务来制造通过。
后台 scheduler 的完成合批同样必须使用 root 索引选择 exact ids、canonical 文件复核；focused 测试要让
全量 `list_runs_report` 主动报错，证明 managed root 查询没有偷走旧路径。真机关闭空闲旧 TUI 后还要对
Gateway 做 Python stack/CPU 采样，不能把客户端减少误报成 scheduler 降载已完成。
当前 root 的 canonical child 损坏必须保守返回 load error；另一棵无关历史树损坏不得再阻塞本 root 汇报。

2026-08-24 `.7` 真机证据：`c12ea57` 和 `e2aba94` 依次部署到唯一 Gateway。fresh tmux
`ma-e2aba94-session-r14`（`ssh root@192.0.2.7 -t 'tmux attach -t ma-e2aba94-session-r14'`）显示
MiniMax-M2.7；新建 session `sess_1787572173_e90c7030` 后 `/exit` PID 消失、exact resume 未新增 session，
再次 `/exit` 仍 status 0，session 总数全程 324。Gateway PID `481449`/8420 始终唯一；8 秒 `/proc` CPU
差分约 12% 单核，12 次 `py-spy` 后台线程采样均为 wait 且零次出现全量 list/deepcopy；16 个 durable
session 并发 `/client/notices` 全部成功，0.027--0.294 秒、总墙钟 0.315 秒。

子代理详情与控制面的 focused 回归：

```bash
python3 -m pytest agent_py_agent/tests/test_tui_agent_navigation.py agent_py_agent/tests/test_agent_transcript.py agent_py_agent/tests/test_gateway_agent_control_service.py agent_py_agent/tests/test_conversation_agent_activity.py agent_py_agent/tests/test_authorization_gate.py agent_py_agent/tests/test_authorization_gate_r1.py agent_py_agent/tests/test_tui_input.py agent_py_agent/tests/test_tool_round_execution.py agent_py_agent/tests/test_background_notice_display.py -q --tb=short
```

要同时证明：空输入 `↓` 才选择 child，`Enter` 进入 exact run，详情增量展示公开过程、Context/Compact、Todo
和直属下级；`Ctrl+G` 返回父代理但不改任何运行状态，`Esc` 停止当前运行中 child；普通输入以稳定 guidance id
只投递给当前 child。已结束 child 可重新进入查看 final，但不能收消息或静默恢复。历史 view 可以在 attempt
关闭后通过 owner/ancestry 授权，任何写控制仍要求 active binding，越出当前 owner 根树一律拒绝。真机验收
还必须在已公开 tmux 中逐项实际按键，单元测试不得代替。
详情内容回归还要证明：第一条 user block 是 canonical `task.goal` 而不是短 `description`；非 callable 但实现
`write_progress` 的 child sink 能收到工具开始/完成，并在工具边界前冻结过程 commentary；thinking、工具/diff
和 final 继续复用主页面 block renderer。child 内按 `Ctrl+O` 必须冻结当前 child runtime，正文不得混入 root。

2026-08-24 真机证据：`c5026a7` 部署到 `192.0.2.7` 的唯一 Gateway，模型为 MiniMax-M2.7；tmux
`ma-53ff260-agent-nav-r11` 只输入一次原样 Prompt 2 并创建 4 名 child，首次直接暴露 footer 回执被遮挡。
小修后 tmux `ma-c5026a7-agent-nav-r12` 恢复同一个 `sess_1787565844_0fa7e764`，没有重发任务；实按
`↓/Enter/Ctrl+G/Esc`，证明返回不停止、Esc 只取消 worker-1。运行中 worker-4 接收一条普通中文 guidance，
exact agent-run 账本写入且用户消息在该 child 正文立即可见；已停止 worker-1 和已完成 worker-2 均可回看，
输入不会复活终态，worker-2 canonical final 正常展示。

同日完整消息页追补：`91a1c3d` 通过 218 项直接 focused 和本地严格 gate 后部署到 `.7` 唯一 Gateway
（PID `499058`），fresh tmux `ma-91a1c3d-child-full-r17` 显示 MiniMax-M2.7，并只输入一次原样 Prompt 2。
实按 `↓/Enter` 进入 worker-1 后，普通页出现 child 灰色 thinking、过程 commentary、`list_files`、`Bash`
和写文件卡；`Ctrl+O` 保持 child 视角，Home 首行是完整 delegated goal，`Ctrl+G` 才回 main。现场三个 child
JSONL 均含 tool started/completed，worker-1 采样为 6/6；长任务继续运行，不能把此 UI 验收当作游戏完成。

Attempt 生命周期与授权续跑的 focused 回归：

```bash
python3 -m pytest agent_py_agent/tests/test_runtime_db_main_chain.py agent_py_agent/tests/test_r103_closeout_gate.py agent_py_agent/tests/test_run_audit_terminal.py agent_py_agent/tests/test_operation_store_robustness.py agent_py_agent/tests/test_subagent_runtime_guards.py agent_py_agent/tests/test_subagent_runner_result_state.py agent_py_agent/tests/test_capability_auto_grant.py agent_py_agent/tests/test_manager_runner_capability_requests.py -q --tb=short
```

要同时证明：child 创建时只有一条无 runner PID 的 pending generation 1；真实 runner 原子激活同一条
attempt 并取得锁，重复 runner 启动被拒且不生成 generation 2；过期但活着的 holder 不可接管；只有超 grace+明确死亡可回收；终态
run/attempt 不能调工具；可续跑结果关闭旧 attempt 且新 attempt 可重开；已取消的 exact attempt 不接受
迟到 DONE；第一次结果收口后重放失败；当轮已获 grant 的 BLOCKED 转同 run PENDING 并保留可续派；
宿主先关闭的 max-token attempt 只接受同 `turn_end_reason` 的 PENDING，随后可创建 generation 2；
同一窗口的伪 DONE 必须被冲突栅栏拒绝；终态 TUI 短句不得残留“模型已生成回复”。

会话运行时 式分批创建与容量/幂等边界的 focused 回归：

```bash
python3 -m pytest agent_py_agent/tests/test_orchestration_create_subagents_idempotency.py agent_py_agent/tests/test_orchestration_create_subagents_items.py agent_py_agent/tests/test_orchestration_create_subagents_tool.py agent_py_agent/tests/test_orchestration_tools.py agent_py_agent/tests/test_background_main_agent_runtime.py -q --tb=short
```

要同时证明：同一 canonical parent 的后台唤醒轮可以在活跃 sibling 存在时创建不同职责的第二批 child；
相同 idempotency/work-scope 合同仍复用既有 run；单次上限与 root-session 容量仍原子整批拒绝；并发后台
执行器仍由 active-turn claim 控制。不能恢复 `SUBAGENT_ACTIVE_LINEAGE_EXISTS`，也不能增加 Audit、任务名
或 prompt 关键词绕过。

`fb75b68` 的 fresh Prompt 4 r9 使用 `.7` 唯一 Gateway、MiniMax-M2.7、tmux
`ma-fb75b68-p4-lazygit-r9-phased` 与固定 `lazygit@ea916395`。首批 3 名 child 自然完成，其中 worker-3
真实 Compact 1 次；main 自动醒来并通过一个 items 调用创建第二批 worker-4/5/6，没有活跃血缘错误。
这份直接证据覆盖“后台第二批”，但不覆盖“先创建一名后，在其仍活跃时发第二次独立创建调用”；后者继续
以 focused 回归为当前证据，不得把同批原子创建夸成 exact 真机覆盖。

Prompt 4 r6 使用 `.7` 唯一 Gateway、MiniMax-M2.7、tmux `dsh-p4-lazygit-r6-ea91639` 和固定
`jesseduffield/lazygit@ea916395`，只输入一次原样 prompt。10 个 child 全部自然 DONE、3 个真实 Compact，
main 自主补派并把 Todo 全部打钩；但 canonical 产物只有 9,543 行、55 个空桩、5 个测试定义，8 项集成
测试全部 skip，另有两套未整合输出，因此 r6 仍判失败。部署候选后的 r7 除继续做原版代码/测试/运行审计，
还必须证明：isolated 用户回执不出现 thinking、不改变 main/child context；真实任务 thinking 仍连续可见；
一项 `items` 的系统 child 沿 sibling 历史编号；Todo 标题显示真实 `完成 X/Y · 进行中 Z`，四行折叠不冒充
完成数。open Todo 仍不得成为宿主自动续轮或质量验收门。

Prompt 4 r7 使用同一固定源码，在 fresh cwd/tmux `dsh-p4-lazygit-r7-ea91639` 只输入一次原样 prompt。
r6 的四个投影缺口全部通过真实 TUI：isolated thinking/context 不再污染主任务，单项补派连续到
worker-6/7，Todo 从 0/11 到 11/11 均显示真实计数与四行视窗。任务产物仍失败：只有 6,985 行产品代码，
主 App 仅欢迎页；安装和 App 构造都失败。模型先看到 6 个、后看到 2 个测试失败，却删除
`test_app_instantiation` 并削弱其余断言后拿 20 passed 宣称完整。r8 必须继续用 fresh cwd/tmux 和一次原样
Prompt 4，观察 root/child/lifecycle wake 共用软纪律是否让模型保留完整范围、重跑真实失败入口并拒绝为
绿灯删/skip/放宽有效测试；测试者不向 TUI 追加修复指令，也不修改产物。

Prompt 4 r8 的 canonical 失败样本要求新增两层回归。其一，任意 runner 的合法结果若把 exact run 投影回
`PENDING`，必须回收该 run 自己的 `background_start`，即使记录中的 PID 仍是承载兄弟 runner 的共享活进程；
session 退出后既有 `auto_start_orphan_run` 应立即续派同一个 run。其二，`context_scope=task_local` finalize
不得创建 `ordinary_task_resume`；历史 policy 或 wake 若绑定 canonical child task，
`BackgroundMainAgentRuntime` 必须在调用 root 模型前退役或无模型确认，不能改 root thread/task link、Todo，
也不能把 root 工具授给 child。fresh r9 仍只输入一次原样 Prompt 4，并核对不存在
`ordinary_task_resume(task_id=child)`、不存在 main/child 双执行器，PENDING child 能自行续跑。

Prompt 4 r9 使用 `93e18f6`、`.7` 唯一 Gateway、tmux `dsh-p4-lazygit-r9-ea91639` 和同一固定源码，
仍只输入一次原样 prompt。5 名 child 的 thread/parent/runner 身份正确，未出现 child
`ordinary_task_resume` 或双执行器；但没有自然 PENDING 样本。新失败是未声明 `output_files` 的 child 被
运行时强塞 task-local Markdown，runner 文件合同又称其为“用户要求的业务产物”，两名编码 child 只交
分析报告。产物最终分裂为两套约 4,970 行 Python/16 个测试，普通环境不能收集，隐藏 venv 启动真实 TUI
在 `Stylesheet.parse()` 抛 `TypeError`。r10 必须证明：未声明输出的 create payload、context bundle、
completion wake 和 child result index 都没有系统默认业务 ref；child 仍通过最终消息与
`final_report_ref` 完成交接；显式输出路径继续锚定、授权和锁冲突；测试者仍不追加推动消息或修改产物。

Prompt 4 r10 使用 `0c6c916`、`.7` 唯一 Gateway、tmux `dsh-p4-lazygit-r10-ea91639` 和固定
`jesseduffield/lazygit@ea916395`，仍只输入一次原样 prompt。前两名 Rust child 没有系统默认业务 ref，
但第二次 completion wake 后 root 改成 Go 并派出“只建基础框架”任务。r11 必须用 fresh cwd/tmux 证明：
每次 child lifecycle wake 的 provider `User Task/root_user_prompt` 都是同一原始 Prompt 4；synthetic wake
只在 runtime continuation；root 之前的 task_progress/create_subagents 结构化调用按 exact root task 恢复，
child 私有调用不混入；已选目标语言和完整范围不因连续 child 完成而重置。仍只输入一次原样 prompt，测试者
不得修改产物或给技术推动消息。

`931ee20` 在 `.7` 唯一 Gateway 上以 tmux `dsh-p3-research-931ee20-verify` 执行第 3 条原样任务，prompt
只输入一次。4 个 child 的短职责、实时 context 总 token、一次 attempt 自然 DONE 和 Todo 打标均正确；
但 main 只收到不含最终正文/报告 ref 的生命周期通知，猜测 `research_reports/` 后反问用户，未继续第二批，
因此该轮判定失败。canonical 证据显示四份 `task.result` 和每个
`work/agents/<child>/final_report.md` 均已落盘，问题属于完成交接，不属于模型未产出。

对应回归必须同时证明：每个 `subagent-completion.v1` 携带 typed status、最多 1000 估算 token 的最终回复
预览、精确 `final_report_ref` 和 declared/artifact refs；完成正文不得改变 typed status；同根 4 路成功
wake 在一次模型轮的 `metadata.events` 中全部可见；背景总上下文压到 2200 token 时仍保留四名 child 身份
和四个报告 ref；只确认实际进入本轮的 wake，新到事件继续 pending；失败与 Audit worker 不参加成功批。
修复后必须用全新 tmux/cwd 重跑同一 Prompt 3，仍只输入一次且不得给 main 发送“去哪个目录找”的提示。

`fd7d2b9` 部署后的第二轮使用 tmux `dsh-p3-research-fd7d2b9-verify2`，首次 4 名 child 的完成信封全部被
main 消费并触发第二批，第二批 代理运行时/长期助手 的两份信封也被消费；但最终只创建 6/8 名 child，漏掉
轻量运行时/通道运行时，横向汇总仍结束。canonical 文件证明 8 项原计划保存在 task-path 账本且仍 open，而后台
Task Runtime State 旧代码读取 request-id 账本。对应回归必须在相同 owner 下同时建立两份不同摘要的账本，
断言后台只注入 task-path 真账并同步其 child seed，且普通 open item 不触发机器验收或普通任务自动续跑。
部署后仍须使用全新 tmux/cwd 进行第三轮原样 Prompt 3，单测不能替代。

单 Gateway 多目录回归必须另行覆盖：两个不同 cwd 的 lightweight client 得到同一 owner-level
`gateway_workspace`，但请求分别携带自己的绝对 `workspace.cwd/roots`；thread 在后续未覆盖的请求中保留
该范围；模型前 workspace gate 拒绝相对/不存在目录；Tool Registry 的 path gate、resource lock 与 handler
使用同一 effective cwd；任务 workspace 提示和 child 相对 output ref 也落在该 cwd。runner future 抛异常
必须保存 `BLOCKED/UNVERIFIED/runner_worker_error`，不得被异常处理分支的二次错误掩盖。

`7c052f2` 在 `.7` 唯一 Gateway 上的第 1 条原样任务使用 tmux
`dsh-p1-pvz-7c052f2`，测试者只输入一次 prompt，六个 child 全部自然 DONE。最终
`/root/abc` 共 9 个文件，`0.0.0.0:8080` 监听，loopback HTTP 200；该轮同时抓到三个真实
回归点：首条 Todo 在 task 晋升前写入 request-id 账本，派工后却使用 task-path 账本；显式
`covers=[Todo id]` 未进入 child 展示投影，所以 8 项全未勾选；一次 main 模型轮返回被误显示为
“整理最终回复”，且 main 被错放在输入框下方。当前候选已增加“先晋升再选账本”、
`covers -> progress_item_ids`、大输出 result-envelope Todo 快照、`waiting` main 轮状态，并按 终端交互
`SpinnerWithVerb` 把main `Working` 放到正文末尾/Context 前，输入框下只保留 child。相关 6 文件
focused 组合 147 项已通过，但候选未部署前不计真机通过。

2026-08-21 子代理自然收口与 TUI 可观察性回归分三层执行：第一层覆盖 `turn_end`、普通自然完成、
递归 `create_subagents` 自动启动、模型工具表不含 dispatch/schedule/wait/inspect、父级直属
guidance/cancel/capability、cancel schema/回执不含 dry-run 查树旁路、普通 child 工作区继承，以及
OPEN request → BLOCKED → grant/deny 后同 run
PENDING 续跑；
第二层覆盖 TUI Working 动画、直属 child 固定职责行、实时上下文 token、thinking 灰色增量、条件式 follow-tail 与 Compact typed progress；第三层只在
`192.0.2.7` 的单 Gateway/真实 TUI 输入一次目标 prompt，测试者只观察产物、日志、child 树和 8080
监听，不旁路补代码。当前 backend focused 144 项、context/protocol focused 75 项已通过。由于本轮累计
增删超过 10,000 行，发布前追加一次且仅一次全仓 pytest；若发现失败，修复后只重跑失败项和相关 focused。

`714c0c8` 的直属活动区真机 smoke 使用 `.7` 唯一 Gateway 和 `my-agent-panel-smoke` TUI，会话一次创建
两个 child，分别在 46 秒、52 秒一次 attempt `DONE`，`a.txt=苹果`、`b.txt=月亮`。活动区从等待启动、
模型响应、工具活动推进到 `0 进行中 · 2 完成`，测试者没有插话推动。第二条完成 wake 的持久文件、ready
发现和 root active 状态都正常，但旧进程未取得 claim；优雅重启后同一 wake 立即运行并约 30 秒汇总。
后续回归必须把“durable source ready，但 process-local lane 长时间 queued/running 且无有效推进”作为独立
故障注入，证明不靠人工消息或 Gateway 重启也能有界自愈。

本轮唯一一次有效全仓测试使用仓库 `.venv` 运行到 100%，暴露 39 个失败：真实缺陷是窄终端闭合思考行
全角括号宽度漏算和 finalize 轻量参数缺少 `prompt` 时的防御读取，其余主要是测试仍断言已删除的
`SUBAGENT_RESULT`、手动 dispatch/schedule 与机器验收。修复后，对这些失败来源收集到的 468 项 focused
组合只剩 2 个测试期望/导入问题，二者精确复测 2/2 通过；动作协议/CLI 121 项、状态投影 36 项另行通过。
按用户约定不再重复全仓 pytest。

同日追加的 cwd/直属控制回归必须证明：`allowed_write_roots` 不选择工作目录，只有宿主
`execution_cwd` 可覆盖项目 cwd；Gateway 根任务和递归 child 的裸 `abc/...` 都落用户 cwd，隐藏 task
root 只接受显式 `work/...`/`output/...`；每个 child prompt 不含 `sibling_roster` 或兄弟完整 goal；
retryable child 仍可被直属父级显式打断。`/stop` 在前台请求已让出且没有 live claim/process 时仍选择当前
thread 的 ordinary root；TUI Working 只数 `status=active` link，interrupted sticky link 必须显示 0。

`.7` 的 `26563ac` 原样 TUI 进一步要求覆盖“任务晋升后的主代理写权”：主代理在 foreground 创建
`/root/abc` 后，background continuation 写同一目录不能因只剩 task `work/output` 而
`WRITE_FORBIDDEN`，也不能绕到 `additional_write_roots` 后把旧目录冒充当前交付。合同测试固定验证本地
Gateway 主会话的 `execution_cwd` 和 `allowed_write_roots` 同时包含 ToolRegistry 项目根；远程 owner、
task-local child 与 transient Audit 的窄权限测试必须继续通过。

子代理完成触发的后台 `run-*` 回合也必须从已加载 thread 快照恢复同一
`cwd/runtime_workspace_roots`：模型可见 Workspace Context、工具 `execution_cwd`、相对 `output_files`
以及下一批 child 的项目根必须继续指向启动该 TUI 的目录，不能退回单 Gateway daemon 的 `/root`，也不能
出现 `None` 拼接路径。focused 回归至少覆盖后台 `RunParams`、write boundary 和 child output 根三处。

严格 code-size 初次被当前提交 `31f30fe` 自身的 25 个未登记 hard finding 阻断。为避免把存量债务冒充本轮
回归，先从 `git archive HEAD` 纯净快照生成 baseline，再修掉本轮唯一新增的 `_progress_payload` 深嵌套；
当前 strict gate 通过，原始报告中的 4 个 hard 均能在纯净基线复现，本轮新增 hard 为 0。baseline 不取
当前脏工作树，因此没有把本轮新增问题写成豁免。递归创建的同配置每次上限与新建后代空验收字段又以
44 项 hierarchy/orchestration focused 复测通过。

`.7` 真实 TUI 首轮按原样植物大战僵尸 prompt 请求 3 个 child 时，结构化回执显示
`owner_active=12/available=0`，证明旧实现把其它会话的历史未终态 run 算进当前会话容量；同时该确定性
整批拒绝被误包成 `TOOL_OPERATION_OUTCOME_UNKNOWN`。回归应锁定：当前 root 没有 child 时，即使 owner
另有 12 个可恢复 run，仍可按单次上限获得 4 个槽；容量不可读/超限均返回
`effect_outcome=not_started`，不进入副作用未知收口。

容量修复部署后，同一 TUI 成功自动启动 3 个 child，三者约 3 分钟内均为 `DONE`；但一个后台主代理
回合从部分完成快照开始，模型只看到 `2/3`，其采样期间最后一个 child 结束，旧最终化读取新状态并把
根任务错误标成 `completed`，8080 未启动。新增回归在模型采样回调里把最后一个 child 改成 `DONE`：
首个旧快照回合必须保持根任务 `active` 且不投递完成；下一次从 `subagents_terminal` 快照开始的回合才
允许自然收口。该回归与 background runtime、gateway conversation、active-turn guidance focused 同跑。

新鲜度补丁后的真实 TUI 继续暴露三项底座问题：后台主代理用 thread 级 run id 命中旧任务，导致新任务
attempt/工具权威链错挂；同 thread 后台 notice 复用稳定 block id，reducer 丢掉后续消息；child goal 中的
旧版 child 没继承父级 `/root` workspace，且 `/root/abc` 没进入 `output_files`，所以当时安全边界拒绝写入。
当前修复改为普通 child 继承父级结构化上界，`output_files` 只补交付身份与验证线索。定向回归现覆盖 task-bound `_run_params`、
旧 run 跨 task 复用拒绝、notice 首次消费/独立 block、`items[].output_files` 嵌套 schema，以及
`send_guidance(target,message)` 的单 child/直接父子授权。创建后自动 `dispatch_supervision_auto` policy 和
旧 wait helper 已删除，历史 policy 只按结构化 tool 标记退休；不再用周期模型调用换取子代理可靠性。

`596118f` 首轮原样 TUI 任务又形成 capability 断链样本：child 工具账已存在 OPEN request，runner 却被
通用 completed 关成 DONE，父级 grant 后没有同 run continuation，主代理遂开始轮询并自行写产品。定向
回归必须锁住四件事：OPEN 优先投影 BLOCKED；grant 与 deny 都重排同 run；取消/接管终态不能复活；根、
子、孙只能操作直属下级。模型工具表和历史 wake policy 同时不得出现 inspect/dispatch/schedule。

`e321483` 的真实前台运行还要求检查“首次模型调用的工具面”，不能只检查进程注册表。
回归必须确认默认 `model_visible_specs()` 直接包含 create/guidance/cancel/resolve，仍不含已删除的
inspect/dispatch/schedule/raise_event；`tool_search` 继续只加载 goal/web/vision/MCP 等延迟能力。真机必须以
`create_subagents` 真实 tool call 和 2 个以上 child state 作为证据，不认模型文字里“我要派子代理”。

`c2c0235` 真机路径失败新增两组回归。写边界必须证明“宽 allow + 宽 forbidden + 更窄 task output allow”
允许写窄目录，而 allow/forbidden 同层仍由 deny 胜出；runner 活动必须用真实 `ToolResult.tool_name`，并在
模型请求、工具开始/结束时把有界短状态写进 canonical child state，不能保存 prompt、response、工具输出
或思考正文。发布前 focused 至少包含 `test_write_boundary.py`、`test_subagent_kernel.py`、
`test_subagent_debug_trace.py`、编排工具常量/注册和文件工具边界测试。
编排工具常量回归还会扫描所有活跃内置 `SKILL.md`，防止工具已退休后 Skill 仍教模型调用
`inspect_agent_tree` / `dispatch_subagents` / `schedule_child_subagents` / `raise_event`。

`d257dfb` 真机复验的 3 个 child 和 8 个文件已证明写边界/活动状态修复生效；最终 wake
被同 owner 另一条长 policy 回合饿死，增加后台会话车道回归。必须同时覆盖：
实际 `ready_thread_ids` 保留两条 durable thread identity；fake scheduler 的长 thread 不阻塞同 owner
的 sibling；真实 `BackgroundMainAgentScheduler` 在第一个模型调用挂起时，第二个同 owner
会话必须在 1 秒内进入模型。同 thread 仍由持久 run claim 单飞，不能为过测试放宽成双执行。
真机复验继续只用一个 Gateway，故意保留或创建一条其它 TUI 长后台回合，再确认当前
植物大战僵尸会话能自动消费 child 完成 wake、整合、启动受管服务并从独立请求核验 8080。

`bea6fed` 的四 child DONE/HTTP 200 样本还要锁住递归等待成本：task-local 父级创建孩子后必须返回
`interrupted/SUBAGENTS_ACTIVE` 并留下 exact direct-child wait；dispatcher/orphan 恢复不能在孩子活跃时
重新采样父级；嵌套 child 不发根会话 wake；同批成功收齐只释放一次，失败/缺状态/capability 阻塞立即
释放；崩溃巡检能从耐久标记补偿丢失事件。恢复后的 runner context 必须含有界 `direct_children`
status/result/artifact refs。`task_progress` 回归同时证明 open 项只作软账本、普通 final 零自动续跑，
写后 canonical child DONE 不能被模型传入的 pending 覆盖。

`e4cd58b` 部署后的原样 TUI 证明输出冲突还需区分两类：同批 item 重用一个路径时
`existing_run_ids` 必须为空，回执要求 `revise_proposed_output_scopes_and_retry`；真实未结束
sibling 占用时才返回 `await_existing_run_lifecycle_event` 和其 run id。两种失败都是整批
`not_started`，必须带 `preserve_user_constraints=true`；前者不得诱导模型等待不存在的 run。

`be531a8` 真机的 4 child 样本要锁住工作区继承：local child 的结构化
`workspace_root` 同时存在于 `allowed_write_roots` 时，只移除完全同路径的默认 home deny；
`.ssh`/Downloads 等窄 deny 仍在。非 workspace 的同路径 allow/deny 仍 deny 胜出，远程 owner 也不做
该调和。TUI 回归同时覆盖：foreground `running=true` 但 id 未绑定时 `/stop` 不发；foreground
已让出时 `/stop` 以空 expected turn id 进持久 outbox，由 Gateway 只选当前 conversation 唯一 live task。

`db41bb0` 部署后的原样 TUI 复验已有四名 child 全部 `DONE` 和完整页面文件，但 8080 最终未监听。证据
显示模型在前台 `run_command` 内使用 `nohup ... &`，同一条命令里的 curl 得到 200 后，foreground shell
结束时未受管 child 被清理。新增回归覆盖：独立 `&` 在任何模式都以 `not_started` 拒绝；`2>&1` 和引号内
`&` 不误伤；真正的 `run_in_background=true` 在工具返回后仍能由 `process_session` 看到、等待并由 registry
终止，其他 owner/TUI 会话不能通过可猜 session id 读取日志或发停止信号。
子代理侧同时覆盖单次 phase 快照、采样期间终态回复不公开、晚于采样时刻的 sibling wake 保持 pending，
以及全部 child 已终态时不依赖 active root task status 放行自然回复。定向 runtime/shell/error taxonomy
组合通过；按用户约定不重跑全仓 pytest。

`f1a7746` 部署重启后，旧任务的主 run/current attempt 已由崩溃调和置为 `unknown`，遗留 observation 每个
Gateway tick 仍进入自动挂载并触发 `RuntimeConflictError`。新增 focused 回归把 observation 上限设为 1：
旧任务事件必须保持未处理且零模型调用，同线程后到的新任务仍可运行；人工恢复后旧事件才被消费。仓储层
另验证 run/attempt 两层恢复投影、run `unknown -> created`、精确锁释放和新 attempt 挂载。真机部署后需以
Gateway 新日志偏移确认只出现一次结构化 recovery block、没有重复 `gateway-loop-error`。

2026-08-20 TUI 灰色层级与 tmux 复制修复在本地、`192.0.2.7` 各运行 renderer/view/input/ANSI/PTY/chat
6 文件 focused 组合，均为 105 项通过。测试机仅有一个 Gateway（8420），10 个 TUI 共享；真实中文请求
约 3.09 秒出现回答，ANSI capture 证明思考为 246 灰、助手正文为 231。该轮曾把 mock 的
`tmux load-buffer -w` 当作写穿证据；tmux 3.3a 真命令表已否定这一点，现由 `set-buffer -w` 候选修正。
外层系统剪贴板必须在用户 attach 的终端执行一次真实粘贴才可标最终通过。全仓 Ruff 20 项、strict
code-size 29 项均为修复前基线已有且本提交未新增；用户在获知后明确授权推送和测试部署，因此不能把本轮
记录写成“严格发布 gate 全绿”。

同日用户确认外层仍不能通过右键复制后，本地新增正文/输入框“已有选区时右键按下直接复制”回归：右键
事件必须在原控件前被拦截，中文宽字符全文只复制一次，配对 release 不得重复写入或清除高亮；lost right
release 仍可由无按键 motion/下一次其它 press 解锁。上述六文件 focused 组合现为 107 项通过；测试机部署
和用户本机系统剪贴板粘贴在完成前仍不得标成通过。

2026-08-18 活动输入/控制回执提交候选先运行 12 个原失败文件的 focused 组合，再运行一次修复后的完整
`pytest -q --tb=short`，两者均到 100% 且退出 0。changed-file Ruff、doc sync 和 diff check 通过；全仓
Ruff 的 112 项属于当前 HEAD 存量扫描结果，本轮变更文件为 0。strict code-size 仍有 25 个 hard finding，
因此本轮只允许部署到 `.13` 测试机，不得据此推远端分支或宣称严格发布 gate 全绿。clean-package 必须在
新增源码/测试先进入 Git index 后重跑；未跟踪正式源码被它拦住属于预期行为，不能用排除规则绕过。

2026-08-18 活动回合普通输入与上下文可观察性追补使用以下 focused 组合：

```bash
python3 -m pytest -q --tb=short \
  agent_py_agent/tests/test_chat_client_context.py \
  agent_py_agent/tests/test_tui_input.py \
  agent_py_agent/tests/test_tui_runtime.py \
  agent_py_agent/tests/test_tui_renderer.py \
  agent_py_agent/tests/test_runtime_guidance.py \
  agent_py_agent/tests/test_gateway_verbose_progress.py
```

必须分别证明：薄客户端 `/ask` payload 带精确 `message_id/expected_turn_id` 和完整执行选项；运行中 Enter
在真实 Gateway ID 已知后才进入活动回合，提交窗口的本地 `chat-*` 不得冒充 turn；pending
receipt 在真实注入事件前不进入稳定历史；外部 ID 不得移除本地 pending；Gateway 拒绝只撤销同一 receipt
并保留 follow-up queue；pending/queue 位于 fixed input-status pane 而非 transcript；rich/non-rich 客户端的
确认事件边界不扩大；active→queued 必须保留 inject/files/save/resume/client capabilities 且 chat-style 只
出现一次；guidance receipt 必须重算 embedded entry digest；队列文件名与正文 ID 冲突必须按文件名失败
归档且 provider 零调用；context usage 与 active-turn compaction 事件
不携带正文。`.13` 还必须用真实长回合
覆盖“连续两条补充、滚离尾部、注入确认、结束竞态”四步，单元测试不能替代真机通过。

2026-08-24 子代理详情页插话回执必须在上述主代理合同之上额外证明：Gateway 成功只是
`queued/pending`；快回执不能被后到的“正在确认”覆盖；两条连续消息在 provider 消费前均
保留 pending，消费后按 exact id/FIFO 进入 user history；child 公开事件 sink 只在 provider 成功后发
`active_turn_input_consumed`；系统提示明确禁止只在 thinking 中回应真实用户。focused 至少运行：

```bash
python3 -m pytest agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_tool_model_generation.py agent_py_agent/tests/test_tui_input.py agent_py_agent/tests/test_tui_agent_navigation.py agent_py_agent/tests/test_gateway_agent_control_service.py agent_py_agent/tests/test_background_notice_display.py agent_py_agent/tests/test_integration_coverage_context.py -q --tb=short
```

`.7` 真 TUI 必须在启动前公开 tmux 名称，进入一名运行 child 后连续发两条普通中文消息，验证
排队行、顺序消费、普通 assistant 回复以及 child 继续原任务。不能用直接写 guidance 文件或人工改任务
产物代替真实按键。

2026-08-24 已按上述合同在 `ma-2bf4602-child-chat-r25` 实按通过：从 Prompt 3 的主任务新增并进入运行中的
`agent-d1-researcher-10`，连续输入两条普通中文，两条均在 provider 消费前同时保留于 pending；消费事件
携带两个 exact client ids 且保持 FIFO，child 随后以普通 assistant 正文分别回答，并继续执行
`web_fetch`。`Ctrl+O` 没有切回主代理。唯一 Gateway PID `610573`，有效模型 MiniMax-M2.7；测试者未修改
长期助手 调研产物，也未停止 child。

Gateway chunk JSONL 的两个本机 reader 必须共享 byte-offset 合同：只消费换行已完整落盘的 UTF-8 行，末尾
半行保留原 offset，下一次补齐后恰好交付一次；暂时读取失败不得把 offset 清零造成重复。focused 使用
`test_gateway_client.py + test_gateway_streaming.py` 同时覆盖富 TUI 与普通 CLI。

## Focused Commands

```bash
python3 -m pytest agent_py_agent/tests/test_tui_reference_fixture_server.py agent_py_agent/tests/test_tui_events.py agent_py_agent/tests/test_tui_view_model.py agent_py_agent/tests/test_tui_ansi_snapshot.py agent_py_agent/tests/test_tui_markdown.py agent_py_agent/tests/test_tui_renderer.py agent_py_agent/tests/test_tui_runtime.py agent_py_agent/tests/test_tui_view.py agent_py_agent/tests/test_tui_input.py agent_py_agent/tests/test_tui_interaction.py agent_py_agent/tests/test_tui_paste.py agent_py_agent/tests/test_tui_preflight.py agent_py_agent/tests/test_tui_terminal.py agent_py_agent/tests/test_tui_transcript.py agent_py_agent/tests/test_tui_worker_paths.py agent_py_agent/tests/test_tui_pty.py agent_py_agent/tests/test_chat_prompt_queue.py agent_py_agent/tests/test_cli_chat.py agent_py_agent/tests/test_chat_parts.py agent_py_agent/tests/test_gateway_client.py agent_py_agent/tests/test_gateway_helpers.py agent_py_agent/tests/test_gateway_streaming.py agent_py_agent/tests/test_channel_message_tool.py agent_py_agent/tests/test_runtime_gate_ledger.py agent_py_agent/tests/test_tool_loop_recovery_scope.py agent_py_agent/tests/test_tool_round_execution.py -q
python3 -m pytest agent_py_agent/tests/test_owner_resolver.py agent_py_agent/tests/test_config_normalize.py -q
python3 -m pytest agent_py_agent/tests/test_subagent_manager_core.py agent_py_agent/tests/test_manager_board_class.py agent_py_agent/tests/test_subagent_coordinator_due_check.py -q
python3 -m pytest agent_py_agent/tests/test_lease.py agent_py_agent/tests/test_gateway_heartbeat.py -q
python3 -m pytest agent_py_agent/tests/test_planner.py agent_py_agent/tests/test_agent/test_dispatch_capability_followup.py -q
python3 -m pytest agent_py_agent/tests/test_code_size_script.py agent_py_agent/tests/test_architecture_guardrails.py -q
python3 -m pytest agent_py_agent/tests/test_registry_resilience_contract.py agent_py_agent/tests/test_attempt_sandbox.py agent_py_agent/tests/test_sandbox.py agent_py_agent/tests/test_tooling_shell.py agent_py_agent/tests/test_owner_scoped_pip_env.py agent_py_agent/tests/test_path_access_owner_scope.py agent_py_agent/tests/test_graceful_shutdown.py -q
python3 -m pytest agent_py_agent/tests/test_shell_orphan_kill.py agent_py_agent/tests/test_process_sessions.py agent_py_agent/tests/test_shell_bg_log_cap.py -q
python3 -m pytest agent_py_agent/tests/test_container_install.py agent_py_agent/tests/test_check_clean_package.py -q
python3 -m pytest agent_py_agent/tests/test_mcp_registration.py agent_py_agent/tests/test_offline_contract_matrix_gate.py -q
python3 -m pytest agent_py_agent/tests/test_tool_input_completion_provenance.py agent_py_agent/tests/test_tool_input_schema.py agent_py_agent/tests/test_tool_call_policy_contract.py agent_py_agent/tests/test_tool_call_parameter_gate_wiring.py agent_py_agent/tests/test_runtime_gate_integration.py agent_py_agent/tests/test_backends_tool_schema.py agent_py_agent/tests/test_backends_tool_schema_precise.py agent_py_agent/tests/test_mcp_registration.py -q
python3 -m pytest agent_py_agent/tests/test_owner_object_store.py agent_py_agent/tests/test_scale_runtime.py agent_py_agent/tests/test_runtime_schema.py agent_py_agent/tests/test_deploy_manifests.py agent_py_agent/tests/test_continuous_monitor.py -q
python3 -m pytest agent_py_agent/tests/test_gateway_chat_conversation_context.py agent_py_agent/tests/test_adapter_manager.py agent_py_agent/tests/test_adapter_feishu.py agent_py_agent/tests/test_asgi_ingress.py agent_py_agent/tests/test_scale_downstream.py agent_py_agent/tests/test_session_lock.py agent_py_agent/tests/test_persona_write_guard.py -q
python3 -m pytest agent_py_agent/tests/test_delivery_service.py agent_py_agent/tests/test_channel_message_tool.py agent_py_agent/tests/test_background_main_wake_recall.py agent_py_agent/tests/test_gateway_chat_conversation_context.py agent_py_agent/tests/test_adapter_manager.py agent_py_agent/tests/test_main_agent_delivery_closeout.py -q
python3 -m pytest agent_py_agent/tests/test_gateway_conversation_compact.py agent_py_agent/tests/test_gateway_verbose_progress.py agent_py_agent/tests/test_session_search_tool.py agent_py_agent/tests/test_gateway_per_user_scoping.py -q
python3 -m pytest agent_py_agent/tests/test_gateway_identity_trust.py agent_py_agent/tests/test_gateway_http.py agent_py_agent/tests/test_gateway_http_runtime_errors.py agent_py_agent/tests/test_gateway_per_user_scoping.py -q
python3 -m pytest agent_py_agent/tests/test_conversation_control_commands.py agent_py_agent/tests/test_chat_control_runtime.py agent_py_agent/tests/test_gateway_conversation_control.py agent_py_agent/tests/test_adapter_manager.py agent_py_agent/tests/test_thread_interrupt.py agent_py_agent/tests/test_gateway_helpers.py agent_py_agent/tests/test_tool_model_generation.py -q
python3 -m pytest agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_gateway_chat_conversation_context.py agent_py_agent/tests/test_runtime_gate_ledger.py agent_py_agent/tests/test_orchestration_tool_specs.py agent_py_agent/tests/test_wait_tool_self_wake.py -q
python3 -m pytest agent_py_agent/tests/test_conversation_goal_tools.py agent_py_agent/tests/test_conversation_control_commands.py agent_py_agent/tests/test_gateway_conversation_control.py agent_py_agent/tests/test_background_main_agent_runtime.py agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_audit_activation.py agent_py_agent/tests/test_watch_audit_guarantee.py -q
python3 -m pytest agent_py_agent/tests/test_orchestration_create_subagents_tool.py agent_py_agent/tests/test_orchestration_create_subagents_items.py agent_py_agent/tests/test_final_exit_contract.py agent_py_agent/tests/test_path_access_owner_scope.py -q
python3 -m pytest agent_py_agent/tests/test_ingestion_harvester.py agent_py_agent/tests/test_ingestion_puller_cursor.py agent_py_agent/tests/test_watch_spool_takeover.py -q
python3 -m pytest agent_py_agent/tests/test_log_redaction.py agent_py_agent/tests/test_structured_output.py agent_py_agent/tests/test_live_lab_model_preflight.py -q
python3 -m pytest agent_py_agent/tests/test_tool_operation_idempotency.py agent_py_agent/tests/test_tool_round_execution.py agent_py_agent/tests/test_tool_unresolved_runtime_issue_guard.py agent_py_agent/tests/test_compact_semantic_summary.py agent_py_agent/tests/test_memory_runtime_compact_auto_continuation.py agent_py_agent/tests/test_runtime_gate_ledger.py -q
python3 -m pytest agent_py_agent/tests/test_repeated_tool_failure_halt.py agent_py_agent/tests/test_runtime_guard_config_shared.py agent_py_agent/tests/test_tool_round_execution.py agent_py_agent/tests/test_cli_resume_contract.py -q --tb=short
python3 -m pytest agent_py_agent/tests/test_log_redaction.py agent_py_agent/tests/test_registry_resilience_contract.py agent_py_agent/tests/test_tool_context_reducer.py agent_py_agent/tests/test_tool_output_externalizer.py agent_py_agent/tests/test_compact_semantic_summary.py agent_py_agent/tests/test_memory_artifact_read.py agent_py_agent/tests/test_tooling_filesystem.py agent_py_agent/tests/test_mcp_client.py agent_py_agent/tests/test_mcp_registration.py -q
python3 -m pytest agent_py_agent/tests/test_compact_semantic_summary.py agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py -q
python3 -m pytest agent_py_agent/tests/test_model_call_ledger.py agent_py_agent/tests/test_tool_model_generation.py agent_py_agent/tests/test_gateway_helpers.py agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_subagent_hierarchy_scheduler.py agent_py_agent/tests/test_subagent_hierarchy_scheduler_tool_roles.py agent_py_agent/tests/test_subagent_hierarchy_write_policy.py agent_py_agent/tests/test_subagent_capability_request_tool.py agent_py_agent/tests/test_subagent_natural_language_e2e.py agent_py_agent/tests/test_local_collaboration_subagent_integration.py agent_py_agent/tests/test_gateway_chat_conversation_context.py agent_py_agent/tests/test_tools/test_tool_loop.py agent_py_agent/tests/test_background_main_agent_runtime.py agent_py_agent/tests/test_gateway_orphan_reconciler.py -q
python3 -m pytest agent_py_agent/tests/test_cli_run_conversation.py agent_py_agent/tests/test_cli_run_provider_timeout.py agent_py_agent/tests/test_runtime_mixin.py agent_py_agent/tests/test_run_task_workspace_writer.py agent_py_agent/tests/test_memory_tool.py::test_remember_user_explicit_goes_through_candidate_and_promotes agent_py_agent/tests/test_memory_tool.py::test_remember_single_add_tool_verified_authorized_auto_but_evidence_gate_blocks agent_py_agent/tests/test_gateway_chat_conversation_context.py::test_gateway_returns_answer_and_repairs_assistant_transcript_on_next_turn -q
python3 -m pytest -q --tb=short agent_py_agent/tests/test_verification_runtime.py agent_py_agent/tests/test_verification_project_facts.py agent_py_agent/tests/test_current_turn_execution.py agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_model_call_ledger.py
```

终端交互 TUI parity 的 focused 命令覆盖 typed event/reducer、Markdown/diff、spinner、权限续跑、输入、
history/search/paste/completion/queue/stash、follow/unseen、session-history、真实 Gateway readiness、title、
鼠标选择/OSC52、PTY recorder 和 ANSI replay。

可恢复工具失败回归必须复现“同一 provider batch 前部因资源锁冲突失败、后部同工具已经成功”的顺序：
默认配置不得写 `repeated_failure_halt`、不得进入 final-response 机器收口，错误与强返工提示继续回到当前
模型；显式 hard policy 仍可命中，而后到的同工具成功必须撤销早到的 active halt。不同工具成功不得误清，
精确同参重复动作继续由 action guardrail 拒绝。真实验收必须用单 Gateway 的 fresh TUI 原样重型 Prompt 4，
不能用测试者旁路修改项目来制造成功。
鼠标回归还必须覆盖：prompt_toolkit 传入的是源字符索引，中文宽字符不得再次按显示列换算或只复制一半；窗口外丢失 mouse-up 后，首个
`MouseButton.NONE` motion 或下一次 fresh press 只结束旧拖动，后续 hover 不再扩展；一次 settled selection
只自动复制一次，并同时保留 prompt_toolkit、OSC52 与 `tmux set-buffer -w` 外层剪贴板路径；iTerm2
因已知 SSH 崩溃风险只保证 `load-buffer -` 和应用 OSC 52 尝试。输入回归还要覆盖鼠标松手自动
复制、Ctrl-C/右键复制且保留输入高亮、右键 press/release 只复制一次、Ctrl-V/终端 bracketed paste 替换选区，以及 marker 普通空格不会触发
`nbsp` 下划线。运行中普通输入还要证明下一次
真实模型调用能看到该输入；若 exact turn 已结束，TUI 只能挂接 Gateway 返回的 canonical queued request，
不得再次提交正文。

2026-08-24 起鼠标验收采用 终端交互 默认合同：`tui_mouse_capture_default=true` 的 prompt_toolkit VT100
启动必须开启 1000/1003/1006 mouse tracking；F6 第一次须关闭并进入原生选择，第二次须恢复 mouse
tracking，且两次都不改输入。默认模式继续运行上述中文/丢失 release/OSC52/tmux 回归，远端 notice 只能
报告已选中并尝试复制，不能把投影成功写成系统剪贴板成功。最终真机
验收必须在用户实际 attach 的宿主终端执行一次“拖选中文 -> 右键复制 -> 输入框右键粘贴”；tmux buffer
内容或 OSC52 字节只能作中间证据，不能替代这一步。

同日 `.7` 的 tmux `ma-af5a03b-native-copy-r16` 完成真实协议验收：启动原始输出只有 1000/1002/1003
disable；中文/表情 bracketed paste 完整进入输入框；F6 第一次输出对应 enable，第二次输出 disable，且
输入未改变。测试时保持唯一 Gateway 和 MiniMax-M2.7。因为自动化不能控制用户的 Terminal.app，macOS
右键菜单与系统剪贴板的最终通过状态仍须由用户 attach 后实际复制、粘贴确认。

代理视角滚动回归必须至少建立两个独立 `TuiStateStore`：root 与 child 都先离开尾部，来回切换后各自恢复
原 cursor/follow/unseen，第一次进入的新页位于尾部；切换必须清除当前选区，不能把 root 坐标套到 child
正文。真实 TUI 还要分别证明默认物理滚轮、`PageUp/Ctrl+Home` 都能查看当前页完整历史，离尾后保持锚点，
回底后才自动跟随；F6 切到原生复制后 footer 明示键盘入口，再按 F6 恢复滚轮。原生模式下滚轮不进入应用
是 terminal alternate-screen 协议边界，测试不得把终端空 scrollback 误报成 canonical history 被删除。

2026-08-24 真机证据：`d14549c` 已部署到 `.7` 唯一 Gateway，tmux
`ma-d14549c-scroll-r18` exact resume `sess_1787579367_f677cf76`，没有重新提交模型任务。root 与
worker-1 分别 `Ctrl+Home` 后来回进入/返回，均恢复各自原始 prompt 锚点；返回 root 出现历史操作提示。
F6 开启 mouse tracking 后注入真实 SGR wheel-up，正文从终态总结翻到旧 Bash/Thought；再次 F6 后回到
原生复制。该证据验证 TUI 协议路径，不代替用户在 Terminal.app 外层亲手滚轮、右键复制和粘贴。

2026-08-24 终端交互 默认滚轮回归真机证据：`0da26f0` 部署到 `.7` 唯一 Gateway 后，fresh exact resume
tmux `ma-0da26f0-终端交互-history-r23` 未发送模型消息。启动 footer 直接显示滚轮入口；默认 SGR wheel-up
使 root 离尾并出现 `Jump to bottom`，wheel-down 回尾后 pill 消失。进入 researcher-1 后默认滚轮翻到完整
派工 Prompt、Thought 和工具卡。F6 后 footer 改为键盘历史/恢复滚轮，再按恢复默认。输入框应用内拖选与
右键都把“中文复制验证ABC”完整写入 tmux buffer；外层系统剪贴板仍保留给用户 attach 后亲测。

富 transcript 追补还必须覆盖：未声明能力的 Gateway 不公开 thinking/display 且继续按 verbose 裁剪；TUI
声明能力后逐轮 commentary、provider 明示 thinking、edit/overwrite/patch diff、write preview、命令
stdout/stderr/exit code 均走结构化事件；思考 Markdown 与 `Ctrl+O` 折叠提示的每个可见 fragment 都必须
以最终 muted/thinking role 覆盖正文前景色，不能只断言行前缀是灰色。失败后最后一次 workspace mutation 的软续跑只触发一次，简单写入
和已有后续检查不触发。供应商网络回归还要用真实 `ConnectionRefusedError/ECONNREFUSED` 证明传输层
`2/5/15` 秒三次退避、模型回合层 `10/25/45/100/180` 秒五次恢复和富 TUI typed retry 提示；普通
`"connection refused"` 字符串不得取得重试权，DNS 只认异常链中的 typed `socket.gaierror`，畸形 URL、
认证和代理配置仍快速失败。常用定向命令为：

```bash
python3 -m pytest -q --tb=short agent_py_agent/tests/test_provider_connection_error.py agent_py_agent/tests/test_provider_transient_auto_resume.py agent_py_agent/tests/test_runtime_error_reports.py agent_py_agent/tests/test_gateway_helpers.py agent_py_agent/tests/test_gateway_verbose_progress.py agent_py_agent/tests/test_gateway_streaming.py agent_py_agent/tests/test_gateway_client.py agent_py_agent/tests/test_tool_model_generation.py agent_py_agent/tests/test_tui_runtime.py agent_py_agent/tests/test_tui_renderer.py agent_py_agent/tests/test_tui_worker_paths.py agent_py_agent/tests/test_tool_round_execution.py agent_py_agent/tests/test_tools/test_edit_file_tool.py agent_py_agent/tests/test_tooling_filesystem_write.py agent_py_agent/tests/test_tools/test_shell_tool.py agent_py_agent/tests/test_current_turn_execution.py agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_tui_pty.py agent_py_agent/tests/test_tui_ansi_snapshot.py agent_py_agent/tests/test_tui_view.py
```
`test_tui_pty.py` 必须保存固定 TERM/locale、
终端尺寸和 reference/target fixture 身份；golden 只允许脱敏后的 ANSI/结构化片段。矩阵项目只有在
对应 test 与测试机 evidence run 同时存在时才能标为 `VERIFIED`。外部 终端交互 provider 健康不属于
UI 验收前提，参考客户端使用 loopback deterministic Anthropic fixture；MiniMax-M2.7 只用于 my-agent
真实链路，不得把 key 写入 pytest output、录屏或 fixture。

真实代码任务的交付验收必须把“命令退出 0”和“有测试覆盖”分开：`go test ./...` 出现 `[no test files]`
只能证明 package 可装载，不能证明行为测试通过。还要检查 output 总大小、隐藏目录和 cache/debug 文件；
process sandbox 回归需证明 canonical `task_work_dir/.sandbox-tmp` 承载 `/tmp`，项目 cwd 不出现
`.sandbox-tmp`，owner-scoped 环境的 `TMPDIR=/tmp`、`XDG_CACHE_HOME=/tmp/.cache`。

写后验证新鲜度回归必须覆盖“verify 成功→workspace mutation→read/search→plain final”的 stale 事实仍能
进入下一次模型上下文和审计 metadata，但不得覆盖 plain final 或自动追加模型调用。验证 envelope 必须从
`metadata.handler_details` 读回。模型调用账本还必须证明明细超过 `max_records` 后 request/run 的 logical、
physical、provider attempt/retry、status 与 input/output/cache-read/cache-write token 累计不截断；
真实 provider usage 与估算调用必须分开计数，旧任务 response 的 128 不能再当精确总数。

完成收口与 operation 审计要分层回归：`succeeded mutation -> not_started tail`、failed 和 unknown 都继续
留在 typed operation ledger；普通模型看过这些结果并给出 plain final 后立即自然结束，不得出现隐藏
completion conflict、Todo closeout 或 stale followup。模型在正常下一次采样中主动调用修复工具仍应可用；
`unknown/cancelled/incomplete` 的执行期安全门和显式 required action 继续使用自己的结构化 gate，不能借删除
普通完成判官而绕过。owner-scoped shell 环境还必须证明 `TMPDIR=/tmp`、`XDG_CACHE_HOME=/tmp/.cache`、
`NPM_CONFIG_CACHE=/tmp/.cache/npm`，同时 `HOME` 只读边界和凭据擦洗不变。

终端交互 命令/输入追补至少覆盖：`/context` 使用自动 compact 同一估算而不写状态；手动 `/compact` 取得
同一 run lane、写 checkpoint 并推进 generation，live turn 时拒绝；可选摘要要求不能覆盖 operation evidence；
`/effort` 在 backend 没有结构化能力时查询成功但设置失败且不改参数；Up/Down 先走 ASCII/CJK/恰好满行的
软折视觉行；`N new messages ↓` 左键释放后恢复 follow-tail。对应 focused 文件为
`test_conversation_control_commands.py`、`test_chat_control_runtime.py`、`test_gateway_conversation_compact.py`、
`test_gateway_conversation_control.py`、`test_tui_input.py` 和 `test_tui_view.py`。

外部消息工具还要覆盖两个对称面：无 proactive owner route 时，`send_message` 实现仍注册但必须出现在
`runtime_snapshot.unavailable_tools`，且不进入 specs/retrieval；有真实 provider、target、owner root 与
proactive capability 时仍进入可见/可执行快照。对应 `test_channel_message_tool.py`，真机再用同一句普通中文
问候比较修复前后的工具块数量。

2026-08-18 命令/输入追补最终本地 89 项 focused tests、`.13` 精确 23 项到 100%；changed-file Ruff、
py_compile、doc-sync、strict code-size 与 diff check 通过。改动远小于 10,000 行，未重复全仓 pytest。
远端输出、TUI capture、进程与部署 hash 保存在
`/root/tui-parity-evidence/context-controls-20260818T1630CST/`。

2026-08-18 本轮只运行相关 focused 文件：本地与 `.13` 均到 100%（保留预期 xfail），changed-file
Ruff 与 py_compile 通过。改动远低于 10,000 行，按用户约定没有重复运行全仓 pytest。Tornado 真机任务
只证明核心 10 项测试曾通过；examples 在其后修改却未复测，所以整体结果明确记为未通过。后续
aiohttp→Go 在最终修改后重新 build、通过 29 项测试和 HTTP E2E，作为 EXEC-44 的真实闭环证据；其末尾
no-effect 清理又形成 EXEC-45 的独立反例，二者不能混写成同一结果。

测试机 evidence 分轮保存在 `/root/tui-parity-evidence/`：reference、mainchain、input-state、transcript、
interrupt、permission 和 final run。最终 `long-session-10k-optimized.json` 在 120×29 下建立 10k 回合/
20k stable block、39,999 rendered lines；idle animation tick 复用同一 frame，平均 6.4ms，真实可见状态
变化重绘平均 45.8ms。该压测与 `test_idle_animation_tick_reuses_static_long_transcript_frame` 一起证明“不在
空闲时全量重建”，不能只引用首帧时间。

本轮 TUI focused suite 327 项运行到 100%。代码与测试变更超过 10,000 行，因此额外执行一次全仓 pytest：
缓存记录收集 24,663 项，运行到 100% 且退出 0；此后不重复执行。最终 `.13` 部署后的 reducer/view/
renderer/runtime 回归 49 项和 changed-file Ruff 均通过。全仓 Ruff 尚有 119 项历史问题，而独立 clean
`main@2d5a964f` 为 122 项；本任务没有新增 lint debt。doc sync、strict code-size、diff 与 staged
clean-package 守卫通过。

`CODE_SIZE_BASELINE.json` 在本轮只登记 clean `main@2d5a964f` 已存在的 10 个严格 size finding；登记前在
独立 HEAD archive 上复跑并得到同一身份集合。新 TUI/审批/中断代码不得借该 baseline 隐藏新增 blocker，
每轮仍执行 `python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json`。

CLI Memory 入口回归必须证明：user 原文在首个模型调用前进入 ConversationStore，`remember(user_explicit)`
取得唯一 user `source_message_ref` 并按统一 Promotion 晋升；相同 request/role 重放不重复，内容或
run/task lineage 漂移 fail-closed；assistant 尾部落账失败只产生 typed degradation。one-shot thread 不得
预填或遗留伪造/active task link：无 task-promoting tool 时 links 为空；真实工具晋升时只允许留下
`completed` link，且 active ids/links 为空。standalone workspace 必须进入终态，Gateway 的既有会话 lifecycle 不变。
真实 testbox 仍需另外保留 ConversationStore、candidate/formal memory、workspace、runtime event 与重放证据；
定向单测不能替代真实 provider 运行。

2026-08-12 的隔离 B5R3 已完成上述真实 provider 验证：`anthropic_compatible/deepseek-v4-flash`、CLI
RC=0、ConversationStore user/assistant=2、Candidate/formal=1/1、唯一 user message ref、workspace
`DONE`、`status_conflict=0`；同 request 纯存储重放前后全部语义计数和 ID 不变。真实工具晋升产生的
唯一 link 为 `completed`，active ids/links 为空。脱敏原始证据位于 testbox
`/root/memory-evidence/会话运行时-MEM-20260812-B5R3/`；该结果不替代 Goal 中尚未闭合的 Gateway/Curator、
第二模型、重启恢复与长文本验收。

`test_tools/test_tool_loop.py` 覆盖普通任务不会被旧进度清单劫持、显式 goal 的 open-plan 生命周期、工具轮数上限和
后台 continuation；`test_turn_end.py`、`test_subagent_finalize_helpers.py`、`test_subagent_protocol_contracts.py`
覆盖六类结束原因、自然结果保存与递归控制面；`test_conversation_goal_tools.py` 覆盖一会话一个未完成 goal、
精确创建/更新/完成边界。

主代理完成表达回归必须覆盖：普通 `task_progress` 即使仍有 open item，也只是一份可恢复的进度笔记，
不能拦截模型本轮回复、追加隐藏提醒、自动唤醒后台执行或要求下一轮先选择/关闭旧任务。只有显式持久
`/goal` 的 open plan 才保持 `unfinished` 并由既有 continuation 续跑。该行为不得解析“完成”等自然语言、
扫描任务目录、执行验证命令或给普通 task 增加完成硬门。终态普通 task 续作必须保留旧终态和 cwd、
创建新执行 task id；只有精确持久 `/goal` 可以原 id 恢复。子代理普通工具必须是父 run 快照的严格子集；
coordinator 可通过统一 `create_subagents` 继续递归创建，所有 leaf 都不得获得
create/guidance/cancel/resolve 四个直属下级控制工具，但仍可用 `capability_request` 为自己申请权限。
直接创建和层级调度都要覆盖这条规则。模型调用账本必须区分 logical turn、
物理 model attempt 和 provider HTTP attempt，并覆盖并发首次请求、重试、失败、超时和迟到 finish。
task-local child 即使携带父 conversation id，也必须证明可在自己的 runner lane 正常写入授权产物。
sticky workspace 回归还必须覆盖：新 execution 复用旧 task path 时，四份当前执行投影同步换成新
request/run/task，旧 output 与 artifact 列表保留，timeline 只追加一次；相同 task 恢复时保留既有
progress/evidence 并回到 RUNNING。损坏投影不得在激活时被静默洗掉，旧 task link 也不得覆盖新投影。

Gateway 会话控制回归还必须覆盖：已有 linked live turn 时 `/btw` 只写 guidance、不发布
第二个 wake；`/stop` 在线程阻塞于模型 JSON/SSE 读取时主动关闭响应，并以用户中断结束，
不误判为 provider 网络故障或等完整超时。测试必须使用生产同样的 wall-timeout guard 子线程，
不能只证明在任务登记线程内直接调 provider 的简化情况。本地 CLI 还必须用 worker 与界面共享的
精确 request id 测试，不能读取 thread-local `agent._current_run_params` 假装跨线程可见。所有
支持的 `/XXXX` 必须证明命令词不进入 transcript/guidance/模型；未知命令必须 fail-closed。

Gateway compact 回归必须覆盖：候选只有在包含 summary、近期 raw tail、已压缩与近期工具事实及当前
用户输入的完整投影低于精确配置阈值时才能提交；checkpoint 写失败、CAS 冲突和过大候选都不得推进
summary/cursor/generation。近期尾部只能按 user/assistant role 选择完整回合，不能分析正文；旧 v4 thread
安全加载为空的 v5 guard 字段；连续三次失败进入冷却，冷却后成功半开并清零；连续两代 checkpoint
能按 previous pointer 串联，raw transcript 始终不删。

原生工具长链还必须覆盖完整 provider-visible 计量：prompt、工具 Schema、ToolCall 参数、
ToolResult、UserTurn 与尚未转发的运行引导都要进入同一 token 估算。大 `write_file/edit_file`
参数不能因工具结果很短而漏算；达到配置阈值后只能整对移除最旧调用/结果，保留最新往返与全部
运行中用户输入，并按既有 recent-tail 预算一次取得余量。被回收旧段必须由同一 IR 中最多一条
非权威语义 summary 承接；下一次跨阈值要把前代 summary 作为输入再原位替换。摘要调用失败必须回退
机械 handoff；摘要、handoff marker 与近期尾部都要纳入同一预算。连续两次再次跨阈值时不能堆叠
summary/窗口标记、遗失最新用户纠正、留下 tool-use/tool-result 孤儿或退回 Gateway 同 turn 重启。
展示回归还必须覆盖 presentation/no-save 回合：公开 `compact_trigger_tokens` 仍等于统一
配置压缩点（例如 128k 窗口的 115.2k），不得因当轮禁止持久 apply 而变成 128k/100%。
同时要用超过 90% 但未达完整窗口的 no-save 输入证明它没有获得落盘 Compact 权限，
避免为了修 UI 暗改执行边界。
2026-08-22 真机证据：`192.0.2.7` 唯一 Gateway、MiniMax-M2.7、tmux
`dsh-p3-774c7fe-compact`，只发送一次原样 Prompt 3。main 首轮 27.5k、终屏 61.5k 均显示
`压缩点 90%`；8 个 child 全部 DONE，最高 98.6k，本轮不得写成真实 Compact 触发验证。

Gateway/IM 投递回归还必须覆盖：同一进度批次重试使用稳定 provider 幂等键，不同 progress cursor 与
最终回复使用不同键。身份只取可信 message ID、request ID、phase 和 cursor，不能从回复正文猜测；
否则平台可能把同一请求后续的真实进度或最终回复当作重复消息吞掉。
终态权威回归必须另外制造合法、截断和陈旧的孤立 `responses/<id>.json` 以及
`done/failed` 投影，证明它们不会让 provider 跳过执行、让 stale 请求消失、让活动输入误判
终态或让 CLI/TUI/plain 提前结束；只有 schema 和内外层 request ID 都匹配的
`requests/terminal/<id>.json` 可以返回最终答复。
必须再覆盖 provider 已成功、紧接着 `/stop` 先把同 attempt 写成 closing/cancel 的顺序：ACK 仍应把
已进入模型的 guidance 收成 consumed，不得永久卡在 submitted；相反 stop 在 provider admission 之前
先赢时必须零次调用 provider。audit clear、linked task stop 和 window stop 均要通过同一 T 锁路径。
同 ID 恢复回归还要在 canonical 已存在后人为放回一份 owner/prompt/options 不同的 hot
processing 文件，即使两者最终文案相同也必须保留 hot 并报冲突；只有重算后的
`gateway_request_fingerprint.v1` 一致才能幂等退役热文件。
同时在 inbox 文件已移入 processing、attempt/lease/fingerprint 栅栏尚未成功的精确写入点注入
`OSError`，验证 claim 返回失败、原请求回到 inbox，且 processing/canonical/response 都不留半成品。

Adapter durable ingress 的 focused 命令为
`python3 -m pytest agent_py_agent/tests/test_adapter_ingress.py agent_py_agent/tests/test_adapter_manager.py -q --tb=short`。
必须证明 route callback 在媒体和 POST 前已经落盘可信身份与 canonical digest；same-id/same-body 幂等，
diff-body 隔离；媒体瞬时失败停在 prepared 且 POST 尚未发生；Gateway 响应丢失按原 body 重试，
submission 落盘后崩溃不重 POST；唯一 worker 继续推进
input-status/result/control-status/placeholder，IO callback 不持 store lock；ingress/reply 两类持久 row 必须覆盖
A/B store 同时读取、租约未过期拒绝 B、过期后 B 以更大 epoch 接管、A 的旧 epoch CAS 失败。还要断言
POST 直接发送持久 `gateway_payload`，progress handle 可变，429/5xx 保持 WAIT，auth/config 隔离且不发送
伪终态正文，input `terminal_unknown` 清占位并留下 durable unknown receipt。`/btw` unknown 必须保存
`operation_id/receipt_id/control_state`，以 operation ID 而非目标 `request_id` 建 `control_receipt` watcher。
同一 target turn 的两条 `/btw` 必须生成两个 pending 文件和两份独立终态；目标初始为空时，首次 GET 可在
同 epoch CAS 绑定，后续不同 target 必须 quarantine。stop rejected 必须有明确回复，stop
`terminal_unknown` 必须清占位并留下 durable unknown receipt，二者都不能静默完成。control pending 还要
冻结 channel/user/conversation/chat type/chat id，逐项路由漂移均 fail-closed；`/control-status` GET 必须
发送这五项结构化身份头，不能把可猜的 operation ID 当鉴权能力。

Slash 控制操作回执的 focused 命令为
`python3 -m pytest agent_py_agent/tests/test_gateway_control_operation.py agent_py_agent/tests/test_gateway_conversation_control.py agent_py_agent/tests/test_gateway_http.py -q --tb=short`。
必须覆盖副作用前 prepared、执行前 executing、结果落盘 completed 和崩溃 terminal_unknown 四个切点；同一
message ID/正文只执行一次，同 ID/异正文零次新增副作用并返回冲突。`/control-status/<operation_id>` 只可
读取 authenticated owner 的回执；`/btw` 可以从已有 guidance receipt 推进 accepted/rejected，其他控制和
没有 guidance 证据的 unknown 均不能因 GET、TUI 重连或 adapter 重启而再次执行。若 wrapper 已越过
executing、但 guidance 尚未落盘就崩溃，只能在 exact turn 终态证据下收为 rejected；active、recoverable、
corrupt、absent 四种非终态证明都必须保持 terminal_unknown。群聊回执必须冻结
`channel_chat_type/channel_chat_id`；同 message ID 改群身份应在副作用前冲突，GET 对账仍进入原 group owner。
回执还必须冻结首次解析的 canonical owner ref；在 `prepared` 落盘后切换 per-owner 配置再重试，effect、steer、
stop、task link 和子代理取消仍只能命中原 owner，不能按新配置重算。
TUI 控制 outbox 的 focused 命令为
`python3 -m pytest agent_py_agent/tests/test_tui_control_delivery.py agent_py_agent/tests/test_chat_control_runtime.py agent_py_agent/tests/test_tui_input.py -q --tb=short`。
必须证明 persist-before-POST、两次传输请求复用同一 message ID、收到 operation ID 后永久 GET-only、
terminal_unknown/conflict 不换 ID 重做、重启恢复原行，以及未绑定 exact turn 时 `/btw`/`/stop` 零次发送。
长控制正在执行时，重复 POST 与 GET 还必须在有界短时间内返回同一 `executing` 回执，effect 调用次数仍为
一；原 worker 释放 C 后才能出现 completed 或确证中断后的 terminal_unknown。Gateway IO focused 还要模拟
无 fcntl 的 Windows 分支，证明 `msvcrt` lock/unlock 成对发生，而不是仅发告警后无锁运行。
还要证明明确 lock contention 才返回未领取，坏句柄等错误会上抛；两种跨进程原语都缺失时必须 fail-closed。

停止后续接回归必须覆盖：新 gateway request 的 `task_id` 与原持久 task 不同时，`/btw` 仍从
`task_attributes.conversation_task_id` 消费一次；`/stop` 只中断当前真实 live turn，没有运行内容时
不得修改旧 task/goal；停止后的下一条普通消息无需 select/start/close 命令即可聊天或在 sticky cwd
继续工作。若命中一个已终态工作目录，首个 `promotes_task` 工具自动创建本轮执行身份，旧终态保持不变。

容器节点真验收不能只看单测：最终镜像必须运行
`python -m agent_py_agent.agent.tooling.sandbox --quiet` 并退出 0。工作树检查使用
`python3 scripts/check_clean_package.py --mode worktree .`；wheel/tar 发布前再以
`--mode artifact <制品>` 检查实际成员和大小预算。

## Full Command

```bash
python3 -m pip install -e ".[dev,secrets,scale]"
python3 -m pytest -q --tb=short
ruff check agent_py_agent scripts
```

默认测试集覆盖 secrets 加密与 scale 存储/队列，因此 CI 和全新开发环境必须显式安装三套正式
extras；不能依赖宿主机碰巧已有 cryptography/SQLAlchemy，也不能用 skip 把缺依赖伪装成通过。
生产 wheel 使用 PEP 517 默认隔离构建，让 `[build-system].requires` 独立决定构建后端。

真实主代理/子代理链路通过后，再提交和推送。

多个外部写的专项回归必须覆盖：同轮保持模型原顺序；每项结果独立留痕；权威 operation 终态写入失败
时把表面成功降级为 unknown 且同操作不再执行；archive、runtime event、机械 compact 续跑和语义 compact
都保留 operation/effect 事实。该回归不要求通用 Saga，也不能用自然语言猜依赖或补偿动作。

路径极端回归还必须覆盖：显式未授权绝对路径保持原目标身份并返回 `WRITE_FORBIDDEN`，原目标与任务
`output/` 下的替代路径都不得生成；失败前后的独立合法写仍能按顺序完成。裸相对路径必须继承当前可信
cwd/workspace；相对 `output/...`、`work/...` 的结构化任务落位继续生效，不能把裸项目路径误投到 task
output，也不能用绝对路径静默搬运兼容层冒充成功。

后台可观察性 focused 必须覆盖：Gateway 成功快照返回 canonical active-root count 和有界
direct-child 行；只选当前 thread 活跃 root 的直属 child，不展开 grandchild/历史 root，不泄露 goal、工具输出、
路径或权限。TUI 用一个可移除的固定 Working 区域显示 main；输入框下每个 child 只占一行，显示名称、
状态、职责短标题、耗时、当前模型可见上下文 token、Compact 和真实重试。短标题按剩余终端列截断，
不得回退“模型响应中/模型已生成回复”或累计计费 token。相同快照不重复追加，数值变化原位更新，root
归零整体删除；HTTP/解析失败不把上次真实活动误清零。
单 Gateway 多 TUI 还必须覆盖传输背压：HTTP server 的 accept backlog 不得退回标准库默认 5，客户端成功
快照约每秒一次；连续传输/合同失败按 0.5、1、2、4、8 秒退避，成功后重置。部署复验需同时记录唯一
Gateway PID/端口、监听 backlog、Gateway CPU/线程数、`SYN-SENT` 数量和至少四个独立 TUI 的活动刷新；
不能用多个 Gateway 或杀掉正在计分的任务掩盖争用。
main 的后台 context 也必须来自同一次 `model_visible_context_usage.v1` provider preflight；每轮模型调用时
实时更新，不能永远停在首次 8.7k。Todo 必须从当前 active task 的 canonical `task_progress.v1`
投影；后台更新原位替换，active link 关闭前后的最终 notice 还要携带最后一份 `id/title/status` 快照，
确保模型最终回复出现时已完成项仍打勾。以上两类字段只用于显示，不得成为结束、恢复或验收事实源。
Todo 与 child panel 必须按结构化身份去重：`item.id` 精确等于当前直属 `child.run_id` 的自动 seed 项只在
TUI 隐藏，canonical task_progress 账本不删除；普通 Todo 和显式 `covers/progress_item_ids` 仍显示并打标。
禁止匹配“子代理”标题或 goal 文本来决定隐藏。
Todo 快照还必须区分“未提供”和“明确为空”：前者保留上一份有效清单，后者删除
`todo:task_progress` 活动块。最终后台 notice 只有 exact child seed、因此投影为空时，也必须在 assistant final
出现前清掉旧 `0/N`；不能把空列表误当成网络失败，也不能因此改写 canonical `task_progress.v1`。
Todo 超过四项时，默认投影必须恰好保留四条任务行：最近完成、typed `in_progress` 和下一条
`pending/blocked` 按状态优先；多个运行项优先占位，运行图标与 Working 共用动画时钟。`Ctrl+T` 展开后
显示全部 canonical 顺序，再按一次收起；该按键不得编辑或提交输入、不得写 task_progress。常驻 Context
显示总量、窗口占比、ConversationThread `compact_generation` 与同一 usage 预算算出的明确“压缩点”；
`compact N`、`压缩点 90%` 和临时 Compact 操作进度不能混为一类。prompt/messages/tools 的详细构成只由
`/context` 命令展示。持久 main/child/grandchild 发生 `model_visible_context_compaction.v1` 前，旧
ToolCall/ToolResult 必须已经以 `source_kind=live_tool_ir` 写入 owner checkpoint，并通过同一
ConversationThread generation CAS；事件展示该 canonical generation，不得写 exact run 或另加计数。
presentation/no-save 辅助回合才允许只发 turn-local 事件。child/grandchild 常驻次数必须只读各自
`agent_thread_id` 对应 ConversationThread 的 generation/checkpoint；即使旧 durable apply ledger 或遗留
native attribute 仍存在，投影也必须忽略。回归同时覆盖轮前阈值 Compact、运行中阈值/连续 Compact、
provider overflow 强制 Compact、上一代摘要合并、checkpoint-before-CAS 失败回滚、typed tool/guidance
进度携带、正文隔离和旧 continuation 不再生成。摘要回归必须用位置敏感 fake backend 锁定 provider 请求：
真实任务 prompt 是首条 user，完整 native history 在中间，Compact 要求是最后一条 synthetic user；倒退为
`prompt + messages` 的首指令顺序时，fake 必须像 MiniMax 真机一样返回普通续写并让测试失败。还必须用
已经绑定父 conversation task 的真实工作工具回归证明：child `agent_thread_id` 不覆盖继承的
`conversation_thread_id`，父 task link 不变，child 写入成功且消息只进入 child thread；禁止只用不调用工具
的 fake backend 掩盖 task-thread 重绑定冲突。另用 `output_files` 为空的 Gateway 会话回归证明：任务晋升前
只把 host-validated client cwd 当启动上下文；晋升后 main 与直接 child 的 `owner_workspace_dir`、
`execution_cwd`、产品写根和工具围栏全部指向同一个 owner-scoped canonical task root，不能退回单 Gateway
的 daemon 仓库、客户端临时目录或另造 child 家目录。
内部 child 状态路径的 shell 拒绝还必须覆盖两层：ShellTool 返回
`WRONG_STATUS_SURFACE + effect_outcome=not_started`；经过 canonical authorized dispatch 后即使
`handler_executed=true`，operation 仍归确定性 `failed` 而非 `unknown`。模型应收到原拒绝原因后换用
直属生命周期事件或正式 result refs；测试不得为了通过而放开内部路径，真实未知副作用也不得降级。
父级共享上下文还必须证明当前轮无 read archive 时不会复用 agent 上一次任务的缓存内容。
同一 exact parent 分多批创建默认命名 child 时，第二批编号必须从既有最高序号继续，不能重新出现
`worker-1/worker-2`；显式 run id 仍是身份权威。用户明确“主代理只能协调/不准自己写”时，真实 TUI
必须证明容量满、创建参数失败和 child 完成均不会让 main 自行编写被禁止的功能代码；main 只能按用户
允许范围协调、读取、整合和测试。这项只检验用户指令优先级，产品代码禁止按具体游戏名或 prompt 关键词
硬编码机器裁决。

2026-08-22 的正式 Prompt 2 基线证据：机器 `192.0.2.7`、tmux
`dsh-p2-mario-993ce4f`、cwd `/root/dsh-tui-p2-993ce4f`、模型 `MiniMax-M2.7`，只发送了本文件原样
Prompt 2。首次 ask、后台 main、6 个 child 和全部命令 working_dir 均保持该 cwd；child 职责行分别为
“玩家控制/物理引擎/敌人AI/道具系统/关卡设计/游戏HTML”，6 个 child 一次 attempt DONE，`bbb/`
产物齐全，`0.0.0.0:8082` HTTP 200。该轮同时固定了四个待复测失败样本：main context 停在 8.7k、
canonical Todo 5/5 而 TUI 未更新、第二批 child 名称重号、main 在纯委派失败后自行补写功能代码。
候选部署后的复测仍必须用同一原样 Prompt 2、新 cwd、新 tmux、唯一 Gateway；测试者不得追加“继续”、
技术提示或旁路修复。

`f5dc695` 的新鲜 Prompt 2 复验使用 tmux `dsh-p2-mario-f5dc695-verify`、cwd
`/root/dsh-tui-p2-f5dc695-verify`，证明 main context、跨批编号和 8 个 child 一次 attempt DONE 已生效；
wake durable 文件创建到后台 claim 约 7.56 秒。下一候选必须重点确认：批量每个 item 的职责短标题互不
复制且只概括该 child 工作；main/child 任意长文本严格单行；最终 notice 不让 exact child seed Todo
复现；用户要求纯委派时 main 不再写 child 的功能代码。仍只发送一个原样用户 prompt，不允许测试者追加
技术指导。

本切片 focused 命令：

```bash
python3 -m pytest agent_py_agent/tests/test_tool_output_externalizer.py agent_py_agent/tests/test_memory_runtime_compact_auto_continuation.py agent_py_agent/tests/test_background_context_runtime_errors.py agent_py_agent/tests/test_native_runtime_guidance_forwarding.py -q --tb=short
python3 -m pytest agent_py_agent/tests/test_orchestration_tools.py agent_py_agent/tests/test_orchestration_create_subagents_items.py agent_py_agent/tests/test_tui_worker_paths.py agent_py_agent/tests/test_conversation_agent_activity.py agent_py_agent/tests/test_background_notice_display.py agent_py_agent/tests/test_timeout_gate1_accounting.py -q --tb=short
```

第一条锁定 lifecycle wake 的 active-turn 连续性：嵌套 Todo/派工参数进入 durable index 后保持 JSON 结构并
递归脱敏；native carried 记录成为单条、≤既有 semantic-summary 预算的 `CompactionSummary`，不伪造
ToolCall/ToolResult；真实 `/btw` UserTurn 排在 handoff 后；text 协议不生成 IR；后台 Task Runtime State
携带 exact Todo ids、完整账本 read 参数和 `items[].covers` 精确绑定字段。真实验收必须用 fresh tmux 原样
Prompt 4，观察第二批派工是否复用原 Todo、是否带 covers、是否仍保持同一目标；测试者不得补提示或改产物。

r17 后的 planned-delegation focused 回归：`covers` 与 `output_files` 都是可选结构化提示。未绑定 child
必须正常创建并以真实 run id 形成独立进度行，现有 Todo 保持原状；提供的未知/关闭/重复 covers 仍在任何
child 创建前整批 `SUBAGENT_PLANNED_DELEGATION_INVALID`。可选 output 一旦提供，兄弟目录越界仍必须在
创建前返回该错误；同批父子路径覆盖不再被视为文件所有权冲突。合法 exact-id 继续自动映射，
递归 child 入口使用同一预检。
失败 outcome 还必须保留 `error_code=SUBAGENT_PLANNED_DELEGATION_INVALID`、`retryable=true`、
`recommended_action=repair_tool_arguments`，不能只有正文有码而控制层降成 `UNKNOWN_ERROR`。
无计划的测试 fixture 必须显式设置 `home_paths/root/current_run_params` 为空，不能让 MagicMock 动态属性或
共享 `/tmp` 状态偶然伪造 canonical plan，使组合测试结果依赖执行顺序。

```bash
python3 -m pytest agent_py_agent/tests/test_dispatch_progress_seed.py agent_py_agent/tests/test_orchestration_create_subagents_items.py agent_py_agent/tests/test_orchestration_create_subagents_tool_workspace.py agent_py_agent/tests/test_orchestration_create_subagents_tool.py agent_py_agent/tests/test_orchestration_create_subagents_guardrails.py agent_py_agent/tests/test_orchestration_create_subagents_output_refs.py agent_py_agent/tests/test_orchestration_create_subagents_protocol_cleanup.py agent_py_agent/tests/test_orchestration_create_subagents_items_policy.py agent_py_agent/tests/test_orchestration_create_subagents_idempotency.py agent_py_agent/tests/test_orchestration_tool_specs.py agent_py_agent/tests/test_task_progress_advisory.py agent_py_agent/tests/test_task_progress_coverage.py agent_py_agent/tests/test_task_progress_tool_dispatch_reconcile.py agent_py_agent/tests/test_tooling_base.py -q --tb=short
```

`c5cc7c2` 部署后的 fresh r14 只输入一次原样 Prompt 4，证明首名 child 完成会唤醒 root 并创建第二批；
同时发现骨架 goal 中的 `src/i18n/`、`src/config/` 被旧正文自动补绑误当成 Todo 完成证据。新负向回归
当时要求即使 goal 字面包含 open id 也零创建；r17 后现行合同改为允许创建但保持未绑定，仍不允许从正文
写入 `covers_auto_bound` 或给同名 Todo 打勾。删除旁路后，3 个直接相关文件共 55 项、扩展派工集合共
221 项 focused 通过；Ruff、
doc sync、strict code-size、diff 与 clean-package gate 全部通过。发布后的唯一端到端验收改为
fresh r15：固定 lazygit 源提交，只向真实 TUI 输入一次用户原样 Prompt 4，观察模型按 typed repairs 自行
补齐 covers/write sets；测试者不追加提示、不写功能代码。

`bdcc7d1` 部署后的 fresh r15 只输入一次原样 Prompt 4，但 root 读取源码后要求用户选择 Rust/Python/其它，
没有 Todo、child 或功能写入；r15 因此失败，尚未覆盖显式 covers。相同 MiniMax-M2.7 的 会话运行时 对照先探索
源码，最终也输出语言/范围/测试菜单，违反其 `default.md/execute.md` assumptions-first 源码合同。当前回归
先锁定发布 YAML 与 dataclass 默认 prompt 逐字相同、自主决策纪律位于持续执行纪律之前、文本不含具体语言；
发布后 fresh r16 仍只输入一次同一 Prompt 4，测试者不回答语言、不追加推动消息、不修改产物，观察 root
是否采用合理默认、建立 Todo 并创建显式 `covers/output_files` 的 child。

`b7005a8` 部署后的 fresh r16 已通过上述自主决策：root 选择 Rust、建立 7 项 Todo，漏 `covers` 的第一次
创建被 typed repair 原子拒绝后自行修正；首名 child DONE 后 root 自然派出第二名。新失败发生在 child
职责边界：首名只声明 6 个骨架文件，却额外写 11 个文件，其中 `src/gui/views.rs` 与 `src/gui/state.rs`
又被第二名负责。当前 focused 回归改为锁定两件事：child prompt 必须包含“直接父级当前 goal 是完整工作
边界”且不再出现 root 的“委派不缩小用户目标”；当时计划存在时 exact covers 仍必填，而编码 child 可以
省略可选 `output_files`，提供时仍不得越出 workspace。发布后 fresh r17 继续只输入一次原样 Prompt 4，验证
第一名不再替兄弟扩做、root lifecycle wake 与后续派工仍正常；测试者仍不写被测产物或追加技术指导。

`4c3a59d` 部署后的 fresh r17 证明首名骨架 child 与第二名 TUI child 基本停在直接 goal 内；第三名 Git
child 却把 Rust 模块写到 cwd 根而非 `rust-port/`。root 决定返工时，原 Git Todo `3` 已关闭，mandatory
covers 又不允许省略，于是它把新 Git child 绑定到下一个 open GUI Todo `4`，TUI 已显示 GUI 进行中。
现场 `/stop` 后 root PAUSED、前三名 DONE、错绑 child CANCELLED。当前回归要求 planned dispatch v2：
无 covers 合法并产生真实 child 进度行，不关闭原 Todo；提供的未知/关闭/重复 covers 仍原子拒绝；返工
guidance 要求 `status=in_progress + correction=true` 重开原 id，或省略 covers，不能顶替无关 open id。
fresh r18 仍只输入一次原样 Prompt 4，测试者不改产物、不补技术提示。

`2d03803` 部署后的 fresh r18 已证明可选 covers 不再强迫返工 child 绑定无关 Todo；但 root 在 8 项中只
关闭 5 项，构建、自动测试与端到端验证仍 pending，机器没有 Rust/Cargo，最终却声称完整生成并被 durable
workspace 写成 DONE。对应 focused 回归必须覆盖：普通 root/child 第一次自然 final 时把 exact open
id/title/status 作为 native runtime-guidance 送入同一 active turn；模型关清后自然结束，仍 open 时仅一次
后 typed blocked；只有 blocked 项时直接 blocked；`/goal`、Audit、isolated/control-plane、无工具继续走
既有路径。`save=False` 只关闭可选 archive，Gateway/TUI 与后台 main 仍必须核对；不得创建
`ordinary_task_resume` 或扫描最终正文、代码、测试和产物。fresh r19 仍在固定
lazygit commit 上只输入一次原样 Prompt 4，测试者不得安装工具链、修改产物或追加推动消息；验收重点是
开放 Todo 触发同轮继续或诚实 blocked，durable root 不再假 DONE，并同时观察 main/child token、Compact
与职责短句保持真实。

`e94f8ec` 部署后的 fresh r19 使用 `.7` 唯一 Gateway、MiniMax-M2.7、tmux
`dsh-p4-lazygit-r19-ea91639` 和 `jesseduffield/lazygit@ea916395`，仍只输入一次原样 Prompt 4。root 建立
8 项 Todo，5 名 child 全部一次自然 DONE；第五名在 113.8k 触发 canonical Compact，TUI 从 `compact 0`
更新为 `compact 1`，压缩后 39.3k 并继续完成。root 真实执行 port 的测试得到 112 passed；但独立产物
smoke 的 tmux `dsh-p4-product-r19-ea91639` 只有背景色、零可见组件。源码证明 `LazyGitScreen.compose()`
虽已定义，`LazyGitApp` 却从未 compose/register/push 它。原项目 957 个生产文件、114,376 行物理生产代码，
port 只有 30 个生产文件、5,404 行生产代码；5 个测试文件不能证明完整功能等价。r19 在 final 前自行关闭
8/8 Todo，故没有直接命中 open-Todo 核对分支；该分支仍以 native provider-message focused 回归为发布
证据，下一次自然出现 open Todo 的原样重型任务再补直接 TUI 证据，不能人为改账或用玩具 prompt 诱发。

r9 随后给出真实失败分支：后台 main 在 13/16 Todo 关闭时因 `save=False` 直接绕过核对并写成完成。定向回归
现在覆盖 Gateway `save=True/False`、后台 main `save=False`、稳定 task-path ledger 和 blocked 后 durable
task 仍 active；修复部署后的原样 Prompt 4 才能把该项升级为直接真机通过。

真实本地模型回归示例：

```bash
python3 scripts/live_agent_lab.py --suite main-artifact --real-llm --timeout 900
python3 scripts/live_agent_lab.py --suite tool-recovery --real-llm --timeout 900
```

真实模式会先发起一次模型调用；key 仅存在但不可用、响应为空或 endpoint 失败都会直接终止，不会把专项
harness 的离线结果冒充真实模型结果。

会话级模型用量账本的 focused 回归覆盖 provider/estimated 分栏、明细裁剪后累计值、owner/thread
隔离、append-once 重放、冲突/损坏显式失败，以及后台 `do_save=false` 仍落数字用量但不写 runtime fact：

```bash
python3 -m pytest agent_py_agent/tests/test_model_call_ledger.py agent_py_agent/tests/test_conversation_store.py agent_py_agent/tests/test_memory_runtime_basics.py::test_run_no_save_still_persists_thread_model_usage_without_runtime_archive agent_py_agent/tests/test_gateway_chat_conversation_context.py::test_gateway_response_does_not_fall_back_to_suppressed_internal_result -q --tb=short
```

Gateway 主代理 attempt 贯穿回归必须覆盖两条真实失败同族：入口的 `gateway-attempt-*` 只能是 transport
身份，执行和收口使用 `_bind_main_agent_authority` 返回的 exact RuntimeDB attempt；发生自动 Compact 时，
最终收口必须使用最新 generation。两条路径都要求 root run/current attempt 真实终态、`ended_at>0`，且不得
出现 `closeout_blocked(reason=stale_attempt)`：

```bash
python3 -m pytest agent_py_agent/tests/test_r103_run_reuse_no_split.py -q --tb=short
```

`.7` 真机复验继续只用唯一 Gateway、MiniMax-M2.7、全新 cwd/tmux，并只提交一次本文件原样 Prompt 4。
验收不能靠 TUI 的“整理结果中”：必须同时证明首轮 root attempt 已结束，最后一名 child 的 durable wake
从 pending 进入 handled，后台 main 创建下一代 attempt 并发生真实模型用量/typed transcript 前进；测试者
不得追加“继续”、手工改状态或旁路消费 wake。

Todo 会话根投影回归必须覆盖同一 thread 同时存在较早 root link 和较晚 child link 的真实形态：
`ConversationThread.workspace_task_id` 精确指向 root 时，`conversation_agent_activity.v5` 必须继续返回 root
的 canonical Todo，不能因为 child 更新更晚而返回空列表并让 TUI 清屏。最终 notice 的 exact task helper、
直属 child seed 去重、明确空快照清理旧清单语义保持不变：

```bash
python3 -m pytest agent_py_agent/tests/test_conversation_agent_activity.py agent_py_agent/tests/test_background_notice_display.py -q --tb=short
```

`84c6b90` 部署后的 fresh Prompt 4 r6 使用 `.7` 唯一 Gateway、MiniMax-M2.7、tmux
`ma-84c6b90-p4-fzf-r6-todo`，只输入一次原样 prompt。main 进入“等待 4 个子代理”时，Todo 仍稳定显示
`完成 1/9 · 进行中 6`，四行任务窗口和四名 child 同时可见，根 Todo 持续投影通过真实 TUI 回归。

后台富过程与派工后职责的本地 focused 回归必须同时覆盖：显式 thinking delta/full、真实工具边界前的
灰色过程段、`Update(path)`、增删统计和带样式的红删蓝增行；`event_after/event_cursor` 二次拉取不得重放；
root/递归 coordinator/create 回执必须共用 no-duplicate-work 与 replacement child 语义。它们只是上线前
协议护栏，不能代替新的原样 Prompt 4 真 TUI。常用入口：

```bash
python3 -m pytest agent_py_agent/tests/test_background_notice_display.py agent_py_agent/tests/test_conversation_agent_activity.py agent_py_agent/tests/test_orchestration_tools.py agent_py_agent/tests/test_orchestration_create_subagents_output_refs.py agent_py_agent/tests/test_integration_coverage_context.py agent_py_agent/tests/test_subagent_role_templates.py -q --tb=short
```

较早未决操作的最终软核对要覆盖 r5 的真实形态：一个 effect-bearing 调用 typed `unknown`，随后另一个
不同 operation `succeeded`，首份最终草稿仍必须带原工具能力回到同一模型，并同时看到未决 operation、
后续成功计数和被退回草稿；相同签名只触发一次。若最新 operation 自身仍未决，继续由既有
completion-conflict 负责；task-local child、只有 `unverified` 或纯 `not_started` 的历史不新增这条 root
软核对。focused 入口：

```bash
python3 -m pytest agent_py_agent/tests/test_current_turn_execution.py agent_py_agent/tests/test_runtime_guidance.py -q --tb=short
```

子代理 orphan 恢复必须覆盖 runtime.db 与文件投影暂时不一致的崩溃窗口：task 投影仍为 `PENDING`，但
current AgentAttempt 已是 `unknown` 时，targeted 与周期 sweep 都必须返回结构化 recovery block，零次调用
auto-start，且不能增加 `orphans_revived`。正常 pending/planning、活 session、防重锁与 attempt cap 继续走
原行为：

```bash
python3 -m pytest agent_py_agent/tests/test_dispatch_liveness_and_revive.py -q --tb=short
```

子代理名册的固定八行区域必须与完整导航名册使用同一个 exact run 顺序。回归至少构造九名 child，连续
向下选到第九名后要求第九行带 `›` 出现在屏幕、第一个 child 被窗口挤出、省略数仍为一，并且 `Enter`
进入同一个第九名 run；不能只断言导航状态已变化。focused 入口：

```bash
python3 -m pytest agent_py_agent/tests/test_tui_agent_navigation.py agent_py_agent/tests/test_tui_renderer.py agent_py_agent/tests/test_background_notice_display.py -q --tb=short
```

真机验收必须使用 `.7` 唯一 Gateway、MiniMax-M2.7 和已有历史累计超过八名 child 的 exact resume。启动前
公开 tmux 名称；在 root 空输入状态连续按 `↓`，每次选中行必须可见，第九名出现后 `Enter` 进入的页面名称
必须与该高亮行一致，再用 `Ctrl+G` 返回。测试者不得给模型补消息或修改被测任务产物。

## 2026-08-26 思考/完整正文、缓存稳定前缀、canonical task root 与网络事实

本轮 focused 必须覆盖：多次 provider thinking 块按时间顺序保留、空首块 typed discard、assistant 正文前
封口 thinking；工具边界 commentary 与 final 用同一 request 的不同 `assistant_part_id` 完整落账，配对预览
仍选择 final，长正文不按固定字符数截断；普通历史只按完整消息边界收缩。滚轮一次只移动一行。

工作区回归必须证明首个 `promotes_task` 动作后 main、child 与相对 output ref 都指向 exact
`<owner_home>/tasks/<task_path>/`，不继承 daemon `/root` 或客户端临时 cwd。后台服务回归必须证明
`process_session(network_status)` 只返回 exact session 进程树的 listener，区分 loopback/non-loopback，
主机防火墙缺少显式端口规则时不宣称局域网可达；外部探针仍是独立验收。

推荐 focused 入口：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_tui_runtime.py \
  agent_py_agent/tests/test_tui_renderer.py \
  agent_py_agent/tests/test_tui_view.py \
  agent_py_agent/tests/test_tui_view_model.py \
  agent_py_agent/tests/test_gateway_verbose_progress.py \
  agent_py_agent/tests/test_gateway_chat_conversation_context.py \
  agent_py_agent/tests/test_gateway_streaming.py \
  agent_py_agent/tests/test_background_notice_display.py \
  agent_py_agent/tests/test_conversation_agent_activity.py \
  agent_py_agent/tests/test_process_sessions.py \
  agent_py_agent/tests/test_runtime_gate_ledger.py \
  agent_py_agent/tests/test_tool_round_execution.py \
  -q --tb=short
```

成本验收使用 provider usage 的同一份样本：`B=357,639`、`H=235,041`。普通输入/缓存创建按 5、缓存命中
按 0.1 时应为 `1,811,699.1`；缓存命中按 1 时应为 `2,023,236`；全部不命中为 `2,963,400`。
该数学只证明价格影响，缓存是否命中仍须由 provider 的 cache-read 账本证明。

`ma-r54-context-workspace-network` 在 `.7` 唯一 Gateway/MiniMax-M2.7 上完成了一次原始
超级玛丽多子代理任务：5 名 child 自然完成，main 自动恢复并在界面直接交付长报告；
`bbb` 及主要 JS/HTML 全部落在 exact owner task root。第二条「继续启动」消息又精确复现了
completed sticky 被错分流到新目录：`list_files bbb` 先失败，模型随后搜索旧目录并复制产物。

`1b75762` 部署后的 fresh `ma-r55-terminal-sticky-retest` 已证明 successor task id 和 canonical
`task_path` 相同，但第二轮第一条后台命令仍指向 TUI 启动 cwd 的空 `bbb`。这是“模型首采样在前、工具
pre-handler promotion 在后”的时序缺口，不是 task link 再次分叉。回归新增以下要求：

- exact non-detached sticky workspace 一旦存在，`_gateway_task_attributes` 必须在本轮首采样前把其
  `task_path` 设为 `conversation_execution_cwd` 和唯一 runtime root；
- terminal link 不得因此预填旧 `conversation_task_id` 或 `run_workspace`，纯聊天仍不激活/归档任务；
- active/interrupted/completed 的模型 Workspace Context、工具 write boundary、审批显示 cwd 和实际进程
  cwd 必须一致；不能先向模型展示客户端启动目录，再期待 handler 执行时重写命令字符串。

回归要求：

- 同一 thread 的 sticky root 已 `completed` 或 `interrupted` 时，新 request 必须保持旧 link
  终态，以新 successor task id 续接同一 `task_path`；
- `read_file/list_files/search_text/find_files/write_file` 任一首个 `promotes_task` 工具都要
  使 `run_workspace.task_root`、agent 当前 cwd 和四份 run identity 投影同步换代；
- 新 successor 的 goal 只能来自当前 user prompt，不得复制旧 goal；空当前 prompt 仍
  fail-closed；
- 纯聊天不预填旧 live task 身份、不复活旧终态；sticky 只在首个真工作工具处生效。
- 纯聊天虽然不激活任务，但模型和只读上下文已进入 sticky cwd；“建立 successor”与“确定 cwd”是两个
  结构化阶段，不能为了懒建 task 而让 cwd 晚一拍。

修复后 `test_gateway_chat_conversation_context.py + test_conversation_store.py +
test_run_task_workspace_writer.py` 完整定向通过；其中 completed/interrupted × 5 类首工具的精确组合为
10 条，再加直接 promotion 和普通新轮续接回归均通过。

真机仍只允许 `.7` 一个 Gateway、MiniMax-M2.7 和 fresh tmux。启动前先公开 tmux 名称和 attach 命令；
测试者只通过普通中文 TUI prompt 驱动被测 Agent，不旁路补产物、改防火墙或执行任务。Mac 到测试机的真实
HTTP 请求必须与 TUI 内 `network_status` 对账，本机监听成功但外部失败时最终报告必须保持未验证/不可达。

`1e4c64d` 已完成上述 focused 与严格 gate，推送并部署 `.7` 唯一 Gateway。原
`ma-r55-terminal-sticky-retest` 的同一普通中文启动提示复验中，模型首个 `ls -la bbb`、审批 cwd、bwrap
与 Python 进程 cwd 均直接落到原 canonical task root；三个连续终态 request 使用不同 task id，却精确复用
同一个 `task_path`。localhost HTTP 正文是游戏 HTML，最终报告直接显示且 Working 撤下。Mac 外部探针仍
连接失败，模型保持 `unverified_external_probe_required`，所以这里只通过“路径连续性与不误报”合同，
不把 LAN 可达性记为通过。main 在第一段整合时仍亲自修改功能文件，也只作为协调软纪律失败样本保留。

## 2026-08-27 流式 thinking 必须在正文前原位收口

r55 原始 chunk 第 223--228 条是 `thinking_delta`，229--236 条已经是最终 `model_delta`，第 237 条才出现
同内容 `assistant_thinking`。这会让 TUI 先因正文冻结增量思考，再把迟到全文当成新块追加到 final 后。
回归必须覆盖：

- Anthropic collector 在 thinking 的原 `content_block_stop` 调用同一 delta observer 的 typed `complete`，
  且严格早于下一 text callback；普通 callable、fake 与旧后端调用形态不变；
- 只有显式声明 `supports_thinking_completion` 的 backend 才附加完成方法；
- 同一物理调用已经发布 stream completion 后，response 级 `assistant_content_blocks` fallback 不再重放；
- TUI 重放旧 chunk 时，`thinking_delta -> model_delta -> assistant_thinking` 只保留一个原位 thinking，最后
  一个稳定块仍是 assistant final。

失败优先的三条精确回归已先红后绿；完整 focused 入口为：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_backends_native_tool_use.py \
  agent_py_agent/tests/test_tool_model_generation.py \
  agent_py_agent/tests/test_gateway_verbose_progress.py \
  agent_py_agent/tests/test_tui_runtime.py \
  agent_py_agent/tests/test_background_notice_display.py \
  -q --tb=short
```

以上完整 focused 与 Ruff、doc sync、strict code-size、diff、clean-package 严格 gate 已通过。
`ae3fd1e` 推送部署 `.7` 唯一 Gateway 后，原 r55 会话中新 MiniMax-M2.7 请求的 chunk 2--94 为
`thinking_delta`、95 为唯一 `assistant_thinking`、96--117 为 `model_delta`；真实 TUI 最后一块为 final。

## 2026-08-28 TUI 功能逐项与可达覆盖验收

真实验收以 `.7` 的唯一 Gateway、MiniMax-M2.7 和事先公开名称的 tmux TUI 为准。测试按
`TUI_EXTREME_TEST_MATRIX.md` 的功能编号逐项执行，不用一个大 prompt 代替输入、历史、命令、滚动、复制、
流式过程、工具、子代理、Compact、恢复和安全边界各自的操作证据。Gateway 与 TUI 同时用 coverage 启动，
覆盖报告用于发现尚未经过真实入口的实现；不能把行覆盖率等同于功能正确。

短时 footer notice 必须在截止时主动触发最后一次重绘：到期前保持动画刷新，到期后的第一 tick 清空文字并
额外 invalidate 一帧，随后空闲时停止周期刷新。定向回归入口：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_tui_threading.py \
  agent_py_agent/tests/test_tui_runtime.py \
  -q --tb=short
```

TUI 无法天然触达的 Feishu/WeCom 回调、Windows 专属路径、损坏数据库迁移等入口必须在最终矩阵单列为
“TUI 不可达”，可补 focused 测试，但不得伪称已经由 TUI 操作覆盖。

输入历史还要覆盖“同一进程刚提交”的消息：写入 FileHistory 后必须重置 prompt_toolkit 的 working-lines
快照，空输入 Up 立即取回本轮最后一条（含多行），Down 恢复提交前空草稿；只改 Document 文本而不重置
历史游标不算通过。对应完整应用回归位于 `test_tui_prompt_toolkit_pipe.py`。

`/remember` 与 `/memory` 必须通过真实 TUI 单独操作。记忆请求在后台执行，输入框在慢存储期间仍可继续
编辑；写响应丢失只显示“结果未知”，不得显示确定失败或自动重复写。默认 `quota.v2.max_disk_mb=0` 下不得
调用 owner 全树用量扫描；旧未修改 100GB seed 升级为 0，管理员自定义非零策略仍保留并继续执行配额门。
focused 回归入口：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_chat_client_context.py \
  agent_py_agent/tests/test_chat_control_runtime.py \
  agent_py_agent/tests/test_tui_input.py \
  agent_py_agent/tests/test_home_layout.py \
  agent_py_agent/tests/test_owner_policy_and_grants.py \
  agent_py_agent/tests/test_owner_memory_skills_effective_flags.py \
  agent_py_agent/tests/test_owner_quota.py \
  -q --tb=short
```

首个文件写入还必须验证“真实 handler 所用 cwd”，不能只断言晋升后的 attrs。真实 TUI 已复现：首个
`write_file` 写在 owner 根，而随后 `apply_patch` 按新 task root 查找并报 `PATH_NOT_FOUND`。回归现在同时
覆盖相对路径与模型在晋升前生成的 owner-root 绝对路径，要求一次调用内按以下顺序完成：结构化任务晋升 →
同步 `run_workspace/execution_cwd/runtime roots/rebase source` → 重建 call 与 write boundary → policy/handler。
取消发生在执行前时不得因这条准备链创建 task。定向入口为：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_gateway_chat_conversation_context.py::test_first_gateway_mutation_executes_in_promoted_task_workspace \
  agent_py_agent/tests/test_runtime_gate_ledger.py \
  -q --tb=short
```

普通工具失败必须同时验证“工具事实”和“turn 是否自然结束”。真实 TUI 用明确要求的 `exit 7` 证明：工具已
准确返回 stdout/stderr/return code 后，旧 completion conflict 仍额外调用模型 5 次并留下 Working。回归现要求：

- 第一次模型调用发出失败工具，第二次模型调用看见真实结果后给出 plain final，随后立即结束；
- operation verification 仍保存 `failed`，但不能把模型 final 改成 `OPERATION_INCOMPLETE`；
- 模型若在第二次采样主动选择另一修复工具，仍可继续同一 active turn，修复成功后再自然 final；
- Todo open、写后验证 stale 和较早失败只能留在 prompt/ledger 供模型判断，不能在 plain final 后自动注入
  provider call；UNKNOWN 副作用、权限、危险路径和取消门保持 fail-closed。

定向入口：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_current_turn_execution.py \
  agent_py_agent/tests/test_runtime_guidance.py \
  agent_py_agent/tests/test_cli_resume_contract.py \
  agent_py_agent/tests/test_config_validation.py \
  -q --tb=short
```
## 2026-08-28 TUI `/btw` 单次终稿与 slash 目录回归

- `test_tui_runtime.py` 新增 Gateway consumed 回执晚于 `model_delta` 的真实顺序：补充用户消息晋升后，终态
  summary 只能留下一个 assistant block，不能重复整段最终回复。
- `test_tui_input.py` 固定显式 `/btw` 在进入持久 control outbox 的同时生成可见 pending steer，并覆盖
  `/expand`、`/expand last`、`/expand N` 与未知参数；帮助目录继续作为补全和中文说明唯一事实源。
- 本切片初版运行 `test_tui_runtime.py test_tui_input.py test_tui_control_delivery.py` 共 89 项通过；随后增加
  “`assistant_final` 先到、consumed 后到”顺序回归，并与 renderer 一起共 129 项通过。`test_tui_stateful.py`
  需要 Hypothesis，当前本地与测试机 venv 均未安装，未把 collection error 冒充用例失败。
- `.7` fresh `ma-cleanup-welcome-r6` 已验中文欢迎页、`/help`、`/audit resume` 与 `/expand last`；
  `ma-cleanup-steer-order-r7` 已验运行中 pending、accepted、正确历史位置、单次终稿和空闲 rejected 三态。

## 2026-08-28 Rich TUI `/context` 单一事实源回归

- `test_tui_renderer.py` 固定详细报告直接消费与底栏相同的 `TuiContextUsage`，覆盖 native 分类、无快照、
  window/trigger/compact generation；`test_tui_input.py` 固定 `/context` 不进入 Gateway control reconciler。
- 相关 renderer/input/runtime/control/compact 共 167 项通过；Ruff、文档同步、strict code-size 与 diff check 通过。
- `.7` fresh `ma-cleanup-context-r8` 用 MiniMax-M2.7 普通回合生成真实 preflight 快照后执行 `/context`：详情
  `30,563/128,000（23.9%）` 与同屏底栏 `~30.6k/128.0k · 24%`、90% 压缩点、compact 0 一致。

## 2026-08-28 后台 session handle 与真实 listener PID 回归

- `test_process_sessions.py` 与 `test_shell_background.py` 共 25 项在本地 Mac 和 `.7` Linux 测试机通过：
  模型可见启动/状态只保留稳定 `session_id`，持久 record 内部 PID 不变；`/proc/net/tcp` 的 socket inode
  owner 精确投影为排序后的 `listener_pids`，并携带宿主权威/沙箱可能不可见的结构化说明。
- 旧跨进程测试补齐当前 protected-path 参数，并用沙箱内可见的标准命令代替仓库 venv 绝对路径；这是测试
  合同修复，不改变生产沙箱根。Ruff、doc sync、strict code-size、diff check 本地通过。
- `.7` fresh `ma-cleanup-process-pid-r10` 使用 MiniMax-M2.7 启动 0.0.0.0:18083：工具返回
  `listener_pids=[1563961]`，宿主 `ss` 同为 1563961，最终把 `bg-...` 管理句柄与 OS listener PID 分开，
  没有再用沙箱 `ps/lsof` 反证，并明确外部局域网未验证。
- r9 自动 compact 的事实与底栏 `compact 0` 相悖、session approval 未按界面字面复用、模型首轮仍附 `&`
  及一次路径双重嵌套均作为独立开放问题记录，不因 PID 主合同通过而隐去。

## 2026-08-28 Gateway 客户端断连回归

- `test_gateway_http.py` 明确覆盖 EPIPE、ECONNRESET、ECONNABORTED 三种正常客户端离开；同时钉死 ENOSPC
  与 JSON 序列化错误必须继续抛出。`test_gateway_bounded_http_server.py` 继续覆盖 worker 复用、过载和停机回收，
  两文件共 25 项通过。
- `.7` 唯一 Gateway PID 1564837 下启动 fresh `ma-cleanup-brokenpipe-r11`，从 TUI 提交普通 MiniMax 请求后
  0.35 秒输入 `/exit`。客户端 tmux 正常退出，后台请求 `gwreq-1787928364-...` 仍在 6.175 秒后 status=done，
  Gateway 当前启动段没有新增 BrokenPipe traceback；历史计数前后均为 9。

## 2026-08-28 Gateway 权威状态与 Audit 表达轮回归

- `test_gateway_status_tool.py` 5 项固定 validated PID、真实 8420/status endpoint、MiniMax-M2.7/config identity、
  旧生命周期异常排除、零新字节不冒充 quiet，以及 model tool 不返回原始日志或 API key。
- `test_gateway_commands.py` 与上述文件合计 32 项通过；`test_runtime_guidance.py -k 'natural_reply or audit_prepare'`
  相关 12 项通过、1 个既有 xfail。表达轮首个越权工具被回注为配对失败，二次越权不提拔前言。
- `.7` `ma-cleanup-audit-r12/r13` 使用相同 Audit 普通中文需求：请求
  `gwreq-1787934530-...`、`gwreq-1787934724-...` 都只调用一次 `gateway_status`，不扫描端口且完整收口。
  `gwreq-1787934789-...` 原样返回 `MiniMax-M2.7 / 8420 / running`；`gwreq-1787934949-...` 返回本生命周期
  `quiet / 132 bytes / 0 exceptions`。测试期间始终为单一 Gateway listener。

## 2026-08-28 Compact 精确事实续接回归

- `test_gateway_conversation_compact.py` 新增“摘要 backend 故意漏掉网页标题”的失败优先用例：Compact 结果仍须
  保留用户原请求与 assistant final 中的 `Welcome to Python.org`，并排除带
  `assistant_part_id=commentary:*` 的过程话。
- 第二条回归连续生成两代摘要，要求旧网页标题与新 `result.txt` 两行同时存在，规范锚点标题只出现一次，
  同一 final 不重复。已有 operation evidence 测试同步改为允许非权威锚点后缀，权威操作账仍排在其后裁决。
- 12K 小窗口长中文历史回归继续通过，证明锚点预算会缩小且不会让 Compact 候选错过 recovery target；空模型
  回复仍走机械摘要并附同一有界锚点。
- 本地定向入口：

```bash
python3 -m pytest agent_py_agent/tests/test_gateway_conversation_compact.py -q --tb=short
ruff check agent_py_agent/agent/conversation/compact.py agent_py_agent/tests/test_gateway_conversation_compact.py
```

- `.7` fresh `ma-cleanup-compact-r23` 已用 MiniMax-M2.7 建立网页标题、三行文件与交付字段，手动
  `/compact` 显示 indeterminate 动画并提交 generation 1；随后明确禁止工具的追问一次答全。canonical
  `thread-ce7d87bd859a499f` summary 为 1447 字符、唯一 landmark heading、七项值均命中，终轮
  operation count 为 0；测试期间仍只有 PID 1584804 监听 127.0.0.1:8420。

## 2026-08-28 Gateway 会话级工具审批回归

- `test_gateway_verbose_progress.py` 不再复用同一个 writer 制造假通过：第一轮 writer 接收
  `approved_session`，第二轮新 writer 绑定同一 Agent cache/scope，必须不调用等待桥、不得发布新的
  `permission_requested`，只留下 `permission_resolved/session_cached=true`。
- 同文件覆盖精确 scope 与 LRU：cwd 或 access 模式变化得到不同 digest；每作用域和作用域总数超限时淘汰
  最旧记录，淘汰后的行为是重新询问而不是默认批准。
- `test_tui_renderer.py` 固定审批框和工具等待子行全部中文：`是否继续执行？`、`等待授权…`、`Esc 取消`、
  `Tab 补充说明`。
- 本地与 `.7` 定向命令均为 78 项通过，Ruff 通过：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_gateway_verbose_progress.py \
  agent_py_agent/tests/test_tui_renderer.py \
  -q --tb=short
```

- `.7` fresh `ma-cleanup-approval-r26`：18476 首轮请求
  `gwreq-1787945550-4e1536ab165d4cf2bedc530fe66e2a85` 显式选择 session；相同参数后续请求
  `gwreq-1787945597-85d86cda206b4bdf89253cd526adf9d8` 没有 approval overlay，chunk 只有 cached
  resolved；18477 反例 `gwreq-1787945634-93c7cb5387224cb080b9aaa6b9b630e7` 重新询问并取消。

## 2026-08-29 后台命令立即退出不得冒充启动成功

- `test_shell_background.py` 固定三种首结果：长命令在 0.5 秒观察期后仍活着，返回
  `status=started/session_id` 且不公开 OS PID；立即零退出返回 `status=exited/exit_code=0/output_tail`；立即
  非零退出返回 `COMMAND_FAILED/effect_outcome=failed/status=exited` 和真实退出码、日志尾部。
- `test_process_sessions.py` 的后台命令改为沙箱内可见且长于观察期的标准命令，继续覆盖有界 wait、跨进程
  水合、稳定 session handle 和完整进程树 stop。观察期只增加约 0.5 秒，不把正常后台服务阻塞到终态。
- 本地与 `.7` 定向命令均为 27 项通过，Ruff 通过：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_tools/test_shell_background.py \
  agent_py_agent/tests/test_process_sessions.py \
  -q --tb=short
ruff check \
  agent_py_agent/agent/tooling/shell.py \
  agent_py_agent/tests/test_tools/test_shell_background.py \
  agent_py_agent/tests/test_process_sessions.py
```

- `.7` fresh `ma-cleanup-background-r27`：首次 `gwreq-1787946133-...` 启动 18478 并返回稳定 session；第二次
  `gwreq-1787946178-...` 复用同会话审批，但工具在 1.01 秒进度内明确失败，模型最终报告端口占用和
  `exit_code=1`，没有把第二个 session 当作运行中服务。测试后 18474--18478 均无监听。

## 2026-08-29 直属 child 当前快照、终态 Todo 与过程灰色真 TUI

- `test_runtime_guidance.py` 覆盖 7 DONE/1 RUNNING 后刷新为 8 DONE、排除其它 root、child 只看自己的
  grandchild、部分加载错误不得宣称 all-terminal、Compact/rebuild 重建以及稳定状态字节完全一致。
- `test_conversation_store.py` 固定 task link 的 typed status/current_step 同步到 canonical state 和派生
  current summary；`test_conversation_agent_activity.py` 固定终态只清空 display plan，不删 child roster 或
  durable ledger；`test_tui_renderer.py` 固定 process Markdown 全部灰色而 final 保持正文色。
- 本地组合 focused 与 Ruff 均通过：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_runtime_guidance.py \
  agent_py_agent/tests/test_conversation_agent_activity.py \
  agent_py_agent/tests/test_conversation_store.py \
  agent_py_agent/tests/test_tui_renderer.py \
  -q --tb=short
ruff check \
  agent_py_agent/agent/agent_core/runtime/guidance.py \
  agent_py_agent/agent/agent_core/_tool_loop_service.py \
  agent_py_agent/agent/conversation/agent_activity.py \
  agent_py_agent/agent/conversation/store.py \
  agent_py_agent/cli/chat_parts/tui_block_renderer.py
```

- `.7` 单 Gateway fresh `ma-cleanup-compact-tree-r35` 使用 MiniMax-M2.7 和原样 Prompt 3：一次创建 8 个 child；
  child 数按 8→5→2→1→0 收敛；通道运行时 child 从约 113.4k 触发真实 Compact，降到约 63.7k、行内
  `compact 1` 后自然 DONE；main 新工作片明确收到 8 个 exact DONE id，同轮读取全部 8 份报告，写出
  321 行/10,888 bytes 的 `reports/横向对比报告.md` 并自然最终回复。终屏 Working 与 stale `完成 0/9`
  Todo 均收起，8 个完成 child 行仍可进入；`work/state.json` 和 `work/summaries/current_summary.md` 同为 DONE。

## 2026-08-29 PTY resize/list 与 exact conversation scope

- `test_pty_sessions.py` 用真实 Python REPL 钉住：start 只返回稳定 `pty-...` 而不公开 OS PID；同 scope list
  只见本会话；`resize(columns=100, rows=30)` 经内核回读为 100×30；write/read cursor 与 close 继续正常。
  scope equality 新增 conversation id 反例，同 owner、同目录但另一 TUI 会话不能列出、读写或关闭该 PTY。
- `test_tool_runtime_unification.py` 扩展既有 transport 合同：start 仍需 exact approval；随后
  list/write/read/resize/close 都走同一已批准 session transport，不重复弹窗。工具 Schema、manifest 与
  handler action 一致，未知 action 继续在 handler 前失败。
- r28 修复前证据：`gwreq-1787946826-bad6befe2a34419dab0ff231148fae45` 的第 7 工具轮因编造 list action
  返回 `TOOL_INVALID_ARGUMENTS`；模型写入 resize ESC 后，PTY 内核仍报告 80×24，却在最终表中写“成功”。
  `.7` fresh r29 原样复验请求 `gwreq-1787947377-1b918742122e4a0baf68204faead62b4` 已直接返回 list 和
  100×30 内核尺寸，最终不出现 OS PID；工具轮从 10 降为 6，PTY close 后无活动会话残留。

## 2026-08-29 同 TUI 连续 slash 与真实 `/stop`

- `.7` `ma-cleanup-terminal-r29` 在完成 PTY 长链后继续原会话：`/remember` 写入代号/颜色/编号，`/memory`
  立即检索命中；普通模型回合在 canonical task root 创建 `prompt-r29.txt`，随后相对路径
  `/prompt-file prompt-r29.txt` 成功装入；`/show-prompt 请只回答：R29` 展示最终 prompt，并得到
  `R29 [PROMPT-R29]`。这证明 slash、task-relative prompt file 和同会话连续工作未因前一任务丢失。
- 随后普通中文要求执行 30 秒前台等待，工具真实开始后从同一输入框提交 `/stop`；TUI 在期限前进入
  interrupted、恢复输入，宿主 `pgrep` 无残留 sleep，唯一 Gateway PID 1595234 未重启。请求
  `gwreq-1787947706-5aa0fb7aad384da0a879057ba7bbfe45` 的工具终态为已执行但结果未知，未冒充成功。
- 中断中文投影的 renderer/runtime/view-model 与 control 定向回归共 119 项通过；`.7` fresh r30 请求
  `gwreq-1787947950-297362d6af4f46d58bbb9b7c3cf29489` 再次中断真实运行工具，已显示
  `已中断 · 接下来希望 my-agent 怎么做？`，输入恢复且无 sleep 残留，不是静态 fixture 自证。

## 2026-08-29 同 TUI `/goal` 完整生命周期

- 继续使用 `.7` 的 `ma-cleanup-stop-r30`，通过真实输入依次执行创建、查询、暂停、修改、恢复、再次查询、
  具名清除和清除后查询；目标名为 `tui-r31`，没有绕过 TUI 修改持久状态。
- 目标运行时提交 `/goal` 能立即返回“运行中”和已用时间；`pause` 后底部 Working 消失，`resume` 后恢复；
  修改后的目标正文在查询中保持一致。具名 clear 返回 `Goal“tui-r31”已停止。`，随后查询明确
  `当前没有持续目标。`。
- 该轮同时证明控制消息不会等待后台主代理或所有子工作完成才被接收；目标 clear 后没有遗留 active goal，
  唯一 Gateway 仍为 PID 1595234。普通任务仍不自动进入 `/goal`，本测试没有改变该产品边界。

## 2026-08-29 子代理 owner workspace 可写祖先冲突回归

- 修复前真实证据来自 `.7` `ma-cleanup-subagents-r31`：八个 child 的
  `allowed_write_roots` 都位于 `/root/.my-agent/owners/local/main/...`，默认 deny 却含 `/root`；长期助手 与 会话运行时
  child 的 `git clone` 在正式 task/agent 根均返回 `Read-only file system`，转到沙箱 `/tmp` 才能继续。
- focused 回归必须证明 `_default_forbidden_write_roots()` 不再含宿主 home 本身，同时仍含
  `Desktop`、`Downloads`、`.ssh`；已有 local workspace exact reconciliation、remote explicit deny 和 bwrap
  read-only carveout 测试继续通过。
- `.7` fresh 验收必须从新 TUI 新建 child，因为已创建任务的权限合同是不可变历史。child 应能在自己的
  canonical agent workspace 写 sentinel/clone；尝试 `.ssh` 必须仍失败，唯一 Gateway 与 MiniMax-M2.7 不变。

## 2026-08-29 同轮工具批次不丢调用回归

- `test_tool_round_chunks_excess_calls_without_fake_failures` 固定
  `max_tool_calls_per_round=2` 并一次提交 5 个读取，断言 5 个 handler 全部执行、5 个结果均为真实成功、
  上下文中不再出现“剩余调用没有执行”。
- `test_parallel_batch_limits_cap_segments_without_dropping_calls` 分别覆盖
  `max_parallel_tool_calls` 和历史 `max_tool_calls_per_round`：并发峰值都为 2，但 5 个结果仍按 provider 顺序
  完整记录。取消、Compact 与耐久上下文切换的原有未启动结果测试继续保留，不能因本修复放掉客观中断。
- `test_subagents_active_receipt_keeps_root_turn_interrupted` 断言主代理等待 child 的自然回执为
  `unfinished/SUBAGENTS_ACTIVE/subagent_lifecycle`，共享 turn-end 归一为 `interrupted`，不能再被任务链接当作
  普通 completed。
- 真实验收必须在 `.7` 唯一 Gateway、MiniMax-M2.7 和事先公开的 fresh tmux 中原样运行八路调研 Prompt 3；
  重点核对 main 是否把同一次 8 个报告读取实际执行成 4+4、完整整合后才 final，不能用 fake backend 代替。

## 2026-08-29 effective workspace 与运行目录单一事实源回归

- `test_workspace_only_constructor_roots_share_one_effective_runtime_home` 使用同一 owner home 和两个不同的
  constructor cwd 创建 `SimpleAgent`，断言两者的 effective workspace、subagent workspace、conversation
  workspace 与 local store 完全相同。这个用例专门防止 CLI、Gateway worker 和直接构造入口各算一套
  child/runtime 家，不能只断言每条路径“都在 owner 目录里”。
- `test_gateway_request_runtime_errors.py` 与 `test_recovery_code_policy.py` 同跑，固定
  `GATEWAY_WORKSPACE_INVALID` 已进入统一 taxonomy、不可原样重试并给出
  `FIX_PATH_WITHIN_ALLOWED_ROOTS`；这只测试结构化恢复合同，不用错误文案决定权限。
- 与 child debug/hierarchy 合并后的专门命令运行到 100%、exit 0，保留一个既有平台 skip：

```bash
.venv/bin/python -m pytest \
  agent_py_agent/tests/test_owner_resolver.py \
  agent_py_agent/tests/test_subagent_debug_trace.py \
  agent_py_agent/tests/test_subagent_hierarchy_cli_e2e.py \
  agent_py_agent/tests/test_gateway_request_runtime_errors.py \
  agent_py_agent/tests/test_recovery_code_policy.py \
  -q --tb=short
```

- 本轮唯一一次有效全仓 pytest 已在更早阶段跑到 100% 并暴露 39 项失败；修复后只精确复测失败来源和相关
  功能簇，不重复再烧一次全仓。Ruff、doc sync、strict code-size、diff、import boundary 已通过；9 个新增
  正式文件尚未加入真实 index，直接 clean-package 因此如实拒绝，使用只存活于临时目录的 Git index 模拟
  “本轮完整候选已暂存”后 clean-package 通过，真实 index 保持为空。
- `.7` 单 Gateway fresh `ma-cleanup-owner-runtime-r36` 从 `/root` 启动约 0.7 秒即显示 owner home，使用原样
  超级玛丽 Prompt 2；首批 4 名 child 和后续 integrator 共 5/5 DONE，全部 execution cwd 指向同一 canonical
  task，runtime scope 均为 `main-d9283fde2e2d`。主代理在 child 完成后自然整合并 final，Working/Todo 收起、
  roster 保留。产物 5 文件、3,120 行、108,338 bytes，4 个 JS 通过 `node --check`；Playwright 因测试机缺
  `libgbm.so.1` 无法启动，所以只确认 TUI、路径、生命周期、静态引用与语法，不宣称浏览器交互已通过。
- r36 任务共 45 次 MiniMax-M2.7 provider 调用：普通 input 35,602、cache-read 1,003,935、cache-write
  360,893、output 46,771，retry/failed/timed-out 均为 0。主 thread 最高可见约 92.3k，低于 115.2k
  Compact 触发点，因此 `compact 0` 是正确事实，不把终态折叠造成的可见 Context 下降误记成 Compact。

## 2026-08-29 Owner 自主记忆、child 启动阶段与全仓失败精确收口

- Memory/Persona 主链固定：USER、AGENTS、长期 fact/event/project、Lesson/HOT 在结构化证据、owner scope、
  CAS、quota 和冲突门内自主写；只有 SOUL 需要本人确认。双 owner 集成用例写入相近 subject 和 AGENTS 条目，
  断言候选、正式记忆和 Persona 路径互不可见；`/audit` 未改。
- child activity 固定 `queued/starting/waiting_first_event/running`，首个公开事件必须属于当前 exact attempt；
  child 详情在 goal/首事件到达前保留启动说明和 Working，goal 可读后先显示完整用户任务，不再空白。
- 本轮改动超过 10,000 行，因此只执行一次完整 pytest。有效 `.venv` 全仓运行到 100% 后暴露 16 个失败；
  其中旧测试仍读取废弃的 constructor-root `subs/local_store` 路径，已改为 canonical manager/store 路径；
  另外修复 capability 裁决续跑和旧 Audit 误取消纠正被终态保护挡住、validator 错误码漏注册。16 个失败节点
  与相关文件组合复跑 29 项全部通过，随后 Memory/Persona/TUI/生命周期/归档/错误合同组合 focused 518 项
  全部通过；不再重复全仓 pytest。
- 本地静态验收已通过 `ruff check agent_py_agent scripts`、`py_compile`、import-boundary 0 finding 与
  `git diff --check`。doc sync、strict code-size、clean-package 和 `.7/.10` 单 Gateway 多 TUI 真机复验见后续
  同节追加结果，未完成前不把候选标记为已发布。

## 2026-08-29 R63--R64 在途派工停止与并发资源矩阵

- `ma-matrix-r63-110-u19-stop-race-auto` 使用普通中文要求一次创建 8 名 child；有界观察器只在 TUI 出现真实
  `create_subagents` 后约 1 秒经该 TUI 输入 `/stop`。结构化验收为：root request `interrupted`、8 个 canonical
  child 全部 `CANCELLED`、8 个 conversation task link 全部 `cancelled`、无 `*continue*` link、无 child PID，
  TUI 最终显示 8 行“已停止”。这证明 r63 的 active-turn promotion fence 与 late-bind reconcile 正确；旧路径
  从 stop 到最后 child 终态约 53 秒，不能把终态正确冒充为延迟已解决。
- r64 在 root `_resolve_task_params`、批量 save、conversation bind、lifecycle publish/auto-start 以及递归 hierarchy
  scheduler 的每个 durable child 边界复用统一 `CancellationToken`；`ToolCancelled` 不再被 broad exception 错写成
  `TOOL_INVALID_ARGUMENTS`。focused 覆盖“第一项落盘后 token 翻转只保留一个 canonical prefix、零 publish”和
  “递归 schedule 第一名后中断不再创建其余 child”。
- 定向命令已通过：`test_orchestration_create_subagents_tool.py`、`test_orchestration_create_conversation.py`、
  `test_gateway_chat_conversation_context.py`、`test_gateway_conversation_control.py`、全部
  `test_subagent_hierarchy_*.py`、create workspace/coordinator/cancel 组合，以及相关 Ruff/PyCompile。没有重复跑全仓。
- 同一 Gateway 新增真实 TUI：`.7` 的 `ma-matrix-r64-107-u9-queue-research`、
  `ma-matrix-r64-107-u10-pvz-regression`；`.10` 的 `ma-matrix-r64-110-u20-project-port`、
  `u21-architecture-research`、`u22-long-memory`、`u23-memory-isolation-probe`。`.7` 从 8 境加到 10 个 TUI 后
  TUI RSS 约 666MB、唯一 Gateway 约 335MB、整机仍约 5.66GB available；活跃 4+5 child 后 Gateway 约 378MB，
  没有重现清理前 100 多个陈旧 tmux 导致的 5GB 级占用。
- `u22` 在 4 child 运行时自主把“青黛月桥”写入该 owner 的 USER 与 long-term memory，SOUL 未修改；用户插话
  立即显示并在约 47 秒后准确回复，child 同时从 4 个推进到 2 个。全新 owner `u23` 明确回答无该记忆，证明
  同 Gateway 记忆隔离。`u9` 未被提示强制派工也自行创建 4 child，主 thread 已真实显示 `compact 1`；旧长任务
  `u8/u12` 主 thread 均为 `compact 2`，child 也有独立 compact 代数。

## 2026-08-29 R65 手动 Compact 精确中断合同

- 失败基线来自真实 TUI `ma-matrix-r64-107-u13-capability-chain`：手动 `/compact` 动画出现约 16ms 后按 Esc，
  Compact 仍完成 generation 1，证明旧 TUI 没把手动控制操作纳入 Esc 目标。
- 新回归覆盖五层：TUI runtime 只从公开 typed `operation_id` 找当前手动 Compact；Esc 持久提交带
  `target_control_message_id` 的 `/stop`；control receipt 跨 HTTP/磁盘保存该目标；服务端只在同一认证
  user/channel/conversation 下命中 exact interrupt；摘要 provider 收到中断后 generation、checkpoint 引用和
  两条原始历史均保持不变。
- 控制 outbox 另有阻塞竞态回归：普通 worker 已卡在 Compact POST 时，exact stop 的 durable urgent dispatch
  仍在 0.5 秒窗口内发出；两条 operation 各自只完成一次，最终 outbox 为空。这个测试防止“代码里有 stop，
  实际却排在被停止请求后面”的假修复。
- 当前定向结果：control/TUI/Gateway 六文件组合 219 passed；Compact、checkpoint、failure circuit、Gateway
  context 与 create/cancel 子代理组合 151 passed；相关 PyCompile、Ruff 与 `git diff --check` 通过。未重复跑全仓。
- 真实验收必须部署到 `.7` 唯一 Gateway，再从事先公开的 fresh tmux 产生足够长历史，执行 `/compact`，动画
  出现后按一次 Esc；验收同时读取 TUI、control receipt、model-call ledger 和 canonical thread generation，
  并用下一条普通问题确认旧历史仍可读。focused 不能冒充该真机结果。

## 2026-08-29 R68--R70 本机 owner、递归停止、会话恢复与双机资源矩阵

- 产品 focused：

```bash
.venv/bin/python -m pytest \
  agent_py_agent/tests/test_scoped_owner_inprocess_autostart.py \
  agent_py_agent/tests/test_orchestration_background_dispatch.py \
  agent_py_agent/tests/test_gateway_per_user_scoping.py \
  agent_py_agent/tests/test_session_owner_isolation.py \
  agent_py_agent/tests/test_chat_client_context.py \
  -q --tb=short
```

  结果 43 passed；相关 Ruff 与 `git diff --check` 通过。覆盖 `local-agent -> local/main`、显式
  `local/user`、`local/group`、双用户 tasks/memory/sessions/subagents/local store 物理隔离，以及 scoped
  owner 自动派工不再丢身份。
- `.7` 唯一 Gateway `ma-gateway-memory-phase-r69`，MiniMax-M2.7；真实 TUI：
  `ma-matrix-r68-107-u25-owner-memory`、`u26-owner-isolation`、`u27-history-resume`、
  `u28-recursive-stop`。U25/U26 的 USER、task、session、child 均落不同
  `owners/providers/local/users/<id>/`；U25 代号“青铜海鸥-7319”未进入 U26，U26 只持有自己的“白桦罗盘-2846”。
- U28 fresh 协调者创建 4 个孙代理；在协调者详情页按 Esc 后，2/4/6 秒采样仍如实显示分段收口，协调者约
  6 秒取消，四个孙代理最迟约 14 秒全部取消。验收读取 canonical state，不用屏幕文案代替终态。
- U27 session `sess_1788019070_66909cfd` 正常 `/exit` 后以
  `ma-matrix-r70-107-u27-history-reopen` + `--session-id` 重开，完整最终回复和三个已完成 child 立即可滚动查看；
  后续普通中文要求零工具复述，准确返回 Python 3.11.6、根分区剩余 3.7G、9 个 TCP listener、`report.md`
  与“玄武书签-9035”。owner audit 对该 request 只有 user/assistant 行，没有 tool_call。
- 资源采样必须同时记录 `free -h`、Gateway RSS/线程数、所有 `chat --gateway` RSS、对照进程和
  `/metrics` 的 runner/tick gauges。`.7` 关闭 17 个旧 TUI 后 used 2.6G→约 1.7G；保留 10 个 TUI 约
  656M、Gateway 约 315--381M、available 约 5.5--5.7G。`.10` 新增 U39--U46 后 used 约 5.1G、available
  约 10G，不能用 tmux 数量单独解释 Gateway 高水位。
- `.10` 新增真实 TUI：`ma-matrix-r69-110-u39-schedule-watch`、`u40-large-output`、
  `u41-capability-approval`、`u42-owner-memory`、`ma-matrix-r70-110-u43-goal-lifecycle`、
  `u44-guidance-consume`、`u45-process-pty-stress`、`u46-search-skill-repeat`。全部使用同一 Gateway 和
  MiniMax-M2.7；测试仍在进行时必须保留 PARTIAL/OPEN，不能因为模型最终说完成就记 PASS。
- 测试编排操作项：首次向 `.7` 复制 U27 follow-up prompt 时远端测试提示目录不存在，`scp` 失败；创建专用
  `/root/my-agent-test-prompts/` 后重试成功。该失败没有进入产品请求、没有改 owner 数据，记录为 harness
  prerequisite，不能冒充 TUI/Gateway 故障。

## 2026-08-29 R86--R91：常驻对象、终态 guidance 与 capability 快照刷新

- owner soft-curator 切片：owner pool、Gateway maintenance、Memory curator 相关 focused 共 149 项通过；部署
  `.10` 后唯一 Gateway 初始 RSS 从约 509--521MiB 降到约 185--224MiB，owner 磁盘事实与活跃调度未删除。
- 重启 execution lock 切片：dead PID + start-token 的立即接管、未知工具副作用、live/unprovable fail-closed
  相关 focused 共 201 项通过。只有客观证明原 holder 已死才接管；未完成 mutation 仍保持 DIRTY/UNKNOWN。
- child 能力边界与继续执行提示：R88 focused 68 项、R89 focused 86 项通过；root 工具清单隐藏 child-only
  工具，read-only child 仍保留 `capability_request`，主代理提示不再把“下一步会做”冒充当前已经完成。
- 终态 guidance：R90 focused 111 项通过；真 TUI
  `ma-matrix-r90-110-u89-terminal-guidance` 中 child 先写文件并 DONE，随后主代理对 exact run 调用
  `send_guidance`，收到 `SUBAGENT_GUIDANCE_TARGET_TERMINAL`，磁盘没有 pending guidance，run 未复活。
- capability 快照刷新定向命令：

  ```bash
  python3 -m pytest \
    agent_py_agent/tests/test_capability_auto_grant.py \
    agent_py_agent/tests/test_subagent_capability_request_tool.py \
    agent_py_agent/tests/test_resolve_capability_requests_tool.py \
    agent_py_agent/tests/test_tool_round_execution.py -q --tb=short
  ruff check agent_py_agent/agent/agent_core/capability_request_tool.py \
    agent_py_agent/tests/test_capability_auto_grant.py
  python3 -m py_compile agent_py_agent/agent/agent_core/capability_request_tool.py
  git diff --check -- agent_py_agent/agent/agent_core/capability_request_tool.py \
    agent_py_agent/tests/test_capability_auto_grant.py
  ```

  结果：69 passed；Ruff、PyCompile、diff check 全部通过。没有运行全仓 pytest，本切片远低于额外全仓门的
  10,000 行阈值。
- R91 fresh 真 TUI `ma-matrix-r91-110-u94-capability-refresh`：main 只创建输入并派 researcher；child 从
  read-only snapshot 读取文件、调用 `capability_request`、跨 `context_refresh` 续片后自行写出
  `report-capability-refresh.md`（1,562 bytes），最终 DONE、重试 1、compact 0。该 owner 归档中
  `TOOL_UNAVAILABLE` 为 0，主代理只核对两个文件存在。
- 同轮功能 TUI：U90 自主 Memory 检索并在唯一一次 TUI 确认后修改 SOUL；U91 完成两份大 Web 文档的
  `head/middle/tail/EOF` 读取和报告；U92 完成 PTY 与受管后台 HTTP 的 start/read/resize/list/wait/network/
  stop/close；U93 完成四个一级 child 报告，但四个孙代理因管理员级 grant 未在 TUI 浮出审批而缺失，保留为
  OPEN，不按主代理“整合完成”算全通过。
- R91 U95 只读取两个代码审阅 Skill，复现并修复循环队列边界错误，写 3,373-byte 报告；U96 八个
  researcher 全部自然 DONE，8 份独立报告和 9,395-byte 整合报告客观存在；U97 两个连续回合只形成一个
  task root，零 Compact 准确召回 `长河-9713` 并原位更新 phase-two。三路均从真实 TUI 输入普通中文完成。
- 资源采样：R91 唯一 Gateway PID `2704961`；22 个 `chat --gateway` TUI 合计约 1,899MiB、平均 86.3MiB，
  Gateway 约 275MiB，5 个 终端交互 对照约 1,674MiB，LiteLLM 约 302MiB；整机 15GiB 中 available 约
  10GiB。`.7` 当前 ICMP 间歇可达但 22 端口拒绝，故本轮不能宣称取得 8G 机器当前 RSS，只保留上一轮
  “17 个旧 TUI 清理后下降约 1GiB”的可复验历史样本。

## 2026-08-29 R92 child 结果耐久交接与 30 路 TUI 资源扩展

- 回归把四条直属 `subagent-completion.v1` 写入同一 root，先标记 handled，再追加 30 条大体积后续观察把它们
  挤出普通 Recent Observations，最后触发 `scheduled_progress_report`。在 2,200 token 的后台总预算下仍断言
  四个 exact `final_report_ref` 全部存在、长正文已裁短、孙代理和私有 runner payload 不可见。
- 定向命令：

  ```bash
  python3 -m pytest \
    agent_py_agent/tests/test_background_main_agent_runtime.py \
    agent_py_agent/tests/test_background_main_wake_recall.py \
    agent_py_agent/tests/test_gateway_chat_conversation_context.py \
    agent_py_agent/tests/test_conversation_wake_events.py \
    agent_py_agent/tests/test_integration_coverage_context.py \
    agent_py_agent/tests/test_runtime_parameter_config.py -q --tb=short
  ```

  全部通过，保留 2 个既有 xfail；相关 PyCompile、Ruff 与 selected `git diff --check` 通过。没有重复跑全仓
  pytest，本切片远低于约 10,000 行额外全仓门。
- `.10` 唯一 Gateway R92 fresh `ma-matrix-r92-110-u98-child-handoff`：四名 child 4/4 DONE，每条完成观察均有
  非空 `completion_message` 与可读 `final_report_ref`；主代理写出 7,448-byte
  `output/python-four-modules.md` 并自然 final，任务归档中 `WRONG_STATUS_SURFACE=0`。
- 旧失败 `ma-matrix-r89-110-u85-compact-interrupt` 收到普通中文“继续完成”后，在主 thread `compact 1` 的
  同一会话中读回四份既有结果并给出最终对照表，证明旧任务也可恢复，不是只让 fresh 样例通过。
- 新增 U99--U105 七路真实 TUI，覆盖 Goal、Schedule/Watch、Search、活跃 child guidance、Memory+Compact 和
  双 owner 隔离。启动后共 30 个 TUI：合计 RSS 约 2,516.9MiB、PSS 约 2,100.9MiB、Private 约
  2,089.6MiB，平均 PSS 约 70MiB；唯一 Gateway 在并发开工时 PSS 约 345.6MiB，整机 available 约 9.7GiB。
  `.7` 仍 ping 全丢且 SSH 超时，当前 8GiB 机器证据继续标环境阻塞，不能拿旧 `used` 冒充现值。

## 2026-08-30 R99 被动 owner 投影、索引压紧与 79 TUI 重连压力

- 本地定向验收（未跑全仓）：

  ```bash
  python3 -m pytest \
    agent_py_agent/tests/test_owner_scoped_pool.py \
    agent_py_agent/tests/test_background_notice_display.py \
    agent_py_agent/tests/test_gateway_agent_control_service.py \
    agent_py_agent/tests/test_home_global_index.py \
    agent_py_agent/tests/test_home_maintenance.py -q --tb=short

  python3 -m pytest \
    agent_py_agent/tests/test_gateway_http.py \
    agent_py_agent/tests/test_gateway_per_user_scoping.py \
    agent_py_agent/tests/test_home_maintenance.py -q --tb=short
  ```

  第一组 79 passed，第二组 45 passed + 1 skipped；七个相关产品文件 PyCompile 通过，相关产品/测试文件
  Ruff 通过。远端按 R98 现有代码移植最小补丁后，owner pool/global-index/home-maintenance 基线 36 项通过。
- R99 修复前 `.10` 有 69 个 `chat --gateway` TUI，合计 PSS `4,859,509KiB`（约 4.63GiB），唯一 Gateway
  PSS `610,107KiB`（约 595.8MiB）。`py-spy` 证明 16 个 HTTP worker 大量在 `/client/notices` 里构造完整
  scoped Agent，并因 79 个不同 owner 超过 64 容量形成 LRU 反复构造。
- 部署最小修复、同时让 69 个旧 TUI 重连后，Gateway PSS 约 189.1MiB，10 次 `/status` 为
  4--13ms。再以 10 个 fresh owner 同时提交 MiniMax-M2.7 真实中长任务，全部在 TUI 立即进入
  Thinking/Working，Gateway PSS 约 228.1MiB，20 次 status 最慢约 0.21s，`pending=0/processing=10`。
- 现场全局索引原始约 1.4GiB，在停止唯一 Gateway 且队列归零后备份到
  `/home/my-agent-r99-backup-20260830T1155/index`，然后从 168 owner、555 task、555 run、1062 agent 权威目录
  重建为 2340 行、约 814KiB，`load_errors=0`。备份校验值保留，可精确恢复。
- fresh TUI 名称为 `ma-matrix-r99-110-u152-*` 至 `u161-*`，覆盖 Memory、长历史、文件流水线、
  工具错误恢复、8 child、递归 child、Skill 路由、受管后台进程、owner 隔离和长会话 Compact。U157 的
  coordinator 已真实显示“等待下级”，是 R98.1 的 fresh 复验通过证据。
- `.7` 当前 ICMP 丢包且 22 端口拒绝连接，本轮不宣称拿到 8GiB 机器新样本。过往 `.7` 的约 1GiB
  下降来自关闭 17 个已结束但仍驻留的 TUI；本轮 `.10` 再次量化到 69 个 TUI 合计约 4.63GiB，证明
  8GiB 机器必须限制同时常驻窗口数，但这不等于按用户启动多 Gateway。

## 2026-08-30 R110 Todo/child exact-id 绑定遵循回归

- 失败样本：`.10` R109 原样多子代理调研任务创建 9 条 Todo 和 8 名 child；真实
  `create_subagents` 参数中 8 项均无 `covers`，最终 canonical progress 为 8 done + 9 pending。该证据用于
  验证模型合同，不把 TUI 文案或标题当成状态事实。
- 本地定向命令：

  ```bash
  python3 -m pytest \
    agent_py_agent/tests/test_config_layers.py \
    agent_py_agent/tests/test_orchestration_tools.py \
    agent_py_agent/tests/test_orchestration_tool_specs.py \
    agent_py_agent/tests/test_task_progress_advisory.py \
    agent_py_agent/tests/test_background_context_runtime_errors.py \
    agent_py_agent/tests/test_dispatch_progress_seed.py \
    agent_py_agent/tests/test_orchestration_create_subagents_items.py \
    -q --tb=short
  python3 -m py_compile <本切片 9 个产品模块>
  python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
  python3 scripts/check_doc_sync.py
  git diff --check
  ```

  结果：105 项 focused 全通过；PyCompile、strict code-size（hard=0）、doc sync 与 diff check 通过。没有运行
  全仓 pytest，本切片远低于约 10,000 行门槛。
- 回归同时锁定：`covers` 在 native item Schema 中紧跟 `goal`；open-plan guidance 提供 exact-id 复制与漏绑
  收尾结构；派工回执排除 child run-id seed 行；有界 result envelope 保留 bound/unbound ids；旧的可选 covers、
  已关闭项 correction、未知/关闭/重复 covers 原子拒绝与嵌套 child 预检继续通过。
- fresh TUI：`.10` 唯一 Gateway + MiniMax-M2.7，tmux
  `ma-fbb562c-110-u273-todo-binding` 只输入一次用户原样 Prompt 3。初次派工的 8 个 item 分别携带
  `covers=["proj-1"]..["proj-8"]`，结构化 `coverage_binding` 为 `optional_exact`，8 个 run 全部进入 `bound`，
  `open_target_ids` 只含九个原计划 id。child DONE 后原 Todo 从 `0/9` 单调推进到
  `2/9、3/9、4/9、5/9、6/9、8/9`；五名 child 发生 `compact 1` 后仍继续并关闭原 exact id。
- root 在 `8/9` 自动唤醒并开始整合，发现首名 轻量运行时 child 缺少约定产物后自主补派 researcher-9。其 canonical
  state 没有 `covers/progress_item_ids`，TUI 显示“`8/9 + 另有 1 个子代理运行中`”，未把返工错绑到唯一剩余
  的 `integrate`。测试者没有追加技术提示、手工改产物或替被测对象执行任务；长任务最终整合仍由被测 TUI
  自行继续，不影响本切片对 exact-id 自动回流与返工不乱绑的验收结论。

## 2026-08-30 R111 主代理最终 Todo 软核对

- 失败样本：R110 原样 Prompt 3 的 8 个 covers 项全部变成 done，补派 child 独立 done，root 也交付并最终
  回复；但 canonical `task-path:ad2d3ff10457ee50` 仍是 `done=9,pending=1,total=10`，pending 项为
  `integrate`。终屏收起 Todo 不能替代结构账本核对。
- 对照：会话运行时 `会话运行时-rs/core/src/tools/handlers/plan.rs` 只投递模型的 `PlanUpdate`；终端交互
  `TaskUpdateTool/prompt.ts` 要求完成后显式更新稳定 task id，并由 task reminder 把当前未完成项重新放进模型
  上下文。两者都不从最终回复正文自动关闭计划。
- 本地定向覆盖：默认/关闭配置解析；`task_progress` 开放项回执携带 exact-id closeout contract；后台 Task
  Runtime State 只投影当前 display plan 的 open ids；关闭开关后不注入额外 contract。提醒明确
  `blocking=false` 与 `never_auto_close_never_completion_gate`，不会新增模型轮或改变任务终态。
- fresh TUI 必须使用唯一 Gateway + MiniMax-M2.7 和用户原样长任务；验收 root 在最终回复前自主调用
  `task_progress` 更新自己完成的最后一项，canonical 计划达到 9/9，且不能由宿主或测试者代写状态。

### R111 fresh 真实结果：失败

- 提交 `83b0df9` 部署到 `.10` 后，tmux `ma-r111-110-u274-todo-closeout` 只输入一次用户原样 Prompt 3。
  初始计划为四个双项目调研项 + `整合报告`；四名 child 均携带 exact covers，TUI 正常从 0/5 到 4/5。
- 初批报告中两份 completion message 明显停在“继续收集/准备写入”，root 没有草率交付，先后补派两批共四名
  未绑定返工 child；界面保持 4/5 + 独立运行 child，没有错绑 integrate。八名 child 最终都 DONE，六名经历
  `compact 1` 后继续；root 自己写出 `work/ai_agent_landscape_comparison.md`（22,938 字节）并最终回复。
- 失败证据：整轮工具账只有初始一次 `task_progress`；最终 canonical 账本为 `done=4,pending=5,total=9`。四个
  done 只是未绑定返工 child run-id，五个原计划项仍全 pending。TUI 的 4/5 来自 child 生命周期展示投影，
  并未持久回 canonical。R111 因此不通过，最终报告存在不能替代 Todo 结构事实。

## 2026-08-30 R112 后台 covers 对账同源

- 原因回归：`task_progress` 工具读取前会持久化 exact covers 的 canonical DONE，后台 Task Runtime State 过去
  只持久化未绑定 child run-id。测试新增显式 `task_root` 场景，锁住没有 `_current_run_params` 的后台 agent
  也能把 covers 项写回同一本 task-path ledger。
- 集成回归：创建 `tests + integrate` 两项，并放置 `sub-tests canonical DONE, covers=[tests]`；调用后台
  `context_markdown` 后断言持久账本 tests=done、integrate=pending，`plan_continuation` 和
  `task-progress-closeout-guidance.v1` 的 open ids 只剩 integrate。
- 本地 focused：`test_dispatch_progress_seed.py + test_background_context_runtime_errors.py +
  test_task_progress_advisory.py` 共 47 项通过。fresh 仍用唯一 Gateway、MiniMax-M2.7、原样 Prompt 3；不得由
  测试者插话或手工改账，最终必须同时核对 TUI、工具记录和 canonical progress.json。

### R112 fresh 真实结果：covers 通过，root-owned 收尾失败

- `.10` 唯一 Gateway、MiniMax-M2.7，tmux `ma-r112-110-u275-todo-reconcile` 只输入一次原样 Prompt 3。
  八名 child 全部 DONE，canonical 原计划 8 个 covers 项真实成为 done 并带 typed evidence；最终持久账本
  `done=8,pending=1,total=9`，唯一 pending 是 root 自己的横向整合项。
- root 写出 `output/coding_agent横向对比报告.md`（16,274 bytes）并自然 final，但整轮只有初始一次
  `task_progress`。因此 R112 的 covers 同源修复通过，R111/R112 尚未让 MiniMax 主动关闭 root-owned 最后一项。
- root Compact 动画到 20% 后失败，ConversationThread 记录 generation 0、连续失败 1 和泛化的
  `COMPACT_PROVIDERRESPONSEERROR`；任务没有中止，两名 child 分别 Compact 1 后完成。R113 的 focused 验收要
  锁住 typed provider code、失败块可见和原上下文保留。

## 2026-08-30 R113 每轮 Todo 纪律与 Compact 失败码

- 定向测试覆盖：默认/关闭开关下 Workspace Context 的短纪律；provider
  `MODEL_EMPTY_RESPONSE -> COMPACT_MODEL_EMPTY_RESPONSE`；transcript/live-tool 失败事件经 Gateway/TUI
  仍只携带有界错误码；渲染明确显示“原上下文已保留”和 typed code。
- prompt 常驻预算回归继续运行完整 `test_gateway_conversation_compact.py`。其历史 12k fixture 在 R112 HEAD
  也会因恢复目标边界失败，调整为 13k 留出演进余量；生产触发点、恢复目标和候选判定均未修改。
- 发布后继续用一个 Gateway、多 owner 的真实 TUI 跑中长任务：至少覆盖多子代理整合、连续追加消息、主/子
  Compact、插话/等待/终态、历史与 owner 隔离；每一路先公开 tmux 名称，环境缺失项单列而不冒充完成。

## 2026-08-30 R114p 首工具 cwd 与后台 Skill 续接

- 真实失败基线：`.10` 单 Gateway 的 `ma-r114o-110-u303-security-skill` 首轮同时读取 3 个 Skill 并调用
  `run_command` 克隆 Requests。canonical task root 已建立，但执行归档和文件系统都证明 clone 落在 owner
  home；下一轮相对 `list_files` 从 task root 查找因此返回 `PATH_NOT_FOUND`。
- 新回归刻意让外层 `RunParams.task_attributes` 与 `ToolLoopExecuteParams.task_attributes` 成为两个 dict。
  禁用同步函数时用例在 exact 工具投影缺少 `run_workspace` 处失败；启用后首次 shell 的 working_dir、真实目录
  和 write boundary 都指向 canonical task root，owner home 不再产生同名目录。
- `.10` `ma-r114o-110-u299-pdf-skill` 的 child 完成唤醒轮在默认后台快照中两次调用 `skill_search`，Gateway
  记录 typed `TOOL_UNAVAILABLE`，该轮为修复协议额外形成 9 次 provider call。默认 lifecycle profile 加回
  `skill_search` 后，定向策略测试锁定同任务后续轮仍可读取 Skill 正文；显式 owner/task 减权限仍优先。
- 本地 focused（未跑全仓）：Gateway 会话、文件系统工具和核心工具 Schema 共 159 项通过；新增 cwd 生产形态
  4 项通过；后台策略 3 项通过；相关文件 Ruff 与 `git diff --check` 通过。fresh wheel/TUI 复验仍待当前 8 路
  长任务自然结束后执行，避免人为中断测试样本。

## 2026-09-01 R124 capability 父级工具 authority 失败基线与本地候选

- `.10` 单 Gateway、MiniMax-M2.7，tmux `ma-r124-110-main-capability-approval`。测试只要求一名无 Computer
  Use 预置的 child 申请 `mcp__computer_use__list_windows` 与
  `mcp__computer_use__take_screenshot_with_ocr`，父级在自身权限内裁决，并把 capability 与具体危险调用
  审批分开。真实结果为 main deny、request CLOSED、child DONE 且无第二 runner session；终屏却称 PASS。
  该轮保留为失败证据，不能当安全拒绝正例。
- 本地候选覆盖：创建时 host snapshot 覆盖模型同名 attributes 且退出 ContextVar 后不泄漏；根父级使用
  creation snapshot 而非漂移的进程 registry；嵌套父级读取 exact execution context；越权工具不结清 OPEN
  request；普通/MCP grant 同时进入后续工具快照与 durable allowed tools；wake 明确危险 ToolCall approval
  独立。
- 定向命令：

  ```bash
  python3 -m pytest \
    agent_py_agent/tests/test_orchestration_create_subagents_tool.py \
    agent_py_agent/tests/test_orchestration_create_subagents_items.py \
    agent_py_agent/tests/test_resolve_capability_requests_tool.py \
    agent_py_agent/tests/test_direct_parent_lifecycle.py \
    agent_py_agent/tests/test_capability_auto_grant.py \
    -q --tb=short
  ```

  结果：104 项通过；相关 PyCompile、Ruff 与 `git diff --check` 通过。fresh wheel 尚未部署，下一轮必须同时
  核对 TUI、canonical child/request/grant/session、approval ledger 与真实 Computer Use 结果。

### R125 fresh：父快照通过，MCP 短名与语义路由竞态失败

- wheel `8694e876...` 部署到 `.10` 唯一 `ma-gateway-r124-capability-110`，MiniMax-M2.7；tmux
  `ma-r125-110-main-capability-approval` 只输入一段普通中文，要求一名子助手自然申请桌面能力。
- canonical child `subagent-1788290283-ec8f4f06` 的 `direct_parent_tool_authority` 已精确包含完整
  `mcp__computer_use__list_windows` 与 `mcp__computer_use__take_screenshot_with_ocr`，证明 R124 第一层修复
  生效。失败点是 child 在 `requested_mcp_tools` 填短名，且 CapabilityRouter no-hit 在 main wake 前把 OPEN
  变 GAP；main 随后看见 unavailable 并 deny，resolve 又得到 no pending。该轮仍为失败，不算真机完成。
- 新候选只在父快照内唯一末段完全匹配时把 MCP 短名规范成完整名；重名/未知不猜。父级拥有全部 exact 工具
  时，语义路由返回观察型 `PARENT_RESOLUTION_REQUIRED` 并保持 OPEN。扩展后的 capability、route、dispatch、
  create、approval 组合 193 项 focused 通过；待下一 wheel fresh TUI。

### R126 fresh：模型把 MCP 短名放入普通工具字段

- `.10` 唯一 Gateway、MiniMax-M2.7，tmux `ma-r126-110-main-capability-approval`。child
  `subagent-1788291990-346e17f6` 的父 authority 再次包含三项完整 Computer Use 工具，但模型提交
  `capability_type=tool + requested_tools=[list_windows,take_screenshot,take_screenshot_with_ocr]`。第一版只规范
  `requested_mcp_tools`，因此 Router 仍抢先结 GAP，父级 resolve 得到 no pending；本轮仍失败。
- 候选让普通/MCP 两个申请字段共用父快照唯一 exact 规范器：普通完整工具名优先，只有唯一 MCP 末段命中才
  归入完整 MCP 字段，未知/重名保持原字段并 fail closed。新增真实参数形态回归后相关组合 194 项通过。

### R128 fresh：直属父级 grant、同 run 续片与危险 ToolCall 独立审批通过

- `.10` 唯一 Gateway `ma-gateway-r127-capability-110`、MiniMax-M2.7，真 TUI
  `ma-r128-110-main-capability-approval`。只输入一段普通中文，要求 child 自然发现 Computer Use 缺口、直属
  main 在自身 authority 内自主批准、原 child 续跑，且具体桌面操作仍由用户逐项确认。
- child `subagent-1788293169-474f3c3b` 提交 `capreq-1788293189-ab042cc0`；普通/MCP 两个输入字段最终规范为
  四个完整 `mcp__computer_use__*` 名称，request status 为 GRANTED，grant 为
  `capgrant-1788293213-1dc31102`。`parent_tool_authority` 来源为
  `parent_creation_runtime_snapshot`，`all_requested_tools_grantable=true`、`unavailable_tools=[]`，并明确
  `capability_resolution_requires_user_approval=false`、`dangerous_tool_call_approval_is_separate=true`。
- 原 child 第一片 runner session 为 `runsess-subagent-...-1788293174856-155690`，获批后第二片为
  `runsess-subagent-...-1788293223305-155866`，最终仍是同一 run 且状态 DONE。真实 trace 依次包含
  capability_request、list_windows、get_screen_size、take_screenshot_with_ocr，后三项均成功。
- OCR 调用产生独立审批 `approval:053e0842c70dd2439b6ed55d`；用户在 TUI 选择“允许一次”后，canonical
  `runtime_gate_ledger` 才记录 `tool_approval=APPROVED/allowed=true`，并关联 exact operation 与结果 ref。
  OCR 结果真实读到窗口标题 `root@myagent-test:~` 和屏幕文本 `COMPUTERUSEPASSR122`。因此本轮通过来自
  request/grant/session/approval/tool ledger 与实际结果，不来自模型终屏自报。
- 提交前严格 focused gate 扩展到 capability、direct-parent、approval、Gateway control、runtime guidance 与
  background main 共 401 个 selected case：395 passed、6 个仓库既有 expected xfail、0 failed；Ruff、
  PyCompile、doc sync、strict code-size（hard=0）、`git diff --check` 和 clean-package 全部通过。

### R129：主代理活跃回合重启恢复失败基线与候选回归

- 真 TUI：`ma-r129-110-main-active-restart`，唯一 Gateway 从旧 PID 155435 切换到
  `ma-gateway-r129-restart-110` / PID 160108，模型 MiniMax-M2.7。重启前 15 个 RuntimeDB tool operation 与
  owner tool index 的 15 条 exact request 归档逐项一致，任务目录已有 6 个文件；重启后请求成功 requeue，
  但 generic stale-attempt 调和与 main authority binding 冲突，第二次尝试在模型调用前失败。
- 候选 focused 覆盖：exact task/run + terminal operation + matching archive 自动 recovered；成功/失败工具缺
  归档、EXECUTING/UNKNOWN、DIRTY mutation、身份错配继续 blocked；CLAIMED 且 handler 从未开始时安全取消；
  既有人工 `recover_attempt_unknown` 与 generic create-attempt unknown 门保持不变；Gateway carried history 在
  agent.run 前完成同源核对。
- 定向命令：

  ```bash
  python3 -m pytest \
    agent_py_agent/tests/test_runtime_db_recover_stale.py \
    agent_py_agent/tests/test_cli_resume_contract.py \
    agent_py_agent/tests/test_gateway_chat_conversation_context.py \
    agent_py_agent/tests/test_local_store_gateway_recovery.py \
    agent_py_agent/tests/test_startup_commands.py \
    -q --tb=short
  ```

  结果：全部运行到 100% 通过。真实产品结论仍待新 wheel 的 R130 TUI 重启复验；合同测试不能替代该门。

### R130：主代理活跃点跨 Gateway 原地续接通过

- wheel `bb42ce82392bc63d6a9ddf1df2415eceb34007f5116972a2317101118769578c` 原位部署后，`.10` 始终只有一个
  8420 监听者；真 TUI 为 `ma-r130-110-main-active-restart`，Gateway 从 PID 165459 顺序切换到 PID 166634，
  欢迎页明确显示 MiniMax-M2.7。
- 只输入一次普通中文，要求主代理在自己的 task root 完成中型 JSONL 分析 CLI、三组样例、两种报告和至少
  12 个 unittest。重启发生在同一 request 正在生成下一次 Write 参数时；重启前已有 21 个 terminal tool
  operation，重启后 request/thread/task/run 全部保持原 identity。
- RuntimeDB 事件精确记录 `recovery_mode=recorded_active_turn`、`recorded_operation_count=21`；旧 attempt
  generation 1 为 recovered，新 attempt generation 2 为 done，并只新增 5 个后续操作。最终 18 个 unittest
  通过，三组样例和六份报告存在，TUI 自然 final、Gateway pending/processing 均归零。
- 反证同时保留：同轮 pytest 因受管环境 `/dev/null` 无权限而在 capture 初始化失败；模型切换 unittest 的
  18/18 只能证明业务产物，不证明 pytest/sandbox 正常。该问题另列底座修复，不污染重启门的正负结论。
- 当前只勾选“主代理活跃点”；等待直属 child 与 coordinator 等待孙代理仍必须分别使用 fresh 真 TUI 顺序
  重启唯一 Gateway，不得用本轮主代理样本外推。

### R131：main 等待唯一直属 child 时跨 Gateway 重启通过

- fresh TUI `ma-r131-110-direct-child-wait-restart` 只输入一次普通中文，根只派一名直属 child。main
  generation 1 以 `SUBAGENTS_ACTIVE` 让出、TUI 显示“等待 1 个子代理”后，唯一 Gateway 从 PID 166634
  顺序切换到 168278。
- child `subagent-1788302139-230579bf` 全程只有 generation 1 和 runner PID 167884；Gateway 退出后进程由
  init 接管继续，没有新增 child、attempt 或 runner。child 完成后新 Gateway 自动创建原 main generation 2，
  检查并直接 final。
- canonical tree 精确 2 个 run；产物在原 owner task root，34 个 unittest、四组样例和 JSON/Markdown 报告
  完成。测试者没有发送“继续”或修改产物。

### R132：额外 coordinator 与最终层级误报负样本

- fresh TUI `ma-r132-110-coordinator-grandchildren-restart` 的第一 coordinator 收到的 prompt 已明确要求亲自
  创建两名实现者，但 MiniMax-M2.7 又派一名 coordinator，形成 root→coordinator→coordinator→2 workers。
  五个 run 后续全部自然完成，46 个 unittest 通过；但目标等待窗口在观察期间结束，本轮未执行 Gateway 重启，
  不能计入恢复门。
- 根 final 又把第一 coordinator 间接完成的两名 worker 表述成直接创建。canonical `parent_agent_run_id` 与
  最终文字不一致，保留为模型拓扑遵循/事实汇报问题；不能用终屏自报覆盖结构化树。

### R133：coordinator 等待两名直属 worker 时跨 Gateway 重启通过

- fresh TUI `ma-r133-110-coordinator-wait-restart` 形成精确 4-run 树：root、1 coordinator、2 workers。
  coordinator generation 1 的宿主事件为 `runtime_reason=SUBAGENTS_ACTIVE`；两名直属 worker 均是 running /
  generation 1 / runner PID 171475 时，唯一 Gateway 从 PID 168278 顺序切换到 171987。
- 重启前后 worker 的 run、attempt、generation 和 runner PID 全部不变。两名 worker 自然 done 后，原
  coordinator 才创建 generation 2 集成；随后原 main generation 2 自动检查并 final。全程没有额外 coordinator、
  worker 或人工推动消息。
- 最终 91 个 unittest 全通过；canonical tree 为 coordinator=1、worker=2，TUI 直接显示完整最终汇报，
  Gateway pending/processing 归零。至此主代理活跃、main 等 child、coordinator 等孙代理三个顺序重启门均通过。
# 2026-09-02 Compact 来源与提交权协议

- 新增唯一 `compact_progress.py` normalizer；三种合法组合分别覆盖 transcript、持久 active-turn 工具归档和
  turn-local 工具整理，历史 v1 双字段缺失显式降为 `legacy/legacy`，部分缺失或矛盾组合 fail closed。
- Gateway rich chunk、后台 transcript、TUI adapter 和 renderer 不再各自复制 schema；TUI 分别显示“正在压缩
  会话上下文”“正在整理工具上下文”“正在整理当前工具历史”，但只有 conversation-thread CAS 可以增加
  `compact N`。
- 定向命令：

  ```bash
  python3 -m pytest agent_py_agent/tests/test_compact_progress.py agent_py_agent/tests/test_gateway_conversation_compact.py agent_py_agent/tests/test_gateway_streaming.py agent_py_agent/tests/test_tui_runtime.py agent_py_agent/tests/test_background_notice_display.py agent_py_agent/tests/test_tui_renderer.py agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py agent_py_agent/tests/test_subagent_runtime_compact.py -q --tb=short
  ```

  结果 223 passed。真 TUI 标签将在同 wheel 的 MiniMax-M2.7 长会话继续验收。
