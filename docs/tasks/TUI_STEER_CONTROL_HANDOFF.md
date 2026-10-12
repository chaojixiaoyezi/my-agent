# TUI 插话与停止修复实施记录

状态：核心插话与控制修复已在独立分支实现，并在 192.168.1.8 原生 TUI + MiniMax-M2.7 验证。主线集成尚未进行；历史空回复归因、P6 维护排空及真实飞书入口仍按下表接续。

## 本轮结论

普通输入、Esc 和 `/stop` 现在按服务端实际持有执行权的 claim 与 core attempt 找目标，不再由 TUI 正在观察的等待请求决定。已证明连续插话在 Compact generation=0 时消费，也证明自动 Compact 前、中、后的输入全部消费；原生 TUI 的子代理等待、跨线程停止隔离和两类崩溃恢复也已验证。

原问题有三层原因：一是等待请求占住显示位置，使 UI 目标与后台实际执行者错位；二是输入对账把同名运输 request 的结束当成持久 task 的结束；三是旧提交批次或显示接回失败会阻碍后继消费/观察。修复分别落在原执行绑定、typed 生命周期结算、原提交账本与原 TUI outbox，没有靠提示词要求模型“记得看插话”。

本轮没有切换正在工作的生产安装。严格 gate 的最终定向组合为 863 passed、4 个既有 xfail；1.8 另有 41 项核心回归及 63 项恢复提示/UI 回归通过。九组真机记录、实际失败分支和剩余边界见下文。
状态：用户已授权开始；核心插话与控制修复已在独立分支实现，在 192.168.1.8 原生 TUI + MiniMax-M2.7 验证。完整 T01—T26 仍有下表列出的缺口；本记录不代表可以切换生产。

## 范围、基线与环境

- workstream：T；owner：本任务；日期：2026-10-09；worktree：Codex 管理的 `tui-steer-control-20261009` 独立 checkout。
- 本线为 T，按原《TUI_STEER_CONTROL_REVIEW_2026-10-09》的 T01—T26 推进；普通输入、Esc、stop、恢复、交接和维护排空分别保留合同。
- 分支 `codex/tui-steer-control-20261009`；起始提交 `c52257ef52a05dd24ed99dbba133bf58b2b7d7dd`，包含已确认部署源码 `0b938e57e`。未修改原 checkout 或生产安装。
- 开工核对的远端 main 为 `8ade40d6f`。B1b 集成分支现为 `e8b1623b7`，P6 合同分支为 `d9631eb37`；尚未将这两条依赖集成进本线，不声称多进程控制已验收。
- 用户将测试机从不可连接的 192.168.1.11 改为 **192.168.1.8**；真实模型为已有官方档案的 **MiniMax-M2.7**，端点 `https://api.minimaxi.com/anthropic`。
- 1.8 根分区满，测试全部放在 `/home/tui-steer-control-20261009`；独立虚拟环境、my-agent home、私有 8438 端口，一个候选 Gateway 服务多个测试 TUI。已有 8420 服务保持运行。
- 候选进程使用清空后显式构造的环境；凭据只保存在测试机权限 0600 的私有文件中，不进入仓库、终端输出和证据。只测试合成任务。
- 现场排查只读取白名单身份、状态和代码；未读取现有任务正文、工具输出、终端画面或交付文件。

## 参考与设计依据

已读对应三个 `*_contract_code_files.xlsx` 索引，再定位源码，未更新参考仓库。

- Codex `codex-rs/core/src/session/mod.rs::steer_input`：在活动回合锁内核对实际 task 和 expected turn；排队请求不能替代执行者。
- Hermes `run_agent.py::steer`：在下一工具循环接收插话；不同阶段的中断范围保持区别。
- OpenClaw `src/agents/embedded-agent-runner/runs.ts::prepareEmbeddedAgentQueueMessage`：使用真实运行句柄及结构化可投递状态。
- 本项目复用原 claim、RuntimeDB、guidance、输入回执和控制回执；没有增加第二份当前任务库或投递账本。

## 实现与代码地点

下列路径均相对本分支根目录。

| 位置 | 当前改动 |
| --- | --- |
| `agent_py_agent/agent/gateway_parts/execution_target.py` | 从有效 claim、原请求 write-ahead 绑定和 task link 读取实际执行；投影包含 thread/kind/turn/task/claim/attempt/runtime authority；发送前在原锁内复核 |
| `agent_py_agent/agent/gateway_parts/execution_control.py` | TUI/IM 认证 scope 共用目标解析；旧 expected execution 拒绝改投；普通后台与前台使用同一控制合同 |
| `agent_py_agent/agent/gateway_parts/control_service.py` | 普通插话、显式 steer、Esc、stop 绑定实际执行；去除旧的普通后台弱 claim 查询；会话派活的独立显式绑定入口保留 |
| `agent_py_agent/agent/conversation/claim_execution_binding.py` | core 在模型前将真实 run/attempt CAS 发布到当前原 claim；Compact 更新 attempt，换 claim 或偷换任务/run 被拒绝 |
| `agent_py_agent/agent/conversation/background_claim.py`、`run_claim.py` | 实际前台/后台领取入口登记 claim 专属中断名；旧控制不能按复用任务名取消新工作片 |
| `agent_py_agent/agent/conversation/background_execution.py` | 接通后台 core 身份发布；异常/中断退出只释放该精确 attempt 的提交前 reserved；submitted 未知保留 |
| `agent_py_agent/agent/conversation/store_guidance_submission.py` | 旧 rejected 提交批次保留为历史证据，不能重新取得已释放消息或后继 attempt 的所有权；原 submitted 未知仍不重放 |
| `agent_py_agent/agent/gateway_parts/input_delivery_service.py` | 周期对账、入口收尾从原 guidance 的 target_type/target_id 判断生命周期；同名 request 终态不代表 task 终态 |
| `agent_py_agent/agent/conversation/store_guidance_recovery.py`、`gateway_parts/recovery.py` | 原结算合同增加精确 target 范围；先修提交/确认批次，防止运输收口批量拒绝活跃 task 邮箱 |
| `agent_py_agent/agent/gateway_parts/http_handlers.py` | notices 返回 execution_target 的身份投影，不返回任务正文；成功空快照清掉客户端旧目标 |
| `agent_py_agent/cli/chat_parts/tui_runtime.py`、`tui_threading.py`、`tui_actions.py` | 展示/观察 job 与执行目标分开；普通输入、Esc、stop 使用服务端执行投影；回执重复接回只创建一个观察 job，接回失败可重试 |
| `agent_py_agent/cli/chat_parts/tui_input_delivery.py` | outbox v3 显式 sequence，旧 v1/v2 按原文件顺序迁移；未知首条 POST 阻止后来 POST 越过，已知请求仍可独立查状态 |
| `agent_py_agent/cli/chat_parts/tui_control_delivery.py`、`control_runtime.py`、`cli/chat_client_context.py` | 原 outbox/HTTP 原样携带 expected execution，不从提示文案、等待 job 或后台显示猜目标 |
| `agent_py_agent/agent/gateway_parts/control_operation_service.py` | 控制回执保存原执行快照，摘要 v4 覆盖该字段；旧 v1—v3 按原合同读取，旧版不能带未签入摘要的快照 |
| `agent_py_agent/agent/conversation/turn_resume_notice.py` | 原恢复事件只确认进入恢复流程；TUI/IM 共用提示不再提前宣称已续跑 |

### 锁与恢复约束

- 涉及输入提交时保持 Gateway 回合 T → guidance 回合 M → task → claim C。C 内不调用续租/结束 claim，不等待模型或工具。
- 文件事务锁不可重入；原锁内结算使用 `settle_turn_locked`。入口回执在释放目标锁之后对账。
- 用户输入稳定 ID、去重键不变；旧回执迟到不能改变当前 execution target。
- active task 不是执行权。缺失、过期、损坏、线程不匹配的 claim 不提供实际目标。
- reserved 与 submitted 分开：已经发布的精确失败 attempt 可释放尚未提交预留；提交批次未知不自动重放。
- queued 回执的接回依据稳定 Gateway ID。显示等待行已被撤下，不等于观察 job 已入队；回调失败后仍重试同一请求。
- 本轮没有新增配置开关、第三方依赖或模型提示词路由。上述都是原行为的身份与恢复修复。

## 已完成的验证

各组有重叠，不将它们相加冒充独立总数。

- 类型/生命周期第一组 28 passed：5 个原缺陷和 2 个混合邮箱缺陷先失败后修复；包括坏 task、缺失 task、不同运输编号、周期和终态收口。
- 会话控制和 runtime guidance 203 passed、4 个既有 xfail；TUI 输入/顺序/控制 132 passed；控制回执、run claim probe、恢复上限 43 passed。
- foreground/lifecycle/recovery/HTTP 后续组合 169 passed；原控制回执与 FIFO 补充组合 28 passed。
- 真实后台 core + 隔离 HTTP + 假供应商：连续两条 TUI 输入在下一模型调用按发送顺序进入，两条最终 consumed，Compact generation=0，等待 B 邮箱为空。
- 后台模块 204 passed；资源停止、会话剩余资源及空回复结构化诊断组合 24 passed。
- 新增两个边界先红后绿：后台异常后 reserved 永久卡住；TUI 接回显示失败后重复回执无法重试。与顺序用例合计 6 passed。
- 最新后台、执行目标、会话控制、预留恢复和 TUI 顺序组合 356 passed。
- 真实测试触发旧 rejected 批次恢复错误后，修复了“旧 attempt 阻挡新 attempt”的原提交账本投影；新增 pending/reserved/submitted/consumed 四种后继状态回归。
- UI 接回失败补充检查发现第二层缺陷：首次失败已经撤下等待行，第二次回调会错误跳过接回。真实 outbox + 回调的重放用例先失败后通过；相关 UI 组合 151 passed。
- Feishu scope 的用户/会话隔离补充后，执行目标组 20 passed；Compact 恢复与空回复诊断的定向组合也通过。
- Ruff 曾报新测试导入顺序，已修正。strict code-size 当前 hard=0；最终 gate 结果见下。
- 未运行全仓 pytest：改动规模未达到项目约定门槛；按受影响模块定向验证。
- 最终去重组合覆盖 30 个受影响测试文件：**863 passed、4 个既有 xfail、0 failed、0 error**，耗时约 112 秒；JUnit 共 867 项。前一版 29 文件为 854 passed。另在 1.8 的 Linux/Python 3.11 上运行四个新增回归入口 **41 passed**，恢复提示/UI 三文件 **63 passed**；不同组合不相加。
- 最终去重组合覆盖 29 个受影响测试文件：**854 passed、4 个既有 xfail、0 failed、0 error**，耗时约 138 秒；JUnit 共 858 项。另在 1.8 的 Linux/Python 3.11 上运行四个新增回归入口，**41 passed**。
- **本地严格 gate 全部通过**：focused pytest、Ruff、doc sync、strict code-size（hard=0）、diff、clean-package。clean-package 首次因九个新增文件尚未登记 Git 而拒绝，登记后复跑通过。线上 CI 未作为验收来源。

最终定向测试的可复现命令：

```bash
python3 -m pytest \
  agent_py_agent/tests/test_gateway_input_target_lifecycle.py \
  agent_py_agent/tests/test_gateway_execution_target.py \
  agent_py_agent/tests/test_gateway_conversation_control.py \
  agent_py_agent/tests/test_gateway_control_operation.py \
  agent_py_agent/tests/test_gateway_stop_background_resources.py \
  agent_py_agent/tests/test_gateway_stop_session_leftovers.py \
  agent_py_agent/tests/test_background_main_agent_runtime.py \
  agent_py_agent/tests/test_background_claim_execution.py \
  agent_py_agent/tests/test_background_claim_interrupt_scope.py \
  agent_py_agent/tests/test_background_claim_attempt_observer.py \
  agent_py_agent/tests/test_background_input_recovery.py \
  agent_py_agent/tests/test_background_steer_gateway.py \
  agent_py_agent/tests/test_runtime_guidance.py \
  agent_py_agent/tests/test_steer_delivery_recovery.py \
  agent_py_agent/tests/test_steer_closeout_replay.py \
  agent_py_agent/tests/test_turn_resume_limit_steer.py \
  agent_py_agent/tests/test_tui_input.py \
  agent_py_agent/tests/test_tui_input_ordering.py \
  agent_py_agent/tests/test_tui_control_delivery.py \
  agent_py_agent/tests/test_tui_threading.py \
  agent_py_agent/tests/test_tui_runtime.py \
  agent_py_agent/tests/test_tui_resource_lifetime.py \
  agent_py_agent/tests/test_tui_injected_input_states.py \
  agent_py_agent/tests/test_gateway_compact_recovery_continuation.py \
  agent_py_agent/tests/test_user_reply_unavailable_diagnostic.py \
  agent_py_agent/tests/test_compact_recovery_release.py \
  agent_py_agent/tests/test_background_compact_recovery.py \
  agent_py_agent/tests/test_task_resource_stop.py \
  agent_py_agent/tests/test_session_stop_background_resources.py \
  agent_py_agent/tests/test_shutdown_turn_resume.py \
  -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json --report /tmp/tui-steer-code-size.md
git diff --check
python3 scripts/check_clean_package.py .
```

### 新增回归入口

- `test_gateway_input_target_lifecycle.py`：T01、目标缺失/损坏、混合 mailbox、真实 terminalizer 两类收口。
- `test_gateway_execution_target.py`：T02—T04、重复普通插话、旧 claim、同 claim 换 attempt、真实后台领取/精确 Esc。
- `test_tui_input_ordering.py`：T05—T07 的 FIFO、未知 POST、重复/反序回执、接回失败重试。
- `test_background_input_recovery.py`：T13/T14 的模型提交边界及下一 attempt 认领。
- `test_background_steer_gateway.py`：T04/T07 的实际后台 core、真实 HTTP 和原提交确认链。

## 测试机真实验收记录

- 录制器在测试机上启动 `python -m agent_py_agent ... chat --gateway`，通过真实 PTY 输入框发文字、Enter、Esc、`/stop`，不是直接调用控制函数冒充 TUI。原 ANSI、动作账、manifest、结构化采样保存在测试目录的 `evidence/`。
- 供应商调用账明确记录 `anthropic_compatible / MiniMax-M2.7`。凭据不进入报告或归档。
- 第一版源码归档 SHA256：`c19b31ea0422c8b60ae79bea1e4a6138ca4fb0c66794efd7e24f4ceeec02a88e`；第二版为 `d99bfbf5d1c290ca0cf244a4b81930a214f3030754a28b908ac5a82e69d250d1`。R5—R7 候选为 `da16e10c8599fc5f4e13841892eb242b4a115919c33924e2f199864884e586ee`；修正恢复提示后的最终 R8/R9 候选为 **`b765f401ba7e079b312c2d7bb790ef1f76d947fa433c9b9fc747dd40be7f6fa4`**。
- 测试机系统时钟与开发机相差约五天。以下先后关系按测试机同一时钟和原账身份核对，不拿跨主机墙钟计算延迟。
- 独立证据归档 `evidence-review.tar.gz` 的 SHA256 为 `3f1a9b330a04a529de4fc9deb0792651ad46a5262a158501ab50c644912a6e86`，包含合成 TUI 录制、动作、manifest、67 条白名单原账投影、19 条模型使用记录、17 个 attempt 及 Linux JUnit；不含测试凭据或现有任务正文。
- 后续 R6—R9 证据为 `evidence-extended.tar.gz`，SHA256 **`05819d592e08900ee94d1961d064e69c475011bf9054f85ad588c08e2ef6f893`**；包含四个原生 TUI 的动作/录制/manifest、阶段对齐、前后原账投影和 Linux 回归报告，同样只含本轮合成任务。
- 收尾：所有测试 TUI 正常退出；候选 Gateway 在 pending=0、processing=0、running claim=0 后正常停止。测试目录、源码和证据保留，自动 Compact 临时阈值已恢复；既有 8420 的原 PID 165438 仍在。见 `extended-closeout.json`。
- 第一版源码归档 SHA256：`c19b31ea0422c8b60ae79bea1e4a6138ca4fb0c66794efd7e24f4ceeec02a88e`；第二版为 `d99bfbf5d1c290ca0cf244a4b81930a214f3030754a28b908ac5a82e69d250d1`。最终本轮候选归档为 `da16e10c8599fc5f4e13841892eb242b4a115919c33924e2f199864884e586ee`。
- 测试机系统时钟与开发机相差约五天。以下先后关系按测试机同一时钟和原账身份核对，不拿跨主机墙钟计算延迟。
- 独立证据归档 `evidence-review.tar.gz` 的 SHA256 为 `3f1a9b330a04a529de4fc9deb0792651ad46a5262a158501ab50c644912a6e86`，包含合成 TUI 录制、动作、manifest、67 条白名单原账投影、19 条模型使用记录、17 个 attempt 及 Linux JUnit；不含测试凭据或现有任务正文。

### R1：后台连续插话、Esc、再插话、stop

- 原生录制 `control-chain`；线程 `thread-3edb4c313a6a4d58`；任务 `goal-task-b8f60fd4aa034ec8`。
- 三条输入 `gwreq-msg-dd1db292aeb48298b5b0af556793ed03`、`gwreq-msg-11ef2228fad730f33b8830d1b5b21f49`、`gwreq-msg-1148006d10340de1aa5c8910a31d5bea` 最终全部 consumed，guidance 也全部 consumed。
- Esc 回执 `gwctl-msg-0527b6efb915ab477f6410b76d81d8e4` 命中旧 claim；之后从 `bgclaim-04257493853649c7` 进入 `bgclaim-33fad7da7a264a59`。第三条插话由新 attempt 接收。
- stop 回执 `gwctl-msg-76f632caf934ab1e10973754ec20f3b2` 命中当时的执行；最终 Goal paused、task interrupted、claim cancelled。
- compact_generation 全程为 0，证明输入不依赖压缩才进入。九次物理模型调用中两次因控制取消，不能把九次都写成正常完成。
- **控制链通过，样例交付未通过**：模型尝试写文件时收到只读文件系统错误，未产出完整样例。没有由测试者补文件；此项环境/工具问题另记，不用控制成功冒充业务交付成功。

### R2：固定时间停止的等待 B 组合——不计为通过

- 录制 `waiting-chain` + `same-thread-view`，B 为 `gwreq-1791166529-e17f8126d40f4d128a983c51bb9de7a9`。
- 三条输入在 stop 前尚未 consumed；一条已提交模型，在约 53 秒后仍未返回时被预设 stop 取消，另外两条等待后续安全点。停止后按原合同转为 queued。
- 因而此轮不能证明错投，也不能算插话通过。原始证据保留，后续 R3 改为观察真实消费后再发控制。
- 此轮出现“后台插话预留恢复未完成”，进一步定位到旧 rejected 提交批次校验新 attempt 的缺陷，已修复并有四种后继状态回归。

### R3：两个 TUI、等待 B、三条插话、Esc 和重连

- 第二版候选；录制 `adaptive-c`、`adaptive-d`、重连 `adaptive-e`；会话 `sess_1791167344_46f1055e`，线程 `thread-88d2c02f80e6473b`，任务 `goal-task-727945c7bd3b44ca`。
- B `gwreq-1791167406-02f10d11006b4b0b951475b5cd387c16` 处于 processing 且已 write-ahead 绑定，仍在等待后台持有的车道。
- 三条输入 `gwreq-msg-e3ee441d619b6d3630e7b34b04f7a34d`、`gwreq-msg-43c01200efb00e3017f6d11fa15b6a65`、`gwreq-msg-0bad51a75732402b2024aa20a260eebd` 全部 consumed，目标仍是后台任务，没有投到 B。
- 三条进入同一个后续调用 `model-call:9a5b29f3fd8da1bd2761061f:attempt-1:46f1f871`，provider 提交后约 6.4 秒确认；compact_generation=0。
- Esc `gwctl-msg-bec7d1ed908e06107a5ffc2bfa17949d` 命中 `bgclaim-9c65f9cf67524029 / attempt-1791167361-aeb4c3f9`；随后 B 完成，Goal 以 `bgclaim-883adcaf57154c2b / attempt-1791167475-1bbf6de9` 继续。
- 一个 TUI 退出后另开 TUI 连回原会话，后续请求正常执行。此证据覆盖客户端重连，不等同于 Gateway 带在途状态恢复。

### R4：真实长 shell 进程树与 `/stop`

- 录制 `resource-g`；前台请求 `gwreq-1791167790-02b83547292e49f1b7052481fa044289`；attempt `attempt-1791167790-2636dd58`。
- 停止前原 `tool_operations` 中 `tool_operation:4c86965c7a60cde26a5ccf013f71177d` 为 EXECUTING、工具为 run_command。此唯一执行中的合成长命令有两个 bwrap 进程、Python 父进程、sleep 子进程；采样保存了每个宿主 PID 和出生标记。
- 从真实 TUI 发 `/stop`，回执 `gwctl-msg-e8e1e41ae4fe4860e1cf827288b26585` 携带原 request、claim、运输 attempt 和 core attempt，收到“受理停止”后另行核查资源。
- 约 21 秒后的核查：四个进程实例均不存在，Gateway 原实例仍存活；attempt cancelled。证据为 `resource-g-before.json` 与 `resource-g-after.json`。
- 工具结果为 `UNKNOWN / TOOL_OPERATION_OUTCOME_UNKNOWN / effect_outcome_unknown:CANCELLED`，保留被中断操作的未知副作用，不伪造成功。这不是后台托管进程或跨重启资源恢复的替代验收。

### R5：候选 Gateway 空闲重启、原会话接回与手动 Compact

- 最终候选源码在队列为空后重新启动，原生 TUI `final-recovery` 连回 `sess_1791167344_46f1055e`。原三条 consumed 回执保留，未创建它们的副本。
- `/compact` 真实调用模型并提交 generation 1；随后的计算请求 `gwreq-1791168556-d3bc9dad7be54617b4dc0dd46b82dbd8` done，回答 58。
- 再发 `/compact`，在约 0.4 秒和 0.8 秒后从同一输入框连续提交两个计算请求。第二次 Compact 在约 7 秒后提交 generation 2；第一个请求在提交前已入队，两个请求依次 done，回答分别为 67、15。
- 两个实际请求是 `gwreq-1791168629-b0f429f955774aa899cc6dd481b45120` 和 `gwreq-1791168641-81f7ed57b70f4d20b6eebde65ff994e9`；最终 checkpoint 为 `compact-v3-2-6940aef4636d554c5508`。第二条先在客户端观察队列等待，随后沿正式入口创建请求。
- 该证据覆盖手动 Compact 期间普通输入及空闲 Gateway 重启；不等同于自动 Compact 内部安全点或带未知副作用的在途崩溃恢复。

### R6：等待子代理时连续输入、停止与不同线程隔离

- 两个原生 TUI `child-wait-h`、`parallel-i` 同时运行不同线程。前者主请求 `gwreq-1791169325-0af16426551e4f3f9200dad049823b2f` 派出两个真实 MiniMax 子代理后结束前台片，TUI 继续显示等待子代理；后者请求为 `gwreq-1791169326-08fb8c21da9745f39c7d075fd3fe356a`。
- 等待期间第一条补充走新的正式用户回合 `gwreq-1791169371-0c83535e4afa40b3b0c5568ca659180c`，core 仍绑定原任务与原 agent_run；紧随的第二条以 `gwreq-msg-918861a3c59233f2a6e5643b2c4cbf55` consumed。这个结果说明等待窗口可接续，不把无主代理 claim 的窗口伪报成后台工作片插话。
- 另一个线程的插话 `gwreq-msg-3a1b6ba2d1e438d0b257e8da7e29c682` consumed，目标为该线程自己的原请求。
- 在子代理仍 running 时从第一个 TUI 发 `/stop`，回执 `gwctl-msg-4653328feb1d8defd04c6100ec02c50a`；两个子代理 attempt 均 cancelled。另一个线程保持同一个 `attempt-1791169326-523cb4d2` running，没有被停止或换代。证据为 `child-stop-before.json`、`child-stop-after.json`。

### R7：工具执行中崩溃、未消费插话与未知结果恢复

- 在 R6 第二线程仍执行时，从真实 TUI 发补充；原入口回执 `gwreq-msg-646b858699b06bb8d5633bf0384fdf0a` 已 active_pending。此时 `tool_operation:3a07dba10d514f4c5c2e7cbe7c0d6db4` 为 EXECUTING。
- 仅对独立候选 Gateway 精确 PID 注入 SIGKILL，随后启动同一候选、同一私有 home。未杀或重启既有 8420 服务。
- 原 attempt 被恢复合同标为 unknown，工具操作 UNKNOWN，原请求以 `ACTIVE_TURN_OUTCOME_UNCERTAIN` 结束。未消费插话沿原 ID 转 queued，接回的请求因该会话待恢复而拒绝继续；没有改账或重做这次工具调用来制造通过。它验证的是未知结果分支，不是自动续跑成功。
- 真机暴露提示先说“已自动续跑”、随后又说恢复暂停的矛盾。原因是 `turn_resumed` 在 core 恢复核验之前发布；已将 TUI/IM 的共用提示改为“已进入恢复流程”，事件身份、恢复拒绝和工具未知状态不变。对应原 UI/宿主提示回归 63 项通过。
- 核查材料是 `inflight-before-kill.json`、`child-parallel-inflight-facts.json` 与两个 TUI 原录制；后一份包含 10 条白名单文件投影、5 个 attempt 和 37 个工具操作状态。

### R8：真实自动 Compact 前、中、后三段连续输入

- 录制 `auto-compact-j`；请求 `gwreq-1791169805-b604f2b9400f4bc2a01274a2b330ee36`，线程 `thread-46bc20474f294e5d`。只在私有测试配置将原 `memory_compact_auto_trigger_max_tokens` 设为 33000，测试后已恢复；没有改默认配置或模型窗口。
- 原生 TUI 启动 18 轮只读计算，实际完成 18 个工具轮，平方和 583220、立方和 52707600 与独立公式一致。期间自动提交三代 Compact，触发源为 preflight；包含活动工具归档和 transcript 两种原恢复来源，最终请求 done、无 error_code。
- 第一条补充 `gwreq-msg-4e6d9391b8939365f73c9b86dad6fa89` consumed；随后程序只观察原 typed Compact 事件，通过 TUI 输入框分别在下一代前、summarizing 中、completed 后发出三条补充。
- 三条分别为 `gwreq-msg-7c9a25673132c77463738a4af8652530`、`gwreq-msg-957fe1b502dbb8ac2a358cfe0e96dd4d`、`gwreq-msg-3f9647580a5c9ca453a8686c13e084e8`，最终均 consumed，目标均为同一原请求；没有产生备用下一轮请求。
- 第三代摘要开始于测试机 1791169943.544，期间输入创建于 1791169944.052，Compact 提交于 1791169954.712，提交后输入创建于 1791169955.239；两条后续均在约 1791169969.7 确认消费。最终 checkpoint 为 `compact-v3-3-fde3bf8cad84f98b4f60`。
- 证据 `auto-compact-injections.json` 将动作与真实阶段对齐，`auto-compact-facts.json` 保留原事件和回执状态。该任务没有复现历史 `USER_REPLY_UNAVAILABLE`，不据此关闭 T22。

### R9：模型流式生成中崩溃，原请求与插话自动恢复成功

- 原生 TUI `model-recovery-k` 创建请求 `gwreq-1791170129-43d36d2e6179441fb4455a1f1501844f`，要求直接生成 80 条短句。观察到真实供应商 `model_delta` 后发补充，入口回执 `gwreq-msg-0237e9ebac316a3b6c0ed4f8b2816df7` 为 active_pending；该 agent_run 的工具操作数为 0。
- 对当前独立候选 PID 注入 SIGKILL 后启动同一私有 home。旧 `attempt-1791170130-9a6de1ea` 为 recovered，新 `attempt-1791170150-292bff37` 最终 done；request、task、agent_run ID 保持原身份。
- 原补充回执 consumed，无新输入 ID；终态回复有 80 个编号条目和补充要求的最后一句，长度 3334 字符。新 TUI 实际显示“已进入恢复流程”，旧“已自动续跑”文案未出现。
- `model-recovery-before.json` 保存供应商增量类型/时间/长度、原执行绑定和回执，`model-recovery-after.json` 保存上述终态事实；正文只属于本轮合成测试。与 R7 一起覆盖可恢复与未知结果阻止恢复两条真实路径。

## T01—T26 验收覆盖与缺口

“回归”表示本地合同/真实 Store/fake provider/HTTP 重放；“真机”只指上面的原生 TUI 证据。没有覆盖的组合不打勾。

| 项目 | 当前证据与状态 |
| --- | --- |
| T01 同名 request 终态/task 活跃 | 两条结算路径及混合 mailbox 回归通过 |
| T02 无有效 claim | 缺失、过期、损坏、已结束及缺任务回归通过 |
| T03 owner/thread 隔离 | Feishu scope 两种错误快照均拒绝，原两任务和 mailbox 不变 |
| T04 等待 B 遮挡后台 | 回归 + R3 真机通过 |
| T05 回执反序 | UI 回归通过，真实网络故障注入未做 |
| T06 重复回执/接回失败 | 回归通过，含真实 outbox 回调中途失败后重试 |
| T07 多次插话 | 回归 + R1/R3 真机 consumed 证据 |
| T08 显式取消等待请求 | 原控制范围回归通过，单独真机按键场景未做 |
| T09 Esc 真正后台 | 回归 + R1/R3 旧 claim 取消、新 claim 继续 |
| T10 stop 后台任务 | 回归 + R1 Goal/task/claim 终态 |
| T11 发现后换 attempt | 精确旧 attempt 回归拒绝；资源不改 |
| T12 旧 expected target | 换 claim、换 attempt、换 scope 回归拒绝 |
| T13 reserved 退出 | 真实后台异常出口和旧 rejected 批次后继四状态回归；R2 提供失败来源 |
| T14 submitted 未知 | 预留恢复回归保留 submitted；不自动补发 |
| T15 consumed 后迟到事件 | 原 TUI 单调消费和重复终态回归 |
| T16 Compact 三阶段插话 | 原恢复/释放回归、R5 手动 Compact、R8 自动 Compact 前/中/后真机通过，四条回执全部 consumed |
| T17 等子代理时插话/中断 | 原控制/等待回归；R6 等待时连续输入、原任务接续和 stop 子代理通过；等待态 Esc 尚未单独验 |
| T18 长模型/长工具 | R2 长采样解释与 R4 长工具取消；未知结果保持 |
| T19 多 TUI/多线程 | R3 同线程及 R6 跨线程同时运行、输入隔离和停止隔离真机通过；跨 owner/thread 回归 |
| T20 重连/Gateway 恢复/半写 | R3 客户端重连、R5 空闲重启、R7 工具在途 unknown 分支、R9 模型在途原身份自动恢复；原半写与对账回归 |
| T16 Compact 三阶段插话 | 原恢复/释放回归及 R5 手动 Compact 前/中/后真机通过；自动 Compact 期间真实连续输入未完成 |
| T17 等子代理时插话/中断 | 原控制/等待回归；R6 等待时连续输入、原任务接续和 stop 子代理通过；等待态 Esc 尚未单独验 |
| T18 长模型/长工具 | R2 长采样解释与 R4 长工具取消；未知结果保持 |
| T19 多 TUI/多线程 | R3 同线程及 R6 跨线程同时运行、输入隔离和停止隔离真机通过；跨 owner/thread 回归 |
| T20 重连/Gateway 恢复/半写 | R3 客户端重连、R5 空闲重启、R7 工具在途崩溃的 unknown 分支；原半写与对账回归；可自动恢复的在途正例待补 |
| T21 完整组合 | R1 控制序列通过；样例文件交付不计通过 |
| T22 Compact 交接与空回复 | 见下述源码结论；历史前后台并存尚未归因，不宣称已修复 |
| T23 维护排空恢复 | 公共内核依赖未集成，尚未实现 |
| T24 任务长进程停止 | R4 前台 shell 进程树真机通过；后台托管/子树由定向回归覆盖 |
| T25 飞书 | 服务端共用入口 + Feishu scope 回归；未连接真实测试 IM 入口 |
| T26 程序化连续输入 | R1/R3 均经真实输入框发送；不把直接 HTTP 单测当此项真机 |

## T22 的当前源码结论

`gateway_parts/request_execution.py::_run_gateway_turn_with_conversation_compact` 在原用户回合内处理 Compact，最多八次恢复，耗尽后明确报持久化错误；它没有“只因后台 task active 就正常交接”的分支。`conversation/background_execution.py` 的公平让出发生在后台片连续提交八代 Compact 后，以 `BackgroundCompactSliceYield` 交给原调度器，`background_claim.py` 将它记录为 yielded。

这两个出口不可混为同一件事。现有公平让出、恢复成功/失败和真实空回复诊断回归通过，但尚未在候选真机复现原来的前台 `USER_REPLY_UNAVAILABLE` 与后台继续并存。当前没有改写空回复错误，没有以 task active 或 claim 存在伪造交接成功。后续应在新诊断版本的合成长任务中对齐最后模型调用、实际可交付长度、foreground 收口及后台接受事实再修复。

## 尚未完成及集成依赖

1. 核心组合、自动 Compact 三阶段、子代理等待、多线程隔离及在途崩溃的成功/未知结果分支已有真机证据；真实 IM 等明确测试群/会话与身份。小项边界仍逐条保留在矩阵中。
1. 核心组合、子代理等待、多线程隔离及在途崩溃的未知结果分支已有真机证据；继续补自动 Compact、可自动恢复的在途正例和真实 IM，矩阵仍有明确缺口。
2. 输入绑定 turn 与持久 task 不同的合法后继路径仍需进一步验证；保留 pending 本身不能证明下片已消费，不放宽 expected turn 校验来掩盖恢复缺口。
3. T22：既有 Compact 公平让出和真实空回复分别有回归，但原现场因果仍未证明；不把 task active 当作成功交接，不关闭 USER_REPLY_UNAVAILABLE 的真实错误。
4. T23：原单进程 restart drain 只统计 foreground admission/tool gate，不能代表后台、压缩、控制、子代理、资源和恢复事实全已排空。本轮尚未修改或宣称完成维护排空。
5. P6 交接要求抽取同一公共 drain 内核，单进程不能伪造一个 WorkerPartition，不能复制一套 Boolean 判定。本线等待公共内核/接口的集成结果后接入单进程；多进程 wiring 由原负责线完成。当前分支未包含该内核。
6. T24 已有真实前台 shell 进程树停止证据和资源定向回归；T25 共享服务端入口已接通，真实飞书入口未验收。
7. 本地代码与候选真机证据已具备交接条件，集成与生产部署仍未执行；没有推送、PR 或线上 CI。本轮不切换正在运行的生产安装。

## 集成时需要重点复查

- 影响范围是 Gateway/后台运行的控制身份、guidance 恢复和 TUI 回执；未变更模型选择、用户提示词或工具权限配置。
- 与 B1b 的交点是原 owner/thread/claim 路由、控制转达和公共 drain；不能仅把本分支测过等同于合入多进程后测过。整合时重新核对 T→M→task→C 锁序和精确 attempt，不回退按最新请求或显示 job 选目标。
- outbox v3 支持读取 v1/v2 的显式迁移，控制摘要 v4 校验原执行快照。旧 TUI 不承诺读取新 outbox；切换时使用一致的客户端版本，回滚先处理或保留未确认回执，不能删除它们来“恢复干净”。
- 请求编号与持久 task 不同的测试已覆盖投递与停止身份；跨不同 invocation 的合法接续仍须由原恢复合同证明，不能因为 task 相同就改绑输入。普通后台 `_run_params` 保留 task 作为稳定输入回合，定时/会话派活保留各自显式 run ID。
- R1 的只读文件系统失败保留为独立环境/工具问题，尚未声称修复；R2 的超时控制时间表不作为错投证据。T22 历史因果、T23 公共依赖和真实 IM 边界见上文。

建议下一步：先把下述已验收临时包交给用户在 Ubuntu 22.04 试用；主线合并、推送、生产切换和交给其它 agent 合并都等待用户明确指令。其它 agent 可以只读审阅本分支与发行记录，不同时修改本线源码或测试实例。后续获准集成时再核对 T22、P6 公共 drain 和真实 IM 的剩余边界。


## 临时离线发行追加记录（2026-10-10）

状态：最终发行 `0.3.0-tui-preview.20261010.2` 已完成打包与下述验收，供用户临时试用；独立工作树，不提交、不合并、不切换生产。

### 需求与发行边界

- 对应用户的 Ubuntu 22.04.5 / x86_64 / appuser，兼顾 Ubuntu 20.04 与 24.04 用户态。
- 自包含 CPython 3.11.15，安装不联网，不要求目标机额外安装 Python、Docker、Node.js 或编译器。
- 业务模块 1544 个转换成 sourceless `.pyc`，保持原 `python -m` 和子进程入口；字节码可逆向，不能描述成不可逆加密。
  配置、提示词、开放插件模板和第三方源码/许可保持可读。生产 wheel 原边界检查通过，无测试、开发模块或遗漏资源。
- 稳定祖先与分支起点见上文；包包含本工作树的未提交修复，以生产源快照 SHA256 标识，不把 HEAD 当作全部修复的提交。
- 最终生产源与构建 helper 快照 SHA256：`9bfcc80fcde58488ad30dcf88dd2b314663416ab29c7f815696b758c924629cf`。

### 代码位置与行为

| 位置 | 本轮行为 |
| --- | --- |
| `scripts/private_release/prepare_runtime.sh` | 隔离 Ubuntu 20.04 根中安装生产 wheel 及原 scale,secrets 依赖；不改宿主 Python |
| `scripts/private_release/prepare_bwrap.sh` | 原 bubblewrap 0.8.0 与原补丁重建，局部 libcap、CA、源码、许可和来源记录随包 |
| `scripts/private_release/build.py` | 编译业务模块、导出参数展示快照、重定位 console script、逐文件清单及自解压包 |
| `scripts/private_release/installer.py` | SHA256 校验、安装目录 owner 校验、活动进程拒绝、版本冲突拒绝、原子 current 切换 |
| `scripts/private_release/configure.py` | 本地交互输入百炼等 OpenAI 兼容 Base URL、model_name 和隐藏 Key，配置权限 0600 |
| `agent_py_agent/agent/attempt/sandbox.py` | Landlock 入口沿模块 origin 找 `.pyc`，保持独立解释器执行 |
| `agent_py_agent/agent/core.py` | 内置包入口识别 `.pyc`，保留原工作区解析 |
| `agent_py_agent/agent/settings/parameter_metadata.py` | 参数读取方的展示信息加载同源结构化快照；不作为权限或路由来源 |
| `agent_py_agent/tests/test_private_release.py` | 安装/更新、默认 owner 目录、坏包、忙进程、元数据、UTF-8、入口重定位 |

固定入口为 `/app/my-agent-preview/bin/my-agent-preview`，固定数据根为 `/app/my-agent-preview/data`，
相当于这份安装的 `.my-agent`。默认用户目录由原 owner resolver 返回 `data/owners/local/main`。
不更改 Linux 登录 HOME，安装/更新不创建顶层外部 workspace、不默认打开 Full Access。
后台使用原 `gateway start`；首次配置后先启动 Gateway，再进入 TUI。临时默认端口 18420。

### 已有证据与修正

- 本地发布相关六文件：81 passed、3 个 Linux 专属 skipped；TUI 控制相关另一组合 49 passed，存在重叠，不能相加为去重总数。
- 三版 Ubuntu 镜像在 `--network none`、无系统 Python 条件下，以 UID 600 普通账户完成首装、含空格路径、异版本更新、坏包拒绝及数据原字节保留。
  容器中把测试账号登录 home 从 `/home/appuser` 改到 `/app/alternate-login-home` 后再次更新，固定 data 保持不变。
- 初版首装曾显式写顶层 workspace，真实 TUI 被 WorkspaceOnly 拒绝；修正为保留空 workspace_root 并复用 owner resolver，补原校验器回归后复验通过。
- Mac 的 amd64 容器由 Rosetta 执行，`/proc/PID/exe` 指向模拟器，不能在该环境证明原生进程归属门；忙进程测试改在原生 x86_64 的 1.8 进行，没有向产品增加模拟器专项分支。
- 1.8 使用独立安装和 18438 端口：原生 PTY → 打包 TUI → 打包 Gateway → 官方 MiniMax-M2.7 → 真实命令工具完整通过。
  请求 `gwreq-1791185335-5625997d677d415bb8d4cef84f2cfab0` 为 done/ok，error_code 空；三次模型调用 finished、一次工具调用/工具轮，25.559 秒。
  实际工具输出 `中文打包测试` 与 `4`，TUI 正确显示中文并正常回复。
- 同一实例活动时用完整 `.run` 更新被拒绝，返回其 Gateway/TUI PID；未终止活动进程、未切换 current。
- 显式指定随包 bwrap 的完整探测返回 SANDBOX_READY：binary、namespace、owner_write、outside_hidden、host_etc_hidden 全部通过。
- 模型测试后原发行文件逐项复核通过，无业务源文件、无新增 __pycache__。最终安装提示补上原 gateway start 命令时，已断言仅 README 与 installer.pyc 改变，业务运行文件完全一致。

完整证据保存在本任务的 `my-agent-private-release-20261010` 本地 artifact 目录和 1.8 的独立 `private-release/evidence`，不进入分发载荷。
Ubuntu 三版为容器用户态兼容验证，原生 TUI/隔离为 1.8 的 openEuler x86_64；未将其写成三版 Ubuntu 实体机内核策略全覆盖。
百炼账号与 Key 由用户在目标机本地配置，本轮没有用用户 Key 发模型请求。

### 最终更新与交付核对

- 文件：`my-agent-0.3.0-tui-preview.20261010.2-linux-x86_64.run`，87,035,355 字节，约 83 MiB。
- SHA256：`b0f217644783c89c80070a55fa9db07245a7a03ae7c709a914a73ca1a9f542c5`。
- 原生 `.1 → .2` 完整安装器更新成功；停机后的 225 个 data 文件逐项哈希完全不变，包含真实配置与刚产生的历史。
- `.2` Gateway 与 TUI 恢复 `sess_1791185258_bb23301a`，新请求 `gwreq-1791186210-08c355f0a951435486e46a67c7fd27e3` done/ok，error_code 空，原样返回上一轮的两行输出。
- 最终相同字节 `.2` 包重新完成 Ubuntu 20.04 / 22.04 / 24.04 断网安装、普通账户、异版本更新、登录 home 改变、坏包拒绝与发行文件校验，三份记录均 `OFFLINE_ACCEPTANCE_OK`。
- 最终 installer 文案改动后的直接回归 8 passed；Ruff、doc-sync、strict code-size、diff、clean-package 通过。此前 81 passed/3 skipped 为发布适配组合，49 passed 为控制组合，各自记录、不重复累加。线上 CI 未作为验收来源。
- 分发目录仅含最终 `.run`、SHA256SUMS 和中文安装说明，不含构建源码、测试凭据或原生录制。

建议下一步：交用户在 Ubuntu 22.04 试用，合并与生产发布继续等待用户指令；其它 agent 可只读审阅，不接管此测试实例。
建议下一步：保留本核心切片，继续补自动 Compact/在途恢复及 T22 合成长任务归因。P6 公共内核与其多进程 adapter 适合由原负责线并行推进；本线随后接单进程 adapter，不能复制第二份 drained 判定。真实 IM 验收使用明确的测试群和身份。所有步骤保持独立测试 home/端口，生产切换前重跑集成后的严格 gate。
