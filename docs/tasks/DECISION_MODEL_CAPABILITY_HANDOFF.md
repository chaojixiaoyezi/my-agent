# Workstream Handoff

## 基本信息

- workstream：决策模型 TODO10 能力推荐与上下文减量
- branch：`codex/decision-model-integration`
- baseline：`ffb44df21`，独立本地工作区
- date：2026-09-22
- 状态：本地接线及定向联合验收完成，未发布部署，不代表真实供应商效果。

## 目标与完成

解决推荐只是增加文字、无关名卡和工具schema仍全部发送的问题。每工作片在原运行准备入口调用一次决策；
原设置、worker、账本、Skill范围、Registry、ToolExecutor和搜索均复用，短名单沿原种子/参数进入PromptBuilder/native schema。

- 原Skill名卡可移到动态推荐区，稳定区保留发现说明；None保持旧字节行为。task_local仅展示原受限快照，isolated/control_plane不增加材料。
- ToolRuntimeSnapshot新增两项不可变展示字段，不改snapshot_hash、runtimes、allowed或handler。progressive收起明确可选类别，原搜索可恢复完整schema；metadata仅调整文字展示。
- 显式allowed、发现入口、原loaded及宿主必要引用保持；原默认deferred类别不变。没有可见发现入口时不隐藏相关能力。
- `points.skill_tool.context_policy`默认progressive，`optional_categories`默认`["plugins"]`，原总/点开关仍off；开放字符串列表、空列表合法。
- 原设置登记、owner/thread CAS/reset、user_config及菜单共用字段；原v1可选覆盖加法，不写默认，不建第二存储。
- 候选按240项分组、每组最多8槽，candidate_N引用state唯一说明，500项本地合法。原64题/255选项/256KiB/节点限制仍有效，超限整体沿原输入。
- 缺目标/工具合同/Skill步骤/环境、弃权或无匹配保持原输入；明确not_needed可留空槽，必要能力仍保留。不自动补读或扩大权限。
- 采用前复核原Skill范围/版本、固定handler的availability、原配置/模型/窗口/任务属性和期限。插件停用不能通过同名新代重绑恢复。

## 文件范围

- `capability/decision_candidates.py`、`decision_recommendation.py`：原候选和可选消费。
- `capability/router.py`、`prompting_parts/builder.py`：原Skill名卡展示。
- `tooling/models.py`、`registry.py`：原工具展示和发现。
- `agent_core/runtime/loop_models.py`、`loop_support.py`、`_runtime_params.py`、`_tool_loop_service.py`：当前工作片的原参数接线。
- 原capability配置、decision设置登记/投影、decision_policy、user_config和TUI菜单：两项原设置字段。
- 新5个测试与原scope断言，中文设计、入口、树、进度及本交接。

子片证据见 [Skill投影](DECISION_MODEL_SKILL_PROJECTION_HANDOFF.md)、[工具投影](DECISION_MODEL_TOOL_PRESENTATION_HANDOFF.md)、
[设置](DECISION_MODEL_SKILL_TOOL_SETTINGS_HANDOFF.md)、[HTTP组合](DECISION_MODEL_CAPABILITY_HTTP_HANDOFF.md)。

## 验证

```bash
python3 -m pytest -o addopts='' agent_py_agent/tests/test_decision_capability_consumer.py agent_py_agent/tests/test_decision_capability_http.py agent_py_agent/tests/test_decision_skill_projection.py agent_py_agent/tests/test_tool_presentation_projection.py agent_py_agent/tests/test_decision_skill_tool_settings.py agent_py_agent/tests/test_decision_settings_notifications.py agent_py_agent/tests/test_tui_decision_menu.py agent_py_agent/tests/test_decision_settings_scope.py -q --tb=short
python3 -m pytest agent_py_agent/tests/test_decision_capability_consumer.py agent_py_agent/tests/test_decision_skill_projection.py agent_py_agent/tests/test_tool_presentation_projection.py agent_py_agent/tests/test_memory_runtime_compact_auto_continuation.py agent_py_agent/tests/test_compact_semantic_summary.py -q --tb=short
python3 -m ruff check agent_py_agent scripts
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
```

第一组127项通过（17.89秒），第二组通过；Ruff/doc sync/diff通过，strict-size无硬阻断，未放宽基线。
本地原生HTTP确实经过正式传输/worker/账本；响应是自有协议样本，主生成输入使用原PromptBuilder和工具schema转换，未调用收费主模型。
不因本地测试成功声明安装版TUI已具备功能、判断质量提升或实际费用减少。

## 协调与风险

主线已确认目录/快照/loop接缝和seed/params字段无冲突；当前主线已到`d4d540c57`，本片尚未rebase合并。
最终集成须把本基线的`tooling.cancellation`导入迁到主线`common.cancellation`，不能恢复已删除facade。
主线反馈测试机磁盘不足；本线不在测试机新增构建/压测，不重启Gateway、不改日常设置。
Python阻塞文件IO不能强杀；最后期限检查拒绝迟到建议，原HTTP等待和实际worker资源仍有界。
原搜索可达不等于主模型一定会选对能力，质量与遗漏率需要真实Jev和MiniMax验证。

## 建议下一步

先做12完整子代理上下文、模型窗口、输出预留和缓存/Compact组合检查，再做13真实模型验收。
max可独立调查预算接缝，主代理整合；共享运行入口先明确归属，保持默认关闭，不把额外展示变成权限或持久加载权威。
