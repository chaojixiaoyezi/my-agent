# Subagent Real E2E Findings

This document is append-only. Record every real subagent E2E issue found during live model/API runs, including the test scene, observed behavior, root cause, fix, verification, and remaining gap.

## 中文阅读说明

这份文档保留英文标题和字段，是为了方便代码、测试、CI、后续 LLM 和外部评审检索；下面补一份中文速览，按每条 E2E 记录解释“这次测了什么、发现了什么、修到什么程度、还差什么”。

字段对照：

- `Test scene`：测试场景，也就是这次真实 E2E 怎么跑、在哪里跑、用了什么模型。
- `Workspace`：用户任务目录，本轮都在 `/Users/example/my-终端应用` 附近做真实测试。
- `Runtime output` / `Internal runtime root`：内部运行目录，放 prompt、response、runner 报告、memory、LocalStore 等用户一般不看的东西。
- `Clean deliverables root` / `Deliverables root`：干净产物目录，只放用户要看的代码、测试、报告等成果。
- `Subagent workspace`：子代理工单和运行状态目录，里面会比较多文件，是系统内部使用的。
- `Model backend`：模型接口类型，比如 `anthropic_compatible` 表示走兼容 Anthropic 格式的接口。
- `Model name`：真实调用的模型名，本轮主要是 `MiniMax-M2.7`。
- `Real execution mode`：真实执行方式，比如 `subagent-run --execute --no-probe` 表示真正调模型跑子代理，不是 dry-run。
- `Observed behavior` / `Symptom`：实际看到的现象。
- `Root cause`：原因。
- `Fix`：本轮怎么改。
- `Verification`：用什么测试或真实命令验证。
- `Status`：当前是否解决。
- `Remaining risk` / `Remaining gap`：还没完全解决、后续还要补的风险。

## 中文速览（每条 E2E 记录）

### 3 个子代理并行真实 E2E

- Finding 1：父级测试一开始跑错目录。子代理已经写好了文件，手动测试也能过，但父级验收从错误目录执行测试，导致误报失败；后来让测试命令自动推断 artifact 所在目录，并记录真实 cwd，字符串和统计任务复测通过。
- Finding 2：图算法子代理写了文件，但最后没有干净收尾。系统正确把它标成 `BLOCKED` 并生成接管线索；这条主要证明坏任务能被发现，但当时还没自动修复。
- Finding 3：测试通过不等于正式验收通过。字符串和统计测试过了，但 evidence/patch review 不完整，所以正式 acceptance 拒绝；后来增加“父级真实测试报告可作为机器证据”的兜底，但坏 evidence 仍不会被掩盖。
- Finding 4：外层测试 wrapper 不能当唯一真相。外层 shell session 可能失真，所以后续 E2E 以文件、board、due-check、acceptance 报告和真实命令输出为准。

### 3 个子代理并行真实 E2E v2

- Result：正向链路跑通。3 个真实 runner 都完成，父级测试都过，父级 next-action 建议验收，显式 apply 后全部 `DONE / VERIFIED`；这证明“runner -> 父级测试 -> 下一步建议 -> 显式验收”主链路可用。

### 5 个子代理故障真实 E2E

- Result：父级能发现并接管被杀掉的 runner。geometry 子代理被手动 `SIGTERM`，系统通过 heartbeat/run timeout 识别异常，并用 action plan 接管。
- Result：父级能抓到已经完成 runner 的真实测试失败。interval 任务跑完了，但父级测试失败，所以没有被验收；这证明“完成状态”不会绕过父级质量关。
- Finding 5：已有失败测试报告时，follow-up 一开始不会生成正确救援建议。后来修成可以从现有 `test_execution.json` 合成 follow-up，并通过 `--apply-followup --take-over-by ...` 进入接管。
- Result：正常任务仍然可以验收。array/search/tree 都通过并 `DONE / VERIFIED`，故障任务进入 `TAKEN_OVER`，due-check 清零。

### 10 个子代理并行真实 E2E

- Result：10 并发部分通过，同时暴露供应商限流。7 个 runner 到等待验收，3 个 blocked；清洁任务可以验收，非清洁任务进入接管。它不是全绿测试，而是压力/恢复测试。
- Finding 6：MiniMax 在 10 个并发 runner 下出现 429 限流。系统不是本地代码坏了，而是模型服务端限流；当前能路由到接管，后续还要加供应商感知的限速、退避和重试。
- Finding 7：模型少了结构化输出结束标记会被干净拦截。matrix 任务因为缺 `[/SUBAGENT_RESULT]` 被标 blocked；当前能救援接管，后续可以加一次安全重试/修复。
- Finding 8：子代理自己写的测试也可能是错的。colors 任务实现看起来对，但子代理写了错误期望；父级测试发现失败并转救援。后续需要父级拥有的 oracle/test pack，不能只信孩子自己写的测试。
- Finding 9：父级验收曾跳过 patch review。roman 任务测试过了，但 patch 没 review，按严格规则应该先 review patch；后来 next-action 会先建议 `subagents-patches --review-apply`。
- Finding 10：blocked runner 没有测试报告时，follow-up 曾提示去跑测试，方向不对。现在 blocked/failed runner 可以直接生成 rescue preview，并通过 take-over 进入接管。

### 多层子代理控制面 E2E（不调真实模型）

- Result：1 主 / 2 子 / 4 孙的控制面通过。重点测任务树、LocalStore、board、due-check、接管视图和 TestExecutor 安全边界，确认多层结构能存、能查、能显示、能发现风险。
- Finding 11：旧的父任务快照保存时可能把 child 链接覆盖掉。后来保存任务时合并磁盘已有 `child_ids`，避免父子关系被旧对象写没。
- Finding 12：接管候选查询一开始漏掉 TIMEOUT 的孙节点。后来 `takeover_candidates` 覆盖 `BLOCKED / FAILED / ERROR / TIMEOUT`，不再只看 blocked。
- Prior Pain Point Matrix：把我们之前的痛点逐条对照了一遍：3/5/10 并发、runner 被杀/阻塞/超时、测试过但验收卡住、孩子测试写错、高危命令安全、reporter/checker 角色、多层主子孙结构。这里是“哪些痛点已经测过、哪些还欠”的总表。

### 真实多层 MiniMax E2E

- Result：真实多层树暴露 runner/验收缺口。真实模型创建 1 root、2 child、4 grandchild；有的写出文件，有的测试发现真实 bug，有的被故障注入终止，due-check/action-plan 能报告接管动作。
- Finding 13：配置里的子代理工具白名单没有传到 `spawn_subagents`。导致 child 没有写文件工具；后来把 `subagent_allowed_tools` 加到配置归一化和 spawn 创建路径里。
- Finding 14：父级测试和验收需要能恢复嵌套 artifact 路径。真实模型常写相对路径，父级一开始找不到；后来按安全目录内的唯一后缀路径恢复，不读正文，只找文件位置。
- Finding 15：任务状态变了以后，旧 follow-up 可能误导。比如旧报告说可以 apply，但任务已经 blocked；后来 preview 和 apply 使用同一套状态一致性检查，状态不合适时转救援。
- Remaining Gaps：图算法任务还没修；共享 workspace 的全局 due-check 仍可能很吵；孩子自己写的测试不够可靠；还需要父级 test packs。
- Finding 16：recovery-tree 一开始漏掉还处于 `RUNNING` 但其实已经心跳超时的孙节点。后来 recovery-tree 读取超时配置，把 stale running descendants 纳入恢复交接包，并给出接管命令。

### 1/4/16/48 干净产物树真实 E2E

- Result：用户产物目录和内部运行目录成功分开。48 个叶子 runner 都跑完，用户看的 `deliverables/` 没有内部 runtime 泄漏，内部复杂文件都留在 `.my_agent_runtime/`。
- Result：父级 oracle 和产品测试命令现在会真实失败，不会假绿。48 个 leaf 的直接父级 oracle 发现 27 过、21 失败；产品命令修复后也能诚实输出 25 过、23 失败/阻塞。
- Finding 17：`subagents-tests --re-run` 曾把空测试报告当成功。后来要求真正执行过测试，否则返回非零，不再假绿。
- Finding 18：`.my_agent_runtime/.../subagents` 新布局下，测试工作目录推断错了。后来优先用 `manager.workspace_root`，再从 artifact 推断 deliverable 目录。
- Finding 19：模型常写 `cd <dir> && pytest ...`，安全层会挡 shell 链。后来只允许“开头一个安全 cd + 后面普通命令”被拆成 `working_dir` 和 plain command，仍保留高危 shell 拦截。
- Finding 20：有些 runner 写了 test 文件，但 `output.json` 没写 `tests` 字段。后来当 `tests` 为空时，会从安全 artifact 里的 `test_*.py` 推断 pytest 命令。
- Remaining Gaps：仍有一个 leaf 既没结构化 tests 也没 artifact，父级只能救援/人工检查；孩子测试仍不能作为唯一真相；UI/CLI 后续要更清楚展示 `deliverables/` 和 `.my_agent_runtime/` 两个根目录。

### 主节点单入口层级烟测 E2E

- Finding 21：runner 内部没有模型工具来创建自己的下一层。后来新增 `schedule_child_subagents`，只能在当前 runner 上下文使用，外部不能绕过主节点直接创建下层。
- Finding 22：child run 没继承用户批准的产物目录。后来下层默认继承父节点的 `allowed_write_roots`，但不会继承父工单目录，避免污染父节点内部文件。
- Finding 23：嵌套 dispatch 曾意外生成 workflow worker。runner 里没显式指定 workflow 时，现在默认 `off`，只推进已有孩子，不自动扩任务树。
- Finding 24：嵌套 dispatch 曾选中正在运行的父节点自己，导致递归跑自己。现在 runner 内 dispatch 默认只选当前节点的直接 child，并排除当前 runner id。
- Finding 25：coordinator 如果拿到“业务产物写入”职责，可能自己代替 leaf 写产物。现在口径改成 coordinator 可以写自己的计划/证据/协调报告，但最终业务产物仍必须交给 leaf/worker/writer。
- Finding 26：模型把 `max_depth=1` 理解成“再创建一层”，系统原来按绝对深度拦住了。现在在 runner context 下会把这种值归一化成“允许多一层”。
- Successful Smoke：小烟测已通过。外层只启动 root，root 创建 child，child 创建 leaf，只有 leaf 写 `proof.txt=hierarchy-ok`；这条证明主节点单入口原则在小链路上能跑通。

### 主节点单入口 1/4/16/48 压测尝试

- Finding 27：父节点重复 dispatch 曾被 one-shot guard 阻断。现在只有创建类工具 one-shot，dispatch/board 这种进度循环工具可以重复调用。
- Finding 28：下层 runner 缺父级目标上下文。现在 child/grandchild 会继承 bounded parent goal/thought，让下一层知道原始目标、产物路径和验收要求。
- Finding 29：模型把 coordinator 误写成 worker。现在如果一个节点有调度工具且没有明确业务产物写入意图，会按 depth 推断成 child/grandchild coordinator；报告写入工具不会阻止这个推断。
- Finding 30：限速下还没跑的 PLANNING child 被误判成失败。现在 dispatch 返回 direct child 进度摘要和 continue hint，告诉父节点还有 PLANNING/RUNNING 的孩子要继续 dispatch。
- Remaining Gap：失败分支自动恢复还没完全闭环。当前已经证明 root->child->grandchild->leaf 能真实写部分文件，root 在一个 child blocked 后能继续推进别的 child；还缺自动救援超时 coordinator、接管残留 PLANNING leaf，以及完整 48 文件 main-node-only 全绿复跑。
- Small Fixed Retest：主节点单入口 1/2/4 小树复测通过。外层只启动 root，root 创建 2 child，child 创建 4 leaf，4 个 leaf 真实写出算法文件并通过父级 Python smoke test；leaf 写工具缺失和旧 failure/capability 状态残留已修复。还剩一个验收噪音：某 leaf 的 acceptance plan 因“空测试命令”进入 `request_human`，需要后续让空测试命令降级成 inspect-only 或生成可执行检查。
- Debug Trace：子代理调试日志改成正式 `subagent_debug_trace_level` 开关。默认 0 不写；1-5 只写内部 runtime 的 refs-only JSONL，用于后续多层真实测试定位谁创建、谁运行、谁收束，不污染 deliverables。
- Stage7 R34：购物站 10 个文件真实落到用户产物目录，但 root/部分 child 的 `output.json` 缺 evidence packet，导致严格验收失败；同时 dispatch 工具缺少“只剩重复记账，不要再调度”的明确提示。现在 output.json 自动收口会从已存在报告/产物路径补最小证据包，dispatch_subagents 会返回 `dispatch_terminal` 让父模型停下来汇报 blockers。

## 2026-05-09 Real 3-Subagent Parallel E2E

- Test scene:
  - Workspace: `/Users/example/my-终端应用`
  - Runtime output: `/Users/example/my-终端应用/real3_parallel_e2e`
  - Subagent workspace: `/Users/example/my-终端应用/.my_agent_subagents`
  - Model backend: `anthropic_compatible`
  - Model name: `MiniMax-M2.7`
  - Real tasks: string utilities, statistics utilities, graph utilities
  - Real execution mode: `subagent-run --execute --no-probe`

### Finding 1: Parent Test Execution Used The Wrong Directory

- Discovered at: 2026-05-09 during the 3-subagent real E2E run.
- Symptom:
  - The string and stats subagents created files successfully.
  - Manual validation in each artifact directory passed:
    - strings: `14 tests OK`
    - stats: `16 tests OK`
  - Parent validation through `subagents-tests <run_id> --re-run` initially failed for strings with `NO TESTS RAN` and exit code `5`.
- Root cause:
  - Runner output declared commands such as `python3 -m unittest discover -s . -p 'test_*.py'`.
  - Parent `subagents-tests` executed the command from the workspace root instead of the artifact directory.
  - The command was safe, but the working directory was wrong.
- Fix:
  - Added `agent_py_agent/agent/subagents/execution_test_items.py`.
  - Added `TestItemPreparationRequest` and `prepare_test_items()`.
  - The preprocessor infers a workspace-local `working_dir` from runner `artifacts` when a command test has no explicit cwd.
  - Updated `TestExecutor` to accept `working_dir` / `cwd`, enforce workspace boundaries, and record the actual cwd in `validation_result` and `metadata`.
  - Wired the preprocessor into both explicit acceptance execution and `subagents-tests --re-run`.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_subagent_test_item_preparation.py agent_py_agent/tests/test_subagent_test_executor.py agent_py_agent/tests/test_subagents_tests_command.py` -> `22 passed`
  - Real rerun:
    - `subagents-tests subagent-1778300301-b11e5640 --re-run` -> `passed=1 failed=0`
    - `subagents-tests subagent-1778300301-61949945 --re-run` -> `passed=1 failed=0`
- Status: solved for the observed strings/stats case.
- Remaining risk:
  - Multi-directory tasks need explicit test cwd from the runner or more precise artifact-to-test mapping.
  - The inference stays conservative: if artifacts point to multiple unrelated directories, parent tests should not guess.

### Finding 2: Graph Runner Wrote Files But Did Not Reach A Clean Final State

- Discovered at: 2026-05-09 during the 3-subagent real E2E run.
- Symptom:
  - Graph subagent wrote `graph_tools.py`, `test_graph_tools.py`, and `README.md`.
  - Its runner CLI log was empty, and `runner_result.json` was missing.
  - Board state became `BLOCKED / UNVERIFIED`.
  - Due check reported a P1 blocked issue.
  - Independent validation failed:
    - `test_single_component` expected one connected component but got two.
- Root cause:
  - The produced graph implementation or test package ended in a failing state.
  - The subagent reached its tool-round/finalization boundary before producing a clean final result.
  - The failure handoff claims a possible `__pycache__` issue, but parent validation still observed a real test failure.
- System behavior observed:
  - `subagents` board surfaced the task as `BLOCKED`.
  - `subagents-due-check` surfaced a P1 issue.
  - `failure_handoff.json` and `takeover_readiness.json` were written.
  - `subagents-plan-actions` recommended `classify_blocker`.
- Status: detected, not repaired yet.
- Remaining gap:
  - For a blocked task with failure handoff and takeover packet, the current action plan classifies the blocker but does not directly offer `takeover_or_reassign`.
  - Need decide whether blocked real-run failures should get a controlled rescue path earlier, or whether classification must happen before takeover.

### Finding 3: Tests Passed But Formal Acceptance Still Rejected The Task

- Discovered at: 2026-05-09 after fixing parent test cwd and rerunning real parent tests.
- Symptom:
  - strings and stats both passed real parent tests.
  - `subagents-acceptance-plan --next-action` correctly recommended `apply_acceptance`.
  - `subagents-acceptance-plan --apply` still returned `acceptance_decision=REJECT`.
  - After the rejected apply, both tasks appeared as `BLOCKED / FAILED` in the board, even though their real parent tests passed.
- Rejection reasons:
  - Missing evidence packets with evidence/artifact refs.
  - For strings, there were applied patch records that had not passed review.
  - Verifier reported evidence packets without refs.
- Root cause:
  - The formal acceptance gate is stricter than test success.
  - It requires evidence/ref hygiene and patch review status, not just passing unit tests.
- Fix:
  - Added `agent_py_agent/agent/subagents/services/acceptance_machine_evidence.py`.
  - When runner emitted no evidence packets, a passed parent `reports/test_execution.json` can now satisfy the traceable machine evidence chain.
  - If runner emitted malformed evidence packets, the test report does not hide the bad packets.
  - Verifier output now explains whether traceability came from worker evidence packets or parent machine tests.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_agent/test_subagent_acceptance.py` -> `8 passed`
- Status: partly solved.
- Remaining gap:
  - Real runner prompts should still prefer evidence packets with artifact/evidence refs.
  - Patch review and acceptance follow-up should be clearer in the board so users can see why "tests passed" is not yet "accepted".
  - Rejected formal acceptance should probably preserve a clearer state split such as "tests passed, evidence rejected" instead of making the task look like the implementation tests failed.

### Finding 4: Test Harness Wrapper Session Was Not A Reliable Source Of Truth

- Discovered at: 2026-05-09 during the first real 3-run wrapper launch.
- Symptom:
  - The wrapper exec session appeared stale from the tool side.
  - `ps` showed no matching live `python3` / `agent_py_agent` runner process.
  - The wrapper did not produce a trustworthy final JSON summary.
- Root cause:
  - The shell/tool wrapper state drifted from the real child process state.
  - Filesystem reports and product CLI state were more reliable than the wrapper session.
- Status: test-method note, not a product bug yet.
- Follow-up:
  - Real E2E reports should be assembled from product facts: task files, reports, board, due-check, acceptance reports, and validation command outputs.
  - Avoid relying on one outer wrapper process as the only truth source.

## 2026-05-09 Real 3-Subagent Parallel E2E v2

- Test scene:
  - Workspace: `/Users/example/my-终端应用`
  - Runtime output: `/Users/example/my-终端应用/real3_parallel_e2e_v2`
  - Subagent workspace: `/Users/example/my-终端应用/.my_agent_subagents_real3v2`
  - Config: `/Users/example/my-终端应用/.my-agent-e2e-config-real3v2.yaml`
  - Model backend: `anthropic_compatible`
  - Model name: `MiniMax-M2.7`
  - Real tasks: string utilities, statistics utilities, graph utilities
  - Real execution mode: three concurrent `subagent-run --execute --no-probe` CLI processes

### Result: Positive Path Passed End To End

- Observed behavior:
  - All three runner processes completed with `ok=OK`.
  - All three tasks moved to `AWAITING_ACCEPTANCE / NEEDS_ACCEPTANCE`.
  - Parent `subagents-tests --re-run` passed for all three:
    - strings: `total=3 executed=3 passed=3 failed=0`
    - stats: `total=1 executed=1 passed=1 failed=0`
    - graphs: `total=1 executed=1 passed=1 failed=0`
  - `subagents-acceptance-plan --next-action` recommended `apply_acceptance` for all three.
  - `subagents-acceptance-plan --apply` accepted all three.
  - Board summary after apply: `DONE=3`, `VERIFIED=3`, `hot=0`.
  - Due check after apply: `total_issues=0`.
- What this verified:
  - Direct parallel CLI runner execution is a more reliable test harness than the previous wrapper process.
  - Parent test cwd inference works in a real model/API run.
  - Passed parent `test_execution.json` can support formal acceptance when runner evidence packets are absent.
  - The positive path now completes: runner -> parent tests -> next-action -> explicit apply -> `DONE/VERIFIED`.
- Status: passed.
- Remaining risk:
  - Need repeat at 5 and 10 concurrent workers.
  - Need intentionally kill, stall, or timeout one worker in a 3 to 5 worker batch and verify the recovery path is understandable and actionable.
  - Need improve rescue path for `BLOCKED` tasks from v1: current planner classifies blockers but does not yet give a direct controlled takeover action in the first action plan.

## 2026-05-09 Real 5-Subagent Fault E2E

- Test scene:
  - Workspace: `/Users/example/my-终端应用`
  - Runtime output: `/Users/example/my-终端应用/real5_fault_e2e`
  - Subagent workspace: `/Users/example/my-终端应用/.my_agent_subagents_real5fault`
  - Config: `/Users/example/my-终端应用/.my-agent-e2e-config-real5fault.yaml`
  - Fast due-check config: `/Users/example/my-终端应用/.my-agent-capability-e2e-fast.yaml`
  - Model backend: `anthropic_compatible`
  - Model name: `MiniMax-M2.7`
  - Real tasks: array utilities, search utilities, tree utilities, interval utilities, geometry utilities
  - Fault injection: the geometry runner process was sent `SIGTERM` after 5 seconds.

### Result: Parent Detected And Took Over A Killed Runner

- Observed behavior:
  - Four normal runners completed with `AWAITING_ACCEPTANCE / NEEDS_ACCEPTANCE`.
  - The killed geometry runner stayed `RUNNING / UNVERIFIED`.
  - Fast due-check reported two P0 issues for the killed run:
    - `run_timeout`
    - `heartbeat_stale`
  - `subagents-plan-actions` recommended `takeover_or_reassign`.
  - `subagents-apply-actions --apply --action takeover_or_reassign --take-over-by real5-parent-recovery` moved the run to `TAKEN_OVER`.
- What this verified:
  - A parent can detect a killed child process through stale heartbeat/run timeout.
  - The action planner can recommend takeover for timeout/stale-heartbeat runs.
  - The action apply path records takeover without treating the killed task as done.
- Status: passed.

### Result: Parent Caught A Real Test Failure In A Finished Runner

- Observed behavior:
  - Interval runner completed normally.
  - Parent `subagents-tests --re-run` failed:
    - `test_interval_tools.py`
    - `TestIntervalIntersection.test_unsorted_input`
    - expected `[(2, 4), (6, 7)]`, actual `[(2, 3), (6, 7)]`
  - Parent next action reported `plan_rescue`.
- Root cause:
  - The generated test expectation appears mathematically questionable for interval intersection: `(1, 3)` intersect `(2, 4)` should normally be `(2, 3)`, not `(2, 4)`.
  - The system correctly did not accept the task because the parent test suite failed, even if the failing assertion may be a bad test.
- Status: detected and routed.

### Finding 5: Existing Failed Test Report Did Not Produce Follow-Up Before Fix

- Discovered at: 2026-05-09 during the 5-subagent fault E2E run.
- Symptom:
  - `reports/test_execution.json` already existed and showed a failed parent test.
  - `subagents-acceptance-plan --next-action` returned `plan_rescue`.
  - `subagents-acceptance-plan --followup` still returned `missing_followup` and recommended `--auto-execution --execute-auto-tests`.
  - `--auto-execution --execute-auto-tests` was blocked because the current action was already rescue, not run_tests.
- Root cause:
  - Follow-up generation only happened in the auto-execution path.
  - Plain `subagents-tests --re-run` could write `test_execution.json` without creating `parent_acceptance_auto_followup.json`.
- Fix:
  - Updated `manager_parent_acceptance.py`.
  - `plan_parent_acceptance_followup()` and `apply_parent_acceptance_followup()` now synthesize a refs-only follow-up from the current `test_execution.json` when the follow-up file is missing.
  - Failed reports now become `needs_manual_rescue`.
  - `--apply-followup --take-over-by ...` can now use the existing action handler takeover gate.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_parent_acceptance_followup_control.py agent_py_agent/tests/test_parent_acceptance_controller.py` -> `20 passed`
  - Real rerun on the interval task:
    - `subagents-acceptance-plan subagent-1778301855-9c537869 --followup` -> `needs_manual_rescue`
    - `subagents-acceptance-plan subagent-1778301855-9c537869 --apply-followup --take-over-by real5-parent-interval-rescue` -> `takeover_recorded`
- Status: solved.

### Result: Normal Runs Still Accepted

- Observed behavior:
  - Arrays, search, and trees passed parent tests.
  - Explicit acceptance apply accepted all three.
  - Final board state:
    - `DONE=3`
    - `VERIFIED=3`
    - `TAKEN_OVER=2`
  - Fast due-check after takeover: `total_issues=0`.
- Remaining risk:
  - `TAKEN_OVER` tasks still need a separate repair/resume run to finish the actual deliverable.
  - Board `hot=2` still shows taken-over tasks as attention-worthy even when due-check has no open issue; this is useful, but the UI should explain the difference between "attention" and "overdue".

## 2026-05-09 Real 10-Subagent Parallel E2E

- Test scene:
  - Workspace: `/Users/example/my-终端应用`
  - Runtime output: `/Users/example/my-终端应用/real10_parallel_e2e`
  - Subagent workspace: `/Users/example/my-终端应用/.my_agent_subagents_real10`
  - Config: `/Users/example/my-终端应用/.my-agent-e2e-config-real10.yaml`
  - Model backend: `anthropic_compatible`
  - Model name: `MiniMax-M2.7`
  - Real tasks: primes, roman numerals, csv, dates, colors, stack, matrix, cache, url, text wrapping
  - Real execution mode: ten concurrent `subagent-run --execute --no-probe` CLI processes

### Result: Ten-Way Concurrency Partly Passed And Exposed Provider Limits

- Discovered at: 2026-05-09 13:02 CST during the 10-subagent real E2E run.
- Observed behavior:
  - Seven runners reached `AWAITING_ACCEPTANCE / NEEDS_ACCEPTANCE`.
  - Three runners reached `BLOCKED / UNVERIFIED`.
  - Parent tests passed for six of the seven finished runners:
    - primes, csvlite, dates, cache, url
    - roman also passed parent tests, but later hit the patch-review gate described below
  - Parent tests failed for colors because the generated test expectation was wrong.
  - Five clean passed tasks were accepted and moved to `DONE / VERIFIED`.
  - Five non-clean tasks were routed into `TAKEN_OVER`.
  - Final due-check after takeover reported `total_issues=0`.
- Status: passed as a stress/recovery test, not as a pure positive-path test.
- Remaining risk:
  - Ten fully simultaneous MiniMax calls can hit provider concurrency/rate limits.
  - Throughput needs a scheduler/backoff layer before enabling large automatic batches by default.

### Finding 6: MiniMax 429 Rate Limit Appeared Under 10 Concurrent Runners

- Discovered at: 2026-05-09 13:02 CST during the 10-subagent real E2E run.
- Symptom:
  - Two runners failed with HTTP 429 `rate_limit_error`.
  - Affected runs:
    - stack: `subagent-1778302224-d5d2c229`
    - textwrapx: `subagent-1778302225-d2d34d9f`
  - Failure handoff recorded the provider message:
    - current request volume is high
    - higher plan or pay-as-you-go API may be needed for higher concurrency or automation
- Root cause:
  - Provider-side concurrency/rate limit, not a local code failure.
- System behavior observed:
  - Both tasks moved to `BLOCKED`.
  - `failure_handoff.json` and takeover readiness refs were available.
  - After the follow-up fix below, `--followup` recommended `plan_rescue`.
  - `--apply-followup --take-over-by real10-parent-rescue` recorded takeover.
- Fix in this slice:
  - No retry/backoff scheduler was added yet.
  - The rescue/follow-up path was improved so blocked provider failures become actionable.
- Verification:
  - Real commands moved both 429 runs to `TAKEN_OVER`.
  - Final `subagents-due-check --all` returned `total_issues=0`.
- Status: routed, not fully solved.
- Next improvement:
  - Add provider-aware concurrency limits, exponential backoff, and retry classification for transient 429s.

### Finding 7: Structured Output Missing End Marker Blocks A Runner Cleanly

- Discovered at: 2026-05-09 13:02 CST during the 10-subagent real E2E run.
- Symptom:
  - Matrix run `subagent-1778302224-aceb1087` completed a model call but failed structured output parsing.
  - Failure type: `structured_output_parse_error`.
  - Failure reason: missing `[/SUBAGENT_RESULT]` end marker.
- Root cause:
  - The model response did not conform to the required structured output envelope.
- System behavior observed:
  - The task moved to `BLOCKED / UNVERIFIED`.
  - The board exposed failure handoff and takeover readiness refs.
  - After the follow-up fix below, `--followup` recommended rescue instead of asking for tests.
  - Controlled takeover succeeded.
- Fix in this slice:
  - No automatic structured-output repair was added yet.
  - Follow-up rescue for already-blocked tasks was improved.
- Verification:
  - `subagents-acceptance-plan subagent-1778302224-aceb1087 --followup` -> `needs_manual_rescue`
  - `--apply-followup --take-over-by real10-parent-rescue` -> `takeover_recorded`
- Status: routed, not fully solved.
- Next improvement:
  - Add one safe repair/retry attempt for missing end marker or partial structured output, capped and fully logged.

### Finding 8: Subagent-Written Tests Can Be Wrong Even When The Implementation Is Correct

- Discovered at: 2026-05-09 13:02 CST during the 10-subagent real E2E run.
- Symptom:
  - Colors run `subagent-1778302224-3774361c` generated `color_tools.py` and `test_color_tools.py`.
  - Parent test failed:
    - `hex_to_rgb('0F0')` returned `(0, 255, 0)`
    - generated test expected `(15, 255, 15)`
- Root cause:
  - The generated test expectation was wrong for 3-digit hex shorthand.
  - Standard `#RGB` shorthand repeats each digit, so `0F0` expands to `00FF00`.
- System behavior observed:
  - Parent tests caught the mismatch and did not accept the task.
  - Follow-up routed the task to manual rescue/takeover.
- Fix in this slice:
  - No color-task artifact was manually corrected as product code.
  - The finding was recorded as an acceptance-quality problem.
- Verification:
  - `subagents-tests subagent-1778302224-3774361c --re-run` produced `failed=1`.
  - `subagents-acceptance-plan subagent-1778302224-3774361c --followup` -> `needs_manual_rescue`.
  - Controlled takeover succeeded.
- Status: detected and routed.
- Next improvement:
  - Add parent-owned fixed validation cases or external oracle tests for common task families.
  - Do not treat child-authored tests as the only acceptance truth.

### Finding 9: Parent Acceptance Skipped A Required Patch Review Step

- Discovered at: 2026-05-09 13:02 CST during the 10-subagent real E2E run.
- Symptom:
  - Roman run `subagent-1778302224-ea7c9d95` passed parent tests.
  - `subagents-acceptance-plan --apply` rejected it because `output.json` contained one `status=applied` patch without `review_status=APPROVED`.
  - The rejected apply moved the task to `BLOCKED / FAILED`.
- Root cause:
  - The strict patch-review gate was correct, but the parent next-action planner skipped it.
  - After tests passed, the planner recommended `apply_acceptance` instead of first recommending patch review.
- Fix:
  - Updated `parent_acceptance_controller.py`.
  - If tests pass but `output.json` contains unreviewed `status=applied` patches, the decision is now `review_patches`.
  - Updated `parent_acceptance_next_action.py` to recommend:
    - `subagents-patches --review-apply --run-id <run_id>`
  - Updated follow-up command/status mapping so post-test follow-up can return `needs_patch_review`.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_parent_acceptance_controller.py agent_py_agent/tests/test_parent_acceptance_followup_control.py` -> `24 passed`
- Status: solved for future runs.
- Remaining risk:
  - Existing tasks already rejected by the old flow need rescue or manual repair because their state was already changed.

### Finding 10: Blocked Runners Without Test Reports Got Misleading Follow-Up Guidance

- Discovered at: 2026-05-09 13:02 CST during the 10-subagent real E2E run.
- Symptom:
  - For 429 and structured-output blocked runs, `subagents-acceptance-plan --followup` initially returned `missing_followup`.
  - It recommended `--auto-execution --execute-auto-tests`, but those runs had no reliable artifacts to test.
- Root cause:
  - Follow-up control assumed the next meaningful step after missing follow-up was always test execution.
  - That is true for normal finished runners, but false for blocked runner-level failures.
- Fix:
  - Updated `manager_parent_acceptance.py`.
  - When a task is already `BLOCKED` / failed and no test follow-up exists, follow-up now synthesizes a non-mutating rescue preview from current task state.
  - `--apply-followup --take-over-by ...` can now record takeover without requiring a failed test report.
  - Follow-up consistency now allows rescue when the current task state itself is failed/blocked, even if tests passed or no test report exists.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_parent_acceptance_controller.py agent_py_agent/tests/test_parent_acceptance_followup_control.py` -> `24 passed`
  - Real blocked runs:
    - matrix: `--followup` -> `needs_manual_rescue`, apply -> `takeover_recorded`
    - stack: `--followup` -> `needs_manual_rescue`, apply -> `takeover_recorded`
    - textwrapx: `--followup` -> `needs_manual_rescue`, apply -> `takeover_recorded`
    - roman old rejected state: apply follow-up rescue -> `takeover_recorded`
- Status: solved for the observed blocked/failure cases.
- Remaining risk:
  - The takeover state means the parent has claimed ownership; a separate repair/resume runner is still needed to finish the actual files.

## 2026-05-09 Multi-Level Subagent Control-Plane E2E

- Test scene:
  - Workspace: `/Users/example/my-终端应用`
  - Runtime output: `/Users/example/my-终端应用/.my_agent_e2e_multilevel_run_1778304762`
  - Config: `/Users/example/my-终端应用/.my-agent-e2e-config-multilevel-1778304762.yaml`
  - Subagent workspace: `/Users/example/my-终端应用/.my_agent_e2e_multilevel_run_1778304762/subagents`
  - Model backend: `echo`
  - Real mode: product persistence, LocalStore control plane, board, due-check, takeover view, and `TestExecutor` safety checks; no model/API calls in this hierarchy-control slice.
  - Hierarchy: 1 main coordinator, 2 child roles, 4 grandchild workers.
    - Child role 1: reporter / progress collector.
    - Child role 2: checker / quality finder.
    - Grandchildren: status reader, risk summarizer, acceptance checker, timeout permission checker.

### Result: 1 Main / 2 Child / 4 Grandchild Tree Passed

- Observed behavior:
  - Seven runs were created and persisted.
  - `LocalStore.list_agent_tree(root_id)` returned all seven runs.
  - Reporter subtree returned exactly three runs: reporter plus two grandchildren.
  - Checker subtree returned exactly three runs: checker plus two grandchildren.
  - `subagents --all --root-id <root>` displayed the tree, shared progress, takeover view, acceptance plan, and acceptance next action.
  - `subagents-due-check --all` reported:
    - one P0 `status_timeout`
    - one P1 `status_blocked`
  - `takeover_readiness.json` and failure handoff refs were visible for both blocked checker and timeout grandchild.
  - `TestExecutor` safety probes passed:
    - `python3 -c "print(123)"` executed inside the workspace artifact directory.
    - `rm -rf /` was rejected before execution.
    - `python3 ... ; rm -rf /` was rejected for high-risk shell chaining.
    - `working_dir: /` was rejected as workspace escape.
    - `file_path: ../outside.txt` was rejected as workspace escape.
- Status: passed after the two fixes below.

### Finding 11: Stale Parent Snapshots Could Drop Child Links

- Discovered at: 2026-05-09 during the multi-level hierarchy E2E.
- Symptom:
  - Creating a child correctly called `add_child(parent_id, child_id)`.
  - Later saving an older in-memory copy of the parent overwrote the newly written `child_ids`.
  - The same issue could happen one level lower: saving an older child object could remove grandchild links.
- Root cause:
  - `SubAgentPersistenceService.save()` persisted the full task object.
  - Append-only hierarchy edges in `child_ids` were not merged with the existing on-disk task before writing.
- Fix:
  - Updated `agent_py_agent/agent/subagents/services/persistence.py`.
  - Before writing, save now merges existing on-disk `child_ids` with the incoming task snapshot.
  - Added a regression test for stale parent and stale child saves.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_local_store_control_plane.py` -> `5 passed`
  - Re-ran the multi-level E2E and verified:
    - parent has two child ids
    - reporter has two child ids
    - checker has two child ids
- Status: solved for hierarchy links.
- Remaining risk:
  - This fix preserves `child_ids`; other append-only collections may still need separate merge policy if future concurrent writers update them from stale snapshots.

### Finding 12: Takeover Candidate Query Missed TIMEOUT Grandchildren

- Discovered at: 2026-05-09 during the multi-level hierarchy E2E.
- Symptom:
  - `due-check` correctly reported the timeout grandchild as P0.
  - `takeover_readiness.json` existed for that timeout run.
  - But `query_agent_runtime(scope="takeover_candidates")` initially returned only `BLOCKED` runs, not `TIMEOUT` / failed runs.
- Root cause:
  - `blocked_runs` and `takeover_candidates` shared the same blocked-only selector.
- Fix:
  - Updated `agent_py_agent/agent/local_storage/control_plane.py`.
  - `blocked_runs` remains blocked-only.
  - `takeover_candidates` now returns `BLOCKED`, `FAILED`, `ERROR`, and `TIMEOUT`.
  - The reserved hint changed to `blocked_failed_timeout_runs`.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_local_store_control_plane.py` -> `5 passed`
  - Re-ran the multi-level E2E and verified takeover candidates include both:
    - blocked checker
    - timeout grandchild
- Status: solved.

### Prior Pain Point Matrix

- 3 / 5 / 10 concurrent subagents:
  - Covered by earlier real MiniMax E2E runs.
  - Current status: positive path works at 3, killed runner/takeover works at 5, 10-way stress exposes provider 429 and routes failures to takeover.
- Runner killed / blocked / timeout:
  - Covered by real 5-run SIGTERM and this hierarchy timeout run.
  - Current status: due-check/action plan/takeover readiness all surface recovery refs.
- Tests pass but acceptance still blocked:
  - Covered by real 3-run and 10-run E2E.
  - Current status: machine evidence fallback and patch-review next action are implemented.
- Wrong child-authored tests:
  - Covered by real colors/interval cases.
  - Current status: parent detects and routes to rescue; external oracle / parent-owned test packs are still future work.
- Permission and high-risk command safety:
  - Covered in this hierarchy E2E through `TestExecutor` allowlist and workspace-boundary checks.
  - Current status: acceptance test executor blocks high-risk shell commands and workspace escapes.
  - Remaining gap: deeper runner/tool-layer command policy should eventually centralize high-risk filesystem operations such as deleting or moving system paths.
- Reporter subagent and checker subagent pattern:
  - Covered as normal subagent roles in the hierarchy E2E.
  - Current status: product can represent reporter/checker roles and show refs-only reports without loading large bodies.
  - Remaining gap: dedicated automatic reporter/checker orchestration is not a separate feature yet.
- Main / child / grandchild mode:
  - Covered at persistence/control-plane level.
  - Current status: `create_run(parent_id=..., root_id=..., depth=...)`, inheritance manifest, LocalStore tree/subtree queries, board, and takeover view work.
  - Remaining gap: a child runner autonomously spawning grandchildren and supervising them end-to-end is not fully implemented yet.

## 2026-05-09 Real Multi-Level MiniMax E2E

- Test scene:
  - Workspace: `/Users/example/my-终端应用`
  - Config: `/Users/example/my-终端应用/.my-agent-e2e-config-real-multilevel-1778308650.yaml`
  - Subagent workspace: `/Users/example/my-终端应用/.my_agent_subagents_real_multilevel_1778308650`
  - Runtime outputs:
    - `/Users/example/my-终端应用/real_multilevel_e2e_1778308650`
    - `/Users/example/my-终端应用/real_multilevel_e2e_1778309023`
  - Model backend: `anthropic_compatible`
  - Model name: `MiniMax-M2.7`
  - Hierarchy: 1 root coordinator, 2 child runners, 4 grandchild runners.
  - Real execution mode: child/grandchild `subagent-run --execute --no-probe` with real model calls and explicit parent tests.

### Result: Real Multi-Level Tree Exposed Runner/Acceptance Gaps

- Observed behavior:
  - The first real hierarchy tree created 1 root, 2 children, and 4 grandchildren, but child runners did not receive write tools from the test config.
  - The second real hierarchy tree propagated write tools and produced actual files:
    - sorting child wrote code/tests/README and parent tests passed.
    - graph child wrote code/tests/README, but parent tests caught a real topological-sort bug.
    - graph-test grandchild produced failing black-box tests that confirmed the graph bug.
    - two grandchildren were deliberately terminated after hanging; due-check and action plan reported stale heartbeat/takeover actions.
  - Reusing one subagent workspace for two real hierarchy trees made global `--all` due-check output noisy with old runs.
- Status: useful failure-driven E2E, not a clean all-green scenario.

### Finding 13: Configured Subagent Tool Allowlist Was Not Propagated By `spawn_subagents`

- Discovered at: 2026-05-09 during the first real MiniMax hierarchy run.
- Symptom:
  - Root `spawn_subagents` created child tasks, but the child tasks had `allowed_tools=[]`.
  - The sorting child blocked because it could not write files.
  - The graph child was also constrained by role defaults and could not write the requested artifact.
- Root cause:
  - `AgentConfig` had no normalized `subagent_allowed_tools` field.
  - `spawn_subagents()` passed the allowlist into complexity estimation but not into `SubAgentManager.split()`.
- Fix:
  - Added `subagent_allowed_tools` to `AgentConfig` and runtime config normalization.
  - Propagated the configured allowlist through `spawn_subagents()` -> `SubAgentBaseMixin.split()` -> `CreateRunParams`.
  - The field accepts YAML lists or comma-separated strings and filters empty values.
- Verification:
  - `python -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_subagent_mixin.py::TestSubagentMixinSpawn agent_py_agent/tests/test_config_normalize.py::TestNormalizeAgentConfig::test_normalize_subagent_allowed_tools_list agent_py_agent/tests/test_config_normalize.py::TestNormalizeAgentConfig::test_normalize_subagent_allowed_tools_scalar agent_py_agent/tests/test_subagent_hierarchy_scheduler.py` -> `11 passed`
  - Real second tree showed root/children receiving `read_file`, `write_file`, `append_file`, and `replace_in_file`.
- Status: solved for config-driven spawn paths.

### Finding 14: Parent Test And Acceptance Needed Nested Artifact Path Recovery

- Discovered at: 2026-05-09 during the second real MiniMax hierarchy run.
- Symptom:
  - Sorting-edge grandchild wrote `grandchild_sorting_edge/sorting_edge_report.md` and `test_sorting_edges.py`.
  - Parent direct unittest passed when run from the real output directory.
  - `subagents-tests --re-run` initially failed because the runner reported a relative artifact path missing the outer run directory.
  - After test execution was fixed, `--apply-followup` still rejected the same task because artifact existence checking had the same relative-path blind spot.
- Root cause:
  - Test preparation and acceptance artifact checks assumed the reported relative path was directly below the workspace or task directory.
  - Real model output often reports paths relative to the task output directory, not the outer E2E run directory.
- Fix:
  - Extended `execution_test_items.py` to recover a unique workspace-local suffix path for runner-reported artifacts.
  - Extended both current service-layer and legacy helper artifact existence checks to look for the same suffix inside safe candidate roots.
  - Kept the lookup metadata-only: it finds files by path suffix but does not read artifact contents.
- Verification:
  - `python -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_acceptance_helpers_class.py::TestCheckArtifactExists agent_py_agent/tests/test_manager_acceptance_findings.py::TestSubAgentAcceptanceOutputFindingMixin::test_nested_relative_artifact_paths_exist agent_py_agent/tests/test_subagent_test_item_preparation.py agent_py_agent/tests/test_subagents_tests_command.py` -> `26 passed`
  - Real rerun:
    - `subagents-tests subagent-1778309080-a1addc2d --re-run` -> passed.
    - `subagents-acceptance-plan subagent-1778309080-a1addc2d --followup` stopped reporting missing artifact paths.
- Status: solved for observed nested relative artifact paths.
- Remaining risk:
  - If multiple files share the same suffix under one workspace, test cwd inference stays conservative; future runner prompts should still prefer explicit `working_dir`.

### Finding 15: Stale Apply Follow-Up Could Mislead After Task State Changed

- Discovered at: 2026-05-09 after fixing nested artifact path checks.
- Symptom:
  - The sorting-edge grandchild had already been moved to `BLOCKED` by the earlier artifact false negative.
  - After the path fix, `--followup` initially said the task was `ready_for_manual_apply`.
  - `--apply-followup` then rejected it because the current task state was no longer awaiting acceptance.
- Root cause:
  - Follow-up preview trusted the stored passing test follow-up without checking the current task state.
  - Apply path eventually hit the stricter state guard, so preview and apply disagreed.
- Fix:
  - Follow-up preview now runs the same consistency check used by apply.
  - If an old apply follow-up exists but the task is now failed/blocked, preview returns `needs_manual_rescue` and recommends `--apply-followup --take-over-by <agent>`.
  - Apply with `--take-over-by` routes through the existing takeover action gate instead of trying to resurrect a stale apply decision.
- Verification:
  - `python -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_parent_acceptance_followup_control.py` -> passed inside the focused 35-test run.
  - Real rerun:
    - `subagents-acceptance-plan subagent-1778309080-a1addc2d --followup` -> `needs_manual_rescue`
    - `subagents-acceptance-plan subagent-1778309080-a1addc2d --apply-followup --take-over-by real-multilevel-parent ...` -> `takeover_recorded`
- Status: solved.

### Remaining Gaps From This Real Hierarchy Run

- Recovery-tree now surfaces stale `RUNNING` descendants when heartbeat/run-timeout thresholds are provided, matching due-check recovery visibility.
- Graph child quality still needs repair/resume; parent tests correctly prevented acceptance.
- E2E test workspaces should still be isolated per run when possible; `subagents-due-check --root-id <root>` now reduces cross-tree due-check noise when a shared workspace is unavoidable.
- Child-authored tests remain insufficient as the only oracle; parent-owned test packs are still needed for common algorithm/task families.

### Finding 16: Recovery Tree Missed Stale RUNNING Grandchildren

- Discovered at: 2026-05-09 after the real MiniMax hierarchy fault injection.
- Symptom:
  - Two grandchild runner processes were deliberately terminated.
  - `subagents-due-check --all` reported them as `heartbeat_stale` / `run_timeout`.
  - `subagents-recovery-tree <root> --hide-healthy` initially did not include them because their status was still `RUNNING`, not `BLOCKED` or `TIMEOUT`.
- Root cause:
  - Hierarchy recovery only considered terminal failure-like statuses, blockers, and failure types.
  - It did not reuse due-check timeout thresholds for active descendants.
- Fix:
  - Added `heartbeat_timeout`, `run_timeout`, and `now` fields to `HierarchyRecoveryRequest`.
  - `subagents-recovery-tree` now reads capability timeout config and marks truly active stale descendants as `due:heartbeat_stale,run_timeout`.
  - The stale rule is intentionally narrower than due-check: it applies to `RUNNING` or runs with an active attempt, so parked coordinator/root `PLANNING` tasks are not noisy recovery candidates.
  - Stale descendants get a refs-only recommended command through the existing takeover action gate:
    - `my-agent subagents-apply-actions --apply --action takeover_or_reassign --run-id <run_id> --take-over-by <agent>`
- Verification:
  - `python -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_subagent_hierarchy_recovery.py agent_py_agent/tests/test_subagent_hierarchy_cli_e2e.py agent_py_agent/tests/test_subcommands_agents_class.py::TestSubagentsSubcommandRegistration::test_add_subagents_subcommands_creates_expected_commands` -> `5 passed`
  - Real rerun:
    - `subagents-recovery-tree subagent-1778309037-dddc7214 --hide-healthy --json` included `subagent-1778309080-620c4772` and `subagent-1778309080-77f7ba19` with `due:heartbeat_stale,run_timeout`.
    - `subagents-apply-actions --apply --action takeover_or_reassign --run-id subagent-1778309080-620c4772 --take-over-by real-multilevel-parent` -> `RUNNING->TAKEN_OVER`
    - `subagents-apply-actions --apply --action takeover_or_reassign --run-id subagent-1778309080-77f7ba19 --take-over-by real-multilevel-parent` -> `RUNNING->TAKEN_OVER`
- Status: solved for stale active descendants in recovery-tree.
- Follow-up fix:
  - Added `subagents-due-check --root-id <root>` so real tests can scope due-check to one hierarchy tree.
  - Real rerun on `subagent-1778309037-dddc7214` reduced the shared-workspace due-check output from 17 cross-tree issues to 3 current-root issues.
- Follow-up fix 2:
  - Added `subagents-plan-actions --root-id <root>` so action planning uses the same scoped issue set.
  - Due-check heartbeat/run-timeout rules no longer treat parked `PLANNING` coordinator tasks that already have child runs as ordinary runners; child `RUNNING` tasks still receive stale timeout issues.
  - Stale coordinators now emit `coordinator_heartbeat_stale` with action `recover_coordinator_leadership`, so a parent can notice the orphan-risk tree and decide whether to appoint a new leader.
  - `recover_coordinator_leadership` now has a controlled apply path requiring `--take-over-by <leader_run_id>`; it marks the old coordinator as `TAKEN_OVER`, moves direct children under the new leader, updates `parent_id` / `depth` / `supervisor` / `final_owner`, and recursively refreshes descendant depths.
  - Real rerun on `subagent-1778309037-dddc7214` produced two scoped issues: coordinator leadership recovery for the root and the remaining graph-child open capability request.
- Remaining risk:
  - `subagents-due-check --all` is still intentionally global and can be noisy in shared E2E workspaces.
  - Standalone stale `PLANNING` runs without children can still become takeover candidates; that remains the backlog/dispatch-stall policy.

## 2026-05-09 Real 1/4/16/48 Clean Deliverables Tree E2E

- Test scene:
  - Workspace: `/Users/example/my-终端应用`
  - Clean deliverables root: `/Users/example/my-终端应用/deliverables/real_tree_1778318291`
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/real_tree_1778318291`
  - Subagent workspace: `/Users/example/my-终端应用/.my_agent_runtime/real_tree_1778318291/subagents`
  - Config: `/Users/example/my-终端应用/.my_agent_runtime/real_tree_1778318291/agent_config.yaml`
  - Model backend: `anthropic_compatible`
  - Model name: `MiniMax-M2.7`
  - Tree shape: 1 root, 4 children, 16 grandchildren, 48 great-grandchild leaf workers.
  - Real execution mode: 48 concurrent leaf `subagent-run --execute --no-probe` CLI processes with concurrency 6.

### Result: Clean User Output And Bulky Internal Runtime Were Separated

- Observed behavior:
  - All 48 leaf runner CLI processes returned exit code 0.
  - Runner duration range: min 57.92s, max 293.59s, avg 134.37s.
  - User-visible deliverables directory contained only deliverable files after cleanup:
    - final file count: 517
    - internal/runtime leak count: 0
  - Runtime-heavy files stayed under `.my_agent_runtime`, including subagent work orders, prompts, responses, reports, local store, memory, manifests, and E2E logs.
- Status: passed for the requested directory split.
- Remaining risk:
  - The product should keep treating `deliverables/` as user-facing output and `.my_agent_runtime/` as internal state. Future cleanups should avoid deleting deliverables unless the task explicitly asks for it.

### Result: Parent Oracle And Product Test Command Now Fail Honestly

- Independent parent oracle:
  - Direct pytest over all 48 deliverable folders produced 27 passed and 21 failed.
  - Failures included real algorithm bugs, questionable child-authored expectations, and one syntax error class.
- Product command before fixes:
  - `subagents-tests --re-run` initially reported all 48 as CLI success because it accepted empty `test_execution.json` reports with `total_tests=0`.
  - After the first fix, it stopped reporting fake success but exposed a workspace-root bug in the clean runtime layout.
- Product command after fixes:
  - Final full product rerun produced:
    - 25 `passed_cli`
    - 23 `failed_cli`
    - Shape breakdown: 25 executed/passed, 22 executed/failed, 1 zero-tests blocked.
  - Final report directory: `/Users/example/my-终端应用/.my_agent_runtime/real_tree_1778318291/product_subagents_tests_logs_after_artifact_infer`
- Status: solved for fake-green reports, nested runtime workspace, safe `cd && pytest` commands, and missing-tests-with-test-artifact fallback.

### Finding 17: `subagents-tests --re-run` Accepted Empty Acceptance Reports As Success

- Discovered at: 2026-05-09 during the 48-leaf clean deliverables tree E2E.
- Symptom:
  - `subagents-tests --re-run` returned exit code 0 even when the acceptance side had written `test_execution.json` with `total_tests=0`.
  - This masked real runner output tests and made all 48 leaves look green from the product CLI.
- Root cause:
  - CLI logic only checked whether `test_execution.json` existed after requesting acceptance test execution.
  - It did not verify that the report actually executed a declared test.
- Fix:
  - `cmd_subagents_tests()` now treats an existing zero-test acceptance report as insufficient when `output.json` declares tests.
  - It falls back to direct product test execution in that case.
  - The CLI now returns nonzero when `total_tests <= 0` or any test failed.
- Verification:
  - `python3 -m pytest -q agent_py_agent/tests/test_subagents_tests_command.py agent_py_agent/tests/test_agent/test_subagent_acceptance.py agent_py_agent/tests/test_agent/test_dispatch_and_planner.py::test_subagent_dispatch_manual_acceptance_test_execution_is_test_only` -> passed.
- Status: solved.

### Finding 18: Acceptance Test Workspace Inference Broke With `.my_agent_runtime/.../subagents`

- Discovered at: 2026-05-09 during product rerun after Finding 17.
- Symptom:
  - Tests for clean deliverables failed with errors such as `file or directory not found: test_solution.py`.
  - The command ran from `/Users/example/my-终端应用/.my_agent_runtime/real_tree_1778318291` instead of the task workspace root or deliverable directory.
- Root cause:
  - The old inference only recognized the legacy `.my_agent/subagents` layout.
  - With the new split layout, `workspace.parent` pointed at the runtime directory rather than the user task directory.
- Fix:
  - Explicit acceptance execution and parent acceptance dry-run now prefer `manager.workspace_root` when available.
  - Tests can still use inferred artifact working directories below the task root.
- Verification:
  - Added a regression where `subagent_workspace=".my_agent_runtime/run/subagents"` and artifacts live under `deliverables/leaf`.
  - The acceptance test report now records `workspace_root` as the task root and command cwd as the deliverable directory.
- Status: solved.

### Finding 19: Model-Style `cd <dir> && pytest ...` Was Blocked Before Real Execution

- Discovered at: 2026-05-09 during product rerun on the dijkstra leaf.
- Symptom:
  - Runner output declared `cd /.../deliverables/... && python3 -m pytest test_solution.py -v`.
  - `TestExecutor` correctly blocked the shell chain, but the test was marked not executed.
- Root cause:
  - The safety layer blocks shell operators such as `&&`.
  - The preprocessor did not normalize the common safe pattern into a bounded working directory plus a plain command.
- Fix:
  - `execution_test_items.py` now converts exactly one leading `cd <workspace-local-dir> && <command>` into:
    - `working_dir=<that directory>`
    - `command=<plain command>`
  - The executor still validates the remaining command through the existing allowlist and shell-character blocker.
- Verification:
  - Regression test covers the conversion.
  - Real dijkstra rerun changed from `executed=0` / command rejected to `executed=1` / real pytest failure.
- Status: solved.

### Finding 20: Some Runners Wrote Test Files But Omitted `tests` In `output.json`

- Discovered at: 2026-05-09 during product rerun after workspace fixes.
- Symptom:
  - Three leaves had `total_tests=0`.
  - Two of them had test artifacts on disk but no structured `tests` entries.
  - One leaf had neither tests nor artifacts in `output.json`.
- Root cause:
  - Real model output can omit the structured `tests` list even when it created `test_*.py` files.
  - The product command only trusted the structured tests list.
- Fix:
  - When `tests` is empty, `prepare_test_items()` now infers bounded pytest commands from workspace-local `test_*.py` artifacts.
  - It does not infer from ordinary source files, non-Python files, or out-of-workspace paths.
- Verification:
  - Regression test covers artifact-only `test_solution.py`.
  - Real rerun:
    - median leaf changed from zero-tests to real executed failure.
    - union-find leaf changed from zero-tests to real executed pass.
    - toposort leaf remained zero-tests because `output.json` had neither tests nor artifacts.
- Status: solved where artifact refs exist; remaining zero-tests behavior is intentional.

### Remaining Gaps From The 48-Leaf Tree

- One leaf still cannot be product-tested because the runner produced no structured `tests` and no structured `artifacts`; parent can only route it to rescue or manual inspection.
- Child-authored tests are not a reliable sole quality oracle. Some failures are real code bugs, while some are bad test expectations. Parent-owned task-family test packs are still needed.
- Large clean deliverables trees work, but future UI/CLI should make the two roots obvious:
  - clean user output: `deliverables/<run_id>/...`
  - internal runtime: `.my_agent_runtime/<run_id>/...`

## 2026-05-09 Main-Node-Only Hierarchy Smoke E2E

### Finding 21: Runner Context Had No Model Tool For Creating Its Own Next Layer

- Discovered at: 2026-05-09 before rerunning the main-node-only smoke E2E.
- Symptom:
  - Product code already had `HierarchyScheduleRequest`, but a running subagent could not call a dedicated tool to create child runs under itself.
  - A controller could still create descendants externally through CLI/API, which does not match the required main-node-only feedback chain.
- Root cause:
  - `SubAgentHierarchyScheduler` existed only as manager/CLI service.
  - The model tool catalog exposed generic `create_subagents`, not a current-runner-bound child scheduler.
- Fix:
  - Added `schedule_child_subagents` as an orchestration tool.
  - The tool reads `agent._current_subagent_run_id` and refuses to run without an active runner context.
  - `run_subagent_flow()` now sets and restores `_current_subagent_run_id` around the model turn.
- Verification:
  - `test_subagent_runner_can_schedule_children_from_current_node_context` first failed with no child created, then passed after the fix.
  - `test_rejects_without_current_runner_context` verifies the tool cannot be used to bypass the main node.
- Status: solved for current-runner child creation.

### Finding 22: Child Runs Did Not Inherit The User-Approved Deliverables Root

- Discovered at: 2026-05-09 while preparing the main-node-only smoke E2E.
- Symptom:
  - The parent task could have `/Users/example/my-终端应用/deliverables/<case>` in `allowed_write_roots`, but scheduled children lost that product write root unless the model repeated `extra_write_roots`.
- Root cause:
  - `SubAgentHierarchyScheduler._create_child()` passed only `spec.extra_write_roots`.
  - Empty child specs defaulted to task-local write roots only.
- Fix:
  - Descendants now inherit parent `allowed_write_roots` except the parent task directory itself.
  - This forwards user-approved product roots while avoiding writes into the parent work-order directory.
- Verification:
  - `test_hierarchy_schedule_inherits_parent_extra_write_roots` first failed, then passed.
- Status: solved for default hierarchy scheduling.

### Finding 23: Nested Dispatch Accidentally Spawned Workflow Workers

- Discovered at: 2026-05-09 during real smoke case `hier_main_only_smoke_1778325266`.
- Symptom:
  - The main node was instructed to create exactly one child coordinator.
  - It did create one child through `schedule_child_subagents`, but its later `dispatch_subagents` call inherited global `subagent_workflow_mode=auto`.
  - Dispatch then planned a `producer_critic_repair` workflow for the running root and spawned 3 extra worker children.
  - The proof file was never produced; the root ended `BLOCKED`.
- Root cause:
  - `DispatchSubagentsTool` used global config workflow mode when the model omitted `workflow_mode`.
  - Inside a subagent runner this is unsafe for hierarchy tests, because dispatch should advance existing child tasks unless the node explicitly asks for workflow expansion.
- Fix:
  - In runner context, `dispatch_subagents` now defaults `workflow_mode` to `off`.
  - Top-level dispatch still follows config, and explicit `workflow_mode` still wins.
- Verification:
  - `test_runner_context_dispatch_defaults_workflow_mode_off` first failed with `auto`, then passed with `off`.
- Status: fixed in code; real smoke rerun still pending.

### Finding 24: Nested Dispatch Selected The Running Parent Instead Of Its Child

- Discovered at: 2026-05-09 during real smoke case `hier_main_only_smoke_1778325522`.
- Symptom:
  - The main node created exactly one child after Finding 23 was fixed.
  - Its nested `dispatch_subagents` call then selected the running main node itself as the runner candidate.
  - The root recursively ran itself, timed out after 30 seconds, and the child stayed `PLANNING`.
- Root cause:
  - Runner candidate selection was global.
  - `DispatchSubagentsTool` did not pass the active runner id as a scope or exclusion.
- Fix:
  - `DispatchParams` / `DispatchContext` now carry `parent_run_id`, `root_id`, and `exclude_run_ids`.
  - When called inside a runner, `dispatch_subagents` defaults `parent_run_id` to the active run and excludes the active run id.
  - Runner job selection filters by those scope fields before choosing candidates.
- Verification:
  - `test_subagent_dispatch_parent_scope_runs_direct_children_not_parent` first failed because `parent_run_id` did not exist, then passed.
- Status: fixed in code; real smoke rerun still pending.

### Finding 25: Coordinator Nodes Can Bypass Leaf Work If Given Write Tools

- Discovered at: 2026-05-09 during real smoke case `hier_main_only_smoke_1778325957`.
- Symptom:
  - The hierarchy chain reached root -> child -> leaf and the proof file was written.
  - However the child coordinator also had `write_file`, so it wrote `proof.txt` itself after believing the leaf was blocked.
- Root cause:
  - The test prompt granted coordinator nodes both orchestration tools and product write tools.
  - This violates the intended rule: coordinators may teach/delegate/observe, but must not do leaf work.
- Fix:
  - Updated the live test pattern: coordinator nodes receive only `schedule_child_subagents`, `dispatch_subagents`, `subagent_board`, and read/search/list tools.
  - Leaf nodes receive write tools for product artifacts.
- Verification:
  - Real smoke case `hier_main_only_smoke_1778326442` confirmed the child coordinator had no write tools and did not use `write_file`.
- Status: solved as a test-template and permission-boundary rule; future prompt templates should encode this split.
- 2026-05-10 update:
  - This finding is still valid for final business/product files, but the tool policy was refined after role-template observer testing.
  - Coordinator nodes now keep report-write tools so they can write their own plans, evidence, and coordination reports; prompts and task-local write boundaries still prevent them from replacing worker/writer deliverables.

### Finding 26: Subagents Interpret `max_depth=1` As Relative Depth

- Discovered at: 2026-05-09 during real smoke case `hier_main_only_smoke_1778326442`.
- Symptom:
  - A child coordinator at depth 1 called `schedule_child_subagents` with `max_depth=1`.
  - The service treated it as absolute max depth and blocked leaf creation with `max_depth_exceeded:1`.
- Root cause:
  - The tool parameter name was model-unfriendly inside nested runner context.
  - The model naturally used `max_depth=1` to mean “create one more layer”.
- Fix:
  - `schedule_child_subagents` now normalizes explicit `max_depth` values that would block all children in the active context.
  - Example: parent depth 1 + `max_depth=1` becomes effective absolute max depth 2.
- Verification:
  - `test_runner_context_max_depth_can_mean_one_more_layer` first failed, then passed.
- Status: solved in tool boundary.

### Successful Smoke: Main-Node-Only Root -> Child -> Leaf

- Completed at: 2026-05-09 with real smoke case `hier_main_only_smoke_1778326811`.
- Runtime root:
  - `/Users/example/my-终端应用/.my_agent_runtime/hier_main_only_smoke_1778326811`
- Deliverables root:
  - `/Users/example/my-终端应用/deliverables/hier_main_only_smoke_1778326811`
- Verified behavior:
  - Outer controller only started root `subagent-1778326811-7c86e224`.
  - Root created one child `subagent-1778326838-c47aa8b4`.
  - Child created one leaf `subagent-1778326850-81b8f4d6`.
  - Leaf wrote `/Users/example/my-终端应用/deliverables/hier_main_only_smoke_1778326811/proof.txt`.
  - Proof content: `hierarchy-ok`.
  - Root, child, and leaf all ended `AWAITING_ACCEPTANCE / NEEDS_ACCEPTANCE`, not `BLOCKED`.
  - Child used only orchestration/read tools; leaf used `write_file`.
- Status: passed as the small main-node-only hierarchy smoke. Larger 1/4/16/48 and shopping-site E2E still need separate runs.

## 2026-05-09 Main-Node-Only 1/4/16/48 Stress Attempt

### Finding 27: Repeated Parent Dispatch Was Blocked By The One-Shot Guard

- Discovered at: 2026-05-09 in case `main_node_tree_1_4_16_48_serial_20260509_201422`.
- Symptom:
  - The root created 4 direct child coordinators.
  - Because `runner_start_rate=1`, the first dispatch only ran one child.
  - The root then tried to call the same `dispatch_subagents` again, but the one-shot guard blocked it.
- Root cause:
  - `dispatch_subagents` and `subagent_board` were grouped with creation tools in `ONE_SHOT_TOOL_NAMES`.
  - Creation tools must be one-shot to avoid duplicate task creation, but dispatch/board are progress-loop tools and must be callable repeatedly by the same parent.
- Fix:
  - The one-shot guard now only covers task creation tools: `create_subagents` and `schedule_child_subagents`.
  - `dispatch_subagents` and `subagent_board` can be repeated inside a parent runner.
- Verification:
  - `test_repeated_dispatch_is_allowed_for_parent_progress_loops` covers two identical dispatch tool calls in one tool loop.
  - The later live case `main_node_tree_1_4_16_48_serial_20260509_2040` showed a grandchild repeatedly dispatching leaf workers until multiple leaves wrote files.
- Status: solved.

### Finding 28: Descendant Runners Needed Parent Goal Context

- Discovered at: 2026-05-09 in case `main_node_tree_1_4_16_48_serial_20260509_201422`.
- Symptom:
  - A grandchild was created with goal `child-04-grandchild-04 任务执行`.
  - It blocked with missing input because its task context did not include the leaf paths or exact content requirements.
- Root cause:
  - `schedule_child_subagents` persisted only the child spec goal and a generic thought.
  - If a parent model wrote a thin child goal, the next layer lost the root task details.
- Fix:
  - Hierarchy-created descendants now inherit a bounded parent goal/thought summary in `thought`.
  - The inherited thought also tells coordinators to make the next layer goal self-contained.
  - This preserves the root -> child -> grandchild feedback path while avoiding external controller intervention.
- Verification:
  - `test_hierarchy_schedule_carries_parent_context_to_child_thought` covers inherited parent goal/thought.
  - The live case `main_node_tree_1_4_16_48_serial_20260509_2040` showed leaf prompts containing parent, grandparent, and root summaries.
- Status: solved with bounded inherited context; future tuning may shrink the summary further if token pressure rises.

### Finding 29: Coordinator Role Slips Created Worker-Shaped Grandchildren

- Discovered at: 2026-05-09 in case `main_node_tree_1_4_16_48_serial_20260509_2040`.
- Symptom:
  - `child-03` created `child-03-grandchild-*` runs with role `worker` even though those runs had `schedule_child_subagents` and `dispatch_subagents`.
  - The hierarchy shape became harder to reason about and recovery rules could classify them incorrectly.
- Root cause:
  - The model-supplied role was trusted even when tools and depth clearly indicated a coordinator.
- Fix:
  - `SubAgentHierarchyScheduler` now infers coordinator roles for children that have orchestration tools and no concrete product-write intent, even if they have report-write tools:
    - depth 1 -> `child_coordinator`
    - depth 2 -> `grandchild_coordinator`
    - deeper -> `coordinator`
  - Explicit non-worker roles are still respected.
- Verification:
  - `test_hierarchy_schedule_infers_coordinator_role_from_tools` first failed with `worker`, then passed with `grandchild_coordinator`.
- Status: solved for common role slips.

### Finding 30: Rate-Limited Pending Children Were Misread As Failed

- Discovered at: 2026-05-09 in case `main_node_tree_1_4_16_48_serial_20260509_2040`.
- Symptom:
  - A grandchild created 3 leaf workers.
  - With `runner_start_rate=1`, only one leaf ran in a dispatch slice.
  - The coordinator reported the remaining PLANNING leaves as failed instead of continuing dispatch.
- Root cause:
  - The dispatch tool response did not explicitly summarize the current node's direct child status after the dispatch slice.
  - The model saw missing files and interpreted not-yet-run children as failures.
- Fix:
  - Runner-context `dispatch_subagents` responses now include a `direct_children` summary:
    - total direct children
    - status counts
    - PLANNING ids
    - RUNNING ids
    - a continue hint telling the parent to call dispatch again when children remain PLANNING/RUNNING.
- Verification:
  - `test_runner_context_dispatch_reports_direct_child_progress` covers the new refs-only progress payload.
- Status: solved for tool feedback; full 1/4/16/48 rerun still needed.

### Remaining Gap: Failed Branch Recovery Is Not Fully Automatic

- The live case `main_node_tree_1_4_16_48_serial_20260509_2040` was stopped after exposing systemic issues.
- Confirmed working:
  - external controller only started root;
  - root created children;
  - child created grandchildren;
  - grandchild created leaf workers;
  - leaf workers wrote real files under clean `deliverables/.../leaf_outputs`;
  - when `child-04` blocked, root continued to `child-03`.
- Still missing:
  - automatic in-tree rescue when a coordinator times out after partial child completion;
  - parent-driven retry/takeover for PLANNING leaf workers left behind by a partial dispatch;
  - final clean 48-file pass under the main-node-only constraint.
- Next recommended test:
  - rerun a smaller `1 root -> 2 child -> 4 grandchild -> 12 leaf` tree first after the fixes above;
  - then rerun full `1/4/16/48`;
  - only after that start the shopping-site E2E.

## 2026-05-09 Main-Node-Only 1/2/4 Fixed Retest

- Test scene:
  - Workspace: `/Users/example/my-终端应用`
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/main_node_small_fixed_1_2_4_20260509_214828`
  - Clean deliverables root: `/Users/example/my-终端应用/deliverables/main_node_small_fixed_1_2_4_20260509_214828`
  - Root run: `subagent-1778334508-b053f5fa`
  - Real execution mode: outer controller only started root with `subagent-run --execute --no-probe --max-cards 0`.
- 中文说明：
  - 这次严格按“外层只启动主节点”的原则复测。
  - 外层没有直接启动、修复或指挥 child / leaf，只读 board、文件和报告观察结果。
  - root 自己创建 2 个 child coordinator；child 自己创建 4 个 leaf worker；leaf 负责写真实文件。
- Observed behavior:
  - All 7 runs ended `AWAITING_ACCEPTANCE / NEEDS_ACCEPTANCE`.
  - Four leaf deliverables were written under clean deliverables:
    - `child-alpha/palindrome/solution.py` and `README.md`
    - `child-alpha/interval_merge/solution.py` and `README.md`
    - `child-beta/bfs/solution.py` and `README.md`
    - `child-beta/stats/solution.py` and `README.md`
  - No `failure_handoff.json` remained after the successful run.
  - `subagent_capability_route_report.json` reported `total=0`.
- Verification:
  - Parent-side Python smoke tests imported all 4 generated `solution.py` files and verified:
    - `is_palindrome`
    - `merge_intervals`
    - `Graph.bfs`, `Graph.bfs_shortest_path`, `bfs_tree`
    - `mean`, `median`, `variance`, `standard_deviation`, `describe`
  - Smoke output: `small fixed 1-2-4 leaf smoke tests passed`.
  - Clean deliverables check removed `.DS_Store` and `__pycache__`, leaving only the 8 expected leaf files.
- Fixes verified by this retest:
  - Leaf coding tasks with explicit deliverable paths now infer write-capable tools when the model omits `allowed_tools`.
  - A successful retry now clears stale failure fields, stale blockers, stale open capability requests, and stale `failure_handoff.json`.
- Remaining gap:
  - One leaf acceptance next-action still became `request_human_confirmation` because a generated check had an empty test command.
  - 中文解释：产物本身和父级 smoke test 都过了，但验收计划看到“空测试命令”后变得保守，要求人工确认。后续应把这种空测试命令降级为 inspect-only，或自动转成安全的文件存在性检查。

## 2026-05-09 Formal Subagent Debug Trace Switch

- Requirement:
  - Real E2E needs optional high-signal logs for multi-layer agent behavior.
  - The logs must not become temporary scattered `print` calls, and must not pollute user deliverables.
- Design:
  - New config: `subagent_debug_trace_level`.
  - Level `0`: default off, no trace file is written.
  - Levels `1-5`: write bounded refs-only JSONL events to the internal runtime workspace.
  - Current file: `<subagent_workspace>/debug_traces/subagent_trace.jsonl`.
- Current events:
  - `task_created` at level 1.
  - `runner_result_recorded` at level 2.
- Guardrails:
  - Trace records include ids, root/parent links, depth, role, status, verification status, short previews, counts, and refs.
  - Trace records must not copy prompt bodies, response bodies, artifact bodies, or large tool outputs.
  - Debug trace is a test observability feature; normal status/board/startup paths remain refs-only and model-call free.
- Verification:
  - `test_subagent_debug_trace_level_defaults_to_off`
  - `test_subagent_debug_trace_level_accepts_zero_to_five`
  - `test_subagent_debug_trace_is_off_by_default`
  - `test_subagent_debug_trace_records_task_creation_when_enabled`
  - `test_subagent_debug_trace_records_runner_result_when_enabled`

## 2026-05-09 Trace Smoke Tool Alias Failure And Fix

- Test scene:
  - First failing case: `main_node_trace_smoke_20260509_2200`
  - Fixed retest case: `main_node_trace_smoke_fixed_20260509_2218`
  - Both used main-node-only execution: the outer controller only created and started the root runner.
- 中文说明：
  - 这次专门验证 `subagent_debug_trace_level=2` 在真实模型运行里是否有用。
  - 它确实有用：不用打开 prompt/response 正文，就能从 trace JSONL 看见 root、child、leaf 的创建顺序、层级、角色和 runner 收束状态。
- Symptom:
  - In the first trace smoke, root created child and child created leaf correctly.
  - Leaf blocked with `write 工具不可用且无 fallback`.
  - Trace showed the exact failing node: depth 2 `leaf_worker`, status `BLOCKED`.
- Root cause:
  - The child model supplied `allowed_tools=["write", "read_file", "list_files"]`.
  - `write` is a natural-language/model alias, but the real ToolRegistry tool is `write_file`.
  - Because explicit `allowed_tools` were trusted as-is, leaf received an unavailable tool name and missed the canonical write tools.
- Fix:
  - Hierarchy scheduling now canonicalizes common model aliases before persisting child runs:
    - `write -> write_file`
    - `read -> read_file`
    - `list -> list_files`
    - `search -> search_text`
    - `append -> append_file`
    - `replace -> replace_in_file`
  - If the task is a leaf write task under an approved deliverables root, the scheduler also appends the safe leaf coding tool bundle.
- Verification:
  - Regression test: `test_hierarchy_schedule_normalizes_model_write_alias_for_leaf_tasks`.
  - Real fixed retest:
    - `root -> child -> leaf` completed without direct lower-layer intervention.
    - `proof.txt` was written at `/Users/example/my-终端应用/deliverables/main_node_trace_smoke_fixed_20260509_2218/leaf_outputs/proof/proof.txt`.
    - File content check output: `proof-content-ok`.
    - Trace file recorded 3 `task_created` events and 3 `runner_result_recorded` events, all refs-only.
- Remaining gap:
  - Root acceptance still produced `request_human_confirmation` because the model emitted an empty test command named `层级派工验证`.
  - 中文解释：实际产物已经写对了，但验收层对“空测试命令”太保守。下一步应把空测试命令变成可执行文件检查，或降级为 inspect-only。

## 2026-05-09 Empty Test Command Noise And Trace Expansion

- Test scene:
  - Triggered by the `main_node_trace_smoke_fixed_20260509_2218` remaining gap.
  - Regression focused on parent acceptance control-plane behavior and debug trace coverage.
- 中文说明：
  - 这次修的是“空测试命令”造成的假阻塞。
  - 子代理有时会在 tests 里写一个名字，例如 `层级派工验证`，但 command 为空。旧逻辑把它当成危险或无效命令，于是要求人工确认。
  - 新逻辑把它当成不可执行的占位检查：不会假装测试通过，也不会要求人工确认；父级验收会进入 inspect-only，让上级继续看证据、产物和 verifier。
- Root cause:
  - `parent_acceptance_controller` used the raw `tests` list for both command safety preflight and missing-report decisions.
  - An empty command therefore reached `TestExecutor._validate_command("")`, which returned `空测试命令` and got mapped to `request_human_confirmation`.
- Fix:
  - Parent acceptance now filters executable validation tests before safety preflight and test-report decisions.
  - Empty command tests are ignored as executable work, but stay visible in refs summary as `ignored_empty_command_tests=N`.
  - Unsafe non-empty commands still request human confirmation.
- Debug trace expansion:
  - `subagent_debug_trace_level=2` now records:
    - `hierarchy_schedule_result`
    - `parent_acceptance_decision`
    - `parent_acceptance_next_action`
  - These events are refs-only and bounded. They show ids, hierarchy fields, decision/action, human gate, mutates-state flag, command preview, refs, and counts.
- Verification:
  - Red tests first failed on the old behavior:
    - empty command produced `request_human`;
    - hierarchy schedule wrote no trace event;
    - parent acceptance wrote no trace events.
  - After the fix:
    - `python3 -m pytest -q agent_py_agent/tests/test_parent_acceptance_controller.py::test_parent_acceptance_plan_ignores_empty_command_tests_as_non_executable agent_py_agent/tests/test_subagent_debug_trace.py::test_subagent_debug_trace_records_hierarchy_schedule_when_enabled agent_py_agent/tests/test_subagent_debug_trace.py::test_subagent_debug_trace_records_parent_acceptance_decision_and_next_action -p no:cacheprovider` -> `3 passed`.
- Current status:
  - Solved for the known empty-command acceptance noise.
  - Trace is now broad enough to observe the next real 1/4/16/48 run without opening prompt/response bodies.
- Remaining risk:
  - Empty command tests are not automatically converted into file checks yet. For now they are visible but non-executable.
  - A future improvement can infer safe `file_check` tests when the output already cites concrete artifact paths.

## 2026-05-09 Main-Node 1/2/4/12 Trace Tree E2E

- Test scene:
  - Case id: `main_node_trace_tree_1_2_4_12_20260509_230212`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/main_node_trace_tree_1_2_4_12_20260509_230212`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/main_node_trace_tree_1_2_4_12_20260509_230212`.
  - Only the root runner was started externally. The root created 2 child coordinators; each child created 2 grandchild coordinators; each grandchild created 3 leaf workers.
- 中文说明：
  - 这次按用户要求验证“主节点只管发令，下层自己继续派工”的真实链路。
  - 我没有直接去指挥叶子节点；叶子节点的文件都是通过 root -> child -> grandchild -> leaf 这条链路产生的。
  - 这次规模是 `1 主 / 2 子 / 4 孙 / 12 叶`，不是最终压力规模，但已经能暴露真实层级调度、产物落盘、父级汇总和叶子能力边界问题。
- Observed behavior:
  - The hierarchy created 19 tasks total:
    - 1 root coordinator.
    - 2 child coordinators.
    - 4 grandchild coordinators.
    - 12 leaf workers.
  - `subagent_debug_trace_level=2` recorded:
    - all `task_created` events;
    - all `hierarchy_schedule_result` events;
    - leaf and coordinator `runner_result_recorded` events.
  - All 12 leaf deliverable folders eventually produced:
    - `solution.py`;
    - `README.md`;
    - `test_solution.py`.
- Verification:
  - Parent-side external pytest ran every generated `test_solution.py` in its own leaf directory.
  - Result: 12 leaf test suites total, 9 passed, 3 failed.
  - Passed suites:
    - `anagram_groups`, `interval_merge`, `palindrome`;
    - `lru_cache`, `valid_parentheses`;
    - `bfs`, `dijkstra`;
    - `binary_search`, `prime_sieve`.
  - Failed suites:
    - `two_sum`: implementation returned a valid pair `[4999, 5001]`, but the test expected one specific pair `[1, 9999]`. 中文解释：题目本身允许多个答案，但测试写得太死，导致“功能可能没错，测试标准不稳”。
    - `topological_sort`: DFS and Kahn outputs were both valid topological orders, but the integration test expected the same relative order. 中文解释：拓扑排序本来可以有多个正确顺序，这个测试把“唯一顺序”当成标准，测试口径不合理。
    - `stats`: `standard_deviation` returned population standard deviation, while the test expected sample standard deviation. 中文解释：实现和测试没有先约定“总体标准差”还是“样本标准差”，所以口径打架。
- Real issues found:
  - Root finalization did not return after lower layers had mostly completed:
    - root runner stayed alive for more than 18 minutes;
    - CPU dropped to 0;
    - trace had no new event for more than 160 seconds;
    - process stack showed it was waiting on model streaming response during final summary.
  - 中文解释：下层已经把活干得差不多了，但主节点最后总结卡住了。后续需要 root-level model-call timeout / heartbeat / partial-finalize fallback，不能让无人值守任务无限挂住。
  - One leaf (`dijkstra`) wrote files successfully, but marked itself `BLOCKED` because it wanted to run `pytest` and did not have a shell/command tool.
  - 中文解释：叶子节点应该明白自己的权限边界：没有命令工具时，应该写好 `test_solution.py` 和建议命令，让父级验收器执行，而不是把自己标记为缺能力。
  - Some intermediate prompts dropped the requested `leaf_outputs` path segment and wrote directly under child/grandchild folders.
  - 中文解释：产物没有写丢，但路径不完全听话。后续要把 deliverables root 当作强字段传递，不能靠自然语言一层层转述。
- Cleanup:
  - Removed `.DS_Store`, `.pytest_cache`, and `__pycache__` only inside this case deliverables directory.
- Next recommendation:
  - Add root finalization timeout / partial-finalize fallback first.
  - Then tighten leaf worker prompt/tool contract: leaf writes artifacts and tests; parent/verifier runs commands.
  - Then add strict deliverables-root propagation checks before moving to larger `1/4/16/48` scale.

## 2026-05-09 Timeout/Leaf-Contract Smoke Retest

- Test scene:
  - Case id: `main_node_timeout_contract_smoke_20260509_2342`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/main_node_timeout_contract_smoke_20260509_2342`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/main_node_timeout_contract_smoke_20260509_2342`.
  - Shape requested: root -> child -> leaf, with only root started externally.
- 中文说明：
  - 这次专门复测两个刚修的点：`subagent-run --execute` 是否有总超时边界，以及 leaf 没有命令工具时是否应把 pytest 交给父级验收器。
  - 测试没有手动操作 child/leaf；只启动 root。
- Observed behavior:
  - The direct CLI timeout boundary worked: root runner returned `TIMEOUT` after 120 seconds instead of hanging indefinitely.
  - The timeout result wrote normal runner refs, including `RUNNER_RESULT.md` and `reports/runner_result.json`.
  - Before timing out, root created one child.
- New issue found:
  - Root created the child with a vague goal: `创建 leaf worker，实现 add(a,b) 并写测试`.
  - The child goal dropped the deliverables path and leaf boundary from the root task.
  - 中文解释：系统已经在 `thought` 里放了父级摘要，但真实模型下一层更依赖 `goal`。如果产物路径只靠 thought 转述，后续很容易丢。
- Fix added after this retest:
  - Hierarchy scheduler now enriches vague child goals with `继承父级目标/边界`.
  - Leaf tool inference uses the enriched goal, so a vague leaf goal can still inherit `.py` / deliverable write intent from parent scope.
- Remaining gap:
  - Timeout can still leave a just-created child in `RUNNING` if root times out before it records child dispatch completion.
  - 中文解释：现在不会无限卡住，但还需要后续的 due-check / recovery-tree 把这种“父超时、子还挂着”的场景收口成可接管动作。

## 2026-05-09 Scope-Inheritance Smoke Retest

- Test scene:
  - Case id: `main_node_scope_inherit_smoke_20260509_2348`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/main_node_scope_inherit_smoke_20260509_2348`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/main_node_scope_inherit_smoke_20260509_2348`.
  - Shape requested: root -> child -> leaf, with only root started externally.
- What improved:
  - The child goal preserved the required deliverables path:
    - `/Users/example/my-终端应用/deliverables/main_node_scope_inherit_smoke_20260509_2348/leaf_outputs/proof/`
  - The child goal also preserved the boundary that coordinator should not write and leaf should write.
  - 中文解释：这说明“父级产物路径/边界传到下一层”已经比上一轮稳定，至少没有再变成一句很短的 `实现 add 并写测试`。
- What still failed:
  - Root created the child but did not dispatch that child before timing out.
  - Root returned `TIMEOUT` after 180 seconds through the new direct runner timeout boundary.
  - Child remained `PLANNING`, with no leaf created and no deliverables written.
- Follow-up:
  - The next section records the refs-only recovery guard added for `parent TIMEOUT + direct child PLANNING/RUNNING`.
  - 中文解释：下面一节就是针对“父级超时后孩子残留”的修复记录，避免读到这里误以为还完全没做。

## 2026-05-09 Parent Timeout Child Recovery Guard

- Trigger:
  - The scope-inheritance smoke retest left a direct child in `PLANNING` after the root returned `TIMEOUT`.
  - 中文解释：父节点超时了，但孩子还挂着。如果没人提醒，上级就可能以为整棵树已经停了，实际还有孩子需要继续派发或接管。
- Fix:
  - `due-check` now builds a scoped task index for the current root tree.
  - When a `TIMEOUT` parent still owns unfinished direct children, it reports `parent_timeout_with_unfinished_children`.
  - `plan-actions` maps that issue to `recover_child_after_parent_timeout`.
  - The suggested command is `my-agent subagents-recovery-tree <root> --hide-healthy`, so the next agent can inspect refs and choose a recovery path.
  - `rescue_context_refs` and `rescue_packet.recovery_entrypoints` include structured refs such as `unfinished_child:<child_id>:PLANNING`.
  - `subagents-recovery-tree --hide-healthy` now also keeps those unfinished children visible as `parent_timeout_unfinished_child:<parent_id>` candidates.
- Safety boundary:
  - This first slice is refs-only.
  - It does not automatically dispatch children, take over runs, modify task state, or write product code.
  - 中文解释：现在先做到“能发现、能提示、能给恢复入口”，不是自动替你接管所有孩子。
- Verification:
  - Red tests first failed because no issue/action existed for this case.
  - After the fix:
    - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_subagent_coordinator_due_check.py agent_py_agent/tests/test_policy_checks.py::test_action_for_issue_parent_timeout_with_unfinished_children agent_py_agent/tests/test_policy_checks.py::test_commands_for_action_recover_child_after_parent_timeout` -> `6 passed`.
    - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_subagent_hierarchy_recovery.py::test_hierarchy_recovery_packet_includes_unfinished_child_after_parent_timeout` -> `1 passed`.
- Current status:
  - Solved for detection and dry-run recovery planning.
  - Remaining follow-up is an explicit, audited apply path if we later decide a new leader should adopt those children automatically or semi-automatically.

## 2026-05-09 Debug Trace Report Summary Expansion

- Change:
  - `subagent_debug_trace_level=3` now records report-level summaries:
    - `due_check_report`
    - `action_plan_report`
    - `hierarchy_recovery_packet`
    - `dispatch_report`
    - `dispatch_watch_report`
  - These events include counts, summary maps, issue/action kinds and recovery candidate ids.
  - 中文解释：以后真实 E2E 卡住时，不用先打开一堆报告正文；可以先看 trace 知道 due-check 发现了什么、action-plan 建议了什么、recovery-tree 看到了哪些候选、dispatch/watch 有没有继续推进。
- Safety boundary:
  - Default level 0 remains silent.
  - The new events are refs-only and bounded; they do not copy prompt, response, artifact body or tool output body.
  - They do not call models, run commands, or touch user deliverables.
- Verification:
  - Red tests first failed because report events did not exist.
  - After the fix:
    - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_subagent_debug_trace.py::test_subagent_debug_trace_records_due_action_and_recovery_reports_at_level_three agent_py_agent/tests/test_subagent_debug_trace.py::test_subagent_debug_trace_records_dispatch_reports_at_level_three` -> `2 passed`.

## 2026-05-10 Runner-Context Acceptance Closure Retest

- Test scene:
  - Case id: `main_node_final_acceptance_retest_20260510_110000`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/main_node_final_acceptance_retest_20260510_110000`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/main_node_final_acceptance_retest_20260510_110000`.
  - Model name: `MiniMax-M2.7`.
  - Real execution mode: only the root/parent runner was driven; lower layers were created and dispatched through runner-context tools.
- 中文说明：
  - 这次验证“父节点自己派发孩子、自己跑孩子验收、孩子测试通过后状态能不能真正收口”。
  - 之前的问题是测试报告已经通过，follow-up 也说可以验收，但孩子还停在 `AWAITING_ACCEPTANCE` / `NEEDS_ACCEPTANCE`，无人值守时会像“明明做完了但没盖章”。
- Symptom:
  - Leaf pytest succeeded:
    - text leaf: `9 passed`.
    - arithmetic leaf: `13 passed`.
  - `test_execution.json` and `parent_acceptance_auto_followup.json` existed.
  - Acceptance follow-up classified the runs as apply-ready, but task state did not become `DONE / VERIFIED`.
- Root cause:
  - Top-level dispatch intentionally keeps follow-up apply manual.
  - Runner-context dispatch reused the same conservative default, so an active parent runner could run tests but could not close its own direct children after tests passed.
- Fix:
  - Added `auto_apply_acceptance_followup` to dispatch/watch/acceptance record bundles.
  - Runner-context dispatch now enables that flag only when both `apply=True` and `execute_acceptance_tests=True`.
  - Top-level CLI/API dispatch remains manual by default.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_agent/test_dispatch_and_planner.py::test_runner_context_dispatch_applies_passed_child_acceptance_followup` -> `1 passed`.
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_agent/test_dispatch_and_planner.py::test_subagent_dispatch_manual_acceptance_test_execution_is_test_only agent_py_agent/tests/test_agent/test_dispatch_and_planner.py::test_subagent_dispatch_apply_with_acceptance_tests_refreshes_aggregate_report agent_py_agent/tests/test_agent/test_dispatch_and_planner.py::test_subagent_dispatch_to_followup_apply_acceptance_chain agent_py_agent/tests/test_agent/test_dispatch_and_planner.py::test_runner_context_dispatch_applies_passed_child_acceptance_followup` -> `4 passed`.
- Status:
  - Solved for direct child closure from an active parent runner after explicit tests pass.
  - Still intentionally manual for top-level user commands, so the outer user/operator does not accidentally mutate task state by inspecting dispatch.

## 2026-05-10 Coordinator Acceptance Robustness Retest

- Test scene:
  - Case id: `main_node_auto_accept_retest_20260510_112000`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/main_node_auto_accept_retest_20260510_112000`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/main_node_auto_accept_retest_20260510_112000`.
  - Model name: `MiniMax-M2.7`.
- 中文说明：
  - 这次继续验证真实模型会不会写出“不完全按我们格式来”的验收字段、测试命令和路径。
  - 暴露的问题都来自真实 runner 输出，不是手写假样本。
- Observed behavior:
  - The tree created 5 nodes.
  - Both leaf workers reached `DONE / VERIFIED`.
  - Two child coordinators stayed `AWAITING_ACCEPTANCE`.
- Finding 1: coordinator emitted `auto_acceptance`.
  - Symptom: `auto_acceptance` was not a recognized validation method, so coordinator acceptance could not close even though direct children were done.
  - 中文解释：模型用了“自动验收”这个自然语言式名字，但系统只认识更严格的测试类型。
  - Fix: `auto_acceptance` is normalized into deterministic child-state acceptance. It passes only when all direct children exist and are `DONE / VERIFIED`.
  - Verification: `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_agent/test_subagent_acceptance.py::test_subagent_acceptance_auto_acceptance_checks_verified_children` -> `1 passed`.
- Finding 2: safe `cd ... && pytest ...` was blocked when `working_dir` already existed.
  - Symptom: a coordinator emitted `working_dir` plus command `cd <same workspace dir> && python3 -m pytest ...`; the executor rejected shell chaining before reaching pytest.
  - 中文解释：安全层挡住 shell 链是对的，但这种“开头切目录再跑 pytest”的常见模型写法可以安全拆开。
  - Fix: tests preprocessor strips exactly one leading safe workspace-local `cd ... &&` even when `working_dir` is already present, then keeps the remaining command shell-free.
  - Verification: `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_subagent_test_item_preparation.py::test_prepare_test_items_strips_safe_cd_chain_even_with_working_dir` -> `1 passed`.
- Finding 3: coordinator hallucinated one path segment in an absolute artifact path.
  - Symptom: it wrote `main_node_auto_accept_retest_20250510_112000` instead of `20260510`, so artifact existence failed although the real file existed under the allowed deliverables root.
  - 中文解释：模型把年份写错了一个数字。不能因为一个路径拼写错就误判整棵树失败，但也不能放开任意路径扫描。
  - Fix: artifact lookup now uses allowed workspace/write roots and can recover by safe unique suffix for absolute paths that are meant to live under those roots.
  - Verification: `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_acceptance_helpers_class.py::TestCheckArtifactExists::test_absolute_path_typo_recovers_by_safe_workspace_suffix` -> `1 passed`.
- Status:
  - Solved for these three real coordinator-output variations.
  - Remaining risk: suffix recovery is intentionally conservative; if two candidate paths match the same suffix, it should continue to fail closed.

## 2026-05-10 Root Child-Acceptance Completion Retest

- Test scene:
  - Case id: `main_node_coordinator_accept_retest_20260510_113000`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/main_node_coordinator_accept_retest_20260510_113000`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/main_node_coordinator_accept_retest_20260510_113000`.
  - Model name: `MiniMax-M2.7`.
  - Root run id: `subagent-1778383475-aabb42dc`.
- 中文说明：
  - 这次验证最终收口：root、child coordinator、leaf worker 都要真实变成 `DONE / VERIFIED`，不是只让叶子文件测试通过。
  - 只在最后用父级验收 API 给 root 补了一次显式验收，验收判断仍来自真实 child 状态和真实测试报告。
- Observed behavior before the last root acceptance:
  - 5 tasks existed.
  - Both child coordinators were `DONE / VERIFIED`.
  - Both leaves were `DONE / VERIFIED`.
  - Root was still `AWAITING_ACCEPTANCE / NEEDS_ACCEPTANCE`.
  - Root `output.json` had artifacts but an empty `tests` list.
- Finding:
  - Root/coordinator tasks with direct children can omit tests entirely.
  - 中文解释：协调节点有时候不写 pytest 命令，因为它自己不产代码；它真正的验收标准应该是“直接孩子都完成并通过验收”。
- Fix:
  - If a coordinator/root task has direct `child_ids` and an empty tests list, acceptance execution synthesizes one deterministic `child_acceptance` check.
  - `child_acceptance` passes only when all direct children exist and are `DONE / VERIFIED`.
- Final result:
  - Root coordinator: `DONE / VERIFIED`.
  - Text child coordinator: `DONE / VERIFIED`.
  - Arithmetic child coordinator: `DONE / VERIFIED`.
  - Text leaf: `DONE / VERIFIED`.
  - Arithmetic leaf: `DONE / VERIFIED`.
  - Root `test_execution.json`: `total=1`, `failed=0`, `passed=1`, `validation_method=child_acceptance`.
  - All four lower task reports passed.
- Independent verification:
  - `python3 -m pytest -q -p no:cacheprovider /Users/example/my-终端应用/deliverables/main_node_coordinator_accept_retest_20260510_113000/leaf_outputs/leaf_worker_text` -> `14 passed`.
  - `python3 -m pytest -q -p no:cacheprovider /Users/example/my-终端应用/deliverables/main_node_coordinator_accept_retest_20260510_113000/leaf_outputs/leaf_worker_arithmetic` -> `5 passed`.
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_agent/test_subagent_acceptance.py::test_subagent_acceptance_infers_child_acceptance_when_coordinator_omits_tests` -> `1 passed`.
- Remaining gap:
  - Running pytest directly in deliverables can create `.pytest_cache` and `__pycache__`; this does not pollute repo code, but future test runners should support cleaner artifact hygiene for user-facing deliverables.
  - Root final acceptance was invoked by parent API after the root runner completed. A later CLI/watch bridge can expose this as a clearer semi-auto operator action without changing the safety boundary.

## 2026-05-10 Role Template Dispatch Smoke

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/role_template_e2e`.
  - Deliverables root: `/Users/example/my-终端应用/role_template_e2e_deliverables`.
  - Model name: `MiniMax-M2.7`.
- 中文说明：
  - 这次验证“模型能不能看到角色模板，并自己选 worker / tester / 找茬 / 验收”。
  - 父级只调用主节点，真实模型自己通过 `create_subagents` 创建四个子代理。
- Observed behavior:
  - MiniMax 创建了 4 个任务：`worker`、`tester`、`bug_finder`、`acceptor`。
  - `worker` 得到 `write_file/append_file/replace_in_file` 等写工具。
  - `tester`、`bug_finder`、`acceptor` 保持只读工具。
- Finding 1: QA roles could be selected before worker.
  - Symptom: `subagents-dispatch --dry-run --max-runners 1` 在旧逻辑下会被文件/更新时间顺序影响，可能先选到 `acceptor`。
  - 中文解释：这会变成“还没做东西就先验收”，真实业务流水线顺序错了。
  - Fix: runner 候选先全部筛出来，再按角色阶段排序，最后才按 `max_runners` 截断。顺序为 coordinator/规划类、worker/产出类、tester/bug_finder/critic、acceptor。
  - Verification: focused test `test_role_phase_order_is_applied_before_runner_limit` 先红后绿；真实 dry-run 复测 `max_runners=1` 选中 worker。
- Finding 2: default 30s timeout killed a real worker before output.
  - Symptom: worker 执行购物 demo 时 30 秒超时，未生成产物。
  - 中文解释：这类真实 E2E 比单元测试慢，固定 30 秒会误杀正常任务。
  - Fix: 按当前用户要求，默认 `runner_timeout_seconds` 改为 `off`；`off/none/disabled/0` 表示不套外层超时，数字秒数表示固定超时，`auto` 表示动态 timeout。
  - Verification: focused timeout tests 先红后绿；真实 worker 重跑超过 30 秒后没有被 timeout wrapper 杀掉。
- Finding 3: unlimited runner can still hang without visible model-stage progress.
  - Symptom: 无限时长重跑超过 5 分钟仍无 response/output/deliverables，只写出 execution context；最终人工停止并把 attempt 标记 abandoned，task 标记 `BLOCKED/manual_stop_after_unlimited_hung`。
  - 中文解释：不限制超时能防误杀，但如果底层模型请求或工具循环卡住，父级现在只能靠外部观察判断卡点。
  - Fix first slice: `subagent_debug_trace_level=3` 现在记录 runner 阶段心跳：`runner_model_request_started`、`runner_model_response_received`、`runner_model_request_failed`、`runner_tool_call_started`、`runner_tool_call_finished`。中文解释：以后再遇到无限等待，先看 debug trace 就能知道卡在“问模型”“模型回来了但没进工具”“工具开始后没结束”哪一段。
  - Verification first slice: focused stub tests 已覆盖模型请求/响应、模型异常和工具调用事件；尚未用真实 MiniMax 重跑大型 E2E。
  - Real smoke: `runner_stage_trace_20260510_145432` 用 MiniMax-M2.7 真实跑通 level 3 trace。事件计数为 `runner_model_request_started=4`、`runner_model_response_received=4`、`runner_tool_call_started=3`、`runner_tool_call_finished=3`、`runner_result_recorded=1`；产物 `/Users/example/my-终端应用/deliverables/runner_stage_trace_20260510_145432/leaf_outputs/proof/proof.txt` 内容为 `runner-stage-trace-ok`。
  - New finding: 这次真实 smoke 也暴露 `spawn-subagents --count 1` 会把测试 root 建成 `worker`，没有创建/调度 child 的工具；模型最后直接写了 proof.txt 并正确标记 `BLOCKED`，原因是缺少创建子代理能力。中文解释：trace 没问题，但这个入口不适合测试“主节点自己拉起子节点”；下一步需要补一个正式的 root/coordinator 创建入口或 spawn role 参数，再重跑层级 smoke。
  - Fix root seed: `spawn-subagents` 现在支持 `--role coordinator --agent-name <name>`。显式 coordinator seed 会创建真正 root/coordinator，带调度/看板、读取工具和报告写入工具；单个 root 不追加 `/ 子任务1`，避免真实 E2E prompt 失真。
  - Follow-up:
    - 需要把“无限 runner 长时间无 response 文件/无工具事件”纳入 due-check，可提示人工诊断或受控取消，而不是静默挂起。
    - 需要用新的 root/coordinator seed 重跑层级 smoke，确认主节点创建子代理、子代理创建孙代理、孙代理创建孙孙代理。

## 2026-05-10 Root/Child/Grandchild/Leaf Guard Smoke

- Test scene:
  - Case id: `coordinator_seed_20260510_153852`.
  - Workspace: `/Users/example/my-终端应用`.
  - Deliverables proof: `/Users/example/my-终端应用/deliverables/coordinator_seed_20260510_153852/leaf_outputs/proof/proof.txt`.
  - Model name: `MiniMax-M2.7`.
- 中文说明：
  - 这次从新的 root/coordinator seed 开始，外层只启动 root，后续必须由 root/child/leaf 自己推进。
  - leaf 实际写出了 `proof.txt=coordinator-seed-ok`，说明“真正的 root/coordinator 入口”可用。
- Finding:
  - leaf 和 child 已经写出/读回正确产物，但 board 一度把 child/leaf 标成 `BLOCKED / FAILED`。
  - Root cause: 真实模型输出了 `artifacts`，但没有输出 `evidence_packets`；父级验收需要 traceable evidence chain，因此把 artifact-only 输出当成证据不足。
  - 中文解释：文件是真的写对了，但“证据包”少了，系统为了严格验收没有直接信模型自述。
- Fix:
  - `result_structured.py` 现在会在 runner 有 artifact refs 但没有 evidence packets 时，合成 refs-only artifact evidence packet。
  - 合成证据只包含 artifact ref 和简短 claim，不读取 artifact 正文，不扩大 token。
- Verification:
  - Red test first failed because `evidence_packets` stayed empty.
  - After the fix:
    - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_result_processors_edges.py::test_process_structured_output_synthesizes_artifact_evidence_packet` -> `1 passed`.
    - Result-processor and acceptance focused regression -> `57 passed`.
- Status:
  - Solved for artifact-only successful outputs.
  - Remaining risk: synthetic evidence is intentionally lower confidence (`0.5`); higher-value tasks should still prefer explicit worker evidence packets or parent tests.

## 2026-05-10 Four-Level Hierarchy Smoke Before Guard

- Test scene:
  - Case id: `hierarchy4_20260510_154710`.
  - Workspace: `/Users/example/my-终端应用`.
  - Deliverables proof: `/Users/example/my-终端应用/deliverables/hierarchy4_20260510_154710/leaf_outputs/proof/proof.txt`.
  - Model name: `MiniMax-M2.7`.
- 中文说明：
  - 这次测试 4 层：root -> child coordinator -> grandchild coordinator -> leaf。
  - proof 最终写对了，内容是 `hierarchy4-ok`，但中间暴露了层级职责被拍平的问题。
- Finding 1: child coordinator created a grandchild coordinator and an extra leaf in the same schedule call.
  - Symptom: 最终 board 变成 5 个节点，而不是预期 4 个节点。
  - 中文解释：child 一次性建了“下一层领导”和“叶子工人”，这等于绕过 grandchild，让层级结构变扁。
  - Fix: `hierarchy_scope_guards.py` 新增 `mixed_coordinator_leaf_children`，同一次 `schedule_child_subagents` 不能同时创建 coordinator/lead 和 leaf/leaf_worker。
  - Prompt update: runner contract 明确告诉 coordinator：同一次 schedule 不要混建 coordinator 和 leaf；遇到该阻断时先只创建下一层 coordinator。
- Finding 2: empty test report was treated like a rescue case even when artifact evidence existed.
  - Symptom: artifact-only root/coordinator 没有可执行 tests，`test_execution.json` total=0 failed=0；父级一度按 rescue 路径处理。
  - 中文解释：协调节点不一定自己有 pytest；它可能只需要检查孩子和产物 refs。空测试不应该天然等于失败。
  - Fix: Parent Acceptance Controller now treats `total=0 failed=0` as `inspect_only` when no executable tests were declared and traceable artifact/evidence refs exist.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_parent_acceptance_controller.py::test_parent_acceptance_plan_inspects_empty_report_with_traceable_artifact_evidence` -> `1 passed`.
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_subagent_hierarchy_scheduler.py::test_hierarchy_schedule_blocks_mixed_coordinator_and_leaf_children` -> `1 passed`.
  - Combined hierarchy/prompt/acceptance regression -> `45 passed`.
- Status:
  - Solved for the two observed control-plane issues.
  - This run itself was manually accepted after the fix; the next section records the clean guard retest.

## 2026-05-10 Four-Level Hierarchy Guard Retest

- Test scene:
  - Case id: `hierarchy4_guard_20260510_155814`.
  - Workspace: `/Users/example/my-终端应用`.
  - Runtime config: `/Users/example/my-终端应用/.my-agent-hierarchy4-guard-smoke.yaml`.
  - Deliverables proof: `/Users/example/my-终端应用/deliverables/hierarchy4_guard_20260510_155814/leaf_outputs/proof/proof.txt`.
  - Model name: `MiniMax-M2.7`.
  - Real execution mode: outer controller only spawned and ran root coordinator; all lower nodes were created by their direct parent.
- 中文说明：
  - 这次严格按用户要求测试：外层只启动 root；root 创建 child；child 创建 grandchild；grandchild 创建 leaf；leaf 才能写最终文件。
  - 这是为了验证以后大型树不能由主控脚本偷懒直接批量创建叶子节点。
- Observed hierarchy:
  - Root: `subagent-1778399922-67cd2afa`.
  - Child coordinator: `subagent-1778399946-8df2b558`.
  - Grandchild coordinator: `subagent-1778399990-28fd53a5`.
  - Leaf: `subagent-1778400040-1f584546`.
- Final result:
  - Board summary: `DONE=4`, `VERIFIED=4`, `hot=0`.
  - Proof file content: `hierarchy4-guard-ok`.
  - No extra sibling leaf was created beside the grandchild coordinator.
  - Root final acceptance decision: `inspect_only`, then explicit `subagents-acceptance-plan <root> --apply` accepted it.
- Trace observations:
  - `debug_traces/subagent_trace.jsonl` showed each `task_created` event came from the direct parent.
  - The leaf used `write_file` and `read_file`; upper coordinators stayed in scheduling/read/board roles.
  - One coordinator tried a few invalid `read_artifact` calls before using board/file refs; this did not break the run, but suggests future prompt/tool docs can make artifact refs easier to consume.
- Status:
  - Passed for the 4-level strict hierarchy smoke.
  - Remaining gap: next real E2E should scale the same rule to larger trees, especially 1 root / 4 child / 16 grandchild / 48 great-grandchild leaf and the shopping-site project scenario.

## Ongoing Real E2E Difficulty Ladder

- 中文说明：
  - 后续测试要不断增加难度和意外情况，但当前真实层级最多先测到 4 层：主 -> 子 -> 孙 -> 孙孙。
  - 代码不能写死 4 层；`max_depth` 只是测试/运行约束，底层数据结构仍按通用 tree 处理。
- Next test categories:
  - Normal 4-level chain: root creates child, child creates grandchild, grandchild creates great-grandchild/leaf, leaf writes deliverables.
  - State feedback: every layer must report created child ids, running ids, blocked ids, done ids, next action, and takeover/recovery refs.
  - Failure injection: parent timeout with unfinished child, child runner blocked, stale RUNNING node, model emits wrong role, model repeats dispatch, empty tests, invalid artifact refs, test failure, acceptance failure.
  - Loop protection: repeated task-creation guard, max children, max depth, direct-child dispatch scope, self-exclude, and progress-loop tools that can repeat safely.
  - Prompt budget: main agent keeps a lightweight template index; coordinator nodes load full template details only when dispatching or scheduling children.
- Current status:
  - Template index/detail split is covered by focused tests, not yet by a new real MiniMax E2E.
  - The next real run should combine this lazy template behavior with a small 4-level chain before scaling back to `1/4/16/48` or shopping-site E2E.

## 2026-05-10 Role Template Observer / Coordinator Report Write E2E

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/role_template_observer_20260510_181700`.
  - Debug trace: `/Users/example/my-终端应用/.my_agent_runtime/role_template_observer_20260510_181700/subagents/debug_traces/subagent_trace.jsonl`.
  - Model name: `MiniMax-M2.7`.
  - Observation rule: outer controller only started the main agent/root seed; lower nodes were created by their direct parent.
- 中文说明：
  - 这次专门看“主代理是否能只启动 root，然后由 root 自己创建不同角色的子代理”。
  - 还验证一个现实问题：coordinator 不能只读，它也需要写自己的计划、分工、证据和协调报告，否则别人看不到它的协调产物。
- Observed behavior:
  - Root run: `subagent-1778408334-bae4a61f`.
  - Root created 6 direct children itself through `schedule_child_subagents`: researcher, worker, writer, bug_finder, tester, and acceptor.
  - `dispatch_subagents` now returned `runner_created_children=6`, child ids, and child roles, so the main agent no longer has to guess from the number of dispatch records.
- Finding 1: Dispatch output needed child refs, not just dispatch records.
  - Symptom: before the fix, the parent could confuse “4 dispatch records” with “4 children created by the runner”.
  - 中文解释：调度报告有几行，不等于下级真的建了几个。父级需要看到真实 child ids 和 roles，才能继续 watch、接管或验收。
  - Fix: dispatch records and tool payloads now carry `runner_created_children`, `runner_created_child_ids`, and `runner_created_roles`.
  - Verification: focused tests cover both direct dispatch payload and persisted dispatch record summaries.
- Finding 2: Coordinator and QA roles need report-write tools.
  - Symptom: the old coordinator template had no write tools but was asked to write `evidence.json`; worker/writer then tried to write parent/root evidence files and hit write-boundary blocks.
  - 中文解释：coordinator 的“产物”不是业务代码，而是协调报告；tester/bug_finder/acceptor 的“产物”是测试/找错/验收报告。没有写报告能力，下级就会乱帮它写，反而破坏边界。
  - Fix: all built-in role templates now include read tools and task-local report-write tools. Coordinator prompts now say: write your own coordination reports, but delegate final business code/pages/docs to worker/writer.
  - Boundary: children still cannot freely write parent directories; task-local write roots remain the hard boundary, and parent acceptance remains the final gate.
- Finding 3: Partial success plus timeout still needs cleaner status semantics.
  - Symptom: the old-template root created all 6 children but eventually timed out after 240 seconds; the parent saw the child refs and reported useful progress, but the runner record itself remained `ok=false`.
  - 中文解释：它已经成功“生孩子”了，但最后总结没收口，所以状态看起来像失败。后续要把“已创建下级但最终总结超时”单独表达成 partial success，便于恢复。
  - Status: recorded as a remaining gap. The next real E2E should rerun with the new report-write templates and confirm root writes its own report and stops faster.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_subagent_role_templates.py agent_py_agent/tests/test_subagent_prompt_contract.py agent_py_agent/tests/test_subagent_mixin.py::TestSubagentMixinSpawn::test_spawn_subagents_explicit_coordinator_creates_root_run agent_py_agent/tests/test_subagent_hierarchy_scheduler_tool_roles.py agent_py_agent/tests/test_orchestration_tools.py::TestRunnerDispatchRecords::test_runner_dispatch_record_carries_created_child_summary agent_py_agent/tests/test_orchestration_tools.py::TestDispatchSubagentsToolExecute::test_dispatch_payload_exposes_runner_created_children` -> `22 passed`.

## 2026-05-10 Role Template Report-Write Boundary Retest

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/role_template_report_write_20260510_190201`.
  - Clean deliverables root: `/Users/example/my-终端应用/deliverables/role_template_report_write_20260510_190201`.
  - Config: `/Users/example/my-终端应用/.my-agent-role-template-report-write-20260510_190201.yaml`.
  - Model name: `MiniMax-M2.7`.
  - Observation rule: the test controller only started the main agent/root seed. Root created direct children through its own runner.
- 中文说明：
  - 这轮继续测角色模板：root 自己创建 researcher、worker、writer、bug_finder、tester、acceptor 六类孩子。
  - 核心要看两件事：每个报告型角色能不能写自己的报告；报告型角色会不会误拿最终产物目录。
  - 真实事实以 `task.json`、debug trace、runner reports 和文件系统为准，不能只信主模型最后的自然语言总结。
- Observed hierarchy:
  - Root: `subagent-1778410985-d82d0df8`.
  - Direct children created by root:
    - `subagent-1778411047-b800886d` researcher.
    - `subagent-1778411048-acb88122` leaf_worker.
    - `subagent-1778411048-e4eb14b3` writer.
    - `subagent-1778411048-f6ce1b83` bug_finder.
    - `subagent-1778411048-8b190ff2` tester.
    - `subagent-1778411048-23b22567` acceptor.
- Observed files:
  - Root wrote its coordination report under its own task directory: `ROOT_REPORT.md`.
  - Worker wrote task-local product/report files under its own task directory.
  - Writer wrote task-local text/report files under its own task directory.
  - The clean deliverables root stayed empty in this run.
- Finding 1: Main-agent final prose hallucinated child ids and paths.
  - Symptom: the final natural-language summary named child ids like `subagent-1778410985-*`, but real trace/task ids were `subagent-1778411047-*` and `subagent-1778411048-*`.
  - 中文解释：模型最后“口头总结”会编错 id 或路径；系统判断必须看结构化 refs，而不是看一句总结。
  - Status: recorded. Next reporting improvement should render final progress from structured child refs instead of free-form model text.
- Finding 2: Explicit root coordinator seed accepted model-added shell/web tools.
  - Symptom: the main model passed `run_command` / `fetch_url` into a root coordinator seed.
  - 中文解释：root coordinator 只应该负责派工、看板、读资料和写协调报告，不应该因为模型多写了工具名就拿到 shell/web 权限。
  - Fix: explicit root/coordinator seed now filters model-supplied tools back to the built-in coordinator tool bundle.
- Finding 3: Report-only roles inherited product write roots.
  - Symptom: researcher/tester/bug_finder/acceptor/coordinator can write reports, but they should not inherit the final deliverables root.
  - 中文解释：会写报告不等于能写最终业务产物。报告放自己的工单目录；业务产物仍交给 worker/writer/leaf_worker。
  - Fix: hierarchy scheduler now separates report-write capability from product-write authority. Report/check/accept/research/coordinator roles keep task-local write roots only; worker/writer/leaf_worker can inherit product write roots. Product paths may remain visible as delegation/test context, but write authority is enforced by `allowed_write_roots`.
  - 2026-05-11 update：这个旧结论被后续真实恢复需求推翻。现在的原则改为“上层权限覆盖下层”，coordinator/tester/reviewer 也保留产物根，方便检查、接管和救援；但角色职责仍要求它们优先写报告、把实际业务产物交给 worker/writer/leaf_worker。
- Remaining gaps:
  - Partial success plus root timeout still needs clearer status semantics. Root created all 6 children but finished as `TIMEOUT / UNVERIFIED`.
  - Not all report-only children executed before the root timed out; a later run should confirm tester/bug_finder/acceptor each write their own task-local reports.
  - Parent/main reporting should use structured refs and task state rather than model-synthesized final prose.

## 2026-05-10 Role Template Boundary Retest After Policy Fix

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/role_template_boundary_retest_20260510_193834`.
  - Config: `/Users/example/my-终端应用/.my-agent-role-template-boundary-retest-20260510_193834.yaml`.
  - Model name: `MiniMax-M2.7`.
  - Observation rule: outer controller only started main/root; no direct child manipulation.
- 中文说明：
  - 这次想复测刚修好的写入边界，但先暴露了一个更上游的问题：main 的第一次 `create_subagents` 工具调用里，有效 JSON 后多了一个 `}`。
  - 解析失败后，模型自己反复缩短 goal，最后 root 只拿到“角色模板边界复测”这个空泛目标。
  - 结果 root 只能写本地运行文件和读看板，没有足够上下文去创建 6 类 child。
- Observed facts:
  - Created root: `subagent-1778413205-25742fa3`.
  - Root task state stayed `RUNNING / UNVERIFIED`.
  - `child_ids=[]`; only one real subagent task directory existed.
  - Root `allowed_write_roots` was task-local only, so the report-write boundary itself held.
  - Debug trace showed model/tool rounds with `list_files` / `read_file` / `read_artifact`, but no `schedule_child_subagents`.
- Finding: tool-call parse failure caused goal degradation.
  - Symptom: the first full `create_subagents` payload contained the correct long goal, but parser rejected it because a trailing `}` made JSON look like `Extra data`.
  - 中文解释：不是 root 不会派工，而是 root 一开始就没拿到完整任务；模型为了修 JSON，把任务内容越删越短。
  - Fix: `tooling/json_repair.py` now narrowly repairs a valid JSON object followed only by extra right braces. It does not swallow a second object or arbitrary broken JSON.
  - Verification: `test_tool_call_parser_recovers_single_extra_trailing_brace` covers the exact MiniMax-style extra-brace shape.
- Status:
  - This E2E run was stopped after the issue was identified to avoid burning API on a known upstream parser problem.
  - Next real retest should rerun the same scenario and confirm root receives the full goal, then creates real researcher/worker/writer/bug_finder/tester/acceptor children.

## 2026-05-10 Role Template Boundary Retest After Parser Fix

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Runtime roots:
    - `/Users/example/my-终端应用/.my_agent_runtime/role_template_boundary_retest_20260510_195010`.
    - `/Users/example/my-终端应用/.my_agent_runtime/role_template_boundary_retest_20260510_200613`.
  - Clean deliverables roots:
    - `/Users/example/my-终端应用/deliverables/role_template_boundary_retest_20260510_195010`.
    - `/Users/example/my-终端应用/deliverables/role_template_boundary_retest_20260510_200613`.
  - Model name: `MiniMax-M2.7`.
  - Observation rule: outer controller only started main/root; root created direct children through its own runner.
- 中文说明：
  - 这轮确认 parser 修复后，main 不再把完整 goal 越改越短；root 能拿到完整任务并真实创建 6 类 direct child。
  - 同时继续查当时的权限边界：coordinator/researcher/bug_finder/tester/acceptor 只拿 task-local 写根，worker/writer 才能写最终产物目录。这个结论在 2026-05-11 后被“上层权限覆盖下层”新原则替代。
- Observed facts:
  - Parser fix worked: root `goal_preview` kept the full deliverables path and six-role instructions.
  - Run `role_template_boundary_retest_20260510_195010` created 6 children, but exposed that explicit root/coordinator still inherited the final deliverables root.
  - Fix: explicit root/coordinator seed now keeps product paths in `goal` as delegation context but strips `extra_write_roots`, so root cannot write final deliverables directly.
  - Run `role_template_boundary_retest_20260510_200613` then verified root `allowed_write_roots` contained only its own task directory.
  - The same run created six children with corrected roles:
    - `researcher`, `bug_finder`, `tester`, and `acceptor` had task-local-only write roots under the old policy.
    - `leaf_worker` and `writer` had task-local plus the deliverables root.
    - Worker wrote product files under `/Users/example/my-终端应用/deliverables/role_template_boundary_retest_20260510_200613`.
- Finding 1: root/coordinator needs path context, not product write authority.
  - Symptom: root knew the deliverables path and therefore inherited it as an allowed write root.
  - 中文解释：root 要知道产物目录在哪里，才能派工和验收；但“知道路径”不等于“自己能写最终产物”。
  - Fix at that time: explicit root/coordinator create-run params set `extra_write_roots=[]`; the path remained in goal so child worker/writer could inherit it through scheduler policy.
  - 2026-05-11 update：这个边界也调整了。root/coordinator 现在会保留产物写根，原因是上层必须能覆盖下层权限，才能在下级挂掉、路径写错或需要接管时直接检查和恢复。系统用角色职责和验收链约束“不要乱写”，而不是让上层完全没权限。
- Finding 2: real models may use `role=child` and put the real role in `agent_name`.
  - Symptom: one root scheduled all six children with generic `role=child` and names like `researcher`, `writer`, `tester`; before the fix, report-only children could inherit product write roots because the policy could not see their real role.
  - 中文解释：模型经常把结构化字段填得不够标准，但名字里已经说明了真实角色。系统要兜底识别，不能因为一个泛化 role 就放大权限。
  - Fix: hierarchy scheduler now recovers concrete role ids from `agent_name` / goal identity when `role` is `child` / `general` / placeholder. This normalizes researcher/tester/acceptor/bug_finder/writer/worker before write-root policy runs.
- Remaining gaps:
  - Root still timed out after creating children, so final root summary remains partial; this is a runner completion/status-semantics issue, not a write-boundary failure.
  - Some report-only children were still `PLANNING` or `RUNNING` when root timed out; later E2E should focus on runner progress/heartbeat, fair dispatch waves, and partial-success finalization.
  - Child runner still sometimes probes missing paths and reads repeated artifacts; this belongs to prompt/tool-use quality tuning, not permission enforcement.
- Follow-up fix:
  - Runner-context `dispatch_subagents` now adds machine-readable direct-child continuation fields when direct children remain `PLANNING` or `RUNNING`: `needs_more_dispatch`, `unfinished_run_ids`, `next_action=continue_dispatch_direct_children`, and `suggested_tool_call`.
  - 中文解释：以后父节点看到“还有孩子没跑完”时，不只看到一句提示，而是能拿到明确下一步工具调用建议，减少 root 超时后不知道怎么继续。
  - Runner dispatch records now also preserve `runner_child_status_counts`, `runner_unfinished_child_ids`, and `runner_partial_success` when a runner times out after creating children.
  - 中文解释：以后 root 自己超时也不会只留下一个“TIMEOUT”；父级/恢复流程还能看见它已经创建了哪些孩子，哪些孩子没收口。
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_orchestration_coordinator_seed_tools.py agent_py_agent/tests/test_subagent_hierarchy_write_policy.py` -> `5 passed`.
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_orchestration_dispatch_child_refs.py` -> `5 passed`.
  - Real MiniMax run `role_template_boundary_retest_20260510_200613` verified the corrected write roots from `task.json` and debug trace.

## 2026-05-10 Role Template Continue Dispatch Retest

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/role_template_continue_retest_20260510_204242`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/role_template_continue_retest_20260510_204242`.
  - Root run: `subagent-1778416997-d49ebeb8`.
  - Model name: `MiniMax-M2.7`.
  - Observation rule: outer controller only seeded root/coordinator and then dispatched that root; all six direct children were created by root itself.
- 中文说明：
  - 这轮验证两个问题：第一，CLI 显式创建 root/coordinator 时，旧策略要求 root 只能写自己的协调目录；第二，root 看到还有 child 没跑完时，能不能根据 `needs_more_dispatch` / `unfinished_run_ids` 继续调度下一波。2026-05-11 后第一点已改为上层保留覆盖权限。
  - 外层没有替 root 创建 researcher/worker/writer/bug_finder/tester/acceptor；这些直接子代理都来自 root 的 `schedule_child_subagents`。
- Observed hierarchy:
  - `researcher`: `subagent-1778417047-c0133cf5` -> `DONE / VERIFIED`.
  - `leaf_worker`: `subagent-1778417047-54945bfd` -> `DONE / VERIFIED`.
  - `writer`: `subagent-1778417047-8ac14739` -> `DONE / VERIFIED`.
  - `tester`: `subagent-1778417047-c2208e30` -> `BLOCKED / FAILED`.
  - `acceptor`: `subagent-1778417047-e2cc4f33` -> `BLOCKED / FAILED`.
  - `bug_finder`: `subagent-1778417047-595d2f54` -> `AWAITING_ACCEPTANCE / NEEDS_ACCEPTANCE`.
- Observed deliverables:
  - `product/role_template_demo/__init__.py`.
  - `product/role_template_demo/calculator.py`.
  - `product/role_template_demo/test_calculator.py`.
  - `product/README.md`.
  - No subagent runtime files were written into the product directory; `.DS_Store` may appear at the macOS deliverables root and is not a my-agent runtime artifact.
- Finding 1: CLI root/coordinator seed still had a separate write-root path.
  - Symptom: the first continue retest exposed that `spawn-subagents --role coordinator` used `_extract_write_dirs(goal)` directly and could grant root the product path.
  - 中文解释：代码路径有两个入口。模型工具入口已经修过，但 CLI 入口还会从 goal 里自动提取产物目录，导致 root 知道路径的同时也拿到写权限。
  - Fix: `spawn_explicit_role_runs()` now keeps product paths in the root goal for delegation context, but sets `extra_roots=[]` for explicit root/coordinator roles.
  - Verification: the rerun root `allowed_write_roots` contained only its own task directory, while the product path still remained in the goal so worker/writer could receive it from scheduler policy.
- Finding 2: continue-dispatch fields worked in a real MiniMax runner.
  - Symptom before the fix: root could stop after the first limited dispatch wave and leave some direct children in `PLANNING`.
  - 中文解释：`max_runners` 限制会让一次 dispatch 只跑一部分孩子。root 需要机器可读字段告诉它“还有谁没跑，下一步继续 dispatch”，不能只靠自然语言猜。
  - Observed behavior: root first dispatched part of the six children, inspected board/artifacts, then called `dispatch_subagents` again. The second wave started `acceptor` and `bug_finder`, proving `needs_more_dispatch` / `unfinished_run_ids` can guide real continuation.
- Finding 3: checker roles correctly found a real product bug.
  - Symptom: direct product pytest failed with `ModuleNotFoundError: No module named 'calculator'`.
  - 中文解释：worker 写出了包，但测试文件用了 `from calculator import ...`。从包目录外执行 pytest 时，这个导入路径不稳定；tester、bug_finder 和 acceptor 都抓到了这个问题。
  - Manual verification: `python3 -m pytest -q -p no:cacheprovider /Users/example/my-终端应用/deliverables/role_template_continue_retest_20260510_204242/product/role_template_demo` -> failed during collection with the same import error.
- Remaining gaps:
  - Root still timed out after starting the second dispatch wave, so root final summary is missing. The recovery refs are present, but next work should improve partial-success finalization or root timeout recovery.
  - Acceptance safety still treats some pytest shell shapes as high-risk when they contain shell chaining or imprecise command text. The long-term fix should normalize safe pytest commands inside allowed working roots, not loosen shell execution globally.
  - The chain currently detects the worker bug but does not yet automatically create a repair worker and re-run acceptance; that belongs to the next rescue/follow-up loop.
- Follow-up fix:
  - Parent acceptance dry-run and manual auto-execution now both reuse `prepare_test_items()` before command safety checks or execution.
  - 中文解释：真实模型经常同时写 `cd <目录> && pytest ...` 和 `working_dir`。系统现在会在确认目录位于 workspace 内后，把它拆成 `working_dir=<目录>` 加 `python3 -m pytest ...` 纯命令；仍然不放开 shell，不允许任意 `&&`、`;`、管道或越界目录。
  - Verification: focused tests cover both the non-mutating parent preflight path and the explicit `ParentAcceptanceAutoExecutionOptions(execute_tests=True)` path.

## 2026-05-10 Context Bundle Single-Subagent E2E

- Test scene:
  - Command: `python3 -m agent_py_agent scenario-test --case happy --workspace /tmp/my_agent_context_bundle_e2e_real --count 1 --max-runners 1 --max-cycles 2 --direct`.
  - Run root: `/private/tmp/my_agent_context_bundle_e2e_real/scenario-20260510-220517-1b2a68`.
  - Root flow: main agent created one subagent, dispatch ran the runner, parent acceptance verified it.
  - Run id: `subagent-1778421942-b122db0e`.
- 中文说明：
  - 这轮只验证“主节点创建和推进子代理时，Context Bundle 是否真实进入 runner 路径”，没有绕过主节点直接替子代理干活。
  - dry-run 版本也验证过 bundle 生成，但 scenario 脚本因为 dry-run 不进入 VERIFIED 会返回 `SCENARIO_FAIL`；非 dry-run 场景使用内置 scenario backend 完成并返回 `SCENARIO_PASS`。
- Observed facts:
  - Legacy run dir wrote `context_bundle.json` and `CONTEXT_BUNDLE.md`.
  - Agent run workspace mirrored the same files under `tasks/<root_id>/agents/<run_id>/`.
  - `EXECUTION_CONTEXT.md` exposes both legacy and agent-run context bundle refs.
  - `runner_prompt.md` includes `## Context Bundle Gate` and `Context Gate: PASS`.
  - Both bundle JSON files report `schema_version=subagent_context_bundle.v1`, `gate.ok=true`, and `missing_fields=[]`.
- Remaining gaps:
  - Context Gate v1 currently guides the runner prompt; it does not yet hard-stop dispatch before runner invocation.
  - Bundle source refs are task-field based; later phases should add compact/resume/daily-ledger refs when those are the real source of a field.

## 2026-05-10 Context Bundle Multilevel / Recovery Control Tests

- Test scene:
  - Focused control-plane tests, not a fresh external-model E2E.
  - Commands covered:
    - `python3 -m pytest -q agent_py_agent/tests/test_subagent_context_bundle.py agent_py_agent/tests/test_subagent_takeover_readiness.py`
    - `python3 -m pytest -q agent_py_agent/tests/test_subagent_hierarchy_recovery.py agent_py_agent/tests/test_subagent_hierarchy_cli_e2e.py`
- 中文说明：
  - 这轮先把“多层代理怎么拿到父级交接包”和“挂了以后接管者先读什么”做成确定性测试。
  - 它不是新的 1/4/16/48 真实模型大跑；真实大跑下一轮仍必须只启动 root，让 root 自己创建 child，child 自己创建 grandchild，grandchild 自己创建 leaf。
- Observed facts:
  - 每个 `context_bundle.json` 现在有 `lineage`，包含 root、parent、depth、自己的 bundle ref 和直接父级 bundle ref。
  - 四层 root / child / grandchild / great-grandchild fixture 中，每一层 child 都能通过 ref 找到直接父级的 `tasks/<root_id>/agents/<parent_id>/context_bundle.json`。
  - `takeover_readiness.json` 的推荐读取顺序现在包含 context bundle refs，位于 checkpoint / artifact manifest 前面。
  - hierarchy recovery-tree 节点现在直接暴露 `context_bundle_ref` 和 `parent_context_bundle_ref`，用于失败、阻塞、超时和父超时残留 child 的恢复定位。
- Remaining gaps:
  - 这一步只保证 refs 和文件落点正确；还没有验证真实模型在大任务里会稳定按 lineage ref 阅读父级交接包。
  - 后续真实测试需要用主节点单入口方式重跑小树，再逐步放大到 1/4/16/48 和购物网站项目。

## 2026-05-10 Context Lineage Trace5 Four-Level Smoke

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/context_lineage_trace5_nostream_20260510_225330`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/context_lineage_trace5_nostream_20260510_225330`.
  - Debug trace: `subagents/debug_traces/subagent_trace.jsonl`, with level 5 detail refs under `subagents/debug_traces/details/`.
  - Model name: `MiniMax-M2.7`, `stream_enabled=false`.
  - Observation rule: the outer controller only dispatched root; root created child, child created grandchild, grandchild created leaf.
- 中文说明：
  - 这轮专门验证刚加的 trace level 5 是否能看到“主给子、子给孙、孙给叶子”的真实提示词、工具参数和工具输出。
  - 外层没有替叶子写文件；leaf 没成功写出 `proof.txt`，所以这条是暴露问题的失败烟测，不是通过烟测。
- Observed facts:
  - Root created child `subagent-1778424848-a10d7506`.
  - Child created grandchild `subagent-1778424901-e17137dc`.
  - Grandchild created leaf `subagent-1778424923-cffb5c5c`.
  - Trace level 5 recorded full prompt/response/tool payload/tool output refs, which made the blocker directly visible.
- Finding 31: leaf blocked because `acceptance_checks` was missing.
  - Symptom: leaf received a clear goal to write `proof.txt=context-lineage-ok`, but Context Bundle Gate returned `BLOCKED` with `missing_fields=acceptance_checks`.
  - 中文解释：模型把“要写什么”写进了 goal，但没有额外填结构化验收项。系统之前太死板，看到 acceptance 空就让叶子停住。
  - Root cause: `schedule_child_subagents` accepted omitted `acceptance_checks` and persisted the child task with an empty list; Context Gate then correctly treated the bundle as incomplete.
  - Fix: hierarchy scheduler now derives minimal acceptance checks from child goal and role when the model omits them. For leaf/write tasks it emphasizes target artifact existence and goal consistency; for coordinator tasks it emphasizes child ids, status, next step, and evidence/artifact refs.
  - Verification: `python3 -m pytest -q agent_py_agent/tests/test_subagent_hierarchy_scheduler.py::test_hierarchy_schedule_derives_acceptance_checks_when_model_omits_them` -> passed.
- Finding 32: max-tool-round final response could still contain a tool call.
  - Symptom: after tool rounds reached the configured limit, the model still emitted another `subagent_board` / `read_artifact` style tool call during the final collection turn.
  - 中文解释：系统已经说“不能再用工具了”，但模型还想继续查；这种内容不能再当成最终答案，否则上层会被误导。
  - Fix: `_tool_loop_service` now gives the model one final response chance after max rounds; if that response still contains a tool call, it returns a deterministic stop message and executes no more tools.
  - Verification: `python3 -m pytest -q agent_py_agent/tests/test_tools/test_tool_loop.py::test_max_tool_rounds_hard_stops_when_model_still_requests_tools` -> passed.
- Remaining gaps:
  - Need rerun the four-level smoke after the fixes to verify leaf now writes the proof file through the proper parent-created chain.
  - The root tried to read a child context bundle before child execution context existed; this is not fatal, but schedule payloads should eventually expose clearer “context bundle available after dispatch” refs.
  - MiniMax Anthropic-compatible streaming returned an empty stream in one prior attempt; this E2E used non-stream mode. Streaming should be tested separately before becoming default for deep subagent chains.

## 2026-05-10 Context Lineage Trace5 Content-Check Retest

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - First fixed runtime root: `/Users/example/my-终端应用/.my_agent_runtime/context_lineage_trace5_override2_20260510_232744`.
  - Passing runtime root: `/Users/example/my-终端应用/.my_agent_runtime/context_lineage_trace5_contentcheck_20260510_234017`.
  - Passing deliverables root: `/Users/example/my-终端应用/deliverables/context_lineage_trace5_contentcheck_20260510_234017`.
  - Root run: `subagent-1778427653-19a3726d`.
  - Model name: `MiniMax-M2.7`, `stream_enabled=false`, `subagent_debug_trace_level=5`.
  - Observation rule: the outer controller only seeded/dispatched root; root created child, child created grandchild, grandchild created leaf.
- 中文说明：
  - 这轮复测严格按“只观察 root，不替下层干活”的方式跑。leaf 自己通过 `write_file` 写出 `proof.txt`，外层只看 board、dispatch report 和 debug trace。
  - level 5 trace 能看到每层模型 prompt、模型回复、工具 payload 和工具输出的 detail ref，方便定位“谁创建了谁、谁传了什么提示词、谁调用了什么工具”。
- Observed facts:
  - Failing-first retest `context_lineage_trace5_override2_20260510_232744` proved leaf could write `proof.txt=context-lineage-ok`, but root acceptance failed because runner output recommended `cat <proof.txt>` and the parent test executor correctly rejected `cat` as non-allowlisted.
  - Focused rerun on the same root after the fix:
    - `subagents-tests subagent-1778426892-0587c5a8 --re-run --timeout 120` -> `total=1 executed=1 passed=1 failed=0`, method=`content_check`.
  - Full rerun `context_lineage_trace5_contentcheck_20260510_234017` completed the chain:
    - root `subagent-1778427653-19a3726d`.
    - child `subagent-1778427689-78c18f36`.
    - grandchild `subagent-1778427721-63ec70f1`.
    - leaf `subagent-1778427774-dc843624`.
    - final dispatch summary: runner OK, acceptance OK.
  - Final artifact exists at `/Users/example/my-终端应用/deliverables/context_lineage_trace5_contentcheck_20260510_234017/proof.txt` with exact text `context-lineage-ok`.
- Finding 33: model-style `cat file` content checks should not loosen command safety.
  - Symptom: root produced a parent test command `cat /Users/example/my-终端应用/deliverables/.../proof.txt`; the bounded executor rejected it as `测试命令不在 allowlist 内: cat`.
  - 中文解释：产物文件是真的对，但验收方式不对。不能为了这一个场景把 `cat` 放进命令白名单，因为 `cat` 可以读大文件或无关文件。
  - Fix: `prepare_test_items()` now rewrites safe, workspace-local `cat <file>` checks into `validation_method=content_check` when an explicit or narrow inferred expected content exists. The executor reads the workspace-local file through its file validation path instead of running `cat`.
  - Extra guard: `content_check` now supports `content_equals` / `expected_content` plus `match_mode=exact`, so exact file contracts do not pass when extra characters or newlines are present.
  - Prompt fix: runner contract now tells models to use `content_check` for file-content validation and not to write `cat` commands.
- Finding 34: coordinator completion override works for tool-limit cleanup, but prompt pressure remains.
  - Symptom: child/root coordinators still sometimes continue reading board/artifact refs after all direct children are already `DONE/VERIFIED`.
  - 中文解释：任务已经完成了，但模型还想“再确认一下”。系统现在能兜底收口，不会因此误判失败；但后续还要继续让 coordinator 更早停止，减少模型调用和 token。
  - Verification: both `context_lineage_trace5_override2_20260510_232744` and `context_lineage_trace5_contentcheck_20260510_234017` reached acceptance after direct children completed even when the model tried extra tool calls near the max-tool boundary.
- Verification:
  - `python3 -m pytest -q agent_py_agent/tests/test_subagent_test_item_preparation.py::test_prepare_test_items_converts_cat_content_assertion_to_content_check` -> passed.
  - `python3 -m pytest -q agent_py_agent/tests/test_subagent_test_executor.py::test_test_executor_content_check_supports_exact_match` -> passed.
  - Real MiniMax rerun `context_lineage_trace5_contentcheck_20260510_234017` -> root dispatch acceptance passed.
- Remaining gaps:
  - Coordinator prompt/tool-use quality still needs tuning so parent nodes stop after direct child verification instead of repeatedly reading artifacts.
  - Streaming mode for Anthropic-compatible MiniMax remains unverified for deep chains; keep non-stream mode for this E2E until a separate streaming smoke passes.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511`.
  - Root run: `subagent-1778434074-6b6d17c5`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
  - Observation rule: the outer controller only seeded and dispatched root; root created child coordinators, child coordinators created or attempted to create their own descendants.
- 中文说明：
  - 这轮按用户要求做购物网站级真实烟测，目标是让层级代理自己拆出前端、后端、认证、购物车、结账等工作；外层只观察 root，不直接替子/孙/叶子干活。
  - 这不是全绿交付，而是暴露问题的 Stage7 第一轮。当前已经证明写保护、trace、board、child 创建和部分 leaf 写文件链路真实发生。
- Observed facts:
  - Root created `frontend-lead` and `backend-lead`.
  - `frontend-lead` created `auth-coord` and `shop-coord`.
  - `shop-coord` recovered from product write denial and created `products-leaf`.
  - `products-leaf` wrote `deliverables/stage7_shop_smoke_20260511/frontend/products/list.html`.
  - `auth-coord` and `backend-lead` became `BLOCKED`; board surfaced failure handoff and takeover readiness refs.
  - The dispatch command was stopped after trace stopped advancing for several minutes during a leaf model request.
- Finding 35: coordinator product-write denial needs to become delegation, not self-grant.
  - Symptom: report-only coordinators tried to write files under `deliverables/stage7_shop_smoke_20260511/...`; write boundary correctly rejected them because their `allowed_write_roots` only allowed task-local reports.
  - 中文解释：协调员本来应该“派人干活”，不是自己去写最终页面。系统挡住写入是对的；问题是模型有时不知道下一步该创建 leaf，而是说要申请权限或让父代理代写。
  - Root cause: the role prompt and schedule tool spec did not state strongly enough that this specific denial is the intended coordinator boundary and should be handled by `schedule_child_subagents`.
  - Fix: coordinator runner prompt, coordinator role template, and `schedule_child_subagents` tool description now state that denied final-product writes must be delegated to worker/writer/leaf_worker, with paths, filenames, and acceptance checks passed through exactly. Coordinators should not request product write grants for themselves and should not ask the parent to direct-write product files.
  - Verification: `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_subagent_prompt_contract.py agent_py_agent/tests/test_subagent_role_templates.py` -> passed.
- Finding 36: large `write_file` tool-call payloads inflated the next live prompt.
  - Symptom: after `products-leaf` generated a large HTML `write_file` call, the next runner prompt grew to about 61k chars and the model request stopped producing trace progress.
  - 中文解释：叶子写 HTML 时，上一轮模型回复里包含整段 HTML。系统以前把这整段工具调用原文塞回下一轮 prompt，等于让模型反复背自己刚写的大文件，慢且容易卡。
  - Root cause: tool output externalization only handled tool results, not assistant tool-call payloads. `ToolLoopService` appended the full assistant response to `tool_context` before executing parsed calls.
  - Fix: added `tool_call_context_reducer.py`. Large assistant tool-call payloads are now summarized for live prompt as tool name, path, field sizes, sha256, and short preview. The full content remains in the target artifact/debug detail refs, not in every subsequent prompt.
  - Verification: `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_tool_output_externalizer.py` -> passed.
- Remaining gaps:
  - Need rerun Stage7 shopping smoke after the reducer/prompt fix, starting again from a clean deliverables/runtime directory.
  - Need reach a usable shopping site path: register -> login -> product list/detail -> cart -> checkout -> success, with no dead buttons and no broken image references.
  - Need decide whether Stage7 should use a bounded runner timeout during smoke tests even if user default remains `off`, so one stalled model call cannot block the whole overnight test harness.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R2

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r2.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r2`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r2`.
  - Root run: `subagent-1778436330-1cfc37a4`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
  - Observation rule: the outer controller only seeded and dispatched root; root created direct coordinators, and coordinators created their own workers/leaves.
- 中文说明：
  - 这轮复测了上一轮的“coordinator 不应自写产物”和“write_file 大 payload 摘要”修复。
  - 系统真实跑出 root -> 4 个直接 coordinator -> 4 个 worker/leaf 的链路，并产出 6 个页面；仍未达到购物网站完整可用。
- Observed facts:
  - Root created `auth-lead`, `catalog-lead`, `cart-checkout-lead`, and `quality-lead`.
  - `auth-lead` created an auth leaf and produced `index.html`, `login.html`, and `register.html`.
  - `catalog-lead` created a catalog leaf and produced `product-list.html`, `product-detail.html`, and `category.html`.
  - `cart-checkout-lead` created a worker, but that worker blocked on missing deliverables write permission.
  - `quality-lead`, `catalog-lead`, `cart-checkout-lead`, and root timed out under the 480s smoke-test harness limit; board produced failure handoff and takeover readiness refs.
  - External validation found missing cart/checkout/order pages and broken static refs such as `cart.html` and literal `${product.image}`.
- Finding 37: worker spec paths must participate in write-root grants.
  - Symptom: `cart-checkout-worker` goal explicitly contained `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r2/build`, but `allowed_write_roots` only contained its task-local runtime directory.
  - 中文解释：子代理自己已经把“我要写到哪里”说清楚了，但 scheduler 只看父节点继承来的路径，没有看 child spec 自己写出来的路径，所以真正干活的 worker 没拿到产物目录写权限。
  - Root cause: `_create_child()` built `requested_write_roots` from `spec.extra_write_roots` or `inherited_extra_write_roots(parent)`, but not from `_extract_write_dirs(spec.goal)`.
  - Fix: `requested_child_write_roots()` now merges explicit roots, inherited roots, and child spec goal paths. Authorization remains role-gated: report/coordinator roles still stay task-local, but worker/writer/leaf_worker can receive the deliverable root they explicitly need.
  - Verification: `test_hierarchy_schedule_grants_worker_path_written_by_child_spec` covers the real cart-worker pattern.
- Finding 38: `_record_tool_call` also needed payload reduction.
  - Symptom: prompts still reached about 60k-70k chars in R2 even after assistant tool-call response summarization.
  - 中文解释：上一轮只压了“模型回复里的工具调用原文”，但工具执行记录里还会再写一遍 `payload`，也就是 `write_file` 的整段 HTML 又从另一个门塞回 prompt。
  - Root cause: `_record_tool_call()` appended `record.payload` directly into `tool_context`; large failed or successful `write_file` payloads bypassed `tool_call_context_reducer`.
  - Fix: `render_tool_payload_for_live_prompt()` now summarizes large recorded tool payloads too, keeping tool name, path, sizes, hash, and short preview while omitting the full body.
  - Verification: `test_tool_call_record_summarizes_large_payload_for_live_prompt` covers the blocked `write_file` path.
- Finding 39: quality validation needs real browser/static-link checks before acceptance.
  - Symptom: generated catalog pages included broken static references: `cart.html` did not exist, query-string links were naively checked as files, and literal `${product.image}` appeared in `src`.
  - 中文解释：部分页面已经写出来，但不能算购物网站可用；下一轮必须让质量/验收子代理用真实规则检查链接、按钮、图片和核心流程，不能只看“文件存在”。
  - Status: detected and documented; not solved in this patch.
- Remaining gaps:
  - Rerun Stage7 R3 from a clean runtime/deliverables after the write-root and payload fixes.
  - Add or strengthen parent-side static site validation so missing pages, literal template placeholders, and broken links/images block acceptance.
  - Improve coordinator stop behavior after partial child success so root can summarize or hand off cleanly instead of timing out while descendants are already done/blocked.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R3

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r3.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r3`.
  - Root run: `subagent-1778437464-84570584`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明：
  - 这轮没有等到完整购物网站完成，而是在 root 已经创建 4 个 coordinator 后主动停下，专门抓调度偏移和工具上下文问题。
  - 外层仍只观察 root；没有直接替 auth/catalog/cart/quality 或它们的孩子写代码。
- Observed facts:
  - Root 创建了 `auth`、`catalog`、`cart-checkout`、`quality` 四个直接 coordinator。
  - Root 想先推进 auth/catalog，但 dispatch 没有精确 run id 入口，实际先跑到了 cart/quality。
  - Cart coordinator 因显式 allowed_tools 漏掉 `schedule_child_subagents`，无法继续创建 worker。
  - Quality coordinator 过早执行，开始检查未完成产物和 runtime 目录，扩大了 prompt 和噪声。
  - 模型有时把上一轮 live prompt 里的 `[tool-call-*]` 记录和 dict payload 复制进新的 `[TOOL_CALL]`，导致 parse error。
- Finding 40: coordinator explicit tools must preserve orchestration tools.
  - Symptom: model created a coordinator with `allowed_tools=["write_file","read_file","list_files"]`; scheduler honored it too literally, so the coordinator no longer had child creation/dispatch tools.
  - 中文解释：协调员哪怕模型只给了读写工具，也仍然必须能继续派下一层。否则它就变成“有计划但没有派工按钮”的节点。
  - Fix: coordinator specs now merge explicit tools with the built-in coordinator tool pack, so `schedule_child_subagents` / `dispatch_subagents` / `subagent_board` stay available.
  - Verification: `test_hierarchy_schedule_preserves_coordinator_orchestration_tools`.
- Finding 41: parent runners need exact dispatch targeting.
  - Symptom: root intended to run auth/catalog first, but generic dispatch selected other ready children from the same parent queue.
  - 中文解释：主节点已经点名“先跑这两个孩子”，调度器却只能说“跑一批孩子”，所以队列顺序会把工作带偏。
  - Fix: `dispatch_subagents` accepts `run_ids` / `include_run_ids`; runner candidates are filtered to those ids and executed in the given order. Progress payload suggested calls now include `run_ids` for unfinished direct children.
  - Verification: `test_dispatch_exact_run_ids_are_passed_to_params` and `test_scoped_runner_tasks_honors_include_run_ids_order`.
- Finding 42: live tool-context labels must not look like tool calls.
  - Symptom: models copied `[tool-call-*]` context snippets and dict-like payloads into new tool-call blocks, producing parse errors.
  - 中文解释：以前历史记录长得太像“可复制的工具调用”，模型容易照抄。现在历史记录改成中性标签和摘要行，减少误触发。
  - Fix: live context markers changed to `[tool-record ...]` / `[tool-output-record ...]`, and dict payloads render as summary lines instead of Python dict repr.
  - Verification: `test_render_tool_payload_keeps_small_payload_readable`.
- Remaining gaps:
  - Rerun Stage7 R4 from a clean runtime/deliverables after exact `run_ids` dispatch and coordinator-tool preservation.
  - Validate whether root now runs auth/catalog first, then cart/quality, instead of letting quality inspect unfinished output too early.
  - Continue toward complete shopping flow validation: register, login, product list/detail, cart, checkout, order success, no broken buttons or image refs.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R4

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r4.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r4`.
  - Root run: `subagent-1778438905-2339a519`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明：
  - R4 验证了精确 dispatch 和 coordinator 工具保留后，root 能继续创建 4 个一级 coordinator，auth coordinator 能再创建 leaf_worker 并写出 `auth.html`。
  - 这轮在发现 catalog URL 写入根误判后主动停止，没有宣称购物网站完整完成。
- Observed facts:
  - Root 创建了 auth、catalog、cart-checkout、quality 四个直接 coordinator。
  - Auth 分支创建 leaf_worker，并产出 `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r4/build/auth.html`。
  - Catalog coordinator 尝试创建 product-list/product-detail worker 时，goal 里的 `https://picsum.photos/...` 和 `https://images.unsplash.com/...` 图片 URL 被路径提取器误判成写入根，导致 scheduler 拒绝创建任务。
  - Quality coordinator 仍然过早启动，在完整产物不存在时开始读取 runtime/tree 和 auth.html，prompt 增长到约 50k。
- Finding 43: write-root extraction must ignore URLs.
  - Symptom: `schedule_child_subagents` rejected a catalog worker with `target=s://picsum.photos/` even though the intended deliverable path was under the workspace.
  - 中文解释：这不是模型想写到外网，而是我们把图片 URL 里的 `https://` 错当成了本地路径，等于把正常图片地址误报成越权写入目录。
  - Root cause: Windows path regex matched the `s:/` fragment inside `https://...`; Unix path regex could also see URL host/path fragments as `/host/path`.
  - Fix: `_extract_write_dirs()` now records URL spans and skips any local path candidate that overlaps a URL. The Windows pattern also requires the drive letter not to be inside a word.
  - Verification: `test_ignores_url_paths_when_extracting_write_dirs` and `test_hierarchy_schedule_ignores_url_image_sources_in_write_roots`.
- Remaining gaps:
  - Rerun R5 from a clean runtime/deliverables to verify catalog worker creation is no longer blocked by image URLs.
  - Add dependency/phase gating so quality does not run before required producer children have at least attempted or produced deliverables.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R5

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r5.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r5`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r5`.
  - Root run: `subagent-1778440060-6046f213`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明：
  - R5 继续按“外层只观察 root”的方式测试；root 自己创建 coordinator，auth coordinator 自己创建 leaf。
  - Auth leaf 真实写出了 `register.html`、`login.html`、`auth.css`、`auth.js`，说明 coordinator -> leaf -> 产物写入这条链路可用。
  - 这轮也暴露出两个新结构问题，所以主动停止，没有宣称购物网站完整可用。
- Observed facts:
  - Root 第一次创建了 auth、catalog、cart-checkout、quality 四个 coordinator；随后又重复创建了 checkout 和 quality-checker 两个同域 coordinator。
  - Auth coordinator 创建了 auth leaf，auth leaf 写入 `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r5/build` 下的 4 个文件。
  - Catalog 分支仍在旧进程里触发了图片 URL 写入根误判，并转成 capability request；后续新进程应使用已修正的 URL span 过滤。
  - Leaf 的 `artifact_refs` 指向 deliverables 产物时，takeover manifest 把它们标成 `blocked_outside_workspace`，虽然真实文件已经存在。
- Finding 44: same-parent coordinator domains need dedupe.
  - Symptom: one root created both `cart-checkout-coordinator` and `checkout-coordinator`, and both `quality-coordinator` and `quality-checker`.
  - 中文解释：同一个老板不能因为模型多说了一次，就重复招两批做同一块的人；不然孩子越来越多，调度会膨胀，也会让验收顺序混乱。
  - Fix: hierarchy scheduling now checks existing same-parent coordination-style children and blocks overlapping domains such as `checkout` or `quality`. The guard is limited to coordinator/checker/tester/reviewer style roles so multiple real worker/leaf tasks are not accidentally blocked.
  - Verification: `test_hierarchy_schedule_blocks_duplicate_coordinator_domains`.
- Finding 45: artifact manifests must trust task allowed write roots.
  - Symptom: auth leaf reported absolute artifact paths under its granted deliverables directory, but manifest resolution only trusted task/run workspace roots and marked them outside workspace.
  - 中文解释：叶子已经被允许把最终页面写到 deliverables，那么接管包也应该能登记这些文件的元数据；否则父级会看到“文件不存在/越界”，但磁盘上其实有文件。
  - Fix: artifact manifest resolution now includes `task.allowed_write_roots` as safe metadata roots. It still stores only path/size/hash/status and never copies file bodies into memory.
  - Verification: `test_subagent_persistence_resolves_allowed_product_artifacts` and existing outside-workspace blocking test.
- Remaining gaps:
  - Rerun R6 from a clean runtime/deliverables with the duplicate-domain guard and allowed-artifact-root fix active.
  - Add producer/quality phase gating so quality/checker runs after auth/catalog/cart workers have produced or explicitly failed.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R6

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r6.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r6`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r6`.
  - Root run: `subagent-1778441176-aab44813`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明：
  - R6 继续遵守“外层只观察 root”的真实测试方式；外层没有替下层创建 leaf，也没有替它们写页面。
  - Root 正确只创建了 auth、catalog、cart-checkout 三个直接生产 coordinator，没有再重复创建 checkout/quality 分支，说明 R5 的同域去重生效。
  - 这轮主动停止，因为暴露出多 runner dispatch 的共享指令污染问题；不能把这个错误路径继续跑成购物网站验收。
- Observed facts:
  - Root 对 3 个 coordinator 使用同一次 `dispatch_subagents(run_ids=[auth,catalog,cart], execute_runners=true)`。
  - 这次调用同时传入了 `runner_instruction="你是 auth-coordinator..."`。
  - Auth、catalog、cart-checkout 三个 runner prompt 都收到了同一段 auth 专属 `Extra Instruction`。
  - Cart-checkout coordinator 因此创建了 `auth-leaf`，并让它写出了 `register.html` / `login.html`；这是子代理自己在错误上下文下执行的结果，不是外层代写。
  - Cart coordinator 第一次尝试 `dispatch_subagents(execute_runners=true, apply=false)` 被安全门拒绝；这是正确保护，但提示词需要更明确告诉 coordinator 跑 child 时用 `apply=true`。
- Finding 46: multi-run dispatch must not broadcast task-specific runner instruction.
  - Symptom: 一个 auth 专属补充指令被广播给 catalog/cart 分支，导致下层节点身份串线。
  - 中文解释：主节点想“一口气叫三个人开工”，但又附带了一句“你是 auth”。结果三个人都听成了“我是 auth”，购物车分支也去写登录注册。这种问题会让越往下的孙代理越跑偏。
  - Root cause: dispatch runner batch used one shared `effective_runner_instruction` for every selected runner, whether the batch contained one child or many unrelated children.
  - Fix: when a dispatch batch contains more than one pending runner and a shared `runner_instruction`, dispatch now clears that instruction before launching workers and records `ignore_multi_runner_instruction` in the dispatch report. Single-run dispatch still keeps the instruction.
  - Prompt/spec update: `dispatch_subagents` docs now say task-specific `runner_instruction` is for a single `run_id`; multiple children need separate dispatch calls or child goal/context-bundle fields. Coordinator prompt also now spells out `dispatch_subagents(apply=true, execute_runners=true)` for running direct children.
  - Verification: `test_dispatch_parallel_runner_pool_does_not_broadcast_specific_instruction` and `test_dispatch_single_runner_keeps_specific_instruction`.
- Remaining gaps:
  - Rerun R7 from a clean runtime/deliverables to verify multi-run dispatch no longer contaminates child identity.
  - Continue producer/quality phase gating so quality/test/acceptance starts after auth/catalog/cart have produced or explicitly failed.
  - Add full static shopping-site validation: missing pages, `${...}` placeholders, dead links, dead images, and inert buttons must block acceptance.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R7

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r7.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r7`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r7`.
  - Root run: `subagent-1778442411-6c83288e`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明：
  - R7 继续按“外层只观察 root”的方式测试；root 自己创建 auth、catalog、cart-checkout 三个直接 coordinator，下层 coordinator 自己创建 leaf/worker。
  - R6 的共享 `runner_instruction` 串线没有复发：auth/catalog/cart 的 runner prompt 都保持自己的任务身份，没有再把 auth 身份广播给其他分支。
  - Auth 分支完成并写出注册/登录相关文件；catalog 分支创建了 config/home/product 相关 leaf 并开始产出；cart 分支暴露出新的 run_id 传递失真问题，因此主动停止。
- Observed facts:
  - Root 创建 `auth-lead`、`catalog-lead`、`cart-checkout-lead` 三个一级 coordinator。
  - Auth coordinator 创建 `auth-worker`，auth worker 写出 `auth.js`、`app.js`、`register.html`、`login.html`，并进入 `DONE/VERIFIED`。
  - Catalog coordinator 后续创建 `config-writer`、`home-page-writer`、`product-page-writer`，写出 `config.js`、`api.js`、`home.html`、`product.html`。
  - Cart-checkout coordinator 创建了真实 child `subagent-1778442674-65f43222`，但后续把它抄成不存在的 `subagent-1778442548-65f43222`。
  - 因为错误 id 没有被工具边界明确阻断，cart coordinator 继续读不存在的 task/run workspace 和 artifact refs，prompt 增长到约 70K。
- Finding 47: scoped runner dispatch must block invalid child ids.
  - Symptom: coordinator 把 direct child run id 的时间戳前缀记错，dispatch/list/read 继续围绕不存在的 id 空转。
  - 中文解释：父节点已经有一个真实孩子，但它把身份证号码抄错了一段。系统以前只是“查不到”，没有立刻告诉它“你抄错了，这些才是你的孩子”，所以它越查越乱、越读越大。
  - Root cause: `include_run_ids` 只用于候选过滤；当 id 不存在或不在当前 parent/root scope 内时，过滤结果为空，但 dispatch report 没有返回明确的 `invalid_run_ids` 阻断记录和可用 direct child ids。
  - Fix: runner dispatch now preflights explicit `include_run_ids`; if any requested id is missing/out-of-scope, it returns a `runner_selection/invalid_run_ids` record, blocks runner execution for that call, lists `valid_scope_run_ids`, and adds conservative `possible_corrections` when a wrong id shares a unique short suffix with a visible child.
  - Verification: `test_dispatch_blocks_invalid_scoped_run_id_with_valid_child_hint`.
- Follow-up guard fix:
  - Full pytest exposed that same-parent duplicate-domain detection treated generated run-id fragments in goals like `subagent-...-fc60` as business domains and could block generic `grand-1` / `grand-2` checker siblings.
  - 中文解释：去重是为了挡“又创建一个 checkout coordinator”，不是为了挡“两个编号不同的 checker”。现在数字/id 片段和 `grand/one/two` 这类泛词不会当成业务域。
  - Verification: `test_hierarchy_schedule_allows_generic_numbered_checker_siblings` plus hierarchy recovery packet tests.
- Remaining gaps:
  - Rerun R8 from a clean runtime/deliverables to verify wrong-id feedback lets the coordinator retry with the exact child id instead of looping.
  - Add producer/quality phase gating so QA/test/acceptance starts only after required producer branches have produced, failed, or explicitly handed off.
  - Add full static shopping-site validation: required pages, no `${...}` placeholders, no dead local links/src, no inert core buttons, and a register -> login -> browse -> cart -> checkout -> order success flow.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R8

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r8.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r8`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r8`.
  - Root run: `subagent-1778444752-9b01d744`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明：
  - R8 继续按“外层只观察 root”的真实测试方式跑；外层没有替 cart 创建 leaf，也没有替任何 leaf 写页面。
  - R7 的错误 id 阻断至少让错误 dispatch 没有直接跑错孩子，但 cart coordinator 仍然把 `build` 猜成了 sibling 目录 `stage7_r8_build`，随后围绕错误目录和错误 refs 继续读。
  - Auth/catalog 两个分支可以完成并写出页面；cart 分支因路径漂移和错误恢复信息可见性不足主动停止，没有宣称购物网站完整可用。
- Observed facts:
  - Root 创建 `auth-coordinator-r8`、`catalog-coordinator-r8`、`cart-checkout-coordinator-r8` 三个一级 coordinator。
  - Auth coordinator 创建 auth worker，auth worker 写出 `register.html` 和 `login.html`，进入 `DONE/VERIFIED`。
  - Catalog coordinator 创建 catalog leaf，catalog leaf 写出 `products.html` 和 `product-detail.html`，进入 `DONE/VERIFIED`。
  - Cart coordinator 先从父级 goal 得到正确目标 `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r8/build`，但后续模型回复中出现了伪造/漂移的历史工具输出，把目录改成 `stage7_r8_build`。
  - Cart coordinator 创建 leaf 时把错误 sibling 目录写入 child goal；这类路径漂移以前会落盘成一个真实 child，后续即使 dispatch id 被拦住，也会继续把模型注意力拉到错误目录。
  - `read_artifact` 还暴露出一个恢复体验问题：模型把已登记 artifact path 的前缀抄成了 `/Users/example/my_agent/...`，导致读取失败；真实 artifact 在 `/Users/example/my-终端应用/memory_archive/...`。
- Finding 48: child write roots must stay anchored to inherited product roots.
  - Symptom: coordinator 把父级给的 `build` 目录改写成 sibling `stage7_r8_build`，然后把错误路径继续传给 leaf。
  - 中文解释：父节点说“成果放在 build 盒子里”，孩子自己猜了一个旁边的新盒子。以前系统只看“这个新盒子也在工作区里”，没有检查它是不是父节点指定的那个成果盒子。
  - Fix: `hierarchy_scope_guards.py` now blocks child specs whose `goal` or `extra_write_roots` contain local paths outside the inherited product roots, when the parent already has authoritative product roots. The block returns `child_write_root_drift` with invalid and valid roots so the coordinator must rewrite the child goal before any child is created.
  - Verification: `test_hierarchy_schedule_blocks_sibling_path_drift` and `test_hierarchy_schedule_allows_child_path_under_parent_root`.
- Finding 49: artifact reads should recover copied-prefix typos without weakening the index boundary.
  - Symptom: model copied a registered tool-output artifact filename but changed the workspace prefix, so `read_artifact` could not find it.
  - 中文解释：身份证号码的后半截和登记表完全一样，但地址前缀抄错了。只要登记表里这个文件名唯一，我们可以安全地找回真正登记的那条记录。
  - Fix: `artifact_reader.py` now falls back to a unique registered artifact basename when a path-like `artifact_ref` misses exact path/hash/call-id lookup. It still reads only paths already present in `tool_outputs/index.jsonl` and still verifies the file remains under the trusted artifact directory.
  - Verification: `test_read_artifact_tool_repairs_wrong_prefix_with_unique_artifact_name`.
- Finding 50: board/dispatch recovery facts must appear before bulky record bodies.
  - Symptom: invalid-run-id and board facts existed, but large tool outputs were externalized/truncated before the model saw the most actionable ids.
  - 中文解释：系统已经知道“你该用哪个孩子 ID”，但这句话埋在一大堆报告后面，模型看到的是一截摘要，容易继续猜。
  - Fix: `dispatch_subagents` now puts `runner_selection_recovery` at the top of the tool payload when run-id selection is blocked. `subagent_board` now puts compact `actionable_run_ids` before item rows and clips long goals, so refs remain visible earlier in the prompt.
  - Verification: focused orchestration and worker-pool tests remained green.
- Remaining gaps:
  - Rerun R9 from a clean runtime/deliverables to verify path-drift blocking makes cart coordinator rewrite the child goal back to `/build`.
  - Add producer/quality phase gating so QA/test/acceptance starts only after required producer branches have produced, failed, or explicitly handed off.
  - Add static shopping-site validation: required pages, no `${...}` placeholders, no dead local links/src, no inert core buttons, and a register -> login -> browse -> cart -> checkout -> order success flow.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R9

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r9.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r9`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r9`.
  - Root run: `subagent-1778446763-af4105e4`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明：
  - R9 仍按“外层只观察 root”的方式跑。外层只创建并运行 root，没有替 root 创建下级，也没有写业务产物。
  - Root 正确创建了 auth、catalog、cart-checkout、quality 四个一级 coordinator，说明 R8 的 path-drift guard 没有挡住合法 `/build` 目标。
  - 随后 root 没有直接使用 `schedule_child_subagents` 返回的真实 `created_run_ids`，而是猜了旧格式 id 去 dispatch；dispatch 已阻断并返回正确 ids，但 root 又用 `subagent_board(status="ALL")` 查板，旧过滤逻辑把 ALL 当成真实状态，返回 0 条，导致恢复信息不足。
- Observed facts:
  - Created valid children:
    - `subagent-1778446829-04f1bee3` / `auth-coordinator`
    - `subagent-1778446829-73872e47` / `catalog-coordinator`
    - `subagent-1778446829-79924b55` / `cart-checkout-coordinator`
    - `subagent-1778446829-22e8faee` / `quality-coordinator`
  - Invalid dispatch ids guessed by root: `subagent-1778446775-6f1fa8a5`, `subagent-1778446776-9f7a0bc3`, `subagent-1778446777-c2a2d4e6`, `subagent-1778446778-d3e3f5a7`.
  - Dispatch output already contained `direct_children.suggested_tool_call.run_ids`, but board follow-up returned `returned=0` because of `status="ALL"`.
- Finding 51: board `status=ALL` must mean no filter.
  - Symptom: root asked for all board rows with `status="ALL"` and got zero rows.
  - 中文解释：模型说“给我全部”，系统却按“状态名必须等于 ALL”来筛选。结果明明有 4 个孩子，看板像空的一样。
  - Fix: `orchestration_board_payload.board_status_filter()` now treats `ALL`, `*`, `ANY`, and empty values as no filter.
  - Verification: `test_status_all_keeps_board_items`.
- Finding 52: invalid-run-id recovery needs machine-readable ids.
  - Symptom: correct run ids were present in a Chinese message and task refs, but root still copied guessed ids.
  - 中文解释：让模型从一大段中文提示里抠 id 太脆。恢复包应该直接给 `valid_run_ids` 这种机器能照抄的字段。
  - Fix: `runner_selection_recovery` now includes `valid_run_ids` extracted from evidence task refs, while still keeping `valid_task_refs`.
  - Verification: `test_dispatch_payload_exposes_recovery_valid_run_ids`.
- Remaining gaps:
  - Rerun R10 from a clean runtime/deliverables to verify root uses `valid_run_ids` / `actionable_run_ids` and actually runs the four child coordinators.
  - Continue producer/quality phase gating so QA/test/acceptance starts only after producers produce, fail, or hand off.
  - Add static shopping-site validation: required pages, no `${...}` placeholders, no dead local links/src, no inert core buttons, and a register -> login -> browse -> cart -> checkout -> order success flow.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R10

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r10.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r10`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r10`.
  - Root run: `subagent-1778447294-8a2fa764`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明：
  - R10 继续按“外层只观察 root”的方式跑；外层没有替下层创建 coordinator/leaf，也没有替它们写购物网站文件。
  - Root 能创建 4 个一级 coordinator，并把 auth/catalog/cart 分支推进到真实产物写入，说明 R9 的 run id / board 恢复可见性改进有效。
  - 这轮主动停止，因为暴露出两个新的结构问题：quality 分支抢跑，以及 root 后续绕过已有 coordinator 直接创建 leaf。
- Observed facts:
  - Root created four depth-1 coordinators:
    - `subagent-1778447377-944f6754` / `auth-coordinator`.
    - `subagent-1778447377-c019b103` / `catalog-coordinator`.
    - `subagent-1778447377-fcc28ded` / `cart-checkout-coordinator`.
    - `subagent-1778447378-96d98343` / `quality-coordinator`.
  - Real deliverables were written under `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r10/build`, including auth pages/assets, catalog pages/assets, cart/checkout pages, root `index.html`, order success page, and shared styles.
  - Before stopping, task status showed auth/catalog awaiting parent acceptance, cart-checkout done/verified, quality blocked/unverified, and root still running/unverified.
  - Root later created direct leaf workers under itself, including `catalog-detail-leaf`, `cart-leaf`, `auth-login-leaf`, `index-leaf`, `order-success-leaf`, `auth-register-leaf`, `catalog-list-leaf`, `css-leaf`, and `checkout-leaf`.
- Finding 53: quality/test runners need phase gating.
  - Symptom: `quality-coordinator` ran before producers had completed and tried to inspect partial/empty build state, then became `BLOCKED`.
  - 中文解释：验收员太早进场了，工人还没把货架搭完，它就开始检查“这里缺东西”。这不是验收能力问题，而是开工顺序问题。
  - Fix: runner candidate selection now computes role phase from role, agent name, and goal text, then releases only the current lowest phase per dispatch wave. Quality/test/review/acceptance candidates wait until producer/coordinator candidates are no longer runnable in that scope.
  - Verification: `test_runner_candidates_defer_quality_until_producers_finish`.
- Finding 54: root must not bypass the coordinator layer after delegation.
  - Symptom: after creating coordinator children, root directly scheduled many leaf/worker children under itself.
  - 中文解释：root 已经把“楼层经理”叫来了，后面就应该让楼层经理带自己的工人。root 不能又直接越过经理去招一堆叶子工人，不然层级会乱，恢复和验收也不知道谁负责谁。
  - Fix: hierarchy scope guard now returns `root_leaf_bypass_existing_coordinators` when a root with existing coordinator children tries to create leaf-like worker children. The guard tells the root to dispatch or repair its direct coordinator children first.
  - Verification: `test_root_with_coordinators_cannot_bypass_into_leaf`.
- Remaining gaps:
  - Rerun R11 from a clean runtime/deliverables to verify quality is delayed and root no longer creates direct leaf workers after coordinator delegation.
  - Add static shopping-site validation: required pages, no `${...}` placeholders, no dead local links/src, no inert core buttons, and a register -> login -> browse -> cart -> checkout -> order success flow.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R11

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r11.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r11`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r11`.
  - Root run: `subagent-1778449356-81c536f3`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明：
  - R11 继续按“外层只观察 root”的方式跑；外层只创建并运行 root，没有替 root 创建下级，也没有替任何 leaf 写页面。
  - R10 的两个修复点有效：quality 没有抢跑，root 也没有再绕过 coordinator 直接创建 leaf。
  - root -> coordinator -> worker/leaf 路径能写出一组真实购物网站页面，但静态链路和 blocked child 收束仍没闭环，所以主动停止 root 进程并保留证据。
- Observed facts:
  - Root first created three production coordinators: auth、catalog、cart-checkout；没有创建 quality/test/review 分支抢跑。
  - Auth coordinator created `auth-worker`; auth worker wrote `register.html` and `login.html`, then became `DONE/VERIFIED`.
  - Cart-checkout coordinator created `cart-page-leaf`、`checkout-page-leaf`、`order-success-page-leaf`; cart and checkout became `DONE/VERIFIED`, order-success became `BLOCKED/FAILED` with failure handoff and takeover readiness refs.
  - Catalog coordinator created `shop-html-worker`; it wrote `products.html` and `product-detail.html`, then became `DONE/VERIFIED`.
  - Final deliverables included `register.html`, `login.html`, `products.html`, `product-detail.html`, `cart.html`, `checkout.html`, and `order-success.html`.
  - A machine static check over the R11 build failed with `placeholder_hits=2; broken_local_refs=2`: `order-success.html` / `products.html` still contained `${...}`, and `cart.html` / `order-success.html` linked to missing `index.html`.
- Finding 55: generated static sites need deterministic parent-side validation.
  - Symptom: multiple leaf workers could report completion while the assembled site still had bad local links and `${...}` placeholders.
  - 中文解释：页面文件“写出来了”不等于“能正常点”。我们需要机器检查：哪些页面必须存在、链接是不是指向真实文件、有没有模板占位符、按钮是不是明显没动作。
  - Fix: added `static_site_check` to `TestExecutor`. It scans only workspace-local HTML, checks required files, local `href` / `src` / `action` refs, `${...}` placeholders, and obvious inert buttons/links. It does not execute JavaScript and does not fetch remote URLs.
  - Verification: `test_static_site_check_passes_valid_site`, `test_static_site_check_blocks_common_generated_site_failures`, `test_static_site_check_rejects_outside_site_root`, plus a real R11 static check that reported the expected failures.
- Remaining gaps:
  - Parent/coordinator still needs a rescue loop that can create a repair child from a blocked leaf handoff instead of staying RUNNING indefinitely.
  - Same-parent duplicate leaf scheduling should be tightened when an equivalent sibling is already `DONE/VERIFIED`.
  - Shopping-site workflows should inject `static_site_check` into parent acceptance automatically for static web deliverables.

## 2026-05-11 R11 Follow-Up Fixes

- Finding 56: direct child recovery must be visible in runner-context dispatch payloads.
  - Symptom: when a coordinator had DONE siblings plus a BLOCKED/FAILED child, the progress payload only exposed PLANNING/RUNNING as actionable. The parent could keep running without a crisp rescue next step.
  - 中文解释：孩子里有人卡住了，父节点不能只看“还有没有没跑完的人”。它还要看到“谁失败了，下一步该拿哪些 run_id 去重试或接管”。
  - Fix: `orchestration_progress_payload.py` now emits `recovery_run_ids`, `needs_recovery`, and an `inspect_or_rescue_direct_children` suggested dispatch call when direct children are BLOCKED/FAILED/TIMEOUT/CHANNEL_ERROR and no PLANNING/RUNNING child remains.
  - Verification: `test_progress_payload_surfaces_blocked_children`.
- Finding 57: completed leaf outputs need concrete target dedupe.
  - Symptom: after `auth-worker` had already written and verified `register.html` / `login.html`, the auth coordinator created another auth leaf for the same targets.
  - 中文解释：同一个父节点下面，已经有工人把同一批页面写完并验收了，就不该再叫一个新工人重复写同样文件。否则会浪费模型调用，还可能覆盖或制造冲突。
  - Fix: `hierarchy_scope_guards.py` now reads direct child metadata plus `output.json.artifacts` path refs and blocks new leaf specs whose concrete file names overlap a DONE/VERIFIED leaf. It does not read artifact bodies and does not block different file targets.
  - Verification: `test_hierarchy_schedule_blocks_duplicate_verified_leaf_targets`.
- Remaining gaps:
  - Auto-inject `static_site_check` for static web outputs so parent acceptance can catch link/placeholders without relying on model-authored tests.
  - Run a fresh root-only R12 shopping E2E after auto-injection and confirm the parent creates repair/rescue work instead of spinning.

## 2026-05-11 R11 Follow-Up Static Auto-Test Injection

- Finding 58: static Web checks should not depend on model-authored tests.
  - Symptom: R11 only exposed broken links and `${...}` placeholders after an external manual static check. If the runner forgot to declare the test, parent acceptance could miss the assembled-site defect.
  - 中文解释：模型写页面时经常会说“完成了”，但不一定自己写“检查所有链接和占位符”。这种基础验收应该由系统看 artifact 自动补上。
  - Fix: `execution_test_items.py` now appends an inferred `static_site_check` when `output.json.artifacts` contains multiple workspace-local HTML files and no existing static-site test. `execution_static_site_items.py` owns the inference and only reads path refs.
  - Verification: `test_prepare_test_items_infers_static_site_check_for_html_artifacts` and `test_prepare_test_items_does_not_duplicate_static_site_check`.
- Remaining gaps:
  - Run a fresh root-only R12 shopping E2E and confirm the inferred check appears in parent acceptance/test reports.
  - If R12 still leaves a blocked leaf, verify the new `recovery_run_ids` hint leads the parent toward retry/rescue instead of unbounded running.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R12/R13

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - R12 config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r12.yaml`.
  - R12 root run: `subagent-1778451668-d99a5adb`.
  - R13 config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r13.yaml`.
  - R13 root run: `subagent-1778453114-17d53dfd`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明：
  - R12/R13 继续按“外层只观察 root”的方式跑。外层只创建并运行 root，没有替 root 创建下级，也没有替任何 child/leaf 写购物网站文件。
  - R12 证明 root -> child coordinator -> 多个 leaf worker 可以由模型自己创建，但暴露了 leaf 写完 `output.json` 后还继续请求模型、导致长时间空转的问题。
  - R13 用修复后的 runner 复跑。leaf 写完购物网站产物后能进入父级验收，自动 `static_site_check` 也被注入并通过；但模型自报的一个“文件结构验证”测试格式不完整，曾误报失败。
- Observed facts:
  - R12 root created child coordinator `subagent-1778451860-30acda3c` / `shop-build-lead`, then the coordinator created four leaf workers for auth/products/cart/styles.
  - R12 base-styles worker tried to write `/Users/xiaoyezei/...` with a typo in the username path; write boundary correctly rejected the out-of-scope path.
  - R12 leaf workers wrote deliverables and `output.json`, but after the completion artifact they continued into another model/tool loop instead of finalizing quickly.
  - R13 root created child coordinator `subagent-1778453143-53fb6199` / `shop-build-lead`, and that coordinator created leaf `subagent-1778453364-f3dfcaae` / `shop-pages-builder`.
  - R13 deliverables were written under `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r13/build`, including eight HTML pages, `style.css`, `app.js`, and local SVG images.
  - R13 static site validation passed after rerun: `total=1 executed=1 passed=1 failed=0`.
- Finding 59: subagent runners should stop after a valid `output.json` completion artifact.
  - Symptom: R12 leaf workers wrote their final `output.json`, but the runner still asked the model for more work. This delayed parent acceptance and made healthy leaves look stuck.
  - 中文解释：孩子已经把“我做完了，这是结果单”放到桌上了，系统却还继续问它“你下一步干嘛”。这会浪费模型调用，也容易让父级误以为孩子没收尾。
  - Root cause: the tool loop treated `write_file(output.json)` like an ordinary file write and always started another model round to ask for final text.
  - Fix: `_tool_loop_service.py` now detects a successful write to the current subagent's own `task.output_json`, reads the JSON back, synthesizes a `[SUBAGENT_RESULT]...[/SUBAGENT_RESULT]` response, and stops the tool loop.
  - Verification:
    - `python3 -m pytest -q agent_py_agent/tests/test_tools/test_tool_loop.py::test_subagent_runner_stops_after_output_json_write -p no:cacheprovider`.
- Finding 60: malformed model-authored checklist tests must not override inferred machine checks.
  - Symptom: R13 inferred `static_site_check` passed, but a model-authored test named `文件结构验证` had `validation_method=content_check` without `file_path` or `content_pattern`, so parent acceptance reported `total=2 failed=1`.
  - 中文解释：真正的机器检查说“页面、链接、CSS/JS 引用都过了”，但模型自己写了一条空壳测试，系统把空壳测试当失败，造成假红。
  - Root cause: `prepare_test_items()` preserved malformed runner-declared test items even when it could infer a stronger deterministic static-site check from artifact refs.
  - Fix: when an inferred `static_site_check` is available, `execution_test_items.py` now drops non-executable model checklist items that lack the required command/file/content/site-root fields. If there is no machine fallback, malformed tests stay visible as conservative failure evidence.
  - Verification:
    - `python3 -m pytest -q agent_py_agent/tests/test_subagent_test_item_preparation.py -p no:cacheprovider`.
    - Real R13 rerun: `subagents-tests subagent-1778453364-f3dfcaae --re-run --timeout 120` -> `total=1 executed=1 passed=1 failed=0`.
- Remaining gaps:
  - R13 coordinator collapsed a medium web project into one large leaf (`shop-pages-builder`) instead of splitting auth/catalog/cart/style/test into multiple leaves. This is functional for the smoke test, but not ideal for speed or 48-leaf scale; role/template prompting should keep coordinators biased toward smaller independent leaves.
  - R14 should run from a clean runtime/deliverables with the two fixes above and verify the root can continue from child production into parent/coordinator acceptance without false rescue loops.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R14

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r14.yaml`.
  - Root run: `subagent-1778454295-9cdf2b5e`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明：
  - R14 继续保持“外层只观察 root”。外层只创建并运行 root，没有直接创建 lower agents，也没有替任何 leaf 写产物。
  - R14 证明“分支拆分”比 R13 好：root 创建 auth、catalog、cart-checkout、shared 四个一级 coordinator；这些 coordinator 再创建自己的 leaf。
  - R14 也暴露了单页静态产物的验收缺口：首页 leaf 只输出一个 `index.html` 加 CSS/JS，之前不会自动生成 `static_site_check`，导致模型写的空壳 `content_check` 被当成失败。
- Observed facts:
  - Root created four depth-1 production coordinators: `auth-coordinator`, `catalog-coordinator`, `cart-checkout-coordinator`, `shared-coordinator`.
  - Coordinators created depth-2 leaves such as `auth-leaf-worker`, `catalog-leaf-worker`, `cart-checkout-leaf-writer`, and `shared-leaf`.
  - Deliverables were written under `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r14/build`, including `index.html`, auth pages/assets, catalog pages, and shared assets before the run was stopped for the product fix.
  - Catalog leaf static validation correctly failed on a real generated-site bug: `placeholder_hits=1` in `product-detail.html`.
  - Shared leaf validation incorrectly failed on malformed model-authored test: `index.html 导航链接验证` used `content_check` but provided no `file_path`.
- Finding 61: single-page static outputs also need inferred static-site checks.
  - Symptom: `shared-leaf` produced one HTML page plus CSS/JS artifacts, but `execution_static_site_items.py` only inferred `static_site_check` when it saw at least two HTML files. The malformed runner-declared `content_check` therefore remained the only test and failed with `缺少 file_path`.
  - 中文解释：首页这种任务只有一个 HTML，也应该机器检查链接、CSS/JS 引用、占位符和按钮。之前系统只给“多页面网站”补静态检查，导致单页 leaf 又被模型空壳测试拖成假红。
  - Fix: `inferred_static_site_items()` now creates a `static_site_check` for one or more HTML artifact refs, not only multi-page outputs. When this inferred check exists, the existing malformed-checklist filter drops the empty `content_check`.
  - Verification:
    - `test_prepare_test_items_infers_static_site_check_for_single_html_artifact`.
- Remaining gaps:
  - R14 should be rerun after this single-page inference fix to verify `shared-leaf` no longer false-fails and catalog's real placeholder failure is still caught.
  - Parent/root-level acceptance still needs a whole-site required-file oracle so a split directory layout cannot satisfy child-local checks while missing top-level `build/products.html`, `build/cart.html`, and related user-facing routes.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R15

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r15.yaml`.
  - Root run: `subagent-1778454846-4ca5f7ae`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明：
  - R15 仍按“外层只观察 root”的方式跑；外层没有替 root 创建 child，也没有替任何 leaf 写购物网站文件。
  - R15 证明四个一级 coordinator 能被 root 创建，auth/catalog/shared 分支能产出页面或资源，但 cart-checkout 分支被底层 guard 和同轮工具依赖问题卡住。
  - 这次主动停止 root runner，保留失败样本；没有手工补购物车、结账和订单成功页。
- Observed facts:
  - `cart-checkout-coordinator-r15` 在同一模型响应里先请求 `schedule_child_subagents`，又在还没拿到真实 schedule 结果前请求 `dispatch_subagents`，并填入自己脑补的 run ids。
  - 真正的 `schedule_child_subagents` 被写入预检误拦截：购物车文案里的 `+/-按钮` 被识别成外部绝对路径 `/-按钮`。
  - 随后的 `dispatch_subagents` 用脑补 run ids 执行，返回 `runner_selection/invalid_run_ids`；真实子节点没有创建，`cart.html`、`checkout.html`、`order-success.html` 未生成。
  - `shared-assets-coordinator-r15` 还暴露了修复任务遇到 `max_children_exceeded` 后缺少更清晰恢复动作的问题。
  - `static_site_check` 继续有效，能抓到 `products.html` / `product-detail.html` 里的 `${...}` 占位符。
- Finding 62: UI text and HTML tags must not look like absolute write paths.
  - Symptom: `+/-按钮` and `</body>` inside page-generation instructions could be parsed as `/...` filesystem targets and rejected as outside workspace.
  - 中文解释：模型写“加号/减号按钮”或者“在 body 结束标签前插脚本”，这只是页面内容，不是要往 `/body` 或 `/-按钮` 这种系统路径写文件。
  - Fix: `orchestration_write_guard.py` excludes slashes immediately after `<` or `+` from absolute-path matching while preserving normal `/Users/...` path detection.
  - Verification: `test_ui_symbols_and_html_tags_do_not_trip_external_write_guard`.
- Finding 63: same-turn dependent orchestration calls must wait for real tool output.
  - Symptom: the model emitted `schedule_child_subagents` and `dispatch_subagents` in one response, then used hallucinated run ids before the scheduler had returned real `created_run_ids`.
  - 中文解释：孩子还没真的出生，模型就先给孩子编了身份证号，然后拿假身份证去开跑。系统不能执行这种“依赖上一个工具返回值”的同轮后续调度。
  - Fix: `tool_round_execution.py` executes the first stateful orchestration call (`create_subagents` / `schedule_child_subagents`) and defers later dependent orchestration tools in the same assistant turn. The next model turn must read the real `created_run_ids` / `actionable_run_ids` before dispatching.
  - Verification: `test_tool_round_defers_dependent_dispatch_after_schedule`.
- Finding 64: orchestration wrapper JSON remains part of the supported tool-call dialect.
  - Symptom: real runners often emit `{"tool":"schedule_child_subagents","orchestration":{...}}`.
  - 中文解释：模型喜欢把参数装进一个 `orchestration` 包里。这个写法应该继续被展开成真正工具参数，不要求模型每次都完全扁平。
  - Verification: `test_tool_call_parser_unwraps_model_orchestration_bundle`.
- Remaining gaps:
  - Re-run a clean R16 root-only shopping E2E after these fixes and verify `cart-checkout` creates real child ids first, then dispatches them in the next round.
  - Add a whole-site required-file oracle at root/parent acceptance so split branches cannot individually pass while the user-facing top-level shopping flow is incomplete.
  - Improve recovery output for `max_children_exceeded` repair attempts, so coordinators can either reuse an existing child or request a bounded repair slot instead of stalling.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R16

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r16.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r16`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r16/build`.
  - Root run: `subagent-1778456564-d88a044e`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明:
  - R16 仍按“外层只观察 root”的方式跑。外层没有替 root 创建 child，也没有替任何 leaf 写购物网站文件。
  - R15 的两个底层修复有效：`cart-checkout-coordinator` 成功创建真实 child，并在下一轮使用真实 run id 调度；`+/-按钮` 文案不再被当成系统路径。
  - root 能发现顶层 required files 缺失并创建修复 coordinator，修复 leaf 最终把 `index.html`、登录/注册、商品、购物车、结账、订单成功、`style.css`、`app.js` 放到顶层 `build/`。
  - 这轮在 root/fix coordinator 最终模型流长期不返回时主动停止；下层产物和修复结果已落盘，未把卡住流当作通过。
- Observed facts:
  - 最终顶层产物包括 `index.html`、`register.html`、`login.html`、`products.html`、`product-detail.html`、`cart.html`、`checkout.html`、`order-success.html`、`style.css`、`app.js` 和 `images/.gitkeep`。
  - board 停止前显示 `total=11`，其中 9 个 `DONE/VERIFIED`，root 和 `fix-structure-coordinator` 仍在 `RUNNING/UNVERIFIED`。
  - `file-restructure-worker` 曾反复用 `read_file` 读取 `memory_archive/artifacts/tool_outputs/*.json`，导致 prompt 从约 111k 增到 134k 字符，说明外置 artifact 包装文件不能再被普通文件读取。
  - 模型仍会偶发输出未闭合 XML-ish 工具调用或 JSON 外多余正文；系统需要给出更明确的下一轮格式修复提示。
- Finding 65: tool-output artifact wrappers must not be read through `read_file`.
  - Symptom: a repair worker read externalized tool-output JSON wrapper files through `read_file`, pulling metadata wrappers and large content back into live prompt.
  - 中文解释：大工具输出已经“搬到仓库里”了，模型又用普通读文件把仓库包装箱整个搬回对话，等于重新把上下文撑大。
  - Fix: `ReadFileTool` now rejects workspace-local `memory_archive/artifacts/tool_outputs/*.json` reads and tells the model to use `read_artifact` with `artifact_ref` and bounded `max_chars`.
  - Verification: `test_read_file_rejects_tool_output_artifact_wrapper`.
- Finding 66: root-level static Web acceptance needs task-promised required files, not only observed artifacts.
  - Symptom: split branch outputs could put pages under subdirectories while top-level `build/products.html`, `build/cart.html`, or shared assets were still missing.
  - 中文解释：孩子各自说“我目录里有页面”，不代表用户打开 `build/` 顶层就能从注册到下单一路点通。父级验收要看用户要求的入口文件是否真的在该在的位置。
  - Fix: parent acceptance test preparation now extracts static deliverable filenames such as `index.html`、`style.css`、`app.js` from task goal / thought / description / acceptance checks, and merges them into inferred `static_site_check.required_files`.
  - Verification: `test_prepare_test_items_merges_task_required_static_files` and `test_static_required_files_from_texts_extracts_static_web_targets`.
- Finding 67: parse-error feedback should tell the model exactly how to recover.
  - Symptom: malformed XML-ish or JSON tool-call text was reported as parse failure, but the model could continue retrying the same broken shape.
  - 中文解释：只说“解析失败”太含糊。模型需要被明确提醒下一轮用 `[TOOL_CALL] + JSON + [/TOOL_CALL]`，别再混未闭合 XML 标签。
  - Fix: parse-error tool results now append a compact retry-format hint without echoing the raw bad tool body.
  - Verification: `test_parse_error_result_includes_retry_format_hint`.
- Remaining gaps:
  - Re-run a clean R17 root-only shopping E2E after the artifact-read guard and whole-site required-file oracle.
  - Watch whether root/fix coordinator still stalls after downstream work is complete; if it does, add a non-destructive finalization watchdog or completion-from-child-summary path.
  - Improve recovery output for `max_children_exceeded` repair attempts, especially “reuse existing child” or “ask parent for bounded repair slot”.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R17

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r17.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r17`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r17/build`.
  - Root run: `subagent-1778460134-7525d829`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明:
  - R17 仍按“外层只观察 root”的方式跑。外层只创建并运行 root，没有替 root 创建 child，也没有替任何 child/leaf 写购物网站文件。
  - root 先正确规划 auth/catalog/cart/shared 四个一级 coordinator，但其中一个 child goal 把用户名路径写成 `/Users/xiaoyuzei/...`。
  - 写入预检正确拒绝了这个越界路径；但错误文案太像权限不足，root 误以为 `/Users/example/.../deliverables/.../build` 也不在允许范围内，于是写了 `capability_request.json` 并 BLOCKED。
- Observed facts:
  - `schedule_child_subagents` 返回的拒绝信息包含 `target=/Users/xiaoyuzei/my-终端应用/...` 和 `workspace_root=/Users/example/my-终端应用`。
  - root 的后续报告把真实正确路径 `/Users/example/my-终端应用/deliverables/.../build` 也误解为越界，申请 `expanded_allowed_write_roots`。
  - board 最终只有 root 一个 run，状态为 `BLOCKED/UNVERIFIED`，没有 child 被创建，也没有购物站产物落盘。
- Finding 68: near-miss workspace paths need retry guidance, not capability escalation.
  - Symptom: a username typo in an absolute path was correctly blocked, but the model interpreted the block as a permission gap and wrote a capability request instead of retrying the corrected path.
  - 中文解释：系统挡住拼错路径是对的，但提示应该说“你把路径拼错了，按这个正确路径重试”，不能让 root 误会成“需要扩大权限”。
  - Fix: `orchestration_write_guard.py` now detects suffix-matching workspace path typos, keeps the deny, and returns `suspected_path_typo=true` plus `suggested_target=<workspace_root + same suffix>` and an explicit instruction to retry `schedule_child_subagents` without writing `capability_request`.
  - Verification: `test_external_write_guard_suggests_workspace_typo_retry`.
- Remaining gaps:
  - Re-run a clean R18 root-only shopping E2E and confirm root retries with `suggested_target` instead of escalating.
  - If R18 reaches downstream production again, continue watching root/fix coordinator finalization and whole-site top-level required files.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R18

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r18.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r18`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r18/build`.
  - Root run: `subagent-1778460769-e9282812`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明:
  - R18 仍按“外层只观察 root”的方式跑。外层只创建并运行 root，没有替 root 创建 child，也没有替任何 leaf 写购物网站文件。
  - R17 的路径 typo 修复有效：root 成功创建 auth、catalog、cart-checkout、shared-assets 四个一级 coordinator，没有再把正确 deliverables 路径误判为权限缺口。
  - 这轮在模型服务 HTTP 529 过载后结束为 root `BLOCKED/UNVERIFIED`；这不是本地 runner 崩溃，但它暴露了多个真实恢复问题。
- Observed facts:
  - auth 分支创建 leaf `auth-page-builder`，并写出 `index.html`、`register.html`、`login.html`、`style.css`；auth leaf 最终 `DONE/VERIFIED`，auth coordinator 进入 `AWAITING_ACCEPTANCE/NEEDS_ACCEPTANCE`。
  - cart-checkout 分支创建 leaf `cart-checkout-worker`，并写出 `cart.html`、`checkout.html`、`order-success.html`。
  - catalog coordinator 尝试创建商品 leaf 时，目标里的 `https://via.placeholder.com/300x200` 被派工预检误切成 `s://via.placeholder.com/300x200`，导致 child 创建被拒。
  - cart coordinator 后续多次把 `/Users/example/...` 拼成 `/Users/xiaoyezei/...`，普通 `read_file` / `list_files` 只返回泛化越界提示，模型继续围绕错误路径尝试。
  - shared-assets 分支创建 style/app worker 后遭遇 API 529，多个 run 写出 failure handoff / takeover readiness。
- Finding 69: orchestration write preflight must ignore URLs, not only local write-root extraction.
  - Symptom: a catalog child goal containing `https://via.placeholder.com/300x200` was rejected as an external write target `s://via.placeholder.com/300x200`.
  - 中文解释：商品图片 URL 是页面内容引用，不是本机目录。之前自动写入根提取器已经会跳过 URL，但派工前的另一层守卫还没跳过，所以同类问题在另一个入口复发。
  - Fix: `orchestration_write_guard.py` now uses shared URL spans from `path_recovery_hints.py` and skips path regex matches that overlap URLs.
  - Verification: `test_external_write_guard_ignores_url_image_sources`.
- Finding 70: filesystem read/list boundary errors need typo recovery hints too.
  - Symptom: after a coordinator copied `/Users/example/...` as `/Users/xiaoyezei/...`, `read_file` / `list_files` returned only a generic outside-workspace error, and the model kept retrying the wrong path.
  - 中文解释：不只是派工会拼错路径，读文件和列目录也会拼错。工具应该直接告诉模型“这是拼写错误，正确路径是 suggested_target”，否则模型容易误解成权限问题或继续瞎试。
  - Fix: `FileSystemTool.resolve_path()` now returns `suspected_path_typo=true` plus `suggested_target` for suffix-matching workspace path typos while preserving the deny.
  - Verification: `test_filesystem_tool_suggests_workspace_path_typo`.
- Remaining gaps:
  - Rerun a clean R19 after the URL skip and filesystem typo hint fixes; R18 was interrupted by API 529 and should not be treated as completed acceptance.
  - Continue watching artifact-read recovery: models may invent old or wrong artifact refs such as `C:/repo/...`; current reader rejects unregistered refs, but the recovery instruction may need to be more explicit.
  - Add or tune a recovery/resume path for API 529 mid-run so root can continue from existing takeover refs instead of leaving many descendants BLOCKED.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R19

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r19.yaml`.
  - Internal runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r19`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r19/build`.
  - Root run: `subagent-1778462398-53c920e2`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明:
  - R19 仍按“外层只观察 root”的方式跑。外层只 seed 并运行 root/coordinator，没有替 root 创建下级，也没有替任何 child/leaf 写购物网站文件。
  - root 成功创建 4 个一级 coordinator：auth、catalog、cart-checkout、shared-assets；这些 coordinator 再创建自己的 leaf/worker。
  - 购物网站 10 个顶层必需文件都由下层代理写出：`index.html`、`register.html`、`login.html`、`products.html`、`product-detail.html`、`cart.html`、`checkout.html`、`order-success.html`、`style.css`、`app.js`。
  - 本轮主动停止 root runner，保留卡住样本；停止前 board 为 `DONE/VERIFIED=10`、`BLOCKED=1`、root 仍 `RUNNING/UNVERIFIED`。
- Observed facts:
  - `cart-checkout-coordinator` 一开始尝试自己写 `cart.html`，写入边界正确阻止；随后它自我修正，创建 `cart-page-writer`、`checkout-page-writer`、`order-success-page-writer` 三个 leaf，三个页面最终 `DONE/VERIFIED`。
  - `shared-assets-coordinator` 的 style/app leaf 已写出 `style.css` 和 `app.js`，但后续发现 `app.js` 缺 `getUrlParam` / `setUrlParam`，尝试创建 `html-ref-worker` 修复时被 `duplicate_leaf_target:app.js` 挡住。
  - duplicate guard 挡住修复 leaf 后，shared coordinator 多次尝试自己写 `app.js`，被 `allowed_write_roots` 正确阻止，最后写 capability request 并进入 `BLOCKED`。
  - root 多次调 `dispatch_subagents` 后只得到 `plan_rescue` / `classify_blocker`，没有真正生成接管/修复 child；同时 prompt 增长到约 82k 字符，长期等待模型响应。
  - root 早期尝试把 `final_report.md` 写到自己的 `agent_run_workspace`，但 write boundary 只允许 legacy `task_dir`，导致内部报告写入被挡。
  - 外部静态站点检查第一次误报 `placeholder_hits=3`，实际是合法 JavaScript template literal（如 `` `${item.name}` ``），不是未替换 HTML 占位符。
- Finding 71: explicit repair leaves must bypass duplicate leaf target dedupe.
  - Symptom: `shared-assets-coordinator` needed a bounded repair leaf for `app.js`, but same-parent leaf dedupe returned `duplicate_leaf_target:app.js` because an earlier app worker had already produced that file.
  - 中文解释：同一个文件已经写过，不代表以后不能修。去重应该拦“重复造一个一模一样的生产工人”，但不能拦“明确修复这个坏文件的修复工人”。
  - Fix: `hierarchy_leaf_targets.py` now lets explicit repair/update/fix leaf specs bypass duplicate target blocking while keeping ordinary duplicate writes blocked.
  - Verification: `test_hierarchy_schedule_allows_explicit_repair_leaf_for_existing_target` and existing duplicate target tests.
- Finding 72: coordinator/root report writes need agent-run workspace access.
  - Symptom: root tried to write `tasks/<root>/agents/<root>/final_report.md`, but write boundary allowed only the legacy task directory.
  - 中文解释：coordinator 不能写用户产物目录，这是对的；但它应该能写自己的内部交接报告。不然 root 明明想留总结，却被当成越界写文件。
  - Fix: runner write boundary now includes the task's `agent_run_workspace_dir` and `agent_run_final_report_md` parent as internal report roots, without adding user deliverable roots.
  - Verification: `test_build_execution_context_write_boundary`.
- Finding 73: model-facing dispatch due-check must honor runner timeout off.
  - Symptom: R19 config used `runner_timeout_seconds: "off"`, but `dispatch_subagents` still used default `CapabilityConfig(subagent_run_timeout=900, subagent_heartbeat_timeout=180)` and kept suggesting timeout/takeover actions for the active root.
  - 中文解释：用户说“runner 不限时”，真实模型调度工具就不应该一边不限时、一边又在内部巡检里把它当超时任务接管。
  - Fix: `DispatchSubagentsTool` now builds its internal capability config from agent config; when runner timeout is disabled, model-facing auto dispatch sets subagent run/heartbeat timeout to 0. Manual CLI due-check can still use an explicit capability config.
  - Verification: `test_dispatch_tool_respects_runner_timeout_off_for_auto_due_check`.
- Finding 74: static-site placeholder checks should ignore JavaScript template literals.
  - Symptom: R19 generated pages contained normal JavaScript template strings such as `` `${item.name}` `` inside `<script>`, and static validation flagged them as leftover placeholders.
  - 中文解释：`${...}` 在 HTML 正文里通常是没替换的模板占位符；但在 JS 里是正常语法。静态检查要分清楚，不能把正常购物车渲染逻辑误判成坏产物。
  - Fix: `static_site_validator.py` strips `script` / `style` blocks before looking for visible `${...}` placeholders.
  - Verification:
    - `test_static_site_check_allows_javascript_template_literals`.
    - Real R19 static check after fix: required files present, no broken local refs, no inert controls, no visible placeholder hits.
- Finding 75: recovery suggestions must not force every root/parent into coordinator mode.
  - Symptom: R19 needed a better "create repair/takeover child" hint, but a naive fix could accidentally teach root that all recovery work must create a coordinator.
  - 中文解释：root 可以直接派 worker，也可以派 coordinator；区别取决于任务需不需要继续拆下级。4 层测试是这轮 prompt 的要求，不是系统永远只能走 4 层。
  - Fix: `orchestration_progress_payload.py` now returns a refs-only `suggested_recovery_child_tool_call` with default role `worker` plus a `role_selection_hint`; parent runner can change it to `coordinator/lead` only when the recovery itself needs another layer of delegation.
  - Verification: `test_runner_context_dispatch_suggests_recovery_child_for_blocked_direct_child`.
- Finding 76: copied tool-output artifact paths need artifact-reader recovery, not file-reader retry.
  - Symptom: debug trace showed the model copied a tool-output artifact path with `/Users/example/...` misspelled as `/Users/xiaoyezei/...`, then `read_file` returned a generic path typo retry against `suggested_target`.
  - 中文解释：这类路径不是用户产物文件，而是“大工具输出仓库”的包装 JSON。即使把路径拼对了，也不应该用普通读文件去搬包装箱；应该用 `read_artifact` 按短 id 或文件名分片读正文。
  - Fix: `tool_context_reducer.py` now exposes `output_artifact_ref`, `output_call_id`, and a copyable `read_artifact` hint for externalized outputs; `_filesystem_read.py` detects typo-repaired `memory_archive/artifacts/tool_outputs/*.json` paths and tells the model to use `read_artifact` instead of retrying `read_file`.
  - Cross-product lesson:
    - 长期助手 uses `resolve()` / `relative_to()` style path checks, read limits, and output truncation.
    - 会话运行时 execution carries structured `cwd` / sandbox settings instead of asking the model to reason from prose paths.
    - 终端交互 distinguishes fresh/forked agents and warns that inherited paths may need worktree translation.
    - 通道运行时 / claw-code use workspace/media path validation and output-size boundaries.
  - Verification:
    - `test_read_file_typo_to_tool_output_artifact_routes_to_read_artifact`.
    - `test_tool_loop_externalizes_large_tool_output_for_archive`.
- Remaining gaps:
  - Re-run a clean R20 root-only shopping E2E after these fixes and verify shared-assets can create a repair leaf instead of capability escalation.
  - Add a stronger root-owned full flow oracle for registration/login/cart/checkout behavior; current `static_site_check` validates files/links/placeholders/buttons but does not execute JavaScript in a browser.
  - R20 still needs to verify the new recovery suggestion works in a live root-only run; it is intentionally role-flexible, not coordinator-only.
  - Later work should move more path passing toward short ids / relative paths / structured cwd bundles so long absolute paths appear less often in prompts.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R20

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r20.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r20`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r20/build`.
  - Model name: `MiniMax-M2.7`, `subagent_debug_trace_level=5`.
- 中文说明：
  - R20 按用户新要求验证“上层权限覆盖下层”。root/coordinator/tester/reviewer 可以继承产物写入根，用于检查、接管和救援；但正常职责仍要把具体页面/代码交给 worker/writer。
  - 外层只 seed root；root 自己创建了 `小傻妞-购物网站架构师`，没有外层直接创建下级，命名规则第一层生效。
- Finding 77: old coordinator prompt contradicted authority coverage.
  - Symptom: root already had deliverables write coverage, but coordinator runner prompt still said final product write denial was expected and told the model not to request final product write permission.
  - 中文解释：权限模型已经改了，但提示词还在讲旧规矩，模型会误以为自己不能继续创建 coordinator/孙节点，容易把可恢复任务报成 BLOCKED。
  - Fix: runner prompt and coordinator role template now say parent authority covers descendants; coordinator can inherit product roots for inspection/takeover/rescue, can create coordinator/child_coordinator/grandchild_coordinator for multi-layer work, and should still delegate product writing to worker/writer/leaf_worker.
  - Verification:
    - `test_runner_prompt_tells_coordinator_to_write_reports_but_delegate_deliverables`.
    - `test_coordinator_template_says_parent_authority_covers_children_but_should_delegate`.
    - focused hierarchy write-root tests, ruff, and strict code-size.
- Finding 78: long multi-child schedule payload can truncate before closing the tool call.
  - Symptom: `小傻妞-购物网站架构师` tried to create three long grandchildren specs in one `schedule_child_subagents` call; the model response stopped mid-JSON before `[/TOOL_CALL]`, so no grandchildren were created.
  - 中文解释：这不是 schedule 工具不能创建孙节点，而是一次塞太长，模型输出半截 JSON，工具根本没法执行。
  - Fix: coordinator runner prompt and schedule tool spec now require schedule params at the tool JSON top level and tell long goals to split into multiple calls with 1-2 children each. The tool entrypoint also tolerates a real-model `orchestration` wrapper and unwraps it before scheduling, while keeping the public bundle interface unchanged.
  - Verification: `test_runner_context_schedule_accepts_orchestration_wrapper`.
- Finding 79: inherited product authority must not mean coordinator writes the product itself.
  - Symptom: after child/grandchild runs became `BLOCKED`, root interpreted inherited write roots as permission to directly create the shopping-site files itself.
  - 中文解释：你说的“上层权限覆盖下层”是对的，但这不等于 root/coordinator 平时亲手写业务文件。它们要能检查、接管、救援，可默认动作应该是再派救援 worker，而不是把 worker 的活抢过来。
  - Fix: runner write boundary now exposes `product_write_roots` and `product_write_policy`. Coordinator/tester/reviewer-style roles keep product roots in `allowed_write_roots`, but `product_write_policy=delegate` blocks direct writes under product roots and tells the model to create or dispatch `worker/writer/leaf_worker`. Worker/writer/leaf roles use `product_write_policy=direct`.
  - Verification:
    - `test_delegate_policy_blocks_coordinator_product_write`.
    - `test_delegate_policy_allows_task_dir_reports`.
    - `test_direct_policy_allows_worker_product_write`.
    - `test_build_execution_context_write_boundary`.
- Finding 80: exact file contracts were lost across hierarchy handoff.
  - Symptom: root required `product-detail.html`, root-level `style.css`, and root-level `app.js`, but the grandchild goal shrank the list to `product.html`, `css/style.css`, and `js/main.js`.
  - 中文解释：模型不是故意乱写，它在层层转述时把“必须叫这些文件名”的合同压缩坏了。后续验收会按父级原始要求查文件，所以这类改名必须在派工时就防住。
  - Fix: `scheduled_child_goal()` now treats explicit parent file names as a hard handoff contract. If the child goal only carries the output directory but misses parent filenames, it appends `父级明确文件/产物名` and requires descendants to pass those names verbatim.
  - Verification: `test_hierarchy_schedule_preserves_shopping_file_contract_when_child_goal_only_has_build_dir`.
- Finding 81: explicit four-layer tests need a coordinator gate before leaf work.
  - Symptom: root intended `root -> 子 -> 孙 -> 孙孙`, but the first child created a depth-2 `worker` directly; that made the chain stop at three levels and prevented a depth-3 `小小小傻妞-*` node.
  - 中文解释：平时 root 可以直接派 worker，也可以派 coordinator；但这轮 prompt 明确要求 4 层链路，所以深度还没到孙孙层时，下一层应该先是 coordinator，再由它继续派 leaf。
  - Fix: `hierarchy_scope_guards.py` now blocks `worker/leaf_worker` children before depth 3 only when the parent goal explicitly mentions a 4-layer/great-grandchild chain. This is scenario-driven, not a hardcoded global depth limit.
  - Verification: `test_hierarchy_schedule_blocks_leaf_before_explicit_four_layer_chain_reaches_depth_three`.
- Finding 82: coordinator product-write blocking needs a real communication lane.
  - Symptom: once coordinator/root product writes are blocked by `product_write_policy=delegate`, they still need a way to correct child paths, announce requirement changes, and tell many descendants to reread shared facts without writing the product themselves.
  - 中文解释：coordinator 可以被拦住不亲自写页面，但不能因此变成“哑巴领导”。少数下属需要不同消息时要能一对一通知；大量下属同一消息时要能写公共看板广播；同时不能越权广播到别的 sibling 分支。平级子代理也要能点对点讨论，给以后 team 功能留口子。
  - Fix: added `subagent_message` orchestration tool. `direct + descendants` writes target inbox/outbox only inside the sender subtree. `broadcast + descendants` appends scoped rows to shared messages/blackboard with `recipient_scope` and `scope_root_run_id`. `direct + peers` allows same-parent sibling discussion only; cousin/other-branch targets are blocked.
  - Verification:
    - `test_direct_message_allows_ancestor_to_descendant`.
    - `test_direct_message_allows_peers_under_same_parent`.
    - `test_broadcast_scopes_to_sender_descendants`.
    - `test_message_blocks_cross_branch_descendant_targets`.
- Remaining check:
  - Rerun a clean R21/R20-style root-only shopping E2E and verify root creates a 4-layer chain with `小傻妞-*` -> `小小傻妞-*` -> `小小小傻妞-*`, product writes are performed by worker/leaf nodes, and the required shopping-site file names stay exact.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R21

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r21.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r21`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r21/build`.
- 中文说明：
  - R21 继续按 root-only 原则测试：外层只给主代理任务，主代理创建 root coordinator，后续 child / grandchild / great-grandchild 必须由上层节点自己创建。
  - 真实模型验证了两个新边界：coordinator 试图直接写 `.gitkeep` 到业务产物目录时被 `product_write_policy=delegate` 拦住；depth=1 coordinator 过早创建 leaf_worker 时被 `hierarchy_chain_requires_coordinator_until_depth_3` 拦住。
  - 真实模型也实际调用了 `subagent_message direct + descendants`，把纠偏消息写进下级 inbox/outbox，证明 coordinator 被禁止写业务产物后仍有通信能力。
- Finding 83: long multi-child schedule calls need a hard tool boundary, not just prompt wording.
  - Symptom: depth=2 `小小傻妞-*` coordinator tried to create 3 long leaf_worker specs in one `schedule_child_subagents` call. The model response/tool call was truncated before the tool returned; no leaf runs were created, and the branch became `BLOCKED/UNVERIFIED`.
  - 中文解释：之前只在提示词里写“长任务拆成 1-2 个 child”，但真实模型还是会一口气塞 3 个。只靠提示不够，要让工具本身拒绝过大的单次调用，并告诉模型拆小重试。
  - Fix: `schedule_child_subagents` tool entrypoint now rejects more than 2 child specs per model call with a clear split-and-retry message. The lower manager API still supports batch scheduling; this guard only protects live runner tool calls from overlong JSON truncation.
  - Verification:
    - `test_schedule_tool_rejects_three_child_batches`.
    - `test_schedule_tool_allows_two_child_batches`.
- Remaining check:
  - Re-run a clean R22 root-only shopping E2E after the hard batch guard and verify the depth=2 coordinator retries as multiple small schedule calls instead of becoming blocked.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R22

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r22.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r22`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r22/build`.
- 中文说明：
  - R22 确认 R21 的批量守卫方向有效：root coordinator 这次按小批次创建了第一层 `小傻妞-*`，没有再一次塞 3 个长 child goal。
  - 通信能力也真实触发：root 对下级发送了 `direct + descendants` 纠偏消息，并发送了 `broadcast + descendants` 的统一文件名/目录约束通知。
- Finding 84: forbidden rename examples must not become required file contracts.
  - Symptom: R22 child goal 的继承块里出现了 `- product.html`，来源是父级文本 `product-detail.html（禁止改成 product.html）` / `不允许把 product-detail.html 改名成 product.html`。这会让下级误以为必须交付 `product.html`，正好违反用户要求。
  - 中文解释：这是 prompt 被误传，不是下级单纯变笨。我们把自然语言里的所有 `*.html/*.css/*.js` 都正则抽出来当“必须文件”，但没有区分“必须包含 X”和“禁止改成 Y”。Y 是反例，不是产物合同。
  - Reference lesson:
    - 长期助手 cron/script 上下文会先做路径作用域校验，再把脚本输出作为带标题的 context 注入，不让任意路径或环境上下文直接混进任务。
    - 会话运行时/claw-code 风格把 workspace/sandbox/write roots 做成结构化字段，而不是只靠自然语言长句让模型复制。
    - 终端交互 的 context usage 路径强调按类别统计/压缩上下文；对我们来说，对子代理 handoff 也应拆成结构化 `required_files` / `forbidden_files` / `write_roots`，减少从散文里二次猜语义。
  - Fix: added `required_file_terms.py`, shared by hierarchy handoff and static required-file extraction. It extracts positive deliverable filenames but skips filenames that appear as forbidden rename/create targets such as `禁止改成 product.html` or `不要创建 legacy.html`.
  - Follow-up from R23: MiniMax rewrote the same rule as `不得改名为 product.html`; the first fix covered `改名成` but not `改名为`, so R23 was stopped and the marker list now covers both forms plus `改为`。
  - Follow-up from R24: the second forbidden alternative in `不要改名成 product.html 或 old-product.html` still leaked because `old-product.html` no longer had the negative verb directly in front of it. The extractor now carries a short negative chain across sibling connectors like `或` / `或者` / `、` / comma, so `不要创建 a.html、b.html` and `不要改成 a.html 或 b.html` both stay forbidden examples instead of required deliverables.
  - 中文解释：这次不是模型单纯写错，而是我们把“不要改名成 A 或 B”里的 B 当成了正常文件名。以后类似“不要创建 A、B、C”的并列反例会一起过滤掉。
  - Verification:
    - `test_hierarchy_file_contract_skips_forbidden_rename_targets`.
    - `test_static_required_files_from_texts_extracts_static_web_targets`.
- Remaining check:
  - Re-run a clean R25 root-only shopping E2E and verify `product.html` / `old-product.html` / `legacy.html` no longer appear in `父级明确文件/产物名` or static required files while `product-detail.html` remains.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R25

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r25.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r25`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r25/build`.
- 中文说明：
  - R25 用更难的否定文件名规则复测：`不要改名成 product.html 或 old-product.html`、`不要创建 legacy.html 或 obsolete.html`。
  - 本轮没有进入真正子代理派工；主模型在第一条 `create_subagents` 工具调用里把 root goal/plan/acceptance 写得过长，导致 `[TOOL_CALL]` 没有闭合，工具系统以前会把半截工具块当成普通最终回答放过去。
- Finding 85: incomplete standard tool blocks need parse-error recovery.
  - Symptom: R25 response contained `[TOOL_CALL]` and a partial `create_subagents` JSON body, but lacked `[/TOOL_CALL]`. Because the parser only accepted fully closed standard blocks, the run ended without creating `stage7-shop-r25-root` or any deliverables.
  - 中文解释：这和前面的“3 个 child 一次塞太长”是同一类问题。模型开始按协议调用工具了，但参数太长被截断；系统不能装作没看见，应该明确告诉模型“这不是回答，是坏掉的工具调用，请短一点重试”。
  - Reference lesson:
    - 会话运行时 / 模型助手 Code / claw-code 类工具调用都更像协议层消息：缺字段、越权、解析失败会变成结构化错误并要求重试，而不是让半截工具调用混进最终回复。
    - 长期助手 / 通道运行时 对输出和上下文会做大小边界；对我们来说，工具层也要拦截“过长导致未闭合”的 payload，逼模型使用短合同/短引用。
  - Fix: standard `[TOOL_CALL]` / `[SUBAGENT_CALL]` blocks now report `__parse_error__` when the closing marker is missing. The retry hint specifically tells the model to shorten `goal` / `plan` / `acceptance_checks` and keep only path, required filenames, and hard constraints.
  - Verification: `test_tool_call_parser_reports_missing_closing_tool_marker`.
- Remaining check:
  - Re-run a clean R26 root-only shopping E2E after parse-error recovery and verify the model retries with a shorter `create_subagents` call, then confirm forbidden alternatives stay out of inherited file contracts.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R26

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r26.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r26`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r26/build`.
- 中文说明：
  - R26 证明 R25 的 parse-error recovery 有效：首轮创建 root 之前模型先误调了一次空 `dispatch_subagents`，随后成功创建 root coordinator `subagent-1778475598-9db62bdf`。
  - root 自己创建了 child coordinator `subagent-1778475629-f3b10917`，child 自己创建了 grandchild coordinator `subagent-1778475662-b24b23f2`，grandchild 创建了 depth=3 leaf workers，满足“外层只观察，层级自己派发”的测试方式。
  - `register.html` 和 `login.html` 已由 depth=3 leaf worker `subagent-1778475770-0114105c` 写出；这说明四层链路已能真实落到孙孙节点产物写入。
  - root coordinator 尝试直接写业务目录 `.gitkeep` 被 `product_write_policy=delegate` 拦住，然后转为派发下级，这是正确的上层权限边界。
- Finding 86: truncated write_file content needs a write-specific retry hint.
  - Symptom: homepage/style leaf `subagent-1778475750-9ecbc43d` tried to write a long `style.css` with one `write_file` call. The standard tool block was truncated twice. The new parse-error recovery caught both as `__parse_error__`, but the hint only mentioned shortening `goal/plan/acceptance_checks`, so the model kept trying long `write_file.content`.
  - 中文解释：这次不是路径传错，而是“文件正文太长”。写 CSS/HTML/JS 时，模型很容易把整段代码塞进一个 JSON 字符串，导致工具调用半截断掉。应该明确教它：先 `write_file` 写短骨架，再 `append_file` 分块追加。
  - Fix: parse-error hints now detect truncated `write_file` payloads with `content` and add a specific instruction to use `append_file` chunks with short content while keeping JSON closed.
  - Verification: `test_parse_error_hint_recommends_append_for_truncated_write`.
- Remaining check:
  - Re-run a clean R27 root-only shopping E2E and verify long CSS/HTML writes recover through `append_file`, not repeated truncated `write_file`.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R27

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r27.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r27`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r27/build`.
- 中文说明：
  - R27 继续按 root-only 原则测试：外层只启动主代理，主代理创建 root，root 创建 child，child 创建 grandchild，grandchild 创建 depth=3 leaf。
  - 本轮没有再看到 `product.html` / `old-product.html` / `legacy.html` 这类禁止反例进入必需文件清单，说明 R22/R24 的正向过滤方向有效。
  - `subagent_message direct + descendants` 在 child 层真实执行过，coordinator 写业务目录 `.gitkeep` 仍被 `product_write_policy=delegate` 正确拦截。
- Finding 87: forbidden examples need a first-class `forbidden_files` contract, not just omission from `required_files`.
  - Symptom: R27 没再把禁止文件名放进 required 清单，但父传子仍主要靠一段自然语言继承块。只“跳过反例”能减少误传，但下层仍不知道哪些文件名是明确禁止项。
  - 中文解释：问题不只是模型记错名字，而是我们把“必需文件”和“禁止反例”混在散文里交接。按理这种合同应该像其他工具一样是结构化字段：哪些必须有，哪些绝对不要创建。
  - Cross-product lesson:
    - 会话运行时 / 模型助手 Code 风格的工具调用更像协议消息：参数、cwd、sandbox、错误都结构化，不让模型从长句里猜机器字段。
    - 长期助手 会保留 MCP `structuredContent`，也有 schema coercion 和大输出外置；机器事实优先走结构化 JSON，而不是只走模型散文。
    - 通道运行时 的 sub-agent prompt 使用显式模板变量和 claims/state 文件，避免每层重新理解同一批路径。
    - 终端交互 把 `agentId` / `parentSessionId` / `agentType` 放进显式 metadata；链路身份不靠模型复述。
  - Fix: `required_file_terms.py` now exposes both positive `required_file_terms_from_text()` and negative `forbidden_file_terms_from_text()`. `context_bundle.output_contract` now contains `required_files`, `forbidden_files`, and `file_contract_source=task_text_positive_negative_extraction`; hierarchy inherited goals render separate `structured required_files` and `structured forbidden_files` blocks.
  - Verification:
    - `test_file_contract_extracts_required_and_forbidden_terms_separately`.
    - `test_context_bundle_output_contract_separates_required_and_forbidden_files`.
- Finding 88: write-specific parse-error hints are too late for long generated CSS/JS/HTML.
  - Symptom: leaf `subagent-1778476196-a3e99e53` repeatedly attempted a long `write_file` for `style.css`. The parser returned `__parse_error__` with a 300-character hint that mentioned `append_file` chunks, but the model ignored it and repeated the same long `write_file` pattern.
  - 中文解释：等工具调用已经坏了再提醒，模型可能已经进入“重复上一招”的循环。长 CSS/JS/HTML 应该在 runner 开始干活前就拿到规则：短骨架 + 分块追加。
  - Fix: runner contract now tells every subagent that long CSS/JS/HTML or large code must use `write_file` for a short skeleton and `append_file` chunks for the body. `write_file` / `append_file` tool specs now carry the same rule, and parse-error hints explicitly say not to repeat a full `write_file.content`.
  - Verification:
    - `test_runner_prompt_tells_leaf_to_chunk_long_file_writes`.
    - `test_parse_error_hint_recommends_append_for_truncated_write`.
- Remaining check:
  - Re-run a clean R28 root-only shopping E2E and verify `style.css` / `app.js` are produced through short skeleton plus `append_file` chunks, and context bundle files contain separate `required_files` / `forbidden_files`.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R28

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r28.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r28`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r28/build`.
- 中文说明：
  - R28 继续按 root-only 方式跑：外层只创建 root coordinator，root 自己创建 `小傻妞-*`，child 自己创建 `小小傻妞-*`，grandchild 自己创建 `小小小傻妞-*`。
  - 文件名 contract 比 R22/R24 稳定：`product-detail.html` 没有被改成 `product.html`，禁止反例没有出现在当前 deliverables 里。
  - 真实产物只完成 5 个 HTML：`index.html`、`register.html`、`login.html`、`products.html`、`product-detail.html`。缺少 `cart.html`、`checkout.html`、`order-success.html`、`style.css`、`app.js`。
- Finding 89: short artifact refs were not scoped to the active runner.
  - Symptom: root 使用 `read_artifact artifact_ref="17-1"` 时，可能读到旧 R15/R19 的同号 tool-output artifact，而不是当前 R28 的同号输出。旧 artifact 里带着旧 case id 和旧路径，随后污染当前 prompt，造成路径/任务误传。
  - 中文解释：这不是“模型凭空写错名字”。系统给了一个太短的编号 `17-1`，而这个编号在多个 run 里都会重复。模型照着编号读，工具却拿了旧任务的内容给它，后面当然会串线。按理这种问题不应该发生，应该像会话 ID / run ID 一样有作用域。
  - Cross-product lesson:
    - 长期助手 gateway/session 会把 transcript 和工具消息绑定 `session_id`，cron 也会生成独立 session id；其 release note 也强调 `structuredContent` 优先，减少从散文里猜字段。
    - 通道运行时 的多代理说明强调 per-agent sessions / workspaces，文件访问也按 workspace root / agentId 做边界。
    - 会话运行时 / 模型助手 Code 类工具协议通常把 tool call、cwd、sandbox、run/session 作为结构化上下文字段处理，而不是让短编号在全局空间里裸奔。
  - Fix:
    - tool-output artifact 现在写入 `run_id`、`task_id`、`request_id`、`scoped_call_id`。
    - 子代理 runner 调 `agent.run(save=False)` 时会传入当前 `run_id` / `task_id`；即使旧路径没显式传，也会从 `_current_subagent_run_id` 回填。
    - `read_artifact` 读取短 `call_id` 时会优先匹配当前 run/task/request；没有作用域时也从最新记录开始，而不是读最老记录。
    - live prompt 里的 `read_artifact_hint` 现在优先给具体 artifact path 和 scope 字段，而不是只给全局短号。
  - Verification:
    - `test_read_artifact_short_call_id_prefers_matching_run_scope`.
    - `test_read_artifact_short_call_id_without_scope_prefers_latest`.
    - `test_tool_loop_externalizer_falls_back_to_current_subagent_run_id`.
- Finding 90: FailureIntrospector treated fenced JSON as parse failure.
  - Symptom: 真实模型返回 Markdown JSON 代码块包裹的失败分析时，FailureIntrospector 打印 `JSON 解析失败` 并降级到规则分类。
  - 中文解释：很多模型会把 JSON 放进 Markdown 代码块，这不应该算“分析不可用”。解析器应该先去掉围栏，再解析 JSON。
  - Fix: FailureIntrospector now accepts bare JSON and fenced JSON blocks.
  - Verification: `test_introspect_with_llm_fenced_json_success`.
- Finding 91: root/coordinator rescue still needs stronger stop-and-dispatch behavior.
  - Symptom: root/coordinator 在下级 529 或结构化输出失败后，曾尝试直接写业务产物；工具层已按 `product_write_policy=delegate` 拦住，但 root 随后进入多轮 `subagent_board` / `read_artifact` 循环，prompt 从约 6 万字符涨到 8 万字符，仍未完成剩余 5 个文件。
  - 中文解释：写入边界已经拦住“领导自己写页面”，但“领导被拦以后该怎么救援”还不够硬。下一轮要让它少读状态，多用 `schedule_child_subagents` 创建明确的 rescue worker，并限制重复读取同一个 board/artifact 的循环。
  - Status: recorded; not fully solved in this patch. The scoped artifact fix removes one major source of wrong context, but R29 still needs a clean rerun to verify rescue convergence.
- Remaining check:
  - Re-run clean R29 after scoped artifact refs. Expectation: `read_artifact("17-1")` no longer imports old run content; if child/coordinator blocks, root should create a rescue worker instead of looping over old artifact/body reads.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R29

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r29.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r29`.
  - Deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r29/build`.
- 中文说明：
  - R29 继续按 root-only 方式测试：外层只启动主代理，root 自己派 `小傻妞-*`，child 自己派 `小小傻妞-*`，grandchild 自己派 `小小小傻妞-*`。
  - 本轮真实产生 10 个目标文件：`index.html`、`register.html`、`login.html`、`products.html`、`product-detail.html`、`cart.html`、`checkout.html`、`order-success.html`、`style.css`、`app.js`。
  - 禁止反例没有落盘：没有 `product.html`、`old-product.html`、`legacy.html`、`obsolete.html`。
  - 真实层级也跑出来了：root -> `小傻妞-html/cssjs` -> `小小傻妞-*` -> `小小小傻妞-*`，也就是用户口径里的 4 层。
- Finding 92: scoped artifact refs worked, but final state must trust machine task status over prose.
  - Symptom: CLI 最后一段自然语言说 E2E passed，但 `task.json` 显示 root、HTML child coordinator、HTML grandchild coordinator 仍是 `BLOCKED/UNVERIFIED`。业务文件在，状态机没过，不能把这类运行当作完整通过。
  - 中文解释：以后汇报不能只看模型最后一句“完成了”。要同时看机器状态：root 有没有 blocked、关键 coordinator 有没有 verified、验收有没有真的过。
  - Evidence: R29 的 10 个 deliverable 文件存在；`task.json` 同时显示 3 个 coordinator blocked。
  - Status: recorded. 后续 finalizer/report 要把 “deliverables complete but coordinator state blocked” 作为非通过状态展示。
- Finding 93: unclosed structured result blocks should be recoverable when JSON is complete.
  - Symptom: root 和两个 coordinator 的 `RUNNER_RESULT.md` 都显示 `structured output parse failed: 缺少 [/SUBAGENT_RESULT] 结束标记。`，但它们已经完成了多轮派工、通信、观察和最终汇总。
  - 中文解释：模型最后少写一个结束标签，不应该直接把整个协调节点判死。如果 `[SUBAGENT_RESULT]` 后面的 JSON object 本身是完整的，系统应该能救回来；只有 JSON 真的没写完时才失败。
  - Fix: runner/parser 现在会在缺少结束标记时做窄范围恢复：只有标记后面直接是完整 JSON、`json` 前缀 JSON 或 JSON fence 时才解析；普通说明里提到 `[SUBAGENT_RESULT]` 不会被误当结果。
  - Verification:
    - `test_parse_complete_json_without_end_marker`.
    - `test_parse_incomplete_json_without_end_marker`.
    - `test_subagent_runner_parser_uses_last_parseable_fenced_block`.
    - `test_subagent_runner_parser_accepts_prefixed_json_block`.
- Finding 94: communication self-repair worked but tool ergonomics can improve.
  - Symptom: root 首次调用 `subagent_message` 时缺 `topic/body` 被拒，随后自己修正为 direct/broadcast 消息，继续推进。
  - 中文解释：这说明错误能被模型自己修正，但工具返回可以更明确、更短，让上层更快知道“少了哪个字段、怎么补”。
  - Status: recorded. 不是本次 blocker，后续可优化 message tool 的错误提示模板。
- Remaining check:
  - Re-run clean R30 after parser recovery. Expected result: 如果 root/coordinator 输出了完整 JSON 但缺 `[/SUBAGENT_RESULT]`，状态不应再被误判为 blocked；若仍 blocked，则优先修 finalizer 的 blocked-state 汇报和 coordinator 收束规则。

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R30

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r30.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r30`.
  - User deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r30/build`.
- 中文说明：
  - R30 继续按 root-only 跑，外层只创建 root，root 自己创建 `小傻妞-shop-build-l1`，child 自己创建 `小小傻妞-shop-build-l2`，grandchild 自己创建两个 `小小小傻妞-*` leaf worker。
  - 这轮没有复现 R29 的缺 `[/SUBAGENT_RESULT]` 误判；root/coordinator 的 `structured_output_ok=True`，说明 parser recovery 方向可用。
  - 但测试没有通过：用户指定的 deliverables build 目录是 0 个文件；10 个文件被写到了内部 agent-run workspace：`.my_agent_runtime/.../tasks/<root>/agents/<root>/build`。
- Finding 95: root seed must preserve product write roots instead of inventing an internal build dir.
  - Symptom: 顶层提示要求 `extra_write_roots=["/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r30/build"]`，但真实模型首次 `create_subagents` 调用漏掉了该字段。系统仍创建 root，root 的 `allowed_write_roots` 只剩自己的 task dir，于是它把 `build` 解释成内部 agent-run workspace。
  - 中文解释：这不是叶子节点单纯写错路径，而是 root 一开始就没拿到用户产物目录。没有真实 `extra_write_roots` 时，coordinator 会用自己“家目录”下面的 build，当成业务产物目录传给所有下级。
  - Fix: `create_subagents` 现在对显式 root/coordinator 做产品写入根门禁：如果目标像“交付网站/文件”，但既没有 `extra_write_roots`，goal 里也没有可提取的绝对产物路径，就拒绝创建，并要求模型用顶层 `extra_write_roots` 重试。
  - Verification: `test_explicit_coordinator_product_delivery_requires_write_root`.
- Finding 96: failed root should let the CLI return instead of hanging.
  - Symptom: root 已进入 `BLOCKED/FAILED`，用户 deliverables 目录仍为空，但顶层 `my-agent run` 进程继续挂起接近 30 分钟，最终由外层测试观察者终止 R30 进程以保留日志。
  - 中文解释：真实无人值守时，任务已经失败就应该明确收束并返回失败报告，不能让主进程继续空转。否则用户看起来像“卡死了”。
  - Status: recorded. 后续需要让主运行循环在 root failed 且没有可执行恢复动作时自然退出，并打印失败 refs。
- Finding 97: acceptance/report must distinguish internal workspace artifacts from user deliverables.
  - Symptom: root 报告 `build目录包含10个文件`，这句话对内部 workspace build 为真，但对用户指定 deliverables build 为假。
  - 中文解释：验收不能只问“有一个 build 目录吗”，必须问“是不是用户指定的那个 build 目录”。后续静态站验收要绑定 authoritative deliverables root。
  - Status: partially covered by Finding 95 guard. 仍建议后续把 authoritative product root 放进 context bundle/output contract，并让 static-site acceptance 优先检查该 root。
- Remaining check:
  - Re-run clean R31 after root-write-root guard. Expected result: 如果模型漏传 `extra_write_roots`，`create_subagents` 应拒绝并要求重试；如果模型按提示补上，root/children 应把文件写入 `/deliverables/.../build`，不是内部 agent-run workspace。

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R31

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r31.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r31`.
  - User deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r31/build`.
- 中文说明：
  - R31 按 root-only 原则跑出真实 4 层链路：`stage7-shop-r31-root` -> `小傻妞-目录管理` -> `小小傻妞-任务拆分` -> `小小小傻妞-文件创建`。
  - 本轮验证了 R30 的产物根目录修复：10 个目标文件全部写入用户指定的 deliverables build 目录，没有写到内部 `.my_agent_runtime/.../build`。
  - root 和 child 都真实执行了 `subagent_message`：broadcast 给 descendants，direct 给具体下级；这说明上层对下层的通信通路已可用。
- Finding 98: product root guard worked, but coordinator result packets were still too weak.
  - Symptom: `小傻妞-目录管理` 完成了派工、通信和产物验证，但最终结构化结果只写了普通 `evidence` / `delivered_files`，没有 `evidence_packets`。父级验收因此给 `acceptance_failed`。
  - 中文解释：业务已经做完，但机器验收需要“可追溯证据包”。只说“文件都在”不够，必须写成 evidence_packets，里面有 artifact_refs 或 evidence_refs，父级才知道证据能追到哪里。
  - Fix: runner required-output template 现在显式包含 `evidence_packets`；提示词明确要求成功时 evidence_packets 必须带 artifact_refs/evidence_refs，并限制 evidence/artifacts/tests/lessons 每类只保留关键 1-5 条，长报告写文件后引用路径。
  - Verification: `test_prompt_contains_result_block_markers`.
- Finding 99: repair prompt was too large, causing the repair answer to truncate again.
  - Symptom: `小小傻妞-任务拆分` 的第一次结果块在 `"cap...` 处截断；repair 回合又把完整原 prompt 和长原响应塞回模型，修复回复也再次截断，没有闭合 `[/SUBAGENT_RESULT]`。
  - 中文解释：修复不是重新写论文，而是把最后结果整理成短 JSON。上下文太胖会挤掉模型输出空间，导致“修复也坏掉”。
  - Fix: structured-output repair prompt 现在裁剪 `original_prompt` 和 `original_response`，只保留尾部最可能包含工具结果和最终报告的部分；同时强制 summary、evidence、artifacts、tests、lessons 短输出，并要求成功时写 evidence_packets。
  - Verification: `test_repair_prompt_clips_large_prompt_and_response`、`test_subagent_runner_repairs_missing_structured_output`。
- Finding 100: real run still exposed repeated long/multi tool-call formatting mistakes.
  - Symptom: 叶子和 root 在连续写多个文件、连续读多个文件时多次缺少 `[/TOOL_CALL]`。系统 parse-error hint 能让它继续前进，但模型仍倾向一次输出太多工具调用。
  - 中文解释：救场能力有效，但还不是最优。后续要进一步让模型默认“一次少量工具调用”，特别是写长 HTML/CSS/JS 和读多个文件时。
  - Status: recorded. Current patch tightens result/repair outputs first；后续可继续调 tool-call batching prompt 或工具层自动分批。
- Finding 101: failed root should still return the top-level CLI promptly.
  - Symptom: root 已经落成 `BLOCKED/structured_output_parse_error`，但顶层 `my-agent run` 进程仍未自然退出，最后由观察者终止进程。
  - 中文解释：失败可以接受，卡住不行。无人值守时，root 没有更多可执行恢复动作就应该输出失败 refs 并返回。
  - Status: recorded; not fixed in this patch. 下一片建议专门修 run-loop 失败退出边界。
- Remaining check:
  - Re-run clean R32 after evidence-packet template and compact repair prompt. Expected result: coordinator 正常结果或 repair 结果应更短、更容易闭合，并给父级足够 evidence_packets；如果仍 blocked，再优先修顶层 run-loop 退出和自动接管策略。

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R32

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r32.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r32`.
  - User deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r32/build`.
- 中文说明：
  - R32 继续按 root-only 原则跑：外层只启动 root，root 自己派 `小傻妞-前端协调`，child 自己派 `小小傻妞-coordinator`，孙节点自己派 `小小小傻妞-HTML`。
  - 真实产物已经写进用户指定 build 目录：8 个 HTML + `style.css` + `app.js`，共 10 个文件；禁止文件没有落盘。
  - 通信链路也跑到了：root 使用 broadcast 给 descendants 发文件名规范，也用 direct 给具体下级发路径提醒。
- Finding 102: bare lineage names could crash scheduling as raw `IndexError`.
  - Symptom: child 多次调用 `schedule_child_subagents` 时只写 `agent_name="小小傻妞"`，没有专业后缀；工具层暴露 `工具执行失败: IndexError`，模型只能反复猜参数。
  - 中文解释：我们的命名规则要求 `小傻妞-xxx`、`小小傻妞-xxx`。真实模型有时只写前缀，没有 `-xxx`。系统不能因为少后缀就炸成裸异常，应该自动回退到 role 后缀，例如 `小小傻妞-coordinator`。
  - Fix: `hierarchy_agent_names.py` 现在能处理只有中文层级前缀的名字；`schedule_child_subagents` 还会把底层参数异常转成结构化工具错误，不再把裸 `IndexError` 交给模型。
  - Verification: `test_hierarchy_schedule_repairs_bare_lineage_agent_name`、`test_runner_context_schedule_bare_lineage_name_returns_payload_not_index_error`。
- Finding 103: truncated result JSON with complete evidence should be recoverable.
  - Symptom: `小小小傻妞-HTML` 已写完 10 个文件，并在最终结果里给出完整 `evidence_packets`；但长 `artifacts` 列表尾部被截断，缺 `[/SUBAGENT_RESULT]`，旧逻辑把整个 leaf 判成 `structured_output_parse_error`。
  - 中文解释：如果模型最后半截 artifacts 没写完，但前面已经有可追溯证据包，系统应该先保住“可验收事实”，不要把实际产物全当失败。没有 refs 的自夸仍不能恢复。
  - Fix: parser 现在支持部分恢复：成功态必须先解析出完整 `evidence_packets`，且至少一个 packet 带 `artifact_refs` 或 `evidence_refs`；如果数组尾部也截断，会保留前面已闭合的证据对象。
  - Verification: `test_parse_partial_success_with_traceable_evidence_packets`、`test_parse_partial_success_with_cut_evidence_packet_array`、`test_parse_partial_success_without_refs_stays_blocked`；用 R32 旧 `runner_response.md` 回放，leaf 与 child coordinator 都能从截断响应恢复出可解析结果。
- Finding 104: long final results should use `output.json` file closeout.
  - Symptom: coordinator 已经写了 `ACCEPTANCE.md`，但仍在对话里输出很长 `SUBAGENT_RESULT`，导致子节点结果块也截断。
  - 中文解释：大任务最后不要在聊天回复里贴一长串文件清单。更稳的办法是写一个短的 `output.json`，系统看到当前 runner 写了自己的 `output.json` 就自动收口。
  - Fix: runner contract 现在明确提示：最终结果很长时优先 `write_file` 写 `execution_context.output_json` 的短 JSON；`tool_round_execution.py` 同时识别 `path` 和 `filesystem.path` 两种工具参数形态，避免模型用 bundle 形式写 `output.json` 时漏触发收口。
  - Verification: `test_tool_round_detects_bundled_filesystem_output_json`。
- Finding 105: top-level run-loop still needs failed-root natural exit.
  - Symptom: R32 在多层恢复/重试后进入长时间运行；观察者终止进程以保留日志和避免继续在同类错误上消耗。
  - 中文解释：这轮修的是“真实问题被看懂、能恢复、工具不裸炸”。顶层 root 失败后自然退出还没完全解决，仍是下一片的重点。
  - Status: recorded; not fixed in this patch.
- Remaining check:
  - Re-run clean R33. Expected result: bare `小小傻妞` 不再触发 `IndexError`；leaf/coordinator 长结果即便尾部截断，也能凭 traceable `evidence_packets` 进入可验收状态；模型若写 `output.json` 应自动收口。若顶层仍长时间不返回，下一片优先修 failed-root run-loop exit。

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R33

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r33.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r33`.
  - User deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r33/build`.
- 中文说明：
  - R33 继续按 root-only 原则跑：外层只启动 root，root 自己派 `小傻妞-html-coordinator` / `小傻妞-cssjs-coordinator`，child 再派 `小小傻妞-*`，孙节点再派 `小小小傻妞-*` 叶子。
  - 购物站 10 个目标文件全部写入用户指定 build 目录：8 个 HTML、`style.css`、`app.js`；禁止文件没有落盘。
  - root 真实执行了 broadcast 和 direct 消息：统一广播文件名规范，并单独纠正 HTML coordinator 的路径；HTML coordinator 试图直接写业务产物时被 `product_write_policy=delegate` 拦住，随后创建 rescue leaf 完成 `order-success.html`。
- Finding 106: audit-only due-check/action apply records could keep the parent loop alive.
  - Symptom: root 已经写出最终报告，业务产物完整，root 任务状态为 `DONE/VERIFIED`；但顶层进程继续每约 31 秒重复 `due_check -> action_plan -> classify_blocker`，只是在 3 个历史失败节点上反复记录 `before_status=BLOCKED / after_status=BLOCKED`。
  - 中文解释：这不是还在干活，而是在重复记账。`classify_blocker` 这种动作只是“记录这个节点需要人工/后续处理”，如果连续两轮完全一样、没有创建孩子、没有状态变化，就不应该让主循环继续空转。
  - Fix: `dispatch_loop` 新增 no-progress fuse。它会比较连续两轮的稳定调度签名，忽略易变 id/时间；如果记录只是 due-check、leadership inspect 或 record-only action，且没有状态/验收/child 创建变化，第二次重复时停止并标记 `stopped_by_no_progress=True`。真实推进的 runner 创建、状态迁移、验收执行仍会继续。
  - Verification: `test_dispatch_loop_stops_when_audit_only_actions_repeat`。
- Finding 107: status source drift still needs a dedicated follow-up.
  - Symptom: 部分 child 的 `output.json` 还显示 `AWAITING_ACCEPTANCE/NEEDS_ACCEPTANCE`，但 `task.json/run.json` 已落成 `BLOCKED/FAILED/acceptance_failed`；root 读到旧 `STATUS.md` / `HANDOFF.md` 占位内容时，也会浪费轮次重新确认。
  - 中文解释：机器最终状态、output.json、状态文档还没有完全同源。后续要让 root 优先读 authoritative task/run 状态，并在 runner 收束后同步 handoff/status，避免旧文档误导上层。
  - Status: recorded. 本次先修无限循环；下一片建议修 authoritative status/handoff sync。
- Finding 108: evidence and artifact ergonomics remain the next quality bottleneck.
  - Symptom: 一个叶子曾以 `AWAITING_ACCEPTANCE` 暴露给父级，但 `evidence_packets=0`、artifacts=0；另有节点仍会尝试用 `read_file` 读取外置 tool-output wrapper JSON，系统能提示改用 `read_artifact`，但会多耗模型轮次。
  - 中文解释：不能让“没有证据的成功”被父级误收，也要减少模型走错读取工具的成本。成功态需要更硬的证据合同，artifact 提示需要更容易复制执行。
  - Status: recorded. 下一片建议合并修：成功无证据自动转待修复/重开；live prompt 进一步突出 scoped `read_artifact`。
- Remaining check:
  - Re-run clean R34 after no-progress fuse. Expected result: 如果 root 已完成且剩余只是重复 record-only blocked 分类，顶层 CLI 应自然返回，不再需要观察者手动 kill。若仍挂住，继续查 run-loop 外层是否还有不看 `stopped_by_no_progress` 的循环。

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R34

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r34.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r34`.
  - User deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r34/build`.
- 中文说明：
  - R34 继续按 root-only 原则跑：外层只启动主代理，root 自己派下级，下级再继续派孙/孙孙节点。
  - 真实购物站产物落到了用户指定 build 目录，目标 10 个文件都存在：8 个 HTML、`style.css`、`app.js`。
  - root 的 broadcast/direct descendants 通信成功；root 尝试 peer 消息失败是合理的，因为 root 没有 parent，不能作为“同级”发 peer 消息。
- Finding 109: output.json closeout needs machine evidence packets.
  - Symptom: root、frontend lead 和一个 HTML leaf 已写报告或产物，但 `output.json` 里 `evidence_packets` 为空，严格验收报 `缺少带 evidence/artifact refs 的 evidence packet`。
  - 中文解释：模型说“我完成了”不够，机器验收需要能追到文件或报告的证据包。R34 说明只靠提示词还不稳，系统在 `output.json` 自动收口时也要帮忙把已经存在的报告/产物路径整理成最小 evidence packet。
  - Fix: `subagent_output_json_response()` now derives a minimal traceable `evidence_packets` entry from existing `artifacts` / `evidence` paths and current task reports such as `coordinator_report.md`, then writes the enriched payload back to `output.json`. If the model wrote a non-empty but malformed packet, the system leaves it strict so acceptance can still reject bad evidence.
  - Verification:
    - `test_subagent_output_json_response_derives_packet_from_report`.
    - `test_subagent_output_json_response_does_not_hide_bad_packet`.
- Finding 110: dispatch_subagents needs a model-facing terminal hint for audit-only rounds.
  - Symptom: after root wrote its report, the outer run still repeated `due_check -> classify_blocker` on historical blocked runs. No child was created, no status changed, and no acceptance ran.
  - 中文解释：R33 修了内部循环保险丝，但 R34 暴露出模型工具返回本身也要说清楚：这一轮只是重复记账，别继续 dispatch 了，应该汇报 blockers 和 refs。
  - Fix: `dispatch_subagents` payload now includes `dispatch_terminal` when a report contains only audit/record actions. It tells the parent model `recommended_next_action=stop_dispatch_and_report_blockers` and lists involved `blocked_run_ids`. The dispatch loop also clears the pending-work flag when the no-progress fuse trips.
  - Verification:
    - `test_dispatch_payload_marks_no_progress_terminal_actions`.
    - `test_dispatch_loop_stops_when_audit_only_actions_repeat`.
- Finding 111: hierarchy role/depth and parent acceptance still need follow-up hardening.
  - Symptom: R34 created a direct child whose name/role looked like a grandchild (`小傻妞-html-coord` with `grandchild_coordinator`) before self-correcting with a deeper `小小傻妞-html-coord`. Some upper nodes also judged completion from “10 files exist” while a lower app worker was still appending `app.js`.
  - 中文解释：路径和文件已经更稳，但“谁领导谁、谁真的完成、父级什么时候可以宣布完成”还要继续收紧。父级不能只看文件存在，还要看 leaf 完成证据、状态机和验收证据。
  - Status: recorded. 下一轮建议做 authoritative status/handoff sync 和 parent acceptance 对 leaf evidence 的闭环检查。
- Remaining check:
  - Re-run clean R35 after evidence-packet enrichment and dispatch terminal hint. Expected result: successful `output.json` closeout should carry traceable packets, and when only historical blockers remain, root should stop dispatching and report refs instead of hanging.

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R35

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r35.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r35`.
  - User deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r35/build`.
- 中文说明：
  - R35 继续按 root-only 原则跑：外层只启动主代理，主代理创建 root，root 自己派下级，下级继续派孙/孙孙节点。
  - 子代理树最终 5 个节点全部 `DONE/VERIFIED`，每个 `output.json` 都有 `evidence_packets`；用户指定 build 目录里 10 个购物站文件全部存在。
  - R34 的 evidence-packet 自动补强有效：这轮没有再因为成功结果缺少可追溯 evidence packet 而卡住父级验收。
- Finding 112: top-level final model call can hang after completed dispatch.
  - Symptom: root 和 direct child 都已经 `DONE/VERIFIED`，dispatch 报告也已经验收通过；但顶层 `my-agent run` 继续等待一次新的模型请求，接近 15 分钟没有新的子代理事件，最后由观察者 `Ctrl-C` 保留栈和文件证据。
  - 中文解释：活已经做完了，系统却还想让最外层模型再“写一段总结”。真实网络请求可能卡住或很慢，这会让用户看起来像任务没结束，也会浪费 token/API 时间。
  - Root cause: 顶层工具循环在 `dispatch_subagents` 后总是回到模型，让模型根据工具记录再输出最终回答；没有识别“当前 workspace 里的所有子代理都已经 DONE/VERIFIED，可以本地收口”这个确定性状态。
  - Fix: 顶层主代理刚执行过 `dispatch_subagents`，且当前子代理 workspace 内所有任务都是 `DONE/VERIFIED` 时，系统直接生成 refs-first 本地收尾回答，不再发起额外模型请求。这个口只对顶层主代理生效；子代理 runner 内仍必须通过自己的 `output.json` 收口，避免破坏 runner 契约。
  - Verification:
    - `test_completed_dispatch_closes_without_extra_model_call`.
    - `test_subagent_runner_stops_after_output_json_write`.
- Finding 113: target depth wording still needs clearer max-depth semantics.
  - Symptom: 本轮提示里“至少 4 层 / at least 4 layers”被模型理解成可以继续往下派，最后出现 depth=4 的 `小小小小傻妞-*` 节点；同时旧 prompt 片段仍带有 “English naming” 表述，和中文前缀规则不完全一致。
  - 中文解释：如果只说“至少 4 层”，模型可能觉得越深越保险。我们真实测试目前只要求主 -> 子 -> 孙 -> 孙孙这 4 层，代码不能写死 4 层，但测试任务需要能明确 `target_depth` 和 `max_depth`，否则模型会多派一层。
  - Status: recorded. 下一片建议在 context bundle / dispatch prompt 里加清晰的 `target_depth`、`max_depth`、`depth_name_prefix` 字段，并让 root 给下级传递同一份深度约束。
- Remaining check:
  - Re-run clean R36 after top-level dispatch closeout. Expected result: 当 root 和所有子代理已 `DONE/VERIFIED` 时，顶层 CLI 应直接返回本地收尾说明，不再等待额外模型请求；同时下一轮 prompt 要明确最多 4 层，避免继续创建 `小小小小傻妞-*`。

## 2026-05-11 Stage7 Shopping-Site Hierarchy Smoke R36

- Test scene:
  - Workspace: `/Users/example/my-终端应用`.
  - Config: `/Users/example/my-终端应用/.my-agent-stage7-shop-smoke-20260511-r36.yaml`.
  - Runtime root: `/Users/example/my-终端应用/.my_agent_runtime/stage7_shop_smoke_20260511_r36`.
  - User deliverables root: `/Users/example/my-终端应用/deliverables/stage7_shop_smoke_20260511_r36/build`.
- 中文说明：
  - R36 继续按 root-only 原则跑，外层只启动主代理；主代理创建 root，root 自己派 `小傻妞`，child 再派 `小小傻妞`，孙节点再派 3 个 `小小小傻妞-*` leaf worker。
  - 链路创建到了目标 4 层，说明“主 -> 子 -> 孙 -> 孙孙”的递归调度通道已经能跑起来；观察者没有直接启动任何下级代理。
  - 本轮没有等到完整产物，因为叶子 worker 在目标 build 目录不存在时误判自己缺少 `mkdir`/shell 能力，其中一个 leaf 进入 `BLOCKED`，观察者用 `SIGTERM` 停止本轮以保留证据。
- Finding 114: initial work-order JSON files were double-encoded strings.
  - Symptom: 新创建任务的 `output.json` 内容形如 `"{"run_id": "...", "status": "PLANNING", ...}"`，外层是 JSON string，不是 JSON object；观察脚本和父级聚合读取 `.keys()` 时会失败。
  - 中文解释：文件看起来像 JSON，但机器读出来其实是“一段字符串”。父级要读 `status`、`artifacts`、`evidence_packets` 时就拿不到字段。
  - Fix: 初始 `output.json`、`status_report.json`、`dependencies.json` 现在由 dict template 生成，再交给 `_write_json_if_missing` 序列化一次；`_read_json_object` 也兼容旧的双层编码，避免历史任务读取失败。
  - Verification:
    - `test_creates_machine_readable_default_json_files`.
    - `test_read_nested_json_string_object`.
- Finding 115: leaf worker misunderstood missing product directory as missing capability.
  - Symptom: leaf worker 的授权写入根包含用户 build 目录，`write_file` 本身也能自动创建父目录；但 runner prompt 没把这点说清，模型看到 `list_files` 返回路径不存在后，直接报告“缺少目录创建工具/无 shell”，导致任务 `BLOCKED`。
  - 中文解释：不是权限不够，也不是工具做不到，是提示词没把工具能力讲明白。叶子节点应该直接用 `write_file` 写短骨架，工具会帮它建目录。
  - Fix: runner contract 明确写入规则：`write_file` / `append_file` 会在授权 `allowed_write_roots` 内自动创建父目录，不要因为目标目录不存在就标记 `BLOCKED`。工具 catalog 的一句话描述也同步补上“父目录不存在时会自动创建”。
  - Verification:
    - `test_prompt_says_write_file_creates_parent_dirs`.
- Finding 116: big inline HTML write still risks tool-call truncation.
  - Symptom: 一个 leaf 尝试把完整 HTML 一次性塞进 `write_file.content`，模型回复缺 `[/TOOL_CALL]`，工具层返回 parse hint 后它又绕回目录检查。
  - 中文解释：大文件一次塞进工具调用容易截断。我们已经提示“短骨架 + append_file 分块”，但模型遇到目录不存在后注意力跑偏。本次先补清目录能力，后续继续观察是否需要更强的“长内容分块执行器”。
  - Status: recorded. Not fully fixed in this patch.
- Remaining check:
  - Re-run clean R37. Expected result: 新任务默认 JSON 是 object；叶子节点遇到不存在的 build 目录时，直接用 `write_file` 创建短骨架并继续 `append_file` 分块，而不是因为缺 mkdir/shell 进入 `BLOCKED`。如果仍出现长 HTML 工具截断，下一片优先做更硬的分块写入引导或受控文件生成 helper。
