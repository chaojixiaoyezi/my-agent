# TODO10 工具展示投影交接

分工：decision_http_max，Astra max；父侧拥有决策消费、配置可选类别与工作片接线，
Skill 渲染由 decision_settings_review 负责。插件线已确认本片精确接口无重叠。
没有提交、推送、部署、收费模型或共享文档修改。

## 接口与行为

原 `ToolRuntimeSnapshot` 新增两个可选字段，均默认 `None`：

```python
presentation_deferred_names: frozenset[str] | None = None
presentation_shortlist_names: frozenset[str] | None = None
```

宿主通过 `dataclasses.replace(snapshot, ...)` 生成本工作片投影。父层须从原配置明确允许的
可选类别和当前授权候选生成名称；本片不复制配置或权限算法，也不解析模型正文授权。
`__post_init__` 仅检查不可变类型、原快照名称子集，以及额外收起后的发现入口条件。
`runtimes`、handlers、`available_tool_names`、`allowed_tools` 和 `snapshot_hash` 保持原权威。
展示字段不参加原 hash，不构成新注册代次、Registry 或跨工作片选择状态。

`presentation_deferred_names` 是额外收起原 direct schema 的名称集合：

- 禁止收起当前快照中的 `tool_search`、`list_tools`、`skill_search`。
- 非空时必须有原可用且模型可见的 `tool_search`，否则构造投影失败，由宿主恢复原快照。
- 若既有 category 配置已经把 `tool_search` 自己折叠，Registry 放弃本轮额外收起。
- 显式 `allowed_tools` 和原实际搜索产生的 loaded 名称优先；不凭短名单假造 loaded。
- 原 `search_deferred_specs` 在原 deferred category 与额外收起名称的并集中搜索，
  使用同一个 retriever，返回同一原 spec/schema。被收起工具不会因短名单未提及而失联。

`presentation_shortlist_names` 只缩原推荐名卡和折叠提示中的名字：

- metadata 模式只填写此字段，实际 direct schema 不收起。
- 原 deferred 工具即使被选中，也必须经真实 `tool_search` 才进入 loaded schema。
- 原完整 `list_tools` manifest 和 `tool_search` 可达范围不裁剪。
- 短名单为空不显示“没有授权工具”，仍提示原发现入口。
- 两字段为 `None` 时，保留原 schema、推荐和目录输出；折叠提示逐字保持原格式。

## 生产文件

- `tooling/models.py`：`ToolRuntimeSnapshot` 字段及客观校验；发现入口名称集合。
- `tooling/registry.py`：原 `model_visible_specs` / `search_deferred_specs`、目录和推荐名字渲染。
- 新 `tests/test_tool_presentation_projection.py`：24 项独立合同与实际组件测试。

未修改 builder、router、loop_support、配置、原执行器、插件生命周期、激活表或 transport。
`PluginProxyTool.availability` 及原固定 activation/transport 的只读复查继续由原链负责。

## 验证

```bash
python3 -m pytest agent_py_agent/tests/test_tool_presentation_projection.py agent_py_agent/tests/test_tool_progressive_disclosure.py agent_py_agent/tests/test_tool_runtime_scope.py agent_py_agent/tests/test_tool_runtime_unification.py agent_py_agent/tests/test_native_tool_protocol_wiring.py agent_py_agent/tests/test_backends_tool_schema_precise.py agent_py_agent/tests/test_plugin_registry.py -o addopts='' -q --tb=short
```

联合结果：106 passed，14.91 秒；新增 24 项单独 4.30 秒通过。
用原 Provider schema 转换器证明实际 schema 减少超过 1000 字符；经真实 ToolExecutor 的
tool_search 返回原完整 schema/hash 和 typed loaded 名称，再恢复同一原生 schema。
覆盖默认输出、metadata 不删 direct、原 deferred 不伪装 loaded、显式 allowed、完整搜索/manifest、
发现入口缺失、越界/不可用/可变名称拒绝，以及相邻 runtime/native 原合同。

实际临时插件使用原安装、启用、Registry 准备与停用组件：投影仍持同一 PluginProxyTool、
同一 snapshot_hash，停用后旧投影调用失败，新快照不再含该工具；没有重绑或重启旁路。
所有临时插件进程沿原 cleanup 收尾。本片不以该组件证据冒充真实模型或 TUI 验收。

所改生产和测试 Ruff 通过，所改生产 strict code-size 为 0 blocker，`git diff --check` 通过。
未跑全仓 pytest 或线上 CI。

参考核对：原渐进发现/ToolRuntimeSnapshot/PluginProxyTool 的实现与测试；本地 Codex
`codex-rs/core/src/tools/handlers/tool_search.rs` 的 deferred 搜索和原 schema 返回方式。
未引入对照项目的注册表、缓存或生命周期实现。

## 建议下一步

父侧接本工作片决策投影并做配置关闭、metadata、progressive 的完整请求对照；
仅从原配置的 optional categories 产生可收起候选，失败保留原 snapshot，真实 loaded 和显式 allowed 继续优先。
Skill 子片可并行；统一验收应检查真正发往主模型的原生 schema、原搜索后的下一轮展开以及插件撤销，
不要以模型自述“工具已加载”替代结构化事实。共享结构、Goal 和测试文档由父侧同步。
