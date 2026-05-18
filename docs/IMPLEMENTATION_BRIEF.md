# IMPLEMENTATION_BRIEF

这份文档是给执行者（会话运行时/AI/人类）看的需求说明。每个需求包含：解决问题、范围、约束、关键文件、验收标准。

开工前读 `LLM_GUIDE.md` 和 `docs/ROADMAP.md`。收工后更新 `ROADMAP.md` 和 `COMPLETED.md`。

---

## 需求 1：Workflow Router/Compiler 集成

### 解决问题

用户必须手写 goal 和 acceptance，workflow 集成后可以从模板自动生成 worker spec 和验收条件，减少人工配置，缓解"派错工、漏验收、机械 PASS、fake done"。

### 范围

1. **修复 `task.raw_json` bug**：`manager_base.py` 的 `create_run()` 里有 `task.raw_json["workflow_plan"] = result`，但 `SubAgentTask` 没有 `raw_json` 字段。需要决定存储位置（建议存到 `task.workflow_plan` 字段或 `execution_context` 中）。

2. **接通调用方**：至少一个调用方传 `workflow_mode != "off"`。当前 `create_run()` 已有 `workflow_mode` 参数但无人传值。建议在 `dispatch_subagents` 工具或 `subagents-dispatch` CLI 中增加 `--workflow-mode` 参数。

3. **Workflow Router**：自然语言任务 → 匹配 `subagent_workflows/` 下的内置 JSON 模板。已有模板加载和校验基础设施。

4. **Workflow Compiler**：匹配到的模板 → 生成 worker spec（包含 QualityContract、acceptance_checks、context_packs）。

5. **Parent Gate**：派工前生成可执行计划，合并到父级验收 checklist。

### 约束

- `workflow_mode` 三档：`off`（默认）/ `plan`（只生成计划不自动派工）/ `auto`（自动派工）
- 不改变现有 subagent 状态机，只在 `create_run()` 入口增加规划步骤
- 模板 schema 校验已存在，不要重复实现
- QualityContract 和 ContextManifest 已有数据结构，直接复用

### 关键文件

- `agent_py_agent/agent/subagents/manager_base.py`：`create_run()` 和 `_try_workflow_plan()`（约 209 行附近）
- `agent_py_agent/agent/subagent_workflows/`：模板加载、校验、planner
- `agent_py_agent/agent/subagents/models.py`：`SubAgentTask`、`SubAgentExecutionContext`、`QualityContract`
- `agent_py_agent/agent/subagent_workflows/planner.py`：`plan_workflow_for_goal()`
- `agent_py_agent/cli/subagents.py`：CLI 命令入口
- `agent_py_agent/agent/agent_core/orchestration_tools.py`：`dispatch_subagents` 工具

### 验收标准

- [ ] `task.raw_json` bug 修复，`create_run()` 在 `workflow_mode != "off"` 时不崩溃
- [ ] `my-agent subagents-dispatch --workflow-mode plan` 能生成 workflow plan 并写入任务
- [ ] `my-agent subagents-dispatch --workflow-mode auto` 能自动派工
- [ ] 至少一个内置模板能被 Router 匹配、Compiler 编译、Parent Gate 验收
- [ ] `python3 -m pytest -q` 全量通过
- [ ] 更新 `docs/ROADMAP.md` 和 `docs/COMPLETED.md`

---

## 需求 2：Patch 自动应用

### 解决问题

runner 输出的 patches 目前只是计划/状态记录，必须由父代理或集成器验收后手动处理，无法自动应用和集成验证。

### 范围

1. **设计独立 patch apply 审核链路**：不能由验收器直接应用未知 patch。需要新增 `PatchApplyReview` 结构，记录 diff 内容、apply 状态、回滚信息。

2. **命令 allowlist**：patch 只能修改 `allowed_write_roots` 内的文件，不能修改锁定文件和禁区（HOME、Desktop、Downloads 等）。

3. **diff 审计**：apply 前必须记录完整 diff，apply 后记录实际变更。

4. **集成验收**：apply 后触发测试命令（如果 acceptance_checks 中有可执行测试项），测试失败则回滚。

5. **dry-run 模式**：默认 dry-run，只展示将要 apply 的 diff，不真正修改文件。显式 `--apply` 才执行。

### 约束

- **设计原则**：DESIGN_LEDGER.md 明确写"patch apply 需要独立审核链路，不能由验收器直接应用未知 patch"。本次实现必须建立这条独立链路，不能绕过。
- 只处理 `write_file` 类型的 patch（文件写入），不处理 `shell_command` 类型
- apply 前检查文件是否被锁定（`locked_files`）
- apply 失败必须回滚到 apply 前状态
- 所有 apply 操作写审计日志

### 关键文件

- `agent_py_agent/agent/subagents/manager_base.py`：现有 patch review 逻辑
- `agent_py_agent/agent/subagents/models.py`：`PatchReviewRecord`、`PatchReviewReport`
- `agent_py_agent/agent/tooling/write_boundary.py`：写入边界检查
- `agent_py_agent/cli/subagents.py`：`subagents-patches` 命令

### 验收标准

- [ ] 新增 `PatchApplyRecord` / `PatchApplyReport` 数据结构
- [ ] `subagents-patches --apply-dry-run` 展示将要 apply 的 diff
- [ ] `subagents-patches --apply` 真正写入文件并记录审计日志
- [ ] apply 超出 `allowed_write_roots` 时拒绝并记录
- [ ] apply 后文件内容与 diff 一致
- [ ] apply 失败时回滚到原始状态
- [ ] `python3 -m pytest -q` 全量通过
- [ ] 更新 `docs/ROADMAP.md` 和 `docs/COMPLETED.md`

---

## 需求 3：Lessons 自动生成自学习草稿

### 解决问题

runner 输出的 lessons 只写到 output.json 和 DEBRIEF.md，无法聚合、无法被后续任务引用、无法沉淀成 skill。

### 范围

1. **LearningCandidate 数据结构**：从 runner output.json 提取 lessons，生成结构化候选草稿。字段包括：来源 run_id、lesson 内文、关联 capability_gap、建议 skill/tool、置信度、创建时间。

2. **学习候选存储**：`agent_py_agent/data/learning_drafts/` 目录，每个候选一个 JSON 文件，按时间戳命名。

3. **聚合逻辑**：相同或相似的 lessons 去重合并，提升置信度。

4. **CLI 命令**：
   - `my-agent learn list`：查看所有候选
   - `my-agent learn accept <id>`：只把 learning draft 标记为 `accepted`，不生成 `SKILL.md`，不安装正式 skill
   - `my-agent learn reject <id>`：拒绝候选
   - `my-agent learn stats`：统计候选数量、接受率

5. **与 capability_gap 关联**：如果 lesson 来源于 capability_gap，标记关联关系。

### 约束

- **设计原则**：必须走"生成候选草稿 → 用户确认 → 显式导出/安装"路径，不能自动提升。
- `enable_self_learning` 配置开关已存在，默认关闭。只在开关打开时才生成候选。
- 不改变现有 skill 加载和路由逻辑，只新增候选生成和确认流程。
- 候选草稿必须记录来源证据（run_id、output.json 路径），方便审计。
- 当前 `learn accept` 只做 review/status gate；生成 skill draft 走 memory gate 的显式 `subagents-memory-gate --export-skill` 流程。

### 关键文件

- `agent_py_agent/agent/subagents/manager_base.py`：`record_runner_result()` 附近，runner 完成后触发候选生成
- `agent_py_agent/agent/subagents/models.py`：现有 runner output 解析
- `agent_py_agent/config/agent_config.yaml`：`enable_self_learning` 配置
- `agent_py_agent/data/learning_drafts/`：新建目录，存放候选 JSON
- `agent_py_agent/cli/`：新增 `learn` 命令组

### 验收标准

- [ ] `LearningCandidate` 数据结构定义
- [ ] runner 完成且 `enable_self_learning=true` 时，lessons 自动写入 `learning_drafts/`
- [ ] 相同 lessons 去重合并，置信度提升
- [ ] `my-agent learn list` 展示候选列表
- [ ] `my-agent learn accept <id>` 标记 accepted，且明确不生成 `SKILL.md`
- [ ] `my-agent learn reject <id>` 标记拒绝
- [ ] `enable_self_learning=false` 时不生成候选
- [ ] `python3 -m pytest -q` 全量通过
- [ ] 更新 `docs/ROADMAP.md` 和 `docs/COMPLETED.md`

---

## 需求 4：并行 Worker Pool

### 解决问题

当前 `threading.ThreadPoolExecutor` 默认 concurrency=1，多任务串行执行，效率低。`runner_concurrency` 和 `runner_start_rate` 配置已存在但未接到后台 worker pool。

### 范围

1. **Runner Worker Pool**：把 `runner_concurrency` 配置接到真实的线程池。当有多个 pending run 时，按 concurrency 数量并行执行 runner。

2. **调度逻辑改造**：`dispatch_subagents()` 中的 runner 执行部分，从串行循环改为线程池 `submit` + `as_completed`。

3. **结果收集**：并行 runner 的结果必须正确写回各自的工单目录，不能互相干扰（工单目录天然隔离，只需确保无共享状态）。

4. **超时控制**：单个 runner 有 `runner_timeout_seconds` 配置。固定秒数会在超时后标记 `TIMEOUT` 并继续其他 runner；默认 `off` 表示不套外层超时，适合当前真实 E2E 和长任务压测；`auto` 才使用动态超时预算。

5. **失败隔离**：单个 runner 失败不影响其他 runner 继续执行。

### 约束

- 默认 `runner_concurrency=1`，保持现有串行行为不变
- 不引入新依赖，继续使用标准库 `threading` 或 `concurrent.futures`
- 不做进程级隔离（那是后续需求），当前只做线程级并行
- runner 之间通过文件系统隔离（工单目录），不共享内存状态
- `runner_start_rate` 控制每轮启动速率，避免同时启动太多 API 调用

### 关键文件

- `agent_py_agent/agent/agent_core/dispatch.py` 或 `agent_py_agent/agent/agent_core/runner_dispatch.py`：runner 执行逻辑
- `agent_py_agent/agent/settings/config.py`：`runner_concurrency`、`runner_start_rate`、`runner_timeout_seconds` 配置读取
- `agent_py_agent/cli/subagents.py`：`subagents-dispatch` 命令
- `agent_py_agent/cli/daemon.py`：daemon 模式下的 runner 执行

### 验收标准

- [ ] `runner_concurrency=2` 时，两个 pending run 并行执行
- [ ] 并行 runner 的结果正确写回各自工单目录
- [ ] 单个 runner 超时不影响其他 runner
- [ ] 单个 runner 失败不影响其他 runner
- [ ] `runner_concurrency=1` 时行为与现有完全一致
- [ ] `runner_start_rate` 限制每轮启动数量
- [ ] `python3 -m pytest -q` 全量通过
- [ ] 更新 `docs/ROADMAP.md` 和 `docs/COMPLETED.md`

---

## 通用要求

1. 每个需求独立可交付，不依赖其他需求先完成。
2. 改代码前先读 `LLM_GUIDE.md`、`docs/ROADMAP.md`、`agent/DIRECTORY_GUIDE.md`。
3. 新增测试用 `test_*.py` 前缀，`run_tests.py` 会自动发现。
4. 完整冒烟默认调用真实 API。
5. 收工后更新 `docs/ROADMAP.md` 和 `docs/COMPLETED.md`，每个功能写"解决问题"。
6. 注释用两层格式：第一行 `LLM:` 给 AI 读，第二行开始给人读。
