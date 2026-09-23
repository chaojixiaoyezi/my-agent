# Workstream Handoff

## 基本信息

- workstream：TODO10 / Skill-tool 减量策略的原配置与 TUI 子片
- branch：`codex/decision-model-integration`
- worktree：现有 `decision-model-plan/my-agent-dsh` 工作台
- owner：父代理下的 decision_settings_review 子代理
- date：2026-09-22

## 本线目标

让用户和原模型配置工具通过同一个设置服务选择 Skill/tool 上下文减量策略及可选工具类别；保存不联网，不改默认授权，也不隐式开启决策。

## 实际完成

- 唯一字段登记在原 `decision_settings_schema`：仅 `skill_tool` 增加 `context_policy` 与 `optional_categories`。`decision_point_fields(point)` 提供点的完整字段，`decision_field_schema(path)` 提供原模型配置工具的类型与枚举；其它接入点没有这两个字段。
- 原 `CapabilityConfig` 与随包 YAML 提供 `decision_skill_tool_context_policy="progressive"`、`decision_skill_tool_optional_categories=["plugins"]`；总开关仍 false、点 mode 仍 off。列表采用 dataclass default_factory，每次原有效值校验也复制列表，不共享可变默认。
- `points.skill_tool.context_policy` 接受 metadata/progressive；`points.skill_tool.optional_categories` 只接受 list[str]，空列表合法，开放自定义类别，不枚举供应商或插件类别。拒绝字符串冒充数组、元组、数字、bool、null、嵌套项、空或纯空白字符串；不静默转型。
- 原 defaults/projection/field_scopes 根据同一登记读回 owner/thread 有效值、来源和覆盖。新字段均支持 owner/thread CAS patch/reset；修改和 reset 上限按已登记字段数计算，支持完整合法字段集合。
- 原 `UserConfigTool.changes` schema 消费同一字段类型表；原身份解析、CAS、权限和结构化失败不变，无新模型工具。
- TUI 只在 Skill/tool 接入点增加策略 RadioList 和类别 JSON 字符串数组表单；显示确认后的有效值及来源，非法输入不保存；明确 `[]` 不额外收起、恢复继承删除覆盖。菜单说明减量可能改变缓存前缀/schema，原搜索与执行权限不变。
- 原 `decision_policy.routing_signature` 包含当前 skill_tool 有效策略与类别。对应变更精确取消在途 skill_tool，其他点不受该通知影响；owner 改动被 thread 遮蔽时不取消，后续 reset 暴露改变时取消。
- 原 `_policy_revision` 已包含整个 effective；原配置默认变化即使不改变持久 CAS revision，也会改变采用前复核摘要。

## 改动文件

- `agent_py_agent/agent/settings/decision_settings_schema.py`
- `agent_py_agent/agent/settings/decision_settings_defaults.py`
- `agent_py_agent/agent/settings/decision_settings_projection.py`
- `agent_py_agent/agent/settings/decision_settings.py`
- `agent_py_agent/agent/capability/config.py`
- `agent_py_agent/config/capability_config.yaml`
- `agent_py_agent/agent/tooling/user_config_tool.py`：仅 `_decision_change_properties`
- `agent_py_agent/agent/conversation/decision_policy.py`：仅 `routing_signature`
- `agent_py_agent/cli/chat_parts/tui_decision_menu.py`
- 新增 `agent_py_agent/tests/test_decision_skill_tool_settings.py`
- `agent_py_agent/tests/test_decision_settings_scope.py`：完整字段集合断言改用点级原登记函数
- 本交接文件

未修改工具 Registry/ToolRuntimeSnapshot、loop_support、builder/router 或共享文档；实际策略消费由父侧与工具披露子片接入。

## 持久兼容记录

这是原 `decision_settings.v1` 中可选 override 字段的加法扩展，不改变信封键、revision、owner 模型顶层 schema 或 thread schema。旧 v1 读回仍保留原字段和 revision，不把新默认复制到持久覆盖，不发生读时写回。reset 删除新覆盖后动态恢复原模块默认/上层覆盖。

旧版本程序不识别新增覆盖时会按原未知字段校验拒绝读取，而不是丢字段后写回；不得把这种旧版本读取能力描述成双向兼容。没有新增配置库或凭据库。

## 测试命令和结果

```bash
python3 -m pytest agent_py_agent/tests/test_decision_skill_tool_settings.py agent_py_agent/tests/test_decision_settings.py agent_py_agent/tests/test_decision_settings_scope.py agent_py_agent/tests/test_decision_settings_notifications.py agent_py_agent/tests/test_tui_decision_menu.py agent_py_agent/tests/test_user_config_decision_operations.py -q --tb=short
python3 -m ruff check <本片修改的 Python 文件>
git diff --check
```

结果：148 项通过（本片 30 项、原相关 118 项），diff 检查通过。

本片定向 strict-size 检查复用原 `_check_ast/compute_strict_blockers`，没有写共享 CODE_SIZE_REPORT；0 个 blocker。Ruff 通过。

覆盖默认/YAML一致性、无默认落盘、v1兼容、开放类别和严格类型、仅本点字段、owner/thread CAS与reset、原模型工具 schema及操作、有效默认改变的版本失效、精确通知与遮蔽后reset，以及真实 prompt_toolkit pipe 保存策略/类别、非法字符串数组不保存、恢复继承且不发模型请求。

## 影响范围

- 总开关和点 mode 默认仍关闭，新增策略默认不会自行改变现有模型输入。
- metadata/progressive 的实际生成上下文消费由父侧 TODO10 接线；本片提供可消费配置，不声称已完成整条业务。
- 不修改 `tool_catalog_deferred_categories`，不静默把插件放进原全局 deferred 默认。

## 需要主线重点复查

父侧消费 `effective.points.skill_tool.context_policy/optional_categories`，冻结实际使用值；采用前仍复查原设置版本和当前候选。旧建议不能利用配置变化继续裁剪。原 service 签名含新字段，但不替代消费者的来源/权限复查。

## 需要其他线协调

与原 Skill 展示和工具延迟展示子片按精确字段并行；父侧统一 CODEBASE_TREE、配置说明、TODO与模块文档，再做完整生成请求组合与严格 gate。

## 剩余风险

本片没有模型请求或真实建议质量测试；TUI 测试使用真实输入管道、临时原配置服务及运输 stub。没有提交、推送或部署。

## 后续建议

建议下一步：父侧把两字段与实际披露投影接通，使用 metadata/progressive/关闭三组同候选的原生成请求验证减量、必要工具保留、原搜索找回和配置失效。共享文档可并行，配置 UI 不应被当作消费者已完成的证据。
