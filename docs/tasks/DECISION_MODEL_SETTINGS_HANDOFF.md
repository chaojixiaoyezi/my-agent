# 决策模型 TODO11：原设置入口与显式连接测试

## 基本信息

- branch：`codex/decision-model-integration`
- 相邻基线：`d77d98a6b`，07/09 已本地提交。
- 本片只在独立工作区开发；无真实供应商请求、日常设置修改、部署或远端推送。

## 解决问题与实际接线

用户能从原 `/model` 打开决策设置，选择 owner/thread、模型引用、总开关、逐点模式、秒数和恢复继承。
原 provider 表单负责新增/编辑模型和凭据，保存不联网；失效服务不阻止进入设置关闭。
修改采用原双层 CAS，冲突重读不重放。Curator 仅用户后台 owner 范围，旧线程后台覆盖只允许清理。

`execute_model_profile_operation` 在生成模型初始化前分派决策操作；原 Gateway 路由和冷 owner 配置宿主无需修改。
嵌套 `decision` 载荷调用唯一设置服务，可信 thread_id 只来自原会话解析。
`decision_models` 复用原私有/共享目录，返回脱敏 decision 配置，不返回凭据或创建第二目录。
原 `user_config` 的目录和显式探测动作沿同一入口，主代理不新增后端、写凭据或指定其他身份。

`decision_probe` 使用已保存且可用的 profile_id 和有限正 timeout_seconds，原生单题只带自有连接测试材料。
后端创建已迁出服务私有函数，正常增强和探测共用 `typesafe_decision.decision_backend_from_profile`。
网络走原严格 HTTP、worker、准入、身份头和账本；准备前冻结绝对期限，不重试。
成功只证明本次连接/协议，并清除这一准确连接的冷却；不能证明实际任务选择质量。
结果回显固定诊断字段，未知输入为 null；菜单输出位置留白，无价格，供应商原 usage 留在内部结果和账本。

探测以独立 request_id 调用原 `settle_standalone_model_usage`，只新增可选 source/usage_only 宿主参数；
原 Compact 默认行为保持。用量追加到原会话账本，探测不覆盖主模型 pending、轮次、工具和缓存状态。
薄客户端只对 decision_probe 使用“用户秒数 + 10 秒运输余量”，原 probe/auth/discover 等超时保持。
本地文件结算沿原实现，不承诺操作系统 I/O 可被强制抢占，不新增结算线程或持久账。

## 原范围合同

`POINT_RUNTIME_SCOPES` 同时驱动可写字段范围、投影及服务调用校验。
Curator 使用 owner enabled/profile/background_timeout_seconds，真实静态上限取点时间与后台预算最小值。
新的线程后台 patch 整笔拒绝；历史值保留可读并能按原 CAS reset，前台范围不偷偷控制后台。
见 [作用域交接](DECISION_MODEL_SETTINGS_SCOPE_HANDOFF.md) 与 [真实按键交接](DECISION_MODEL_TUI_HANDOFF.md)。

## 验证与证据边界

最终设置/菜单/原运输/显式探测/原user_config/原用量14文件联合 **243 passed in 14.20s**。
原工具实际调用交接见 [agent操作](DECISION_MODEL_USER_CONFIG_OPERATIONS_HANDOFF.md)。

- 菜单、原运输、本地 HTTP、原模型接口与用量显示六文件组合 81 项通过。
- 设置作用域、配置、通知、服务、owner、HTTP 和原 user_config 七文件组合 117 项通过。
- 共用后端映射和成功后准确连接冷却恢复后，model_operations/gateway_transport/service 三文件 54 项通过。
- 双 HTTP 验证原菜单运输→薄客户端→Gateway 设置服务→本机原生供应商：保存零请求、显式测试、原选择保持、owner 隔离、额度/服务错误后仍可关闭。
- 测试容器替代认证中间件并使用受控身份头；不据此声称真实认证部署或真实 TUI 进程已验。
- 真实 prompt_toolkit 输入管道覆盖表单与 CAS；收费 Jev 判断质量、实际延迟分布及安装版仍属 TODO13。
- 超时用原账本 `timed_out` 验收；残留 worker 的具体异常类可能不同，不能靠异常名称包含 Timeout 判断是否超时。

本片 Ruff、staged doc-sync、staged diff 检查和 strict code-size（hard=0）通过；未放宽基线。
并行 TODO10 未交接文件未计入本片提交；本轮不是远端发布，未宣称全工作树 clean-package 或线上 CI 通过。

## 协调与后续

插件任务已确认模型菜单、配置操作和 `GatewayChatClientAgent.request_models` 的 decision_probe 分支无重叠。
其主线取消入口已迁到 common；本片仍在原基线上，最终集成统一迁入，不恢复旧 facade。
TODO10 正在独立接展示投影，不把其未交接文件计入本片提交或验证。

建议下一步：完成 TODO10 的真实上下文减量及原搜索可达性，再集中做 TODO12 窗口/缓存与并发组合。
可按 Skill 渲染、工具快照展示、决策消费三部分并行；所有隐藏项必须保留原授权搜索找回路径，不能修改插件生命周期。
