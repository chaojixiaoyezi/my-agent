# TODO11 决策设置作用域交接

分工：decision_http_max，Astra max。父侧统一共享文档、原模型操作/探测和提交；
菜单侧 decision_settings_review 独占 TUI。此片没有提交、推送、部署或收费模型调用。

## 解决问题与结果

原投影先叠加所有线程覆盖，再计算各接入点，使 Curator 显示和配置受线程总开关、模型及前台阶段预算影响。
现在 Curator 只取原默认加 owner 覆盖，后台请求上限为该点等待时间与后台预算的较小值。
其他前台点保持原默认 → owner → thread 继承、字段来源与阶段预算行为。

新的 thread patch 拒绝 `background_timeout_seconds` 和 `points.curator.*`，整笔失败不落盘或通知。
持久 schema 仍接受旧版本曾允许的后台线程字段：read 保留原值供展示，不参与有效计算，
reset 使用原字段白名单删除它们。清理同样要求 owner/thread 双层 CAS，保留线程摘要、Compact 和其他字段。
未新增配置默认值、配置文件、解析器、存储或迁移写入；`decision_settings.v1` 信封保持原结构。

父侧追加授权的运行边界已同步：`begin_decision_stage` 只列出符合当前 scope 的启用点，
`decide` 在调用前拒绝错误 point/scope，返回原 `error/invalid_input`，不创建模型调用。
实际 Curator 保持 `owner_background`，其余现有接入点保持 `thread`。

## 菜单与服务共用接口

权威登记在原 `settings/decision_settings_schema.py`：

- `POINT_RUNTIME_SCOPES`：不可变的接入点运行范围；原 `POINTS` 从它派生。
- `decision_field_scopes()`：返回原全部扁平字段的可 patch 范围副本；后台字段只有 `owner`，其他字段为 `owner`、`thread`。
- `validate_decision_field(..., scope=...)`：新 patch 使用宿主校验后的 scope；持久读取和 reset 不传 scope。

原 `decision_settings_view.v1` 读回新增：

- 顶层 `field_scopes`：上述字段到范围列表的完整映射。菜单应依此过滤可写字段；它不是身份权限凭据。
- 每个 `effective.points.<point>` 的 `runtime_scope`、`enabled`、`enabled_source`：对应此点实际使用的范围、总开关和来源。
- Curator 原 `effective_mode`、`profile_id`、`timeout_seconds` 和其 `sources` 按 owner 计算；
  `max_request_seconds` 不读前台 `stage_timeout_seconds`，`limiting_field` 对应真实限制字段。

菜单侧已收到并开始按 `field_scopes` 筛选；reset 列表可读取 `overrides.thread` 中的旧后台字段。
时间投影是新请求的静态上限，原运行中 stage 和 caller 绝对期限继续只能缩短，不能被设置更新延长。

## 文件与验证

生产文件：原 `settings/decision_settings_schema.py`、`decision_settings.py`、
`decision_settings_projection.py`；追加授权的 `conversation/decision_service.py` 仅接阶段筛选和调用范围校验。
新增测试：`tests/test_decision_settings_scope.py`，14 项。

```bash
python3 -m pytest agent_py_agent/tests/test_decision_settings_scope.py agent_py_agent/tests/test_decision_settings.py agent_py_agent/tests/test_decision_settings_notifications.py agent_py_agent/tests/test_decision_service.py agent_py_agent/tests/test_decision_owner_scope.py agent_py_agent/tests/test_decision_service_http.py -o addopts='' -q --tb=short
```

联合结果：109 passed，1.10 秒。涵盖原 CAS/继承、提交后通知、服务取消和本地真实 HTTP；
新测试验证范围拒绝不落盘、旧覆盖清理、owner/thread 双向开关与模型隔离、后台预算和实际调用期限一致。
所改文件 Ruff 通过，所改生产文件 strict code-size 为 0 blocker，`git diff --check` 通过。
未跑全仓 pytest，未将线上 CI、真实 Jev 或 TUI 真机作为本片验收来源。

参考核对：本仓库原配置/覆盖/通知及 Curator 后台合同；本地 Codex 参考
`codex-rs/config/src/state.rs` 的 `effective_config` / `origins`，确认有效层和值来源应使用同一层级规则。
未引入其配置框架，只在本仓库原投影与校验中修正范围。

## 建议下一步

父侧把本片与原设置分发、native probe、菜单联合验收，并同步共享 Goal、结构和测试文档。
菜单可并行验证 owner/thread 切换、旧覆盖 reset 及 Curator 后台说明；不要把字段元数据当身份授权，
也不要因显示时间改变重置已开始阶段的期限。此片文件已稳定，可进入统一 gate。
