# Subagent / Capability / Runner Runbook

这份文档专门说明当前 subagent 架构怎么用、哪些东西已经落地、哪些只是记录或预留。

一句话总览：

```text
父代理创建工单
  -> 子代理在自己的工单目录里工作
  -> 缺能力时生成 capability_request
  -> 父代理路由 skill/tool card
  -> 命中则生成 capability_grant
  -> grant 变成 execution_context
  -> subagent-run 读取 execution_context
  -> runner 输出 [SUBAGENT_RESULT] JSON
  -> 系统把 evidence/request/artifacts/tests/patches/lessons 写回工单
  -> 父代理再做 patch 审核、验收、路由、接管或重派
```

## 设计目标

这个模块不是简单的“拆几个子任务”。

它要解决的是这些问题：
- 子代理说完成了，但没有证据。
- 子代理卡住很久，父代理不知道。
- 子代理缺工具或 skill，却假装任务失败或任务完成。
- 多个子代理并行时，谁负责、谁验收、谁收口不清楚。
- 100+ 子代理时，异常任务被海量普通任务淹没。
- 工具失败和通道故障混在一起，导致盲目重派。
- 子代理输出只留在聊天里，压缩上下文后难恢复。

所以当前实现优先强调：
- 工单落盘。
- 机器 JSON 和人类 Markdown 双轨。
- 默认 dry-run。
- 能力授权可审计。
- DONE 需要证据。
- runner 不直接标记 DONE，只进入待验收。

## 核心概念

### SubAgentTask

文件里仍叫 `SubAgentTask`，兼容早期代码；语义上它已经是一个轻量 `SubAgentRun`。

它记录：
- `id`
- `goal`
- `thought`
- `plan`
- `parent_id`
- `root_id`
- `depth`
- `owner`
- `supervisor`
- `final_owner`
- `allowed_skills`
- `allowed_tools`
- `used_skills`
- `used_tools`
- `capability_requests`
- `capability_grants`
- `capability_gaps`
- `evidence`
- `status`
- `verification_status`
- 标准工单路径

### CapabilityRequest

子代理遇到能力缺口时上抛。

典型字段：
- `problem`：遇到什么问题。
- `needed_capability`：需要什么能力。
- `expected_output`：拿到能力后想产出什么。
- `tried`：已经试过什么。
- `evidence`：失败证据或观察。
- `constraints`：安全、权限、方法等约束。

子代理不应该自己搜索全局 skill/tool 宇宙；它只描述问题。

### CapabilityGrant

父代理或更高层级下发的授权。

它可以包含：
- skill 名称。
- tool 名称。
- capability card。
- 授权原因。
- 约束。
- 是否仅当前任务有效。

grant 会合并进子代理的 `allowed_skills` / `allowed_tools`。

### CapabilityGap

最终找不到能力时留下的缺口。

它是后续自学习或工具建设的输入，不是失败后随手写一句话。

### Execution Context

`execution_context.json` 是子代理 runner 真正读取的最小上下文。

它只包含：
- 当前任务目标。
- 思路和计划。
- owner / supervisor / final_owner。
- allowed skills / tools。
- granted cards。
- 写入边界。
- 验收要求。
- 已有证据。
- open request / gap。
- 执行硬规则。

它刻意不包含全局 skill/tool registry。

### Runner Result

runner 执行后会写：
- `RUNNER_RESULT.md`
- `reports/runner_result.json`
- `logs/runner_prompt.md`
- `logs/runner_response.md`
- `output.json`

这些文件是后续验收器、集成器和父代理接管的事实源。

## 标准工单目录

创建一个子代理 run 后，会生成类似目录：

```text
agent_py_agent/data/subagents/<run_id>/
|-- task.json
|-- run.json
|-- thought.md
|-- STATUS.md
|-- WORK_LOG.md
|-- ACCEPTANCE.md
|-- DEBRIEF.md
|-- TAKEOVER.md
|-- CHANNEL_PROBE.md
|-- EXECUTION_CONTEXT.md
|-- execution_context.json
|-- output.json
|-- dependencies.json
|-- RUNNER_RESULT.md
|-- data/
|-- output/
|-- tests/
|-- reports/
|   `-- runner_result.json
|-- logs/
|   |-- runner_prompt.md
|   |-- runner_response.md
|   `-- last_channel_probe.json
`-- scratch/
```

不是每个文件一开始都有内容，但路径会尽量初始化，方便接管和验证。

## 命令流

### 创建子代理工单

```bash
python3 -m agent_py_agent spawn-subagents "实现一个功能并验收" --count 2
```

输出里会看到 run id，例如：

```text
subagent-1777390000-abcd1234
```

### 查看看板

```bash
python3 -m agent_py_agent subagents
```

默认展示 hot list 或最近任务。

查看全部：

```bash
python3 -m agent_py_agent subagents --all
```

按状态过滤：

```bash
python3 -m agent_py_agent subagents --status BLOCKED
```

### 查看单个 run

```bash
python3 -m agent_py_agent subagent <run_id>
```

### 巡检 due-check

```bash
python3 -m agent_py_agent subagents-due-check
```

会检查：
- 工单文件是否缺失。
- DONE 是否缺证据。
- DONE 是否未验收。
- 是否长时间无心跳。
- 是否运行超时。
- 是否有 open capability request。
- 是否有 open capability gap。
- 通道是否 BROKEN / DEGRADED。

### 通道探测

```bash
python3 -m agent_py_agent subagents-probe <run_id>
```

当前 probe 先检查本地工单现场：
- 关键文件是否存在。
- JSON 是否可读。
- scratch 是否可写。
- probe 证据是否能写入。

未来可以接模型 session、ACP adapter、远端工具通道。

### 动作计划

```bash
python3 -m agent_py_agent subagents-plan-actions
```

它只生成 dry-run 动作计划，不修改任务。

常见动作：
- `repair_work_order`
- `reopen_for_evidence`
- `run_acceptance`
- `takeover_or_reassign`
- `route_capability_request`
- `triage_capability_gap`
- `probe_or_repair_channel`

### 执行动作

默认 dry-run：

```bash
python3 -m agent_py_agent subagents-apply-actions --dry-run
```

显式 apply：

```bash
python3 -m agent_py_agent subagents-apply-actions --apply --action reopen_for_evidence --run-id <run_id>
```

接管任务：

```bash
python3 -m agent_py_agent subagents-apply-actions --apply \
  --action takeover_or_reassign \
  --run-id <run_id> \
  --take-over-by parent-supervisor \
  --locked-file src/example.py
```

接管会写 `TAKEOVER.md`，并把 final owner 切给接管者。

### 能力路由

先 dry-run：

```bash
python3 -m agent_py_agent subagents-route-capabilities --dry-run
```

只处理某个 run：

```bash
python3 -m agent_py_agent subagents-route-capabilities --dry-run --run-id <run_id>
```

真正 apply：

```bash
python3 -m agent_py_agent subagents-route-capabilities --apply --run-id <run_id>
```

命中时：
- 创建 `CapabilityGrant`。
- request 标记为 `GRANTED`。
- grant 合并进 `allowed_skills` / `allowed_tools`。
- 写全局审计日志和任务 `WORK_LOG.md`。

未命中时：
- 创建 `CapabilityGap`。
- request 标记为 `GAP`。
- 写审计日志。

### 生成执行上下文

```bash
python3 -m agent_py_agent subagent-context <run_id>
```

输出：
- `execution_context.json`
- `EXECUTION_CONTEXT.md`

### 运行 runner

默认 dry-run：

```bash
python3 -m agent_py_agent subagent-run <run_id>
```

这只会生成：
- runner prompt。
- runner result。
- 工单日志。

不会调用模型。

真正执行：

```bash
python3 -m agent_py_agent subagent-run <run_id> --execute
```

执行前默认 channel probe。

如果通道 BROKEN：
- 不调用模型。
- run 标记 `CHANNEL_ERROR`。
- 记录原因。

如果通道可用：
- 读取 execution context。
- 只注入 allowed tools。
- 调用模型。
- 解析 `[SUBAGENT_RESULT]`。
- 写回工单。

## Runner 输出协议

模型最后必须输出：

```text
[SUBAGENT_RESULT]
{
  "status": "AWAITING_ACCEPTANCE",
  "summary": "本轮完成或卡住的摘要",
  "used_tools": [],
  "used_skills": [],
  "evidence": [
    {
      "kind": "command",
      "summary": "验证摘要",
      "command": "",
      "path": "",
      "url": "",
      "ok": true
    }
  ],
  "capability_requests": [
    {
      "problem": "缺少什么",
      "needed_capability": "能力名",
      "expected_output": "希望得到什么",
      "tried": [],
      "evidence": [],
      "constraints": {}
    }
  ],
  "artifacts": [
    {
      "path": "产物路径",
      "kind": "file|report|log",
      "summary": "产物说明"
    }
  ],
  "tests": [
    {
      "name": "测试名称",
      "command": "运行命令",
      "ok": true,
      "summary": "测试结果摘要"
    }
  ],
  "patches": [
    {
      "path": "改动文件",
      "status": "applied|planned|blocked",
      "summary": "改了什么或准备改什么"
    }
  ],
  "lessons": [
    "可沉淀经验，未来可能变成 skill 或规则"
  ],
  "next_actions": [
    "建议父代理下一步动作"
  ],
  "blocked_reason": "",
  "failure_type": ""
}
[/SUBAGENT_RESULT]
```

### 字段含义

`status`

runner 不应该直接让任务变成 DONE。

推荐：
- `AWAITING_ACCEPTANCE`：任务执行完，等待验收。
- `BLOCKED`：缺能力、缺上下文、缺权限或遇到明确阻塞。
- `FAILED`：执行失败。

`summary`

给人看的本轮摘要。

`used_tools` / `used_skills`

只能填写 execution context 授权的能力。

如果模型填了未授权工具：
- 不会进入 `used_tools`。
- 会写入 `output.json.structured_output.ignored_unauthorized_tools`。

`evidence`

会写入 `VerificationEvidence`。

常见 kind：
- `command`
- `file`
- `url`
- `log`
- `note`

`capability_requests`

会自动写成 open `CapabilityRequest`。

用于表达：
- 缺工具。
- 缺 skill。
- 缺权限。
- 缺上下文。
- 当前授权不足以完成验收。

`artifacts`

产物记录。

它只记录事实，不保证文件已经存在。验收器后续需要检查。

`tests`

测试记录。

可以记录命令、结果、摘要。

`patches`

补丁意图或补丁状态。

当前不会自动 apply patch。

推荐 status：
- `applied`：已经通过授权路径完成。
- `planned`：建议父代理或集成器后续处理。
- `blocked`：需要能力或权限。

`lessons`

可沉淀经验。

当前只写入 `output.json` 和 `DEBRIEF.md`，不会自动生成 skill。

`next_actions`

给父代理或下一层调度器看的建议。

如果同时存在 capability request，系统级 `next_action` 会优先是 `route_capability_request`。

## 写回规则

runner 执行后：

```text
evidence -> task.evidence
capability_requests -> task.capability_requests
used_tools -> task.used_tools，未授权项忽略并审计
used_skills -> task.used_skills，未授权项忽略并审计
artifacts -> output.json.artifacts
tests -> output.json.tests
patches -> output.json.patches
lessons -> output.json.lessons + DEBRIEF.md
next_actions -> output.json.next_actions + DEBRIEF.md
blocked_reason -> output.json.blockers + RUNNER_RESULT.md
```

runner 不会：
- 自动标记 DONE。
- 自动 apply patches。
- 自动生成正式 skill。
- 自动扩大 allowed tools。

## Patch 审核

默认 dry-run：

```bash
python3 -m agent_py_agent subagents-patches
```

指定 run：

```bash
python3 -m agent_py_agent subagents-patches --run-id <run_id>
```

真正写回：

```bash
python3 -m agent_py_agent subagents-patches --apply --run-id <run_id>
```

审核器会检查：
- `output.json.patches` 是否存在需要审核的记录。
- patch 状态是否只使用 `applied` / `planned` / `blocked`。
- `planned` / `blocked` patch 不能审核通过。
- 未知状态 patch 不能审核通过。
- 只有全部 patch 都是 `applied` 时，才会写回 `review_status=APPROVED`。

输出：
- 全局 `subagent_patch_review_report.json`
- 全局 `SUBAGENT_PATCH_REVIEW.md`
- 单任务 `reports/patch_review.json`
- 单任务 `PATCH_REVIEW.md`
- apply 时追加 `subagent_patch_review_log.jsonl` 和 `PATCH_REVIEW_LOG.md`

注意：
- patch 审核器只审核 runner 已声明的 patch 状态。
- 当前不会自动应用未知 diff 或改动文件。
- applied patch 如果没有 `review_status=APPROVED`，父代理验收会继续阻断。

## 父代理验收

默认 dry-run：

```bash
python3 -m agent_py_agent subagents-acceptance
```

指定 run：

```bash
python3 -m agent_py_agent subagents-acceptance --run-id <run_id>
```

真正写回：

```bash
python3 -m agent_py_agent subagents-acceptance --apply --run-id <run_id>
```

验收器会检查：
- 工单现场是否完整。
- run 是否处于 `AWAITING_ACCEPTANCE` / `NEEDS_ACCEPTANCE`。
- channel 是否不是 `BROKEN`。
- runner 结构化输出是否可解析。
- 是否至少有一条 ok evidence。
- 是否没有失败 evidence。
- 是否没有 open capability request / gap。
- `output.json.blockers` 是否为空。
- `output.json.tests` 是否没有失败项。
- `output.json.patches` 是否没有 `planned` / `blocked` 未处理项。
- `output.json.patches` 是否没有未知状态项。
- `applied` patch 是否已经通过 patch 审核。

输出：
- 全局 `subagent_acceptance_report.json`
- 全局 `SUBAGENT_ACCEPTANCE.md`
- 单任务 `reports/acceptance_review.json`
- 单任务 `ACCEPTANCE_REVIEW.md`
- apply 时追加 `subagent_acceptance_log.jsonl` 和 `ACCEPTANCE_REVIEW_LOG.md`

写回规则：
- 通过并 apply：`DONE + VERIFIED`
- 不通过并 apply：`BLOCKED + FAILED`
- dry-run：只写报告，不修改任务状态。

## 状态流

常见状态：

```text
PLANNING
RUNNING
BLOCKED
AWAITING_ACCEPTANCE
DONE
FAILED
TIMEOUT
CHANNEL_ERROR
TAKEN_OVER
```

当前 runner 真执行成功后：

```text
AWAITING_ACCEPTANCE + NEEDS_ACCEPTANCE
```

如果结构化输出有 `capability_requests` 或 `blocked_reason`：

```text
BLOCKED + UNVERIFIED
```

如果结构化 JSON 解析失败：

```text
BLOCKED + UNVERIFIED
failure_type = structured_output_parse_error
```

## 防 Fake Done 规则

硬规则：
- 子代理没有证据不能直接 DONE。
- runner 不直接 DONE。
- DONE 任务如果 evidence 数量不足，会被 due-check 标红。
- DONE 但 verification_status 不是 VERIFIED，也会被 due-check 标红。

验收建议：
- 不只检查文件存在。
- 要检查真实入口、命令结果、日志、URL、截图或用户路径。
- 多子代理并行后，要做统一集成验证。

## 能力路由策略

当前策略：
- 子代理只提交 capability request。
- 父代理用 Capability Router 查 skill/tool card。
- 命中则 grant。
- 未命中则 gap。
- 中间层不需要展开 skill 正文，只转发 card / grant。

这样可以支持未来 100+ 子代理场景：
- 下级不需要知道全局能力宇宙。
- 上级拥有更大权限和更完整能力索引。
- 缺能力可以逐层上抛。
- 找到能力后沿链路下发。

## 安全边界

默认安全行为：
- `subagent-run` 默认 dry-run。
- `subagent-run --execute` 才调用模型。
- 执行前默认 probe。
- 工具有 allowlist。
- 未授权工具调用会失败。
- patches 不自动 apply。
- lessons 不自动写 skill。
- 自学习默认关闭。

## 当前还缺什么

优先级高：
- 多子代理调度器：批量启动、限流、心跳、超时、接管。
- 接受层：从 `output.json.tests` / `artifacts` 自动生成验收任务。
- patch 集成器：读取真实 diff/patch，按权限、owner 和审核结果做受控集成。
- lessons -> learning draft：在 `enable_self_learning=true` 时生成草稿。
- 跨层能力上抛：父代理找不到时继续向爷代理或更高层抛。
- ACP / adapter：接外部 agent session 或远端执行器。

## 睡前检查清单

一轮开发收尾时建议做：

```bash
python3 -m py_compile agent_py_agent/agent/*.py agent_py_agent/__main__.py
python3 -m agent_py_agent --help
python3 -m agent_py_agent subagent-run --help
python3 -m agent_py_agent subagents-patches --help
git diff --check
git status --short
```

收口时运行完整 `python3 agent_py_agent/tests/run_tests.py`，它会触发真实 API，并使用临时配置隔离测试数据。
