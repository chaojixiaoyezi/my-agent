# Subagent Real E2E Findings

This document is append-only. Record every real subagent E2E issue found during live model/API runs, including the test scene, observed behavior, root cause, fix, verification, and remaining gap.

## 中文阅读说明

这份文档保留英文标题和字段，是为了方便代码、测试、CI、后续 LLM 和外部评审检索；下面补一份中文速览，按每条 E2E 记录解释“这次测了什么、发现了什么、修到什么程度、还差什么”。

字段对照：

- `Test scene`：测试场景，也就是这次真实 E2E 怎么跑、在哪里跑、用了什么模型。
- `Workspace`：用户任务目录，本轮都在 `/Users/xiaoyezi/my-claude-code` 附近做真实测试。
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
- Finding 25：coordinator 如果拿到写工具，可能自己代替 leaf 写产物。测试模板改成 coordinator 只有调度/读工具，只有 leaf 有写工具。
- Finding 26：模型把 `max_depth=1` 理解成“再创建一层”，系统原来按绝对深度拦住了。现在在 runner context 下会把这种值归一化成“允许多一层”。
- Successful Smoke：小烟测已通过。外层只启动 root，root 创建 child，child 创建 leaf，只有 leaf 写 `proof.txt=hierarchy-ok`；这条证明主节点单入口原则在小链路上能跑通。

### 主节点单入口 1/4/16/48 压测尝试

- Finding 27：父节点重复 dispatch 曾被 one-shot guard 阻断。现在只有创建类工具 one-shot，dispatch/board 这种进度循环工具可以重复调用。
- Finding 28：下层 runner 缺父级目标上下文。现在 child/grandchild 会继承 bounded parent goal/thought，让下一层知道原始目标、产物路径和验收要求。
- Finding 29：模型把 coordinator 误写成 worker。现在如果一个节点有调度工具且没有写工具，会按 depth 推断成 child/grandchild coordinator。
- Finding 30：限速下还没跑的 PLANNING child 被误判成失败。现在 dispatch 返回 direct child 进度摘要和 continue hint，告诉父节点还有 PLANNING/RUNNING 的孩子要继续 dispatch。
- Remaining Gap：失败分支自动恢复还没完全闭环。当前已经证明 root->child->grandchild->leaf 能真实写部分文件，root 在一个 child blocked 后能继续推进别的 child；还缺自动救援超时 coordinator、接管残留 PLANNING leaf，以及完整 48 文件 main-node-only 全绿复跑。
- Small Fixed Retest：主节点单入口 1/2/4 小树复测通过。外层只启动 root，root 创建 2 child，child 创建 4 leaf，4 个 leaf 真实写出算法文件并通过父级 Python smoke test；leaf 写工具缺失和旧 failure/capability 状态残留已修复。还剩一个验收噪音：某 leaf 的 acceptance plan 因“空测试命令”进入 `request_human`，需要后续让空测试命令降级成 inspect-only 或生成可执行检查。
- Debug Trace：子代理调试日志改成正式 `subagent_debug_trace_level` 开关。默认 0 不写；1-5 只写内部 runtime 的 refs-only JSONL，用于后续多层真实测试定位谁创建、谁运行、谁收束，不污染 deliverables。

## 2026-05-09 Real 3-Subagent Parallel E2E

- Test scene:
  - Workspace: `/Users/xiaoyezi/my-claude-code`
  - Runtime output: `/Users/xiaoyezi/my-claude-code/real3_parallel_e2e`
  - Subagent workspace: `/Users/xiaoyezi/my-claude-code/.my_agent_subagents`
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
  - Workspace: `/Users/xiaoyezi/my-claude-code`
  - Runtime output: `/Users/xiaoyezi/my-claude-code/real3_parallel_e2e_v2`
  - Subagent workspace: `/Users/xiaoyezi/my-claude-code/.my_agent_subagents_real3v2`
  - Config: `/Users/xiaoyezi/my-claude-code/.my-agent-e2e-config-real3v2.yaml`
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
  - Workspace: `/Users/xiaoyezi/my-claude-code`
  - Runtime output: `/Users/xiaoyezi/my-claude-code/real5_fault_e2e`
  - Subagent workspace: `/Users/xiaoyezi/my-claude-code/.my_agent_subagents_real5fault`
  - Config: `/Users/xiaoyezi/my-claude-code/.my-agent-e2e-config-real5fault.yaml`
  - Fast due-check config: `/Users/xiaoyezi/my-claude-code/.my-agent-capability-e2e-fast.yaml`
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
  - Workspace: `/Users/xiaoyezi/my-claude-code`
  - Runtime output: `/Users/xiaoyezi/my-claude-code/real10_parallel_e2e`
  - Subagent workspace: `/Users/xiaoyezi/my-claude-code/.my_agent_subagents_real10`
  - Config: `/Users/xiaoyezi/my-claude-code/.my-agent-e2e-config-real10.yaml`
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
  - Workspace: `/Users/xiaoyezi/my-claude-code`
  - Runtime output: `/Users/xiaoyezi/my-claude-code/.my_agent_e2e_multilevel_run_1778304762`
  - Config: `/Users/xiaoyezi/my-claude-code/.my-agent-e2e-config-multilevel-1778304762.yaml`
  - Subagent workspace: `/Users/xiaoyezi/my-claude-code/.my_agent_e2e_multilevel_run_1778304762/subagents`
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
  - Workspace: `/Users/xiaoyezi/my-claude-code`
  - Config: `/Users/xiaoyezi/my-claude-code/.my-agent-e2e-config-real-multilevel-1778308650.yaml`
  - Subagent workspace: `/Users/xiaoyezi/my-claude-code/.my_agent_subagents_real_multilevel_1778308650`
  - Runtime outputs:
    - `/Users/xiaoyezi/my-claude-code/real_multilevel_e2e_1778308650`
    - `/Users/xiaoyezi/my-claude-code/real_multilevel_e2e_1778309023`
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
  - Workspace: `/Users/xiaoyezi/my-claude-code`
  - Clean deliverables root: `/Users/xiaoyezi/my-claude-code/deliverables/real_tree_1778318291`
  - Internal runtime root: `/Users/xiaoyezi/my-claude-code/.my_agent_runtime/real_tree_1778318291`
  - Subagent workspace: `/Users/xiaoyezi/my-claude-code/.my_agent_runtime/real_tree_1778318291/subagents`
  - Config: `/Users/xiaoyezi/my-claude-code/.my_agent_runtime/real_tree_1778318291/agent_config.yaml`
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
  - Final report directory: `/Users/xiaoyezi/my-claude-code/.my_agent_runtime/real_tree_1778318291/product_subagents_tests_logs_after_artifact_infer`
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
  - The command ran from `/Users/xiaoyezi/my-claude-code/.my_agent_runtime/real_tree_1778318291` instead of the task workspace root or deliverable directory.
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
  - The parent task could have `/Users/xiaoyezi/my-claude-code/deliverables/<case>` in `allowed_write_roots`, but scheduled children lost that product write root unless the model repeated `extra_write_roots`.
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
  - `/Users/xiaoyezi/my-claude-code/.my_agent_runtime/hier_main_only_smoke_1778326811`
- Deliverables root:
  - `/Users/xiaoyezi/my-claude-code/deliverables/hier_main_only_smoke_1778326811`
- Verified behavior:
  - Outer controller only started root `subagent-1778326811-7c86e224`.
  - Root created one child `subagent-1778326838-c47aa8b4`.
  - Child created one leaf `subagent-1778326850-81b8f4d6`.
  - Leaf wrote `/Users/xiaoyezi/my-claude-code/deliverables/hier_main_only_smoke_1778326811/proof.txt`.
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
  - `SubAgentHierarchyScheduler` now infers coordinator roles for children that have orchestration tools and no write tools:
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
  - Workspace: `/Users/xiaoyezi/my-claude-code`
  - Internal runtime root: `/Users/xiaoyezi/my-claude-code/.my_agent_runtime/main_node_small_fixed_1_2_4_20260509_214828`
  - Clean deliverables root: `/Users/xiaoyezi/my-claude-code/deliverables/main_node_small_fixed_1_2_4_20260509_214828`
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
    - `proof.txt` was written at `/Users/xiaoyezi/my-claude-code/deliverables/main_node_trace_smoke_fixed_20260509_2218/leaf_outputs/proof/proof.txt`.
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
  - Runtime root: `/Users/xiaoyezi/my-claude-code/.my_agent_runtime/main_node_trace_tree_1_2_4_12_20260509_230212`.
  - Deliverables root: `/Users/xiaoyezi/my-claude-code/deliverables/main_node_trace_tree_1_2_4_12_20260509_230212`.
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
  - Runtime root: `/Users/xiaoyezi/my-claude-code/.my_agent_runtime/main_node_timeout_contract_smoke_20260509_2342`.
  - Deliverables root: `/Users/xiaoyezi/my-claude-code/deliverables/main_node_timeout_contract_smoke_20260509_2342`.
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
  - Runtime root: `/Users/xiaoyezi/my-claude-code/.my_agent_runtime/main_node_scope_inherit_smoke_20260509_2348`.
  - Deliverables root: `/Users/xiaoyezi/my-claude-code/deliverables/main_node_scope_inherit_smoke_20260509_2348`.
  - Shape requested: root -> child -> leaf, with only root started externally.
- What improved:
  - The child goal preserved the required deliverables path:
    - `/Users/xiaoyezi/my-claude-code/deliverables/main_node_scope_inherit_smoke_20260509_2348/leaf_outputs/proof/`
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
