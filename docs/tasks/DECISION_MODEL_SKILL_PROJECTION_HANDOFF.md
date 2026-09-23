# Workstream Handoff

## 基本信息

- workstream：TODO10 / Skill 名卡展示投影独立子片
- branch：`codex/decision-model-integration`
- worktree：现有 `decision-model-plan/my-agent-dsh` 工作台
- owner：父代理下的 decision_settings_review 子代理
- date：2026-09-22

## 本线目标

减少主模型上下文中的无关 Skill 名卡，同时保留原完整授权目录、原搜索与正文读取，不把推荐当作执行权或已读正文。

## 实际完成

- `ToolSections` 新增本次 build 的宿主字段：`selected_skill_ids: tuple[str, ...] | None = None`、`required_skill_ids: tuple[str, ...] = ()`。调用可继续用 `PromptBuilder.build(..., tools=ToolSections(...))`，也支持原 `PromptBuildRequest.tools`；不增加 build 顶层参数，不在 builder/agent/Router 上写共享选择状态。
- `CapabilityRouter.render_skill_metadata_index(*, context_window_tokens=None, selected_skill_ids=None, required_skill_ids=())` 沿原授权卡生成展示投影。None 不走新选择逻辑，旧内容及预算规则保持；非 None 只匹配原 stable_id，名称别名、未知 ID 和当前范围之外的 required ID 均不能扩大授权。
- required 与选中项取并集；必要名卡优先，并至少保留 name/stable_id 行，不因 2% 预算而被静默删掉。极小窗口允许这些明确必要行超过软元数据预算；可选项继续受原剩余预算控制。
- 非 None 时，原稳定前缀只保留固定 `skill_search search/get` 发现说明；名卡进入已有 `prompt.tool_recommendations` 动态来源，复用原工具推荐段落，不新建缓存或持久索引。
- 空选择或仅未知 ID 不渲染名卡，发现入口保留。更换选中项不改变该模式的稳定前缀；从旧完整模式切到选择模式会改变一次前缀，不能宣称跨模式缓存完全相同。
- `task_local` 只有非 None 投影可将原受限 Router 的名卡放入动态段；None 保持原不展示，稳定前缀不恢复人格/项目/完整 Skill 索引。`isolated`、`control_plane` 不新增投影内容，保持各自原输出。
- 不修改 SkillSnapshot、Router 卡集合、SkillSearchTool、正文加载、身份或授权；展示准备不调用 `read_body`。采纳前的源版本/权限刷新仍由父侧决策消费者执行，渲染器只消费当下原 scoped Router。

## 改动文件

- `agent_py_agent/agent/capability/router.py`：原名卡渲染可选参数及选择预算辅助函数。
- `agent_py_agent/agent/prompting_parts/builder.py`：ToolSections 字段、稳定/动态 Skill 展示接线。
- 新增 `agent_py_agent/tests/test_decision_skill_projection.py`。
- 本交接文件。

未修改 loop_support、ToolRuntimeSnapshot、Registry、设置/config、插件目录、执行器或共享模块文档。

## 测试命令和结果

```bash
python3 -m pytest agent_py_agent/tests/test_decision_skill_projection.py agent_py_agent/tests/test_skill_tree_and_recall.py agent_py_agent/tests/test_prompting_builder.py -q --tb=short
python3 -m ruff check agent_py_agent/agent/capability/router.py agent_py_agent/agent/prompting_parts/builder.py agent_py_agent/tests/test_decision_skill_projection.py
git diff --check
```

结果：95 项通过（本片 10 项、原相关 85 项）；相关 Ruff、diff 检查通过。通过原 `check_code_size._check_ast/compute_strict_blockers` 对两个修改模块运行只读定向检查，0 个 strict blocker；未写共享 CODE_SIZE_REPORT。

额外直接加载 HEAD 原 router/builder，与当前同一真实 SkillsService、相同 prompt 参数对照：None 的完整字符串和 cache_layout 逐字一致。60 个 Skill 只展示 2 个时，真实 native PromptBuilder 产物从 13,977 降为 5,980 UTF-8 字节；这是夹具字节量，不是收费 token、真实缓存命中或质量收益。

覆盖：None 旧布局、原 request/direct build 双入口、真实输入减量、名卡动态位置、前缀跨选择稳定、正文不预读、省略项由原 SkillSearchTool 搜索并 get、极小预算必要行、未知和别名 ID、受限 SkillSnapshot、失效候选、并发同 builder 请求隔离，以及 task_local 下真实 restricted SkillSnapshot 保留必要卡且不能读出范围外卡、isolated/control_plane 逐字旧行为。

## 影响范围

- 只有宿主传非 None 选择时启用；未接消费入口的现有调用继续旧行为。
- None 不考虑 required 展示参数，严格保持原输出；父侧需要必要短卡时必须同时传非 None 选择。
- required 来源必须是宿主已验证 refs/grants，不能从普通自然语言推断身份或自动扩权。
- 不增加持久字段，无 schema migration；仍需父侧统一模块文档和 CODEBASE_TREE。

## 需要主线重点复查

- 本片只是展示消费接口，没有请求决策模型，也不替父侧做技能候选版本及决策期限复核。父侧应在成功 apply 且最终范围核对后传入选择，off/observe/error 保持 None。
- 原搜索与正文 get 的完整授权快照必须保持；不要把被省略 ID 从 Router/SkillSnapshot 删除。
- 工具 schema 减量由另一子片完成；本片不能单独代表完整 TODO10 已完成。

## 需要其他线协调

父侧接原决策服务与本次 params；max 子代理接工具延迟披露。新 Skill 参数由 ToolSections 携带，两条线不要在共享 Agent 上缓存选择。

## 剩余风险

尚未检验真实决策模型选择质量、provider 缓存收益或完整 TODO10 组合请求。没有收费模型调用、提交、推送或部署；全仓严格 gate 与共享文档由父侧统一收口。

## 后续建议

建议下一步：父侧组合决策成功/观察/关闭/失效范围与实际生成调用，沿已确认 task_local 受限快照接缝，再做工具与 Skill 同请求的输入减量和原发现恢复验证。可与工具披露子片并行，但不能改原权限或把该展示接口当作执行授权。
