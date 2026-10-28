## 2026-10-27 Wave 7：底座能力闭环、自主学习全闭环、Gateway 自愈与 Web 插件契约接口（W1–W6）

```text
|-- agent_py_agent/agent/capability/
|   |-- asset_promotion.py # 四类一等公民资产确定性静态核验、载体选择与自动晋升
|   |-- skill_learning_promotion.py # 自学技能候选证明持久化、CAS绑定与无人工确认自动发布
|   `-- skill_learning_validation.py # 自学技能、脚本与工作流草稿结构、语法与安全门校验
|-- agent_py_agent/agent/plugin_host/
|   |-- action_specs_v2_portal.py # Web工作台（Coding与渗透测试）新增Host v2动作目录与Schema规范
|   |-- builtin_plugins_v2.py # 内置插件列表查询与按Owner持久化启停切换Handler
|   |-- model_settings_v2_handlers.py # 模型供应商与Profile增删改查、密钥脱敏与掩码保留Handler
|   |-- subagents_v2_handlers.py # 子代理树列表、详情、時間线排序、转向、单节点停止与后台进程停止Handler
|   |-- thread_settings_v2.py # 会话级能力包、模型、权限模式与工作区统一设置及CAS更新Handler
|   `-- workspace_v2_handlers.py # 工作区nofollow文件列表、读写、20MiB上传下载与有界只读Git观察Handler
|-- agent_py_agent/tests/
|   |-- test_capability_router_r4.py # 脚本与工作流选中引用进入提示索引渲染与范围过滤回归
|   |-- test_plugin_host_requests_and_streams_v2.py # 请求中断/接管、SSE after_seq续传与门户动作装配单测
|   |-- test_plugin_host_subagents_and_workspace.py # 子代理监控/停止与工作区文件/Git安全边界单测
|   |-- test_plugin_host_thread_and_model_settings.py # 会话设置、生命周期扩展、模型供应商与内置插件单测
|   |-- test_plugin_host_w6_r2.py # W6门户动作权限映射、幂等与流式回放边界回归
|   |-- test_skill_learning_r4.py # 跨更新采用身份、静态检查与无效反馈回归
|   |-- test_wave7_autonomous_learning_closure.py # 自主学习A1-E1/F01-F17全链路闭环单测
|   |-- test_wave7_broker_workflow_identity.py # 项目资产双层作用域、数据源预检、能力包范围与身份解析单测
|   `-- test_wave7_w1_integration_contracts.py # W1+W2跨模块CAS、工作流恢复快照、ScriptsTool工作区接线与E1闭环单测
```

关键文件说明：`W1–W3` 完成项目级脚本/工作流双层拓扑与写回隔离、数据源执行前强预检与 IM 告警通知、`user_space.identity.resolve` 身份解析、除 `AGENTS.md`/`SOUL.md` 外 100% 机器核验零人工确认的自主学习晋升闭环、以及 `wake_poison` 指数退避/24h 隔离与死进程 `unknown` 运行态自动结清；`W4–W6` 完成两份 Web 插件工作台（Coding 与渗透测试）接口去重合并与 Host v2 落盘。

## 2026-10-26 Wave 6 B3 派生索引TTL协议与专属回归

```text
`-- agent_py_agent/tests/test_local_store_derived_ttl_wave6.py # 白名单30天TTL、永久来源/活跃/证据保护、事务/FTS/正文欠账、重建准入、索引迁移及2000行投影账本
```

实现位于 `local_storage/schema.py`、`records.py`、`search.py`、`gateway_parts/logging.py`、`subagents/services/persistence/projections.py`，扩围后 `cli/local_doctor.py` 的子代理回填入口也复用同一30天准入，先裁剪后重建不复活旧白名单全文；非白名单patch_review仍正常回填。TTL为显式维护API，不修改事件维护调度。Gateway/subagent的02-progress与04-structure已同步结构和边界，实测与未验证限制见TESTS.md。
## 2026-10-26 Wave 6 B5-R2：测试派生进程精确回收

```text
`-- agent_py_agent/tests/test_b5_process_reap.py # 默认收集 B5 创建归属、顶层启动别名、child出生回退、共享fixture缓存与未确认诊断回归
```

关键文件说明：`tests/conftest.py` 仅保留共享 fixtures 与进程归属钩子；原 `test_b5_*` 迁入上述独立测试模块。`tooling/process_registry.py` 按冻结 host/child 出生身份回收，并提供只移除指定 session/目录缓存的 `forget()`，不删除 Store、不全局清共享索引。

## 2026-10-26 正式记忆压实、写安全与显式低延迟检索回归

```text
`-- agent_py_agent/tests/test_memory_jsonl_and_hybrid_wave5.py # 墓碑归档恢复、touch折叠、严格前像、未知字段、刷盘/Windows锁、Daily查重及缓存检索回归
```

关键文件说明：新增回归保留旧同步检索断言，额外覆盖显式 `latency_safe` 模式及后台版本/正文 CAS；不代表前台装配或协调来源后台补齐已接线。
## 2026-10-26 Wave 5 Track B：审计组只读观察、黑盒调用与双 Bot（源码候选）

```text
|-- agent_py_agent/agent/audit_group/
|   |-- __init__.py # 审计组错误、资格校验、观察与黑盒调用公共导出
|   |-- access_policy.py # 本地审计组成员、实时启停与跨用户范围校验
|   |-- blackbox_execution.py # 四类同源资产的冻结定义编排与隔离执行
|   |-- blackbox_invoke_service.py # 宿主黑盒调用、预算、输出净化和私有结果回执
|   |-- caller_controls.py # 调用者原工具禁用、治理策略与配额复核
|   |-- isolated_unit.py # 共享规范解释器、只读允许根和禁网原生沙箱执行单元
|   |-- observation_projection.py # 资产概况、告警筛选和态势汇总安全投影
|   |-- observation_service.py # 审计组用户、资产、告警和态势的有界只读服务
|   |-- owner_catalog.py # 已登记用户及来源目录的只读清单和覆盖范围
|   |-- readonly_store.py # 原身份根下无写入、无链接的资产读取
|   |-- safe_projection.py # 白名单参数结构、秘密和路径的公开净化
|   |-- snapshot_guard.py # 原来源锁与双读快照一致性保护
|   |-- source_snapshot.py # 同源依赖冻结、启停修订复核和禁止跨来源回退
|   `-- README.md # 审计组领域支持范围、隔离边界及未验证项说明
|-- agent_py_agent/agent/conversation/bot_sessions.py # 独立双Bot槽位、非打断轮换与耐久提交代次绑定
|-- agent_py_agent/agent/conversation/bot_tool_policy.py # 只读操作正向白名单、原工具快照交集与当前成员资格
|-- agent_py_agent/agent/plugin_host/action_specs_audit_bots.py # 审计组及双Bot闭合动作目录和参数规范
|-- agent_py_agent/agent/plugin_host/audit_bot_v2_handlers.py # 原审计领域与Bot生产入队准备的Host适配
|-- agent_py_agent/agent/tooling/audit_group_tools.py # 审计组观察与四类黑盒调用工具注册
|-- agent_py_agent/agent/user_space/local_user_channel_policy.py # 普通本地用户纯Web通道与历史IM绑定入组守卫
|-- agent_py_agent/tests/test_audit_group_observation_and_invoke.py # 资格、历史绑定、只读观察、隔离及四类调用回归
`-- agent_py_agent/tests/test_dual_bot_sessions_and_rollover.py # 双Bot轮换、禁学、策略、生产入队与Host范围回归
```

关键文件说明：`audit_group/observation_service.py` 与 `blackbox_invoke_service.py` 分别守只读公开概况和宿主内存冻结执行；`readonly_store.py`、`snapshot_guard.py` 保留原来源无写入读取边界。`isolated_unit.py` 沿共享解释器选择及原生就绪门执行，不能把Python守卫或外层Seatbelt检测当作原生验收通过。

`bot_sessions.py` 与 `audit_bot_v2_handlers.py` 提供槽位、代次及原队列受理准备；Bot工具消费端完整接线不是本轮修复交付。`local_user_channel_policy.py` 在入组、初始创建、重放及发表前只读复核两类原身份账本：provider额外status不作解绑依据，linked仅接受可解释的活跃/撤销状态；既有活跃IM绑定拒绝409，坏字段或不可安全读取则503，撤组不被此门阻断。当前验收事实见 `TESTS.md` 本轮小节，不将源码候选等同部署完成。

## 2026-10-26 跨用户资产投递、受管本地用户治理、Plugin Host v2 21 动作与底座耗时/CPU 深度优化（已落地）

```text
|-- agent_py_agent/agent/assets/delivery_contracts.py # 跨用户资产投递信封/回执/屏蔽名单结构化契约与错误定义
|-- agent_py_agent/agent/assets/delivery_sanitizer.py # 投递前原始字节秘密硬拦截与宿主路径确定性脱敏
|-- agent_py_agent/agent/assets/delivery_service.py # 四类一等公民依赖闭包打包、收发箱、两阶段默认禁用落盘与冲突重命名服务
|-- agent_py_agent/agent/assets/script_store_support.py # 固化脚本存储读写、投递冲突映射与依赖重写辅助
|-- agent_py_agent/agent/conversation/control_delivery_command.py # /delivery 斜杠命令解析与收件箱/屏蔽名单控制执行
|-- agent_py_agent/agent/plugin_host/governance_delivery_v2_handlers.py # 9 个资产投递与 12 个受管用户治理 Host v2 动作接线与目标预检
|-- agent_py_agent/agent/tooling/asset_delivery_tool.py # Agent 跨用户资产投递、收件箱管理与资产启停工具
|-- agent_py_agent/agent/user_space/managed_user_governance.py # 登记目标、宿主权威策略、人格/记忆/资产/任务领域及工具分派
|-- agent_py_agent/agent/user_space/governance_review_store.py # 权威提审记录、目标绑定CAS、批复执行及通知
|-- agent_py_agent/tests/test_admin_managed_users_governance.py # 受管边界、原仓库接线、人格并发及管理员工具回归
|-- agent_py_agent/tests/test_cross_owner_asset_delivery.py # 跨用户资产投递、秘密拦截、屏蔽名单与默认禁用门端到端单测
|-- agent_py_agent/tests/test_managed_user_governance.py # 受管用户改名Skill身份、监控依赖闭包与运行时会话同步回归
|-- agent_py_agent/tests/test_perf_a1_a5_optimizations.py # A1-A5 产物查找、user_config 瘦身、计时拆分与上下文去重单测
|-- agent_py_agent/tests/test_persona_agents_ownership.py # 受管用户 AGENTS/SOUL 提审排队与非受管Owner直拒回归
|-- agent_py_agent/tests/test_sandbox_profile_perf.py # B1 macOS Seatbelt 规则收敛与 TinyScheme 1024B 字面量上限编译单测
`-- agent_py_agent/tests/test_user_config_tool.py # user_config write/patch 严格白名单与拒绝未知字段回归
```

关键文件说明：`assets/delivery_*.py` 实现跨用户四类一等公民拓扑闭包投递、秘密阻断与两阶段默认禁用安装；`user_space/managed_user_governance.py` 与 `governance_review_store.py` 实现主管理员对自建本地普通用户的六大治理域与通用提审流；`plugin_host/governance_delivery_v2_handlers.py` 将新增 21 个动作接入 Plugin Host API v2（总计 86 动作）；`A1..A5` 与 `B1..B4` 完成全链路延迟与 CPU 轮询收敛。

## 2026-10-23 Linux 生产工具耗时治理：产物查找单飞、`self_map` 概览解耦、单线程管道泵与单 Owner 并发闸门

```text
|-- agent_py_agent/agent/agent_core/model/context_estimate_cache.py # 回合内上下文大小估算单飞合并缓存
|-- agent_py_agent/agent/artifacts/registry_projection_cache.py # 按受信任根与原始字节一致性的产物登记解析缓存
|-- agent_py_agent/agent/capability/learned_metadata_projection.py # 单次加载 registry.json 的自学 Skill 批量展示元数据投影
|-- agent_py_agent/agent/capability/learnpack_projection.py # 能力包订单与构建目录只读投影辅助
|-- agent_py_agent/agent/tooling/live_manifest_projection.py # list_tools 在线清单线性字节预算裁剪投影
|-- agent_py_agent/agent/tooling/owner_tool_scheduler.py # 单 Owner 工具并发上限闸门与同 Owner 嵌套重入调度器
|-- agent_py_agent/agent/tooling/process_pipe_pump.py # 单线程 POSIX selector 子进程 stdout/stderr 与唤醒管道复用泵
|-- agent_py_agent/tests/test_artifact_read_lookup_plan.py # read_artifact 单次查找计划与查询内路径缓存回归单测
|-- agent_py_agent/tests/test_artifact_read_path_races.py # read_artifact 锚定无跟随打开、竞争切换与异常路径合同单测
|-- agent_py_agent/tests/test_context_estimate_singleflight.py # 并发工具上下文估算单飞合并与异常传播单测
|-- agent_py_agent/tests/test_learnpack_projection.py # 能力包与插件 self_map 概览/列表投影单测
|-- agent_py_agent/tests/test_linux_shell_latency.py # 宿主沙箱路径单次计算、单线程管道泵与事件唤醒单测
|-- agent_py_agent/tests/test_live_manifest_linear.py # list_tools 在线清单线性预算与大输出预算等价单测
|-- agent_py_agent/tests/test_owner_tool_scheduler.py # 单 Owner 工具并发闸门、跨 Owner 隔离与嵌套防死锁单测
|-- agent_py_agent/tests/test_registry_projection_cache.py # 产物登记表原始字节一致性缓存与深拷贝隔离单测
|-- agent_py_agent/tests/test_self_map_latency.py # self_map 概览无副作用、partial 完整性与 summary_reader 单测
|-- agent_py_agent/tests/test_self_map_metadata_batch.py # 自学 Skill 批量元数据投影与逐项读取实时校验单测
|-- agent_py_agent/tests/test_worker_sqlite_scope.py # 并发工具 Worker 线程独立 sqlite_scope 隔离单测
|-- docs/design/ARTIFACT_LOOKUP_LATENCY.md # read_artifact 查找与安全读取延迟收敛设计说明
|-- docs/design/LINUX_SHELL_LATENCY.md # run_command 宿主路径与子进程管道捕获延迟收敛设计说明
|-- docs/design/OWNER_TOOL_LATENCY.md # 单 Owner 工具并发、list_tools 线性预算与上下文单飞设计说明
|-- docs/design/PORTAL_AND_CORE_FIVE_CAPABILITIES_2026_10_25.md # 五大核心底座能力（--dir 多会话、主会话分叉、对话勾选提炼、数据源钥匙串、浏览器录制）与 18 个插件接口详细设计书
`-- docs/design/SELF_MAP_LATENCY.md # self_map 概览解耦与批量元数据投影设计说明
```

关键文件说明：`memory_archive/artifact/reader.py`、`capability/learned_metadata_projection.py`、`capability/learnpack_projection.py`、`tooling/process_pipe_pump.py`、`artifacts/registry_projection_cache.py`、`tooling/owner_tool_scheduler.py`、`tooling/live_manifest_projection.py` 与 `agent_core/model/context_estimate_cache.py` 协同收敛 2C4G Linux 生产环境 `read_artifact`、`self_map`、`run_command`、`list_tools` 及并发工具批次的固定开销与线程切换风暴。

## 2026-10-23 插件宿主 API v2（42 动作 / v9 授权 / 普通本地用户隔离）、插话带图、非阻塞设置切换与回合完成计时

```text
|-- agent_py_agent/agent/plugin_host/
|   |-- __init__.py # Plugin Host API v2 公共导出与入口
|   |-- action_specs.py # 42 动作目录、条件能力要求与 payload 契约规范
|   |-- artifacts.py # D2 产物列表与元信息只读观察适配器
|   |-- authorization.py # IngressIssuer 入站令牌核验、范围收敛与受限执行策略签发
|   |-- composition.py # 六大领域端口装配、统一调用分发与 SSE 流事件包装
|   |-- contracts.py # 宿主授权绑定、只读/执行上下文与领域结果结构化合同
|   |-- conversations.py # B1/B2 会话线程生命周期、消息历史、发送、轮询、流式与取消领域入口
|   |-- cursors.py # 带 HMAC-SHA256 签名与有效期的分页/流式游标编解码
|   |-- errors.py # HostAPIError 标准错误码、HTTP 状态映射与公开安全文案
|   |-- execution.py # C 受限工具/脚本/工作流执行门面与非管理员身份强制收敛
|   |-- execution_policy.py # 非管理员执行预算、网络禁令与工具白名单裁剪
|   |-- gateway_composition.py # Gateway 生产环境 HostComposition 四大领域装配与响应类型转换
|   |-- gateway_observations.py # Gateway 产物与记忆只读观察端口生产适配器
|   |-- gateway_operations.py # Gateway 受限执行、任务与长期目标端口生产适配器
|   |-- gateway_pending.py # Gateway 待决交互查询与真人票据验证提交端口生产适配器
|   |-- gateway_assets.py # 插件宿主 v2 生产组合的注册脚本、工作流与断点执行适配器
|   |-- gateway_runtime.py # Gateway 线程历史、请求提交与本地用户端口生产适配器
|   |-- goals.py # D1 长期目标查询与状态跃迁领域适配器
|   |-- governance_delivery_v2_handlers.py # WT5九个资产投递与十二个受管用户治理动作的惰性领域接线及目标预检
|   |-- history.py # B1 完整聊天历史安全扫描、内部消息过滤与 MAC 分页
|   |-- http_transport.py # /plugin-host/v2/call 与 /plugin-host/v2/stream HTTP/SSE 传输层
|   |-- interactions.py # D1 待决交互查询与真人票据验证提交领域适配器
|   |-- local_users.py # C 本地普通用户创建、启停、查询与配额管理领域入口
|   |-- memory.py # D2 正式记忆与候选记忆只读观察适配器
|   |-- owner_context.py # A/C 授权桥接、OwnerContextFactory 与 act_as_user 非管理员冷解析
|   |-- requests.py # B2 插件会话请求精确归属校验、状态轮询与最终回复提取
|   |-- revocation.py # 本地用户停用时的关联运行取消与幂等警告回执
|   |-- schemas.py # 严格 JSON 解析、重复键/非有限数拒绝与只读冻结投影
|   |-- streams.py # B2 实时增量回复与终态事件 SSE 流及 MAC 断点重连
|   `-- tasks.py # D1 任务状态、调用链与子代理树只读观察领域适配器
|-- agent_py_agent/agent/plugin_manifest_host_v9.py # 插件 Manifest v9 schema 解析与 host_api_v2 权限声明校验
|-- agent_py_agent/agent/plugin_permissions/host_grants.py # 插件安装/激活授权记录归一化与 v9 能力交集计算
|-- agent_py_agent/agent/user_space/local_user_store.py # 本地普通账号（owners/local/<user_id>）无链接原子存储与状态投影
|-- agent_py_agent/agent/user_space/local_user_lifecycle.py # 本地普通账号创建、密码哈希/改密、幂等回执、种子初始化与启停生命周期服务
|-- agent_py_agent/agent/user_space/user_auth.py # 本地普通用户与飞书首连用户双源统一密码登录、HMAC 会话签发与实时修订验签服务
|-- agent_py_agent/agent/gateway_parts/users_control_service.py # /users 斜杠命令与 admin_controls 工具共用的本地普通用户创建、改密、启停与查询服务
|-- agent_py_agent/agent/gateway_parts/plugin_request_source.py # 插件会话请求来源绑定与同安装/同 Owner 校验
|-- agent_py_agent/agent/gateway_parts/plugin_request_submission.py # 插件消息幂等提交与精确原请求取消服务
|-- agent_py_agent/agent/gateway_parts/session_setting_control.py # 模型与思考档位运行时切换短事务控制通道
|-- agent_py_agent/cli/chat_parts/tui_model_selection_wait.py # TUI 模型菜单异步等待与撤销提示协调器
|-- agent_py_agent/cli/chat_parts/tui_setting_enqueue.py # TUI 独立设置短事务出站队列与非阻塞入队
|-- agent_py_agent/cli/turn_completion.py # CLI / TUI 回合完成本地时间戳与单调时钟耗时格式化
|-- agent_py_agent/tests/test_plugin_host_v2_contracts.py # Plugin Host v2 45 动作目录、严格 JSON 与合同单测
|-- agent_py_agent/tests/test_plugin_host_v2_grants.py # Plugin Host v2 令牌签发、撤销、HTTP 与有界 SSE 传输单测
|-- agent_py_agent/tests/test_plugin_manifest_v9.py # Manifest v9 解析与授权交集单测
|-- agent_py_agent/tests/test_plugin_local_users.py # 本地普通用户创建、启停、配额与幂等故障恢复单测
|-- agent_py_agent/tests/test_local_user_auth_and_password_api.py # 本地用户/飞书首连双源密码认证、users.local.password.set 与会话实时撤权单测
|-- agent_py_agent/tests/test_users_slash_and_admin_controls.py # /users 斜杠命令、密码持久化脱敏与 admin_controls 工具管理普通用户单测
|-- agent_py_agent/tests/test_plugin_public_private_isolation.py # 公共插件与普通用户私有插件隔离契约（CT-01..CT-10）及通配授权单测
|-- agent_py_agent/tests/test_local_user_cross_module_guards.py # 本地普通用户路径策略、人格写守卫、子代理与秘密控制收据跨模块守卫单测
|-- agent_py_agent/tests/test_plugin_act_as_user.py # act_as_user 非管理员隔离、权限收敛与跨 Owner 拒绝单测
|-- agent_py_agent/tests/test_plugin_host_v2_conversations.py # B1 会话线程与完整聊天历史分页单测
|-- agent_py_agent/tests/test_plugin_host_v2_requests.py # B2 消息发送、轮询、SSE 流式回复与取消单测
|-- agent_py_agent/tests/test_plugin_host_v2_interactions.py # D1 交互决策、任务与长期目标接口单测
|-- agent_py_agent/tests/test_plugin_host_v2_observations.py # D2 产物与记忆只读观察接口单测
|-- agent_py_agent/tests/test_plugin_host_gateway_composition.py # Gateway 生产级 Plugin Host v2 四大领域装配与 HTTP/SSE 端到端单测
|-- agent_py_agent/tests/test_active_turn_media_interjection.py # 活动回合带图插话全链路与多模态安全点注入单测
|-- agent_py_agent/tests/test_nonblocking_model_switch.py # 运行时非阻塞模型/思考档位切换与单请求冻结单测
`-- agent_py_agent/tests/test_cli_turn_completion_timing.py # CLI / TUI 回合完成时间与耗时显示单测
```

关键文件说明：`agent/plugin_host/`、`plugin_manifest_host_v9.py` 与 `user_space/local_user_*.py` 构建完整的插件宿主 API v2（42 动作、聊天历史读写、流式收发、本地普通用户管理及宿主强鉴权的 `act_as_user` 非管理员隔离）；`session_setting_control.py`、`tui_setting_enqueue.py` 与 `turn_completion.py` 解决运行时切模型卡顿、带图插话与回合完成计时显示。

## 2026-10-22 线上性能归因与网关排空/卡死治理（.7 增量）

```text
|-- agent_py_agent/agent/gateway_parts/hang_diagnostics.py # Gateway 全线程栈 SIGUSR1/看门狗导出、/proc 内核等待点采集与二代 GC 暂停记录
|-- agent_py_agent/agent/runtime_db/sqlite_scope.py # Gateway 后台单线程规划者跨拍持久 SQLite 作用域与空闲连接提交
|-- agent_py_agent/tests/test_gateway_hang_diagnostics.py # 信号/看门狗导出、SigCgt 位校验、/proc 合成现场与 GC 阈值单测
|-- agent_py_agent/tests/test_gateway_owner_idle_backoff.py # 多 Owner 空闲指数退避、唤醒立即复位与配置热更新单测
|-- agent_py_agent/tests/test_gateway_receipt_directory_gate.py # InputDeliveryService 回执目录指纹跳过与过期清理单测
|-- agent_py_agent/tests/test_model_latency_q2.py # 历史适配零深拷贝、表面指纹缓存、轮间耗时与估算/实报 token 对账单测
|-- agent_py_agent/tests/test_persistent_planner_sqlite_scope.py # 规划持久 SQLite 作用域连接复用、WAL 快照释放与线程隔离单测
|-- agent_py_agent/tests/test_q4_foreground_recovery_regressions.py # 前台自动恢复未知操作、通道/操作者校验与模型核实引导回归单测
|-- agent_py_agent/tests/test_sandbox_command_overhead.py # 沙箱保护目标缓存、跨 Owner Home 只读收敛与前台进程组快速收尾单测
`-- agent_py_agent/tests/test_scheduler_store_payload_cache.py # SchedulerRepository 磁盘快照指纹复用与损坏重新抛出单测
```

关键文件说明：`hang_diagnostics.py` 在 Gateway 失联强杀前保全 `/proc` 内核等待点、内存压力与 Python 全线程栈；`runtime_db/sqlite_scope.py` 为后台单线程规划复用跨拍连接并在拍末释放读快照。

## 2026-10-21 管理员运行治理（P3源码候选）

```text
|-- agent_py_agent/agent/tooling/runtime_governance_tool.py # 三层看门狗只读观察、双授权维护锁、准确pidfd孤儿清理和用户unit配置
|-- agent_py_agent/agent/agent_core/self_map_bootstrap.py # 保留普通脚本/工作流注册，另仅装配可信管理员治理工具
`-- agent_py_agent/tests/test_runtime_governance_tool.py # 临时安装/proc/systemd夹具、双授权拒绝、PID重用与角色保护回归
```

关键边界：`runtime_governance`不拦截普通脚本；服务配置只写文件，不启用或重启。维护锁的监护消费和真实Linux/systemd验收属于集成责任，不以工具侧记录替代。

## 2026-10-21 可组合工作流/固化脚本一等公民与插件遥测 Broker

```text
|-- agent_py_agent/agent/assets/
|   |-- __init__.py # 四类一等公民资产描述符、存储与拓扑导出
|   |-- descriptor.py # 统一标准提取键 AssetDescriptor 与候选数据源评估校验
|   |-- monitor_adapter.py # Monitor 计划与绑定向统一 AssetDescriptor 适配
|   |-- script_store.py # Owner 固化脚本原子存储与数据源/候选评估热更新
|   |-- skill_adapter.py # Owner Skill frontmatter 向统一 AssetDescriptor 适配
|   |-- topology.py # Monitor/Workflow/Script/Skill 自由组合拓扑图与环路检测
|   `-- workflow_store.py # Owner 工作流 JSON+MD 双层定义存储与步骤 Skill 挂载
|-- agent_py_agent/agent/workflows/
|   |-- __init__.py # 工作流执行引擎、脚本运行器与断点存储导出
|   |-- engine.py # 双模态工作流引擎、失败上抛 Agent 与断点续跑
|   |-- models.py # 步骤运行回执与工作流运行快照数据模型
|   |-- run_store.py # 工作流运行快照与步骤产物持久化
|   `-- script_runner.py # 受控子进程脚本执行器与凭据环境清洗
|-- agent_py_agent/agent/observability/
|   |-- __init__.py # 工具耗时遥测账本与任务调用检查器导出
|   |-- task_call_inspector.py # 每次布置任务的完整代理/模型/资产/工具调用数据聚合
|   `-- telemetry_ledger.py # 每个工具调用耗时账本与 P50/P95/Max 统计聚合
|-- agent_py_agent/agent/tooling/scripts_tool.py # Agent 固化脚本管理、候选源评估与单次运行工具
|-- agent_py_agent/agent/tooling/workflows_tool.py # Agent 工作流管理、动态 Skill 绑定与断点续跑工具
|-- agent_py_agent/agent/gateway_parts/plugin_broker_api.py # 插件宿主资产/遥测/受控执行统一封装服务
|-- agent_py_agent/agent/plugin_host_broker_topics.py # 插件宿主只读主题投影与同 Owner 资产适配
|-- agent_py_agent/tests/test_asset_descriptors_and_stores.py # 标准提取键、脚本/工作流存储与拓扑单测
|-- agent_py_agent/tests/test_monitor_and_skill_asset_adapters.py # Monitor 与 Skill 资产描述符适配单测
|-- agent_py_agent/tests/test_workflow_script_engine.py # 双模态工作流引擎、错误上抛与断点续跑单测
|-- agent_py_agent/tests/test_scripts_and_workflows_tools.py # scripts 与 workflows 工具合同单测
|-- agent_py_agent/tests/test_telemetry_ledger_and_inspector.py # 工具耗时账本与任务调用检查器单测
|-- agent_py_agent/tests/test_plugin_broker_api.py # 插件 Broker 资产/遥测/受控执行与 Owner 隔离单测
|-- docs/design/COMPOSABLE_WORKFLOW_SCRIPT_PLUGIN_BROKER.md # 可组合工作流、固化脚本一等公民与插件遥测接口设计文档
|-- docs/design/CROSS_OWNER_ASSET_DELIVERY.md # 四大一等公民跨用户依赖闭包投递、飞书确认与屏蔽名单设计规范
|-- docs/design/ADMIN_MANAGED_USERS_GOVERNANCE.md # 主管理员 Agent 统管自建普通用户全量信息、人设锁定、派单与通用提审设计规范
`-- docs/design/AUDIT_GROUP_CROSS_OWNER_OBSERVATION.md # 审计组（audit_group）纯 Web 跨用户一等公民概况、监控告警汇总与大屏态势只读规范
```

关键文件说明：`assets/` 与 `workflows/` 将 `Script` 和 `Workflow` 提升为与 `Skill`、`Monitor` 同级的 Owner 一等公民；`observability/` 与 `plugin_broker_api.py` 为后续外部插件暴露资产拓扑、每次任务调用树、每个工具调用时间明细与受控执行接口。

## 2026-10-20 strict snapshot 的零链接copyout（源码候选）

新增`agent_py_agent/tests/test_nofollow_zero_link_copyout.py`：确定性copyout模型与原身份、元数据、目录和FD清理边界；产品实现仍集中common/nofollow_fs.py，不新增状态源。

## 2026-10-20 规范Home私有创建回归

```text
`-- agent_py_agent/tests/test_home_initializer_permissions.py # 宿主umask、原C/W消费、排他种子及受管链接边界
```

关键文件说明：`agent/common/nofollow_fs.py`的显式canonical_root目录创建与排他私有文件FD由原Home/owner/builtin producer复用；不替代领域权限或W严格只读资源门。


## 2026-10-20 服务上下文与监护角色回归

```text
`-- agent_py_agent/tests/test_gateway_service_role_identity.py # 原YAML环境运输、独立角色与同代健康/停止fake回归
```

关键文件：`gateway_parts/paths.py`只派生独立监护PID/state位置；`supervisor.py`共用原Gateway角色读门，不能以监护存活或心跳新鲜签业务ready。

## 2026-10-20 迁移摘要窄后继

```text
`-- agent_py_agent/tests/test_gateway_standalone_status_digest.py # 原run协议状态及stop后责任漂移回归
```

关键文件：原standalone_runtime_inventory只读核原run状态常量；standalone_migration比较stop-v1完整责任摘要，不添加新来源或执行权限。

## 2026-10-20 Count0原handle来源回归

```text
`-- agent_py_agent/tests/test_gateway_standalone_handle_origin.py # 原scope handle/FD绑定与只释放原资源回归
```

关键文件：`standalone_lifecycle.py`只从实际guard登记的原capture核身份，属性替换不能改锁来源；未新增执行者或迁移权限。

## 2026-10-20 公开拓扑与Count0候选增量

```text
|-- agent_py_agent/agent/settings/gateway_topology.py # 宿主worker数量唯一严格类型校验
|-- agent_py_agent/agent/gateway_parts/standalone_lifecycle.py # Count0同锁及有限真实身份停机观察
|-- agent_py_agent/agent/gateway_parts/standalone_sources.py # 原MP/未知来源的Count0冷启动纯拒绝
|-- agent_py_agent/tests/test_gateway_public_topology.py # 公开YAML权威及服务配置argv回归
|-- agent_py_agent/tests/test_gateway_standalone_lifecycle.py # 原锁互斥、身份、CAS和不冒称complete
|-- agent_py_agent/tests/test_gateway_standalone_sources.py # 原MP/危险/未知拒绝和旧Count0正例
`-- docs/design/GATEWAY_PUBLIC_TOPOLOGY_AND_STANDALONE.md # S1与有限S2实现、完整迁移缺口
```

关键文件：`standalone_lifecycle.py`不登记MP角色或业务权；`standalone_sources.py`只拒绝、不认证JSON能力。完整责任quiet/迁移尚未完成。

## 本地ask宿主准入补齐（独立候选）

|-- agent_py_agent/agent/gateway_parts/input_identity.py # HTTP与本地提交唯一纯消息ID公式
|-- agent_py_agent/agent/gateway_parts/request_http_client.py # 原认证本地ask运输及受理未确认显式收口
`-- agent_py_agent/tests/test_local_ask_owner_transport.py # 两Count真实宿主准备及本地输入合同回归

### 关键文件说明

`test_nofollow_fstat_publication.py`固定真实fstat完成至current stat之间的发表交错；通用reader只复采已知原FD，新增节点须两平台包运行，不能替领域权限或真实生命周期验收。

- `agent_py_agent/tests/test_native_refusal_lease_pin_ownership.py`：独立原pin API同owner漏读反例，保持评审原字节。
- `agent_py_agent/tests/test_native_refusal_lease_pin_guard.py`：完整lease未知来源、已登记foreign及same-owner/source重绑合同。

- `gateway_parts/multiprocess_native_refusal_source.py`：反查原 admission gate/Root 与首次完整 claim/P1/pin 来源，不构造权限。
- `gateway_parts/multiprocess_native_refusal_terminal.py`：原锁内 FAILED/P3 与 T 外固定响应投影，输入 marker 始终保 pending。
- `gateway_parts/multiprocess_native_refusal_consume.py`：原 W materialize 词法拒绝的同 client/token/canonical 核收，不写账或清输入。
- `agent_py_agent/tests/test_native_first_materialize_refusal.py`：首拒出口、fullclaim/来源负链及故障窗组件合同，不代 native 通过。

- `agent_py_agent/tests/test_native_commit_rejection_receipt.py`：验证严格负回执不变成权限、自动重试或claim终态；wire-only仍保原Future未结事实。

- `agent_py_agent/tests/test_public_agent_home_initialization_order.py`：覆盖真实CLI配置/构造顺序、三类umask、原W父链门与外置工作区，不替代原生服务验收。

`standalone_stop`只保存本次正常停止来源，SQLite设施准备不是quiet。`standalone_inventory/runtime_inventory`不恢复/刷新/结算，`standalone_migration`只在原真实C/lease内消费来源并CAS，磁盘prepared原像不授权；具体边界见公开拓扑合同。

- `agent_py_agent/agent/memory_store/curator_due.py`：纯时间判据，无I/O或执行权；实际batch/lease与C可信配置/来源末门仍由原调用方维护。

`request_http_client.py`只运输原输入和执行快照，不构造业务Agent或签发host owner；`input_identity.py`只编码已认证scope和不透明message_id，不授执行权。HTTP宿主仍唯一准备/准入。

## 2026-10-18 前端目录移除

本轮删除 `frontend/`、`agent_py_agent/agent/web/`、`docs/frontend/`，以及 `test_backend_config_catalog.py`、`test_frontend_settings_labels.py`、`test_web_dashboard.py`；这些目录和测试不再属于当前文件树。保留后端配置登记、Gateway HTTP/API、CLI/TUI/IM 及业务能力包。

<!-- 362：新增候选派发资格运行测试；只收紧两处候选only触发，不改变正式确认或恢复入口。 -->

## 2026-10-17 冷记忆C2与holder候选增量

```text
|-- agent_py_agent/agent/gateway_parts/coordinator_memory_resources.py # 同server、同owner的有界资源借用与生命周期
|-- agent_py_agent/agent/gateway_parts/coordinator_client_memory.py # 原C1记忆装配和client领域组合，J3接线待闭合
|-- agent_py_agent/tests/test_coordinator_client_memory_source.py # 原HTTP及C来源同对象、失效与owner边界
|-- agent_py_agent/tests/test_coordinator_memory_access_flush.py # 真实访问snapshot写成后清理与失败保留
|-- agent_py_agent/tests/test_coordinator_memory_algorithm_components.py # 原remember/search组件与正式记忆复核
|-- agent_py_agent/tests/test_coordinator_memory_resources.py # 同owner合流、有界干净LRU和脏pending
|-- agent_py_agent/tests/test_coordinator_memory_resources_reservations.py # 原借用预留及所有退出路径
|-- agent_py_agent/tests/test_coordinator_memory_holder_publication.py # 原publication竞态字节反例的回归
`-- agent_py_agent/tests/test_coordinator_memory_reservation_review.py # Future/operation异常及取得锁后关闭的复核
```

关键文件：coordinator_memory_resources只持原资源，不保存权限或执行源；原_LocalControlWriteSource在每末写核当前原来源，客户端与管理模式互不代授。

## 2026-10-17 冷记忆J2候选增量

```text
|-- agent_py_agent/tests/test_memory_j2_write_ports.py # Vector/cache/lesson/HOT的原写叶合同
|-- agent_py_agent/tests/test_memory_j2_review_callers.py # long_term真实callee与普通门槛复核
|-- agent_py_agent/tests/test_memory_j2_original_caller_regression.py # 原lesson来源异常调用方反例回归
|-- docs/design/MEMORY_J2_PROJECTION_WRITE_PORTS.md # 投影写叶与默认兼容边界
`-- docs/tasks/MEMORY_J2_PROJECTION_HANDOFF.md # 接JSONL/builder前的冻结交接
```

关键文件：原vector/cache/lessons仍为唯一数据与锁；固定末写只消费本次scope，不因叶子已合就开放完整C记忆能力。

## 2026-10-17 冷记忆J1候选增量

```text
|-- agent_py_agent/tests/test_memory_j1_write_ports.py # 原候选/配额/锁与固定写叶合同
|-- agent_py_agent/tests/test_ledger_redaction_coordinator.py # 原SQLite完整raw CAS及隐私清理
|-- agent_py_agent/tests/test_ledger_redaction_review_outer.py # 实际事务内原异常对象与回滚复核
|-- docs/design/MEMORY_J1_WRITE_PORTS.md # 六写叶默认与C策略边界
`-- docs/tasks/MEMORY_J1_WRITE_PORTS_HANDOFF.md # 独立切片与后继JSONL接线交接
```

关键文件：原candidates/promotion/owner_quota继续唯一领域链，C固定末写复用当前scope，不能据此开放冷HTTP或换用新权限源。

```text
`-- agent_py_agent/tests/test_group_control_lock_birth.py # 实际群配置锁FD出生原P2与锁等待门外、拒绝不重放
```

关键文件：`group_authorization_context.group_capability_guard`只可选运输原锁出生末门，默认原身份与额外监控授权规则不变。

```text
|-- agent_py_agent/tests/test_common_single_replace.py # C本地存储、原scope及一次replace的回归
|-- agent_py_agent/tests/test_local_store_common_ports.py # C本地存储、原scope及一次replace的回归
|-- agent_py_agent/tests/test_local_store_coordinator_io.py # C本地存储、原scope及一次replace的回归
|-- agent_py_agent/tests/test_local_store_coordinator_review_outer.py # C本地存储、原scope及一次replace的回归
|-- docs/design/LOCAL_STORE_COORDINATOR_IO.md # C存储固定IO合同与后继边界
`-- docs/tasks/LOCAL_STORE_COORDINATOR_IO_HANDOFF.md # C存储固定IO合同与后继边界
```

关键文件：`local_storage/schema.py`承接C固定连接与事务，默认原scope不改；细节见LOCAL_STORE_COORDINATOR_IO。

```text
|-- agent_py_agent/agent/gateway_parts/coordinator_actor_resources.py # 实际操作者线程与本人P2固定端口
|-- agent_py_agent/agent/gateway_parts/coordinator_agents_control.py # 实际冷C到原AGENTS服务领域适配
|-- agent_py_agent/agent/gateway_parts/coordinator_owner_management.py # 原HTTP管理来源及唯一持久回执组合
|-- agent_py_agent/tests/test_coordinator_agents_domain.py # 冷规则领域及原来源/异常边界回归
|-- agent_py_agent/tests/test_coordinator_agents_domain_faults.py # 冷规则领域及原来源/异常边界回归
|-- agent_py_agent/tests/test_coordinator_owner_management_fault_domains.py # 冷规则领域及原来源/异常边界回归
|-- agent_py_agent/tests/test_coordinator_owner_management_first_fault.py # 冷规则领域及原来源/异常边界回归
|-- agent_py_agent/tests/test_coordinator_owner_management_source.py # 冷规则领域及原来源/异常边界回归
|-- agent_py_agent/tests/test_coordinator_agents_domain_review_edges.py # 冷规则领域及原来源/异常边界回归
|-- agent_py_agent/agent/common/owner_write_errors.py # 原owner写拒绝类型唯一规范位置，无权限副作用
`-- agent_py_agent/tests/test_owner_write_errors.py # 冷规则领域及原来源/异常边界回归
```

关键文件：common异常只定义原负向类型；coordinator各叶连接真实源和原服务，不创建业务Agent。

2026-10-17 F3记忆C1新增正式入口（未发行）：

```text
|-- agent_py_agent/agent/memory_store/owner_resources.py # 显式资源纯组合或其原构造前像回归
|-- agent_py_agent/agent/retrieval/embedding_factory.py # 显式资源纯组合或其原构造前像回归
|-- agent_py_agent/tests/_memory_c1_preimage.py # 显式资源纯组合或其原构造前像回归
`-- agent_py_agent/tests/test_memory_c1_resources.py # 显式资源纯组合或其原构造前像回归
```

关键文件说明：memory_store/owner_resources组装原真实记忆依赖；retrieval/embedding_factory承接唯一原embedding客户端和降级诊断，不另存权限或业务状态。

2026-10-17 F3审批终态集成52新增测试（未发行）：

```text
|-- agent_py_agent/tests/test_approval_retirement_agent_run.py # 审批原终态、回收和异常责任回归
|-- agent_py_agent/tests/test_approval_retirement_exec_lock.py # 审批原终态、回收和异常责任回归
|-- agent_py_agent/tests/test_approval_retirement_finish_transport.py # 审批原终态、回收和异常责任回归
|-- agent_py_agent/tests/test_approval_retirement_future.py # 审批原终态、回收和异常责任回归
|-- agent_py_agent/tests/test_approval_retirement_lifecycle.py # 审批原终态、回收和异常责任回归
|-- agent_py_agent/tests/test_approval_retirement_transport.py # 审批原终态、回收和异常责任回归
|-- agent_py_agent/tests/test_approval_retirement_worker_future.py # 审批原终态、回收和异常责任回归
|-- agent_py_agent/tests/test_finish_debt_regression.py # 审批原终态、回收和异常责任回归
`-- agent_py_agent/tests/test_runtime_outer_debt_regression.py # 审批原终态、回收和异常责任回归
```

关键文件说明：上述测试沿实际HTTP/Future/原run账验证审批清理与终态核收，不新增恢复或授权来源。

## F1 网关减负集成增量（2026-10-16）

2026-10-16 F2公平队列新增回归入口（产品复用原backends与Gateway模块）：

```text
|-- agent_py_agent/tests/test_provider_queue.py # 同owner顺序、跨owner公平、取消和资源归还
|-- agent_py_agent/tests/test_provider_queue_transport.py # HTTP/WS物理准入、关闭、迟到与默认关闭路径
|-- agent_py_agent/tests/test_provider_queue_entries.py # 可信owner与真实线程入口作用域
|-- agent_py_agent/tests/test_provider_queue_projection.py # 原进度、计时和模型尝试账本投影
`-- agent_py_agent/tests/test_provider_queue_config.py # 真配置规范化与模型不可改部署额度
```

关键文件：`backends/request_scope.py`持有本进程队列，`gateway_helpers.py`/`responses_websocket.py`围绕真实请求借还lease；不替换权限或建立跨进程配额。


```text
|-- agent_py_agent/agent/user_space/context_bundle_tool_manifest.py # 原快照工具清单的内容寻址、摘要v4及当前owner读口
|-- agent_py_agent/agent/gateway_parts/progress_reader.py # 进度流有界稀疏位置索引、完整行读取及替换重建
|-- agent_py_agent/agent/io/sqlite_connection.py # 请求scope内的物理连接和游标回收
|-- agent_py_agent/agent/io/sqlite_lease.py # 当前借用租期与PID/线程边界
|-- agent_py_agent/agent/io/sqlite_schema.py # 纯schema声明摘要及持久初始化标记
|-- agent_py_agent/agent/io/sqlite_scope.py # 当前请求的有界连接生命周期和到期失效
|-- agent_py_agent/tests/test_context_bundle_single_write.py # 单次真实写、无保存零写及精确事实
|-- agent_py_agent/tests/test_context_bundle_tool_manifest.py # v2/v3旧内联与v4摘要、清单复用和归属
|-- agent_py_agent/tests/test_context_bundle_snapshot_retention.py # 活动引用、任务保留及清理竞争
|-- agent_py_agent/tests/test_gateway_io_update_dedup.py # 精确字节、原私有写状态及外部替换
|-- agent_py_agent/tests/test_gateway_terminal_projection_scan.py # 追平节流、旧游标、欠账与持续积压推进
|-- agent_py_agent/tests/test_gateway_input_delivery.py # 输入投递回执终端缓存、transient重试及排队恢复
|-- agent_py_agent/tests/test_conversation_store_index.py # 会话目录索引单次扫描复用与entry_stamp校验
|-- agent_py_agent/tests/test_gateway_progress_incremental.py # 行游标、增量字节、半行与同长改写
|-- agent_py_agent/tests/test_gateway_sqlite_reuse.py # 正式配置、真实请求与owner拒绝
|-- agent_py_agent/tests/test_sqlite_*.py # 连接/借用/游标/fork/schema/恢复及同条件计量测试组
|-- agent_py_agent/tests/test_release_sqlite_integration.py # 原存储与lease同一次生命周期组合
|-- agent_py_agent/tests/support/sqlite_*.py # 独立子进程和跨线程边界探针
|-- scripts/diag/background_tick_cost.py # 原调度存储阶段同事实计量，不替代全Gateway容量测试
|-- agent_py_agent/tests/test_background_tick_cheap_checks.py # 缓存原子快照、故障和失效预算
|-- agent_py_agent/tests/test_background_tick_cost.py # 同输入时读量和连接次数对照
|-- agent_py_agent/tests/test_background_tick_main_integration.py # 保留主调度取消、到点、事件与owner边界
|-- agent_py_agent/tests/test_sandbox_output_capture.py # 双流排空、真实退出、取消、坏编码及高fd
|-- agent_py_agent/tests/test_pack_verifier_capture_limit.py # stdout完整性及原解析前拒绝
`-- agent_py_agent/tests/test_process_log_tail_limit.py # 同fd有限尾读及截断/错误事实
```

关键文件说明：清单和位置索引是原权威的可重建投影；SQLite lease仅管理生命周期，不授权owner或延迟规范提交。E4保留main原schema hash核验，未引入来源不可靠的免hash冻结快路。

## 七块功能接手评估（2026-10-15）

```text
`-- docs/design/SEVEN_FEATURE_INTEGRATION.md # 固定候选对照、复用与冲突裁决、逐批合并及验收边界
```

关键文件说明：该文档记录接手评估，不代表七块已移植；原始对照与批次回执放仓外工件目录。

## 飞书图片与群监控增量（2026-10-15）

```text
|-- agent_py_agent/agent/adapter/inbound_images.py # 本次飞书图片预算、no-follow导入与永久输入失败
|-- agent_py_agent/agent/conversation/input_media_images.py # 实际图片MIME、完整解码及累计像素/帧预算
|-- agent_py_agent/tests/test_input_image_validation.py # 真图片、损坏、超像素/帧数及配置一致性
|-- agent_py_agent/tests/test_feishu_native_images.py # 原owner下载/引用、冻结重投及永久错误结算
|-- agent_py_agent/tests/test_feishu_vision_model_cards.py # 模型卡模态声明、保存、未知及签名边界
|-- agent_py_agent/tests/test_im_input_media_delegation.py # 原输入持久化、后台/子孙授权和供应商真实图片字节
|-- agent_py_agent/tests/test_group_monitor_mentions.py # 群监控目标真实提及、富文本、歧义和手打昵称拒绝
|-- agent_py_agent/tests/test_group_monitor_authorization.py # 群级/个人监控授权、未来成员及原权限边界
|-- agent_py_agent/tests/test_creator_display_refresh.py # 当前群缺名刷新、完整性、锁与creator不变
|-- agent_py_agent/tests/test_feishu_member_completeness.py # 官方安全限流、总数/分页一致性及代删负面证明拒绝
|-- docs/design/FEISHU_NATIVE_IMAGES.md # 飞书原生图片输入合同、依赖及验收边界
|-- docs/tasks/FEISHU_VISION_ENTRY_HANDOFF.md # 图片入站交接
|-- docs/tasks/FEISHU_VISION_DELEGATION_HANDOFF.md # 图片后台与子孙交接
|-- docs/tasks/FEISHU_VISION_MODEL_CARD_HANDOFF.md # 模型模态卡片交接
|-- docs/tasks/GROUP_MONITOR_AUTHORIZATION_HANDOFF.md # 第一期简明群监控授权交接
|-- docs/tasks/GROUP_PROVIDER_DIAGNOSTICS_HANDOFF.md # 群事实失败安全诊断交接
`-- docs/tasks/GROUP_CREATOR_DISPLAY_HANDOFF.md # 创建者姓名刷新交接
```

关键文件说明：图片仍由原input_media保存原件/refs，授权仍认原tool_policy及HostActor；新增模块不另建媒体、身份或权限权威。

## 群公共凭据增量（2026-10-15）

```text
|-- agent_py_agent/agent/user_space/task_credential_groups.py # 原加密凭据的同群发现、实际填写人选择与当前版本投影
|-- agent_py_agent/tests/test_group_shared_credentials.py # 原群卡、共享选择、管理、替换及越权矩阵
|-- agent_py_agent/tests/test_task_credential_discovery.py # 渐进发现、旧群卡显式共享与注册表闭环
|-- agent_py_agent/tests/test_group_credential_http.py # 实际HTTP引用、认证通知、秘密净化与锁外查询
|-- docs/tasks/GROUP_SHARED_CREDENTIAL_HANDOFF.md # 群共享权威及测试交接
`-- docs/tasks/GROUP_CREDENTIAL_DISCOVERY_HANDOFF.md # 凭据发现和共享工具交接
```

关键文件说明：task_credential_groups只投影原owner私密row，复用原官方群成员事实；不建立第二个秘密库或授权表。

2026-10-15 自动委派调度来源回归：

```text
`-- agent_py_agent/tests/test_scheduler_delegated_origin.py # 原聊天与工作线程同源、旧queued准备CAS及跨owner/actor拒绝
```

关键文件说明：来源复用原ExecutionOrigin；迁移事实附原run，不新增队列或授权账本。

2026-10-15 IM 自动派工模块交接：

```text
`-- docs/tasks/IM_DELEGATION_TOOL_SCOPE_HANDOFF.md # 父工具交集、原能力等待修复与真实委派失败和验收边界
```

关键文件说明：自动派工只改原 im_delegation 的创建参数与 OPEN 申请投影，不新增执行队列、授权账或权限入口。

2026-10-15 既有模块职责补充：adapter/protocol 的会话地址反向解析由群授权、conversation/channels 和Feishu运输共用；逻辑topic保留，运输只用已核basechat。未新增状态、目录或协议账本。输入表达复用completion的公开操作事实，不另存凭据或业务正文。

2026-10-15 原请求引用与后台谱系（实施中）：

```text
|-- agent_py_agent/agent/conversation/host_message_context.py # 原TaskLink/SessionTask唯一宿主引用输入与当前attempt谱系复核
`-- agent_py_agent/tests/test_host_message_context.py # 原根/子孙/后台恢复及跨owner、旧attempt与伪引用阴性
```

关键文件说明：引用快照只保存原HostActor和原消息ID映射，普通新Gateway只读本次认证输入；后台/子孙沿原根读取，模型attrs不能登记或改写来源。与原event_source公开输入共同验证，不扩大群授权或平台权限。

2026-10-15 首次普通 Gateway 凭据入口：

```text
|-- agent_py_agent/tests/test_gateway_credential_live_entry.py # 原队列首次主请求、持久输入与准确回调/Wake/OAuth状态
|-- agent_py_agent/tests/test_tool_approval_foreground_scope.py # 原typed writer、前台车道、owner与当前attempt资格
`-- docs/tasks/IM_CREDENTIAL_LIVE_ENTRY_HANDOFF.md # 现场根因、通用修复、343项独立回归与真实租户边界
```

关键职责：原 ToolRuntimePolicy 负责晋升，tool_approval_scope 复用原执行目标与 RuntimeDB；TaskLink、输入行和 Wake 保持唯一，不增加存储或队列。

2026-10-15 群授权与原调度机器人观察：

```text
|-- agent_py_agent/agent/user_space/group_authorization.py # 原tool_policy里的群显式授权、CAS和可信成员事件失效
|-- agent_py_agent/agent/user_space/group_authorization_context.py # 原HostActor、实时provider事实及同步写租约
|-- agent_py_agent/agent/gateway_parts/group_authorization_control.py # 人工/group控制，不向模型开放授予
|-- agent_py_agent/agent/adapter/feishu_provider_facts.py # 原REST凭据查询完整群事实与准确原消息来源
|-- agent_py_agent/agent/adapter/feishu_group_events.py # 真实成员与机器人事件归一化，不猜邀请人
|-- agent_py_agent/agent/adapter/feishu_observations.py # bot观察字段规范，不制造真人actor
|-- agent_py_agent/agent/adapter/channel_observations.py # 原Ingress观察提交和重投接线
|-- agent_py_agent/agent/gateway_parts/channel_observations.py # 宿主认证观察入口，按原群owner登记
|-- agent_py_agent/agent/scheduler/event_sources.py # canonical事件计划、稳定代次和有界模型视图
|-- agent_py_agent/agent/scheduler/event_source_repository.py # 原scheduler store的事件去重、背压及原srun预留
|-- agent_py_agent/agent/scheduler/event_source_tool.py # 模型只给原消息引用，宿主核验后登记计划
|-- agent_py_agent/agent/scheduler/event_source_lifecycle.py # 原执行与投递共享退代检查
|-- agent_py_agent/agent/scheduler/event_effects.py # 原job事件状态中的有界副作用开始租约，不复制结果账
|-- agent_py_agent/agent/scheduler/effect_scope.py # 原模型/后台/工具投递共用准入与同nonce清理
|-- agent_py_agent/agent/scheduler/dispatch.py # 原SchedulerService入队实现，无第二队列
|-- agent_py_agent/tests/test_group_authorization.py # 角色/成员/撤销与并行上下文借权反例
|-- agent_py_agent/tests/test_group_feature_integration.py # 原工具、部署开关、来源证明及认证路由集成
|-- agent_py_agent/tests/test_group_authorization_settings.py # 配置最终写入、撤权原卡、准确owner与系统初始化
|-- agent_py_agent/tests/test_feishu_bot_observations.py # 官方字段形状、bot分型和原Ingress认证观察
|-- agent_py_agent/tests/test_scheduler_event_sources.py # 原事件store/run、长期有界去重及冷owner恢复
|-- agent_py_agent/tests/test_scheduler_event_effects.py # 真后台模型与运输边界上的暂停竞争，不调用真实模型或外发
|-- docs/design/SCHEDULER_EVENT_SOURCES.md # 来源、背压、时间窗、准入与平台权限合同
`-- docs/design/IM_GROUP_GOVERNANCE.md # 授权、订阅、取消与平台权限边界
```

关键文件说明：群授权只写原策略，event_source只扩原job/rule/store/run；观察与真人请求分型，不增加第二权限账、执行队列或WS连接。真实租户与发行验收单独记录。

2026-10-15 卡片回调安全诊断：

```text
|-- agent_py_agent/agent/adapter/feishu_ws_observation.py # 原SDK实例帧头摘要与卡片固定阶段，不读载荷或改变分发
`-- agent_py_agent/tests/test_feishu_ws_observation.py # 零网络SDK分发/ACK不变与任意表单秘密不入日志
```

关键职责：观测沿原连接与卡片handler，原private_controls统一记录固定失败码/阶段；不创建回调账本、第二连接或新的授权来源。

2026-10-14 工具计划预算复审新增：

```text
|-- agent_py_agent/tests/test_tool_program_budget.py # 累计展开预算、原线程身份与完整归档/有界模型视图
`-- docs/tasks/TOOL_PROGRAM_BUDGET_HANDOFF.md # 非作者预算缺口修复与回放交接
```

关键职责：编排保留JSON的共享2MB预算在展开前计量；最终模型视图按UTF-8 16KB，完整payload仍走原归档；底层工具本身的读取/输出上限继续由原工具负责。

2026-10-14 后续监控/恢复/Skill版本/只读批量入口：

```text
|-- agent_py_agent/agent/agent_core/runtime/minimal_tool_audit.py # 与聊天保存解耦的原owner最小工具事实
|-- agent_py_agent/agent/runtime_db/operation_reconciliation_authority.py # 当前attempt只读核对接管和原操作CAS
|-- agent_py_agent/agent/tooling/feishu_cloud_recovery.py # 原写操作精确目标只读核验，不重复追加
|-- agent_py_agent/agent/tooling/nested_dispatch.py # 原身份派生的有限只读工具派发
|-- agent_py_agent/agent/tooling/program_data.py # 有界JSON引用与筛选去重统计分页
|-- agent_py_agent/agent/tooling/tool_program.py # 渐进加载的确定数据计划，不执行代码
|-- agent_py_agent/agent/capability/skill_learning_versions.py # 已学Skill不可变正文快照授权
|-- agent_py_agent/tests/test_feishu_cloud_error_facts.py # HTTP/CLI原数值错误与副作用分类
|-- agent_py_agent/tests/test_feishu_cloud_operation_recovery.py # 原Registry同/跨attempt核验与追加屏障
|-- agent_py_agent/tests/test_managed_operation_reconciliation_authority.py # 恢复者代次、取消与并发CAS
|-- agent_py_agent/tests/test_minimal_tool_audit.py # no-save原工具审计不存正文与秘密
|-- agent_py_agent/tests/test_skill_learning_versions.py # 迟到学习、回滚、版本不可覆写和旧快照
|-- agent_py_agent/tests/test_tool_program.py # 程序权限、原代理回放与批量测量
|-- docs/design/TOOL_PROGRAM_EXECUTION.md # S17–20受限数据计划/长操作合同与实测
|-- docs/design/FEISHU_CLOUD_RECOVERY_AUDIT.md # 云分类、原操作恢复和审计合同
`-- docs/tasks/FEISHU_CLOUD_RECOVERY_AUDIT_HANDOFF.md # 原账CAS与故障测试交接
```

关键职责：工具计划只整理数据，原执行器和操作账仍是授权/副作用权威；恢复接管不迁移原执行身份，也不自动重发未知追加。Skill历史正文不覆盖，旧snapshot仍核当前授权。

2026-10-14 凭据表单和卡片恢复入口（22项第一批）：

```text
|-- agent_py_agent/agent/user_space/task_credential_storage.py # 原存储安全读取、阶段错误和脱敏诊断
|-- agent_py_agent/agent/user_space/task_credential_forms.py # 收集字段、原任务重发和实际提交者授权
|-- agent_py_agent/agent/user_space/task_credential_replay.py # 同卡同代决定恢复原输入发布
|-- agent_py_agent/agent/user_space/task_credential_verification.py # 原HTTP认证事实更新候选验证状态
|-- agent_py_agent/tests/test_task_credential_storage.py # 密钥/配额/发送前后持久化故障
|-- agent_py_agent/tests/test_task_credential_forms.py # 四种表单、重填候选和精确目标范围
|-- agent_py_agent/tests/test_credential_callback_replay.py # 保存后回执丢失与同决定幂等恢复
|-- agent_py_agent/tests/test_credential_group_callbacks.py # 原群成员回调与私人锁隔离
|-- agent_py_agent/tests/test_card_callback_recovery.py # 配置端口/认证和真实回环模型按钮
|-- docs/tasks/IM_22_ITEM_IMPLEMENTATION.md # 全22项授权范围、实施状态与发行证据
|-- docs/tasks/CREDENTIAL_STORAGE_HANDOFF.md # 分阶段存储与副作用交接
|-- docs/tasks/CREDENTIAL_FORM_HANDOFF.md # 简单收集与重填版本交接
`-- docs/tasks/CREDENTIAL_GROUP_HANDOFF.md # 群回调和模型卡端口交接
```

关键职责：沿原 TaskCredential、输入依赖与 Wake 保存秘密和恢复任务，不创建第二凭据库；模型卡仍走原模型目录和私人配置授权。飞书SDK事件与成员证明由适配器提供，手填姓名不是身份。

2026-10-14 R01–R09 新入口（原任务/模型/监控/权限事实源）：

```text
|-- agent_py_agent/agent/gateway_parts/model_control_cards.py # 原模型目录的签名选择/私人配置卡
|-- agent_py_agent/agent/scheduler/control.py # 人类命令和模型工具共用准确监控取消与恢复
|-- agent_py_agent/agent/scheduler/notification_binding.py # 冻结后续监控通知与管理员绑定代次
|-- agent_py_agent/agent/tooling/query_evidence.py # 实际查询条件/计数与零结果软指导
|-- agent_py_agent/agent/user_space/feishu_sheet_verification.py # 同身份写后有界读回，不重放写入
|-- agent_py_agent/tests/test_feishu_sheet_verification.py # 表格写回一致与未知副作用
|-- agent_py_agent/tests/test_im_credential_card_preflight.py # 原凭据工具参数和内网绑定/真实网络权限
|-- agent_py_agent/tests/test_im_credential_public_redaction.py # 初发/编辑/最终/公开事件统一脱敏
|-- agent_py_agent/tests/test_im_credentials_security_matrix.py # 普通/群/主子实际沙箱与受管秘密隔离
|-- agent_py_agent/tests/test_im_model_cards.py # 原操作者/原群/版本/幂等/模型密钥隔离
|-- agent_py_agent/tests/test_query_evidence.py # 实际零查询与真工具/fake模型基线回放
|-- agent_py_agent/tests/test_scheduler_control_lifecycle.py # 准确取消树/坏记录/未来通知冻结
|-- docs/design/IM_MODEL_CONTROL_CARDS.md # 模型表单作用域与权限合同
|-- docs/design/IM_PROGRESS_RECOVERY.md # 短草稿心跳/失败恢复与停止优先合同
|-- docs/design/IM_SHEET_PROTECTION_METADATA.md # 官方保护缺省字段及写后核对合同
|-- docs/design/QUERY_ZERO_RESULT_REVIEW.md # 查询证据与有限基线边界
|-- docs/design/SCHEDULER_HUMAN_CONTROL.md # 本人控制/创建人权限与管理员接管
|-- docs/tasks/IM_R01_R09_RELEASE.md # 本期统一实现/测试/发行记录
|-- docs/tasks/IM_CREDENTIALS_SECURITY_HANDOFF.md # 非作者受管秘密攻防与边界证据
|-- docs/tasks/IM_R01_R02_HANDOFF.md # 本期独占文件分线交接
|-- docs/tasks/IM_R03_HANDOFF.md # 本期独占文件分线交接
|-- docs/tasks/IM_R05_HANDOFF.md # 本期独占文件分线交接
|-- docs/tasks/IM_R06_HANDOFF.md # 本期独占文件分线交接
`-- docs/tasks/IM_R07_R08_HANDOFF.md # 本期独占文件分线交接
```

关键职责：控制与查询复用原账本；公开净化不改实际执行输入，私人密钥不交给模型或任意脚本。

2026-10-14 无人值守与受控飞书 CLI 新入口（沿原 owner/task/Wake/工具账）：

```text
|-- agent_py_agent/agent/tooling/self_context.py # 本人能力与权限事实只读投影
|-- agent_py_agent/agent/conversation/input_dependencies.py # 原任务私人输入期限、恢复和取消
|-- agent_py_agent/agent/conversation/input_dependency_index.py # 原TaskStore的输入引用索引
|-- agent_py_agent/agent/conversation/input_tool_boundary.py # 归档后退出待输入工作片
|-- agent_py_agent/agent/conversation/task_rejected_actions.py # 原任务准确审批拒绝结果
|-- agent_py_agent/agent/user_space/task_credential_schema.py # 私人密文字段及有效期合同
|-- agent_py_agent/agent/user_space/task_credential_http.py # 原宿主HTTP的引用登录、会话与签名
|-- agent_py_agent/agent/user_space/feishu_oauth.py # 当前本人授权、密文token和刷新
|-- agent_py_agent/agent/tooling/feishu_auth_tool.py # 渐进发现的本人授权交互
|-- agent_py_agent/agent/gateway_parts/feishu_oauth_service.py # 原Gateway的验签OAuth回调
|-- agent_py_agent/agent/adapter/feishu_cli.py # 固定随包程序、私有stdin和单次官方运输
|-- scripts/feishu_cli_host/main.go # 官方Go CLI公开扩展的受控宿主入口
|-- scripts/build_feishu_cli.py # 固定提交编译、Go验证、来源及许可证
|-- agent_py_agent/tests/test_unattended_tool_approval.py # 审批终态与绝对截止
|-- agent_py_agent/tests/test_im_self_discovery.py # 实际工具目录、权限及子代理上界
|-- agent_py_agent/tests/test_persistent_task_input.py # 真工具/假模型/后台claim与一次恢复
|-- agent_py_agent/tests/test_task_input_cancel_deadline.py # 真停止入口、兄弟隔离与1001队列输入期限
|-- agent_py_agent/tests/test_runtime_input_deadline_bridge.py # DB-only C/W原运输、迟到claim及静默取消竞态
|-- agent_py_agent/tests/test_background_main_scope_attempt.py # 原后台绑定、cold审批与准确输入attempt取消闭包
|-- agent_py_agent/tests/test_task_input_capacity.py # 原TaskStore锁内并发32容量和拒绝回滚
|-- agent_py_agent/tests/test_im_task_input_status.py # 同源状态区分运行、待输入和子阻塞
|-- agent_py_agent/tests/test_task_credential_http.py # 引用登录、CSRF、签名和同源边界
|-- agent_py_agent/tests/test_feishu_cli_backend.py # 私有运输、停止与未知写入
|-- agent_py_agent/tests/test_feishu_oauth.py # 本人授权回调、撤销与身份负例
`-- docs/design/IM_UNATTENDED_IMPLEMENTATION.md # 本期实施合同、部署和验收边界
```

关键职责：输入引用是原TaskStore查找投影，原runtime wake_queue负责到期与恢复；秘密仍只在原受保护私人交互目录加密存储。CLI 不成为第二套权限或消息发送入口。

# Codebase Tree

## Tree

### 2026-10-25 七项核心底座与 Host v2 扩展（工作区源码增量，未发布）

以下为本轮新增生产文件和直接测试，源码存在不等于真实入口、完整验收或部署已完成；实现边界见 [设计台账](DESIGN_LEDGER.md)。

```text
|-- agent_py_agent/
|   |-- runtime_sdk.py # 固定脚本数据源SDK公开导出；唯一实现位于agent/workflows/runtime_sdk.py
|   |-- agent/
|   |   |-- assets/
|   |   |   |-- browser_recorder.py # 动作记录规范化、秘密剥离与参数化脚本/工作流/Skill草稿编译，不启动浏览器
|   |   |   |-- datasource_store.py # 数据源规格、0600私有明文凭据引用与受校验SQLite迁移
|   |   |   |-- turn_extractor.py # 用户回合真实轨迹聚合、脱敏提炼及DRAFT/选择性发布
|   |   |   `-- workspace_assets.py # 四类项目资产只读覆盖、CAS晋升与两阶段批量保存
|   |   |-- conversation/
|   |   |   |-- background_process_view.py # 本owner后台进程与有界日志只读投影，可按线程过滤，不消费唤醒
|   |   |   |-- thread_fork_service.py # 主会话安全前缀分叉及Compact/人格重建，源会话零修改
|   |   |   `-- workspace_context.py # 显式目录与owner权限校验、目录/分叉控制及项目上下文
|   |   |-- plugin_host/
|   |   |   |-- action_specs_v2_ext.py # 本轮20个准确Host v2动作的闭合schema、能力与幂等规格
|   |   |   |-- capabilities_v2_handlers.py # 原授权链下新增动作领域分发，不另建权限或幂等账
|   |   |   `-- observations.py # 工作区/资产冷读投影与后台进程线程/task可见性过滤
|   |   |-- tooling/
|   |   |   `-- datasources_tool.py # 当前owner数据源列表、保存、连接测试、探测与迁移工具
|   |   `-- workflows/
|   |       |-- datasource_runtime.py # 脚本合同冻结绑定、临时私有清单与真实秘密值输出脱敏
|   |       `-- runtime_sdk.py # 脚本进程逻辑代号句柄及受只读/表范围约束的SQLite查询实现
|   `-- tests/
|       |-- test_browser_recorder_compiler.py # 动作编译、秘密剥离、URL/网络边界与真实ScriptStore入口
|       |-- test_cli_dir_and_workspace_assets.py # 三目录别名、管理员双根、普通用户边界与资产覆盖/晋升/批量保存
|       |-- test_conversation_thread_fork.py # 主线程安全工具边界、Compact/人格继承、源不变及目录CAS
|       |-- test_datasource_keyring_and_script_binding.py # 私有凭据、SQLite、脚本绑定/脱敏及迁移状态边界
|       |-- test_plugin_host_v2_five_capabilities.py # 20个新增Host动作、闭合参数、能力/幂等与owner隔离
|       |-- test_tui_turn_completion_and_bg_watch.py # 完成行/历史回放、Footer、后台只读观察、导航和日志边界
|       `-- test_turn_asset_extractor.py # 回合检索、真实轨迹、脱敏DRAFT、选择发布、依赖及失败回滚
```

```text
|-- agent_py_agent/tests/test_native_refusal_lease_pin_ownership.py # 原独审同owner pin反例
`-- agent_py_agent/tests/test_native_refusal_lease_pin_guard.py # 完整lease pin来源增量合同
```

```text
|-- agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_source.py # 原首拒完整出处读门
|-- agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_terminal.py # 原FAILED/P3与pending投影
|-- agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_consume.py # 原W词法核收
`-- agent_py_agent/tests/test_native_first_materialize_refusal.py # 首拒出口正负及故障窗
```

```text
`-- agent_py_agent/tests/test_native_commit_rejection_receipt.py # 本次pre-pin typed NACK、同seq/父身份与未知保债运行合同
```

```text
`-- agent_py_agent/tests/test_public_agent_home_initialization_order.py # 公开构造私有Home先行、原W消费及内部链接零外建
```

### Count0→N候选增量

```text
|-- agent_py_agent/agent/gateway_parts/standalone_stop.py # 原Count0正常停止writer的版本化真实来源
|-- agent_py_agent/agent/gateway_parts/standalone_inventory.py # 规范owner/会话/native的无恢复读取组合
|-- agent_py_agent/agent/gateway_parts/standalone_runtime_inventory.py # 原ro事务的完整规范责任行/ref摘要
|-- agent_py_agent/agent/gateway_parts/standalone_migration.py # 原接班内的准确来源variant及首publisher CAS
|-- agent_py_agent/agent/gateway_parts/standalone_migration_errors.py # 固定原因与可行动恢复错误
`-- agent_py_agent/tests/test_gateway_standalone_migration.py # 历史/native/future贯通与必要硬拒绝
```


```text
|-- docs/design/GATEWAY_BACKGROUND_SLICE_SHUTDOWN.md # 原后台片停止、物理退出和真实排空边界
|-- docs/design/GATEWAY_ROLE_INCIDENT_ACCEPTANCE_2026-10-20.md # 现网十二类角色事故、故障演练与长跑验收边界
|-- agent_py_agent/agent/backends/assistant_messages.py # 原正文块的逐消息来源、阶段及整段安全投影
|-- agent_py_agent/agent/agent_core/model/visible_response.py # 在原采纳边界向既有出口提交同次模型正文
|-- agent_py_agent/tests/test_assistant_message_phases.py # 阶段、跨协议历史与整段净化回归
|-- agent_py_agent/tests/test_committed_model_messages.py # 同次已采纳正文的CLI/TUI/Gateway显示回归
|-- agent_py_agent/tests/test_pending_input_public_boundary.py # 原输入失效上限不发布旧正文
|-- agent_py_agent/tests/test_progress_message_revision.py # 真实输出索引与活动候选唯一终态冻结
|-- agent_py_agent/tests/test_credential_card_validation.py # 两种执行入口的表单改正、原权限门与固定回执
|-- agent_py_agent/tests/test_final_response_projection_roles.py # 内部正文和跨段安全上下文的公开出口回归
|-- agent_py_agent/agent/gateway_parts/native_diagnostic_projection.py # 原native日志的有限安全schema及无正文投影
|-- agent_py_agent/tests/test_gateway_safe_native_diagnostics.py # 当前代native诊断、有限资源枚举及拒绝边界
|-- agent_py_agent/tests/test_admin_self_gateway_diagnostics.py # 真实主身份与工具链的自身日志诊断和权限回归
|-- agent_py_agent/tests/test_tui_decision_menu_sink_lifecycle.py # 测试helper输出资源的嵌套与失败恢复回归
|-- agent_py_agent/tests/test_tui_working_indicator.py # typed活动空窗、真实帧动画缓存与终态去重回归
|-- agent_py_agent/tests/test_tui_local_candidate_commit_boundary.py # 未提交候选隔离及本地工具提交显示回归
`-- agent_py_agent/tests/test_committed_stream_public_boundary.py # writer、IM档位、失败关闭和旧候选重放回归
```

```text
`-- agent_py_agent/tests/test_mcp_approval_sink_reuse.py # 实际canonical范围的会话批准复用与失源负例
```

```text
`-- agent_py_agent/tests/test_admin_full_access_mcp.py # 真实注册表下管理员普通MCP确认与普通/群/子代理负例
```

人格写入和正式冷候选入口的长期验证：

```text
|-- docs/design/PERSONA_COORDINATOR_IO.md # 原人格事务的C本地IO、默认与必要FD清理合同
|-- docs/tasks/PERSONA_COORDINATOR_IO_HANDOFF.md # 准确闭包、原反例与后续验收交接
|-- agent_py_agent/tests/test_persona_coordinator_io.py # 实际原事务逐叶撤权、默认、partial与FD验证
|-- agent_py_agent/tests/test_persona_storage_close_priority.py # 两原出口及三个原调用方的真实primary优先级
|-- agent_py_agent/tests/test_persona_native_close_review.py # 非作者同字节失源加secondary close原反例
|-- agent_py_agent/tests/test_coordinator_memory_dispatcher.py # 本地/私聊/群七动作的原HTTP正式接线
`-- agent_py_agent/tests/test_coordinator_memory_dispatch_lifetime_review.py # 非作者complete/event活窗口与原unknown/native重放
```

固定后台入口的发行闭包：

```text
|-- agent_py_agent/agent/gateway_parts/multiprocess_executor_bootstrap.py # 原固定委托入口，认证匿名交接FD
|-- agent_py_agent/agent/gateway_parts/multiprocess_native_host_bootstrap.py # 原固定native host入口，保身份与125失败合同
|-- agent_py_agent/tests/test_gateway_bootstrap_package_closure.py # 同包源码/只读字节码与独立QA来源验证
`-- agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py # 缺失来源拒绝、Count0及cwd不改固定入口的独立复核
```

冷候选接口与来源回归的长期入口：

```text
|-- agent_py_agent/agent/gateway_parts/coordinator_memory_candidates.py # 原source、真人seal与同holder领域组合
|-- agent_py_agent/agent/gateway_parts/memory_preview_transaction.py # 固定nonce、双T try锁、完整raw CAS与一次消费
|-- agent_py_agent/tests/test_coordinator_memory_candidates_source.py # 原入口seal寿命、outer和父未知收口
|-- agent_py_agent/tests/test_coordinator_memory_candidates_domain.py # 七动作、真实正式partial与群原身份
|-- agent_py_agent/tests/test_memory_candidate_services.py # 原默认三引用与C惰性服务束
|-- agent_py_agent/tests/test_memory_preview_json_writer.py # 无换行JSON默认与一次替换策略
|-- agent_py_agent/tests/test_memory_preview_transaction.py # 原preview同对象、完整绑定和raw CAS
|-- agent_py_agent/tests/test_coordinator_memory_binder_native.py # 原固定binder首故障与退出边界
`-- agent_py_agent/tests/test_coordinator_memory_binding_review.py # 非作者实际ingress失效原字节反例
```

审批只读忙重试的原入口和直接验证：

```text
|-- agent_py_agent/agent/gateway_parts/private_approval_control.py # W仅重读输入前校验，原input恰一次
|-- agent_py_agent/tests/test_coordinator_approval_sqlite_read_retry.py # 真实临时WAL锁的同断言红绿及写入不重试
`-- agent_py_agent/tests/test_private_approval_read_retry_review.py # 实际取消等待与extended BUSY复核
```

本轮J3长期入口（沿原目录）：

```text
|-- agent_py_agent/agent/gateway_parts/coordinator_client_memory.py # 同holder的原记忆构造、惰性服务及原HTTP算法
|-- agent_py_agent/tests/test_coordinator_jsonl_write_ports.py # 正式JSONL末写、删除、压缩和当前来源回归
|-- agent_py_agent/tests/test_coordinator_memory_end_to_end.py # 实际临时HTTP的原冷记忆完整链
|-- agent_py_agent/tests/test_coordinator_memory_http_current_review.py # 复核用借用后策略变化及付费前撤权
|-- docs/design/COORDINATOR_CLIENT_MEMORY_CLOSURE.md # 冷C记忆构造、默认与末写合同
`-- docs/tasks/COORDINATOR_CLIENT_MEMORY_HANDOFF.md # 五路径精确来源与验证交接
```

BG queue/replay 新增 `agent_py_agent/tests/test_gateway_queue_recovery.py`：原HTTP同ID恢复、显式同键控制与部分队列发布；产品身份公式统一在http_handlers.http_input_identity，不建立第二状态源。

BG G0 新增 `agent_py_agent/tests/test_gateway_background_fresh_queue.py`：真实首拍未发布、canonical缺席、原队列CAS与迟到控制互斥回归；测试不构成权限来源。

片A新增的长期入口：

```text
|-- agent_py_agent/agent/gateway_parts/owner_resources.py # 原owner配置与路径的唯一纯解析
|-- agent_py_agent/agent/memory_store/owner_services.py # 三真实服务引用及原显式remember算法
|-- agent_py_agent/tests/test_owner_services_extraction.py # 纯提取、真实记忆与原费用顺序验证
`-- agent_py_agent/tests/_owner_services_preimage.py # 获批前像的测试oracle，不供产品调用
```



### 冷C普通控制POST候选（2026-10-17，未发布）

```text
|-- agent_py_agent/agent/gateway_parts/coordinator_control_transport.py # 实际HTTP工作片固定原回执运输，不执行领域或接受任意writer
`-- agent_py_agent/tests/test_coordinator_control_forward.py # 自然冷C/真实Worker池/原领域及锁忙和typed部分发布普通回归
```

### F3 真实执行短门与模型责任接缝（候选未发布）

```text
|-- agent_py_agent/agent/user_space/owner_maintenance_transition.py # 原维护Policy/完整状态CAS与短门
|-- agent_py_agent/agent/memory_store/retention_transition.py # 每次保留清理写沿原维护回调
|-- agent_py_agent/agent/gateway_parts/multiprocess_http_transport.py # 原C设施出生与同Root观察源
|-- agent_py_agent/agent/gateway_parts/wait_acceptance.py # 原受理回执与队列/终态耐久核对
|-- agent_py_agent/tests/test_owner_maintenance_execution_ports.py # 取消/状态变更后拒绝维护写入
|-- agent_py_agent/tests/test_curator_heartbeat_error_origin.py # 原心跳异常来源、复制拒绝与只读诊断
|-- agent_py_agent/tests/test_gateway_heartbeat_request_unknown.py # 原请求心跳停止未结的保守出口
|-- agent_py_agent/tests/test_gateway_background_process_identity.py # 内核出生身份与权限错误不判死
|-- agent_py_agent/tests/test_gateway_pid_record_identity.py # PID发现同来源出生及未知不清理/不重启回归
|-- agent_py_agent/tests/test_gateway_learning_discovery.py # 原registry自主维护与迟到FIFO隔离
|-- agent_py_agent/tests/test_runner_thread_start_boundary.py # 子代理真实Thread ACK异常、原来源及未结P3
|-- agent_py_agent/tests/test_gateway_coordinator_loops.py # 冷C元数据循环和心跳原lease末核
|-- agent_py_agent/tests/test_gateway_c_http_wiring.py # HTTP可选原控制与Root运输
|-- agent_py_agent/tests/test_gateway_c_http_wait.py # 原受理耐久与无业务Agent等待
|-- agent_py_agent/tests/test_gateway_c_http_birth.py # 实际HTTP设施出生失败保unknown
|-- agent_py_agent/tests/test_gateway_multiprocess_http_transport.py # 原HTTP设施短门与错来源拒绝
|-- agent_py_agent/tests/test_gateway_global_write_current_consumers.py # 当前共享writer与真实W构造零漂移
|-- agent_py_agent/tests/test_gateway_worker_global_write_boundaries.py # W共享配置/身份/调度拒门
|-- agent_py_agent/tests/test_f3_frontier_error_contracts.py # 实际启动/维护/隔离边界统一错误分类
|-- agent_py_agent/cli/gateway_coordinated_run.py # 实际C启动、同Root设施出生与规范状态末写
|-- agent_py_agent/cli/gateway_coordinator_request_pump.py # 冷C原准入与周期恢复，只运输metadata不执行Agent业务
|-- agent_py_agent/tests/test_gateway_cold_request_pump.py # 冷C原P3、route与无业务claim
|-- agent_py_agent/tests/test_gateway_cold_processing_recovery.py # 实际控制面同源Store读取与原恢复门
|-- agent_py_agent/tests/test_gateway_cold_recovery_consumer.py # 同gate生产tick恢复与独立修复域
|-- agent_py_agent/tests/test_cold_recovery_optional_index.py # 原C失败归档后跳过可选业务索引及Count0保持
|-- agent_py_agent/tests/test_gateway_coordinator_client_memory.py # C recent同一JSONL只读、认证与零构造副作用
|-- docs/design/COORDINATOR_COLD_REQUEST_PUMP.md # 冷控制面元数据派发与恢复边界
|-- docs/design/GATEWAY_CLIENT_MEMORY_READONLY.md # C正式记忆recent原唯一读取与未接操作
|-- agent_py_agent/tests/test_gateway_c_startup_recovery.py # 正式C启动恢复与原计划标记资格
|-- agent_py_agent/agent/gateway_parts/multiprocess_input_reconcile.py # 原C同源queued修复、cursor与末写try-lock
|-- agent_py_agent/agent/gateway_parts/http_input_receipt.py # 实际C HTTP工作片的耐久普通输入受理
|-- agent_py_agent/agent/gateway_parts/multiprocess_input_receipt.py # 原输入T与同源Root下的固定receipt动作
|-- agent_py_agent/agent/gateway_parts/input_transport.py # 完整v1与显式主BG v2运输codec，定位不授执行权
|-- agent_py_agent/agent/gateway_parts/multiprocess_background_input.py # 原主后台claim的活动输入安全点与最终结算
|-- agent_py_agent/tests/test_gateway_background_active_input.py # 实际C/W绑定、ACK及后台claim/Compact收口
|-- agent_py_agent/agent/gateway_parts/input_target_observation.py # 原owner会话claim与目标的只读观察
|-- agent_py_agent/agent/gateway_parts/input_guidance_payload.py # 原输入与唯一guidance完整载荷
|-- agent_py_agent/agent/gateway_parts/input_guidance_observation.py # 原guidance绑定与消费状态读取
|-- agent_py_agent/agent/gateway_parts/control_guidance_payload.py # 原控制追加的同一guidance字段运输
|-- agent_py_agent/agent/gateway_parts/multiprocess_active_input.py # 原请求实际执行来源的输入安全点
|-- agent_py_agent/tests/test_gateway_active_input_identity.py # 原执行/路径/运输身份与同对象错误
|-- agent_py_agent/tests/test_gateway_active_input_transport.py # C固定路由、完整媒体及幂等运输
|-- agent_py_agent/tests/test_gateway_active_input_worker.py # 原claimed链到模型安全点与ACK
|-- agent_py_agent/agent/conversation/input_resource_retry.py # 同次安全点资源预算与门外中断等待
|-- agent_py_agent/agent/conversation/guidance_injection.py # runtime与Gateway共用已注入ID状态
|-- agent_py_agent/agent/common/runner_thread_origin.py # 原runner线程出生与退出异常的唯一native锚点
|-- agent_py_agent/tests/test_gateway_active_input_closeout_pipeline.py # 原请求最终封口与完整输入排队
|-- agent_py_agent/tests/test_gateway_active_input_publication.py # 原预定位先于规范运输的交错
|-- agent_py_agent/tests/test_gateway_active_input_resource_retry.py # 原资源忙复现及安全点续行
|-- agent_py_agent/tests/test_gateway_active_input_retry_contract.py # 同次总预算与typed未结原责任
|-- agent_py_agent/tests/test_gateway_active_input_budget_fences.py # 每层原锁和末提交共用预算
|-- agent_py_agent/tests/test_guidance_nonblocking_append.py # 原guidance完整追加链可选try-lock
|-- agent_py_agent/tests/test_io_resource_budget.py # 原JSON/JSONL末写与默认重试保持
|-- agent_py_agent/tests/test_coordinator_control_status_readonly.py # 冷C已持久控制回执原权限只读查询
|-- agent_py_agent/tests/test_gateway_c_input_receipt_source.py # 原HTTP工作与receipt耐久CAS
|-- agent_py_agent/tests/test_gateway_input_transport_codec.py # v2保持与v3完整类型解析
|-- docs/design/COORDINATOR_ACTIVE_INPUT_TRANSPORT.md # C/W输入来源、顺序及待修交错
|-- agent_py_agent/tests/test_gateway_c_queued_input_reconcile.py # 冷/健康C完整prepared修复与公平延期
|-- agent_py_agent/tests/test_gateway_c_queued_input_trylock.py # 原JSON真实持锁与Count0等待对照
|-- agent_py_agent/tests/test_gateway_http_current_work.py # 原HTTP工作区间、退出失效与Count0分支
|-- agent_py_agent/tests/test_gateway_worker_parser_entry.py # 原内部worker早分派与Count0解析保持
|-- agent_py_agent/tests/test_gateway_partial_startup_drain.py # 未握手native会话保留排空阻塞
|-- agent_py_agent/tests/test_coordinator_model_card_transactions.py # C原模型选择/默认/保存/取消分段事务
|-- agent_py_agent/tests/test_coordinator_model_card_delivery.py # 原门外投递、同ID幂等和副作用事实
|-- agent_py_agent/agent/gateway_parts/model_metadata_transaction.py # C首次模型菜单与原control receipt、模型资源事务
|-- agent_py_agent/tests/test_coordinator_model_menu.py # 冷/健康C原模型目录、选择及同回执状态读
|-- agent_py_agent/tests/test_gateway_ask_control_parity.py # /ask与/control真实C模型/管理领域、原来源及重放一致性
|-- agent_py_agent/tests/test_model_control_signed_projection.py # 原不可变签名投影的合法载荷回归
|-- agent_py_agent/tests/test_channel_health_paths.py # 原路径健康快照及C不清PID/不建锁
|-- docs/design/COORDINATOR_STARTUP_RECOVERY.md # 原startup恢复门、预算与B4拒绝边界
|-- agent_py_agent/agent/gateway_parts/private_interaction_transaction.py # C私卡原owner分段提交与既存输入资源
|-- agent_py_agent/agent/gateway_parts/private_approval_control.py # 原v5私卡批准/拒绝向真实等待线程转交及负向退休
|-- agent_py_agent/tests/test_coordinator_approval_decision.py # 实际C原私卡决定与原线程消费
|-- agent_py_agent/tests/test_private_approval_worker_trylocks.py # 原W审批执行与结算锁忙的未结责任
|-- agent_py_agent/tests/test_approval_waiter_retirement.py # 原等待线程退休及已决定/资源忙保持
|-- agent_py_agent/tests/test_coordinator_private_credential_transactions.py # C私卡原认证、输入及幂等事务
|-- agent_py_agent/tests/test_coordinator_private_credential_boundaries.py # 实际锁竞争、typed未知与密钥边界
|-- agent_py_agent/tests/test_private_interaction_safe_diagnostic.py # 原503安全异常元数据、秘密与异常hook隔离及回执不变回归
|-- agent_py_agent/tests/test_coordinator_approval_cancel.py # C原签名审批取消、同锁幂等与失活来源
|-- agent_py_agent/cli/gateway_coordinated_shutdown.py # 部分启动与停止共用原资源收尾、unknown保留
|-- agent_py_agent/tests/test_gateway_coordinated_entry.py # 正式内部count、先取锁和默认入口保持
|-- agent_py_agent/tests/test_gateway_c_state_publication.py # 原C完整state与角色双读共门、取消锁序和失源首写拒绝运行回归
|-- agent_py_agent/tests/test_gateway_source_refusal_diagnostics.py # 角色源拒绝日志只含白名单数字与函数位置、禁止消息和凭据正文
|-- agent_py_agent/tests/test_gateway_generation_source_namespace.py # 已发表责任与原辅助流、暂存设施的严格目录分类回归
|-- agent_py_agent/tests/test_generation_source_stable_names.py # 名称集有限整次重读、跨轮固定身份与旧完整来源硬门回归
|-- agent_py_agent/tests/test_generation_source_missing_publication.py # 原终态writer在首读前移走责任及末名称/硬错误优先级回归
|-- agent_py_agent/tests/test_gateway_closed_generation_progress.py # 旧关闭固定范围与本代心跳、新增及规范归档推进的分界回归
|-- agent_py_agent/tests/test_gateway_generation_facility_second_open.py # 原写入失败清理暂存时第二次打开自然消失的回归
|-- agent_py_agent/tests/test_gateway_generation_facility_successful_replace.py # 原写入成功替换同内容时暂存自然消失的回归
|-- agent_py_agent/tests/test_ready_control_receipt_source.py # 真实ready Agent固定管理回执与完整controller、来源失活拒绝回归
|-- agent_py_agent/tests/test_gateway_coordinated_lifecycle.py # 原Thread ACK、规范状态路径和重启拒绝
|-- agent_py_agent/tests/test_gateway_coordinator_client_history.py # C只读原Store、认证分页与无冷owner写
|-- agent_py_agent/tests/test_gateway_client_history_healthy20.py # 已有实际Agent的健康C仍走同一只读分页并拒绝失配来源
|-- agent_py_agent/tests/test_runner_thread_error_reports.py # 原native线程出处分类、复制与普通伪字段拒升级
|-- agent_py_agent/tests/test_shared_seed_write_guard.py # 六共享种子入口在副作用前拒绝非全局写者
|-- agent_py_agent/tests/test_coordinator_global_index_compact.py # 原C共享压缩签名、末写与流式算法回归
|-- agent_py_agent/cli/gateway_worker_runtime.py # 原C启动回执、精确P4对象和真实退出责任组合
|-- agent_py_agent/tests/test_skill_learning_execution_ports.py # main v1来源/FIFO、原候选确认与撤权后拒写
|-- agent_py_agent/tests/test_background_execution_ports.py # 原后台回调、claim和未结对象穿透普通失败出口
|-- agent_py_agent/tests/test_curator_writer_transactions.py # 策展真实文件短门、撤权后拒写及同线程窗口
|-- agent_py_agent/tests/test_recovery_committed_report.py # 原终态计数、实际typed分类与观察读失败保护
|-- agent_py_agent/tests/test_f3_worker_error_contracts.py # worker启动错误唯一分类合同
|-- agent_py_agent/tests/test_control_producer_receipts.py # 原控制分派、claimed身份与精确目标
|-- agent_py_agent/tests/test_control_receipt_delivery_closure.py # 同步领域结果到真实投递链及claimed等待/隔离边界
|-- agent_py_agent/tests/test_control_producer_memory_ingress.py # C中保留普通用户Memory活封印与确认
|-- agent_py_agent/tests/test_gateway_multiprocess_control_forward.py # C唯一初始化新owner、W只读控制与回执重放
|-- agent_py_agent/tests/test_tool_coordinator_execution_ports.py # 原工具claim短scope与实际未结异常传播
|-- agent_py_agent/cli/gateway_worker.py # 内部worker命令解析与冻结启动运输
|-- agent_py_agent/agent/gateway_parts/multiprocess_worker.py # 原W组合根、轮询与停机句柄
|-- agent_py_agent/agent/gateway_parts/multiprocess_state.py # C/W规范就绪投影
|-- agent_py_agent/agent/gateway_parts/multiprocess_bootstrap_ready.py # 原登记和实际端口的ready核验
|-- agent_py_agent/agent/gateway_parts/multiprocess_control_poller.py # 独立控制轮询与原线程启动责任
|-- agent_py_agent/agent/gateway_parts/multiprocess_worker_dispatch.py # 同分区请求与控制泵
|-- agent_py_agent/agent/gateway_parts/multiprocess_worker_drain.py # 原出生、调用表与停止排空观察
|-- agent_py_agent/agent/gateway_parts/multiprocess_control_worker.py # 原控制认领、精确执行及CAS
|-- agent_py_agent/agent/gateway_parts/multiprocess_control_reconcile.py # 既有guidance回执只读观察
|-- agent_py_agent/tests/test_gateway_worker_runtime.py # 空/重复/迟到callback和原租约/P4组件回归
|-- agent_py_agent/tests/test_gateway_admission_closing.py # ordinary末发表与stop同门、原责任收尾及错源拒绝运行回归
|-- agent_py_agent/tests/test_nofollow_atomic_snapshot.py # 原atomic writer安全A/B有限重读、父链/硬错/FD首因运行回归
|-- agent_py_agent/tests/test_nofollow_fstat_publication.py # 真实FD采样后原子发表及元数据/链接/父链/清理边界
|-- agent_py_agent/tests/test_nofollow_zero_link_copyout.py  # 零链接copyout废弃、预算及原身份安全门回归
|-- agent_py_agent/tests/test_gateway_role_marker_snapshot.py # closed第三次role原atomic窗口、默认kwargs及危险叶运行回归
|-- agent_py_agent/tests/test_gateway_closed_worker_publication.py # 原旧代封存后首次当前W发表的有界整组读、跨轮固定来源及硬拒绝运行回归
|-- agent_py_agent/tests/test_gateway_role_heartbeat_stability.py # 当前W原producer动态双读与身份/旧代全raw拒绝运行回归
|-- agent_py_agent/tests/test_gateway_role_lifecycle_closure.py # 原producer一次关闭封存、同home角色消费和未知保留
|-- agent_py_agent/tests/test_gateway_role_lifecycle_publication.py # 完整raw CAS、写后未知和晚变来源运行回归
|-- agent_py_agent/tests/test_gateway_role_lifecycle_successor.py # 三代非空P1、fresh旧scope与一次关闭观察运行回归
|-- agent_py_agent/tests/test_gateway_role_lifecycle_receipts.py # 后继实际HTTP管理/model/local固定回执来源运行回归
|-- agent_py_agent/tests/test_local_control_reconciliation_writer.py # 本机插话/审批固定对账、三投递态与来源/fullraw拒绝运行回归
|-- agent_py_agent/tests/test_gateway_role_lifecycle_old_control.py # 原W控制完成后后继关闭责任的运行回归
|-- docs/design/GATEWAY_ROLE_LIFECYCLE_CLOSURE.md # 原角色关闭范围v2、生产者封口和新C消费边界
|-- agent_py_agent/tests/test_gateway_current_requeue.py # 原claim让出、陈旧来源拒写与Count0重复恢复
|-- agent_py_agent/tests/test_gateway_processing_recovery_source.py # 同原P2/P3/RuntimePolicy的恢复短门与原欠账
|-- agent_py_agent/tests/support/multiprocess_processing_cases.py # 临时旧域记录夹具，合成进程表不作物理P2证明
|-- agent_py_agent/tests/test_curator_uncertain_outcome.py # Curator实际未知出口与原lease/pending/cursor责任保留
|-- agent_py_agent/tests/test_host_command_short_ports.py # 原宿主命令领取、审批时限、未知责任与退出后拒写
|-- agent_py_agent/tests/test_model_call_drain_snapshot.py # 原模型账本的在途/保留对象只读快照
|-- agent_py_agent/tests/test_native_transport_diagnostic.py # 原native阶段与Future安全诊断、hash关联及不改变责任的纯组件回归
`-- agent_py_agent/tests/test_curator_original_transport.py # 同原C/W来源、取消与线程物理退出组件回归
```

原 `runtime_db/executor_liveness.py` 与 `host_command_execution.py` 只把既有事务接到唯一 common 短端口；`contracts/model_call_ledger.py` 不另存线程或执行许可，`memory_store/curator_backend.py` 沿原预算和运输。上述测试不替代完整 Count2/native/排空验收。

### F3 纯合同实施切片（2026-10-16，未启用多进程）

```text
|-- agent_py_agent/agent/gateway_parts/multiprocess_record_schema.py # C/W 版本记录的纯字段声明，不提供进程权威
|-- agent_py_agent/agent/gateway_parts/multiprocess_route.py # 原宿主 owner 与完整请求指纹的纯分区、盖章和校验
|-- agent_py_agent/tests/test_gateway_multiprocess_route.py # 合成归属、代次、媒体与入口 actor 防篡改合同
`-- agent_py_agent/tests/test_gateway_multiprocess_schema.py # 固定版本字段与无运行时依赖核对
```

关键文件说明：route 只投影既有 `host_request_owner.v1`，不产生 owner、actor 或执行许可。字段声明不等于完整记录验证、生产消费者或 Count2 运行；未启用的依赖与后续记录/来源/准入/执行切片分别记录。

B1 的唯一记录编码与完整纯校验闭包（仍无生产消费入口）：

```text
|-- agent_py_agent/agent/gateway_parts/multiprocess_records.py # 原版本记录、严格codec、FSM与规范终态释放校验
|-- agent_py_agent/agent/gateway_parts/multiprocess_bootstrap_schema.py # registry与heartbeat的纯结构校验
|-- agent_py_agent/agent/gateway_parts/multiprocess_executor_domain_schema.py # v2执行域的纯字段、原身份及一致性检查
|-- agent_py_agent/agent/gateway_parts/multiprocess_session_key.py # 原会话三元组的规范摘要投影
|-- agent_py_agent/agent/gateway_parts/multiprocess_stop_schema.py # 唯一取消格式与观察时间校验，不执行停止
|-- agent_py_agent/tests/test_gateway_multiprocess_records.py # 版本字段、规范wire与记录状态机
|-- agent_py_agent/tests/test_gateway_multiprocess_session_key.py # 会话投影、中文/空键与原单进程分组对照
|-- agent_py_agent/tests/test_gateway_multiprocess_record_semantics.py # 多版本终态、分区与嵌套身份反例
`-- agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py # 非法极值、错误记录类型与纯闭包边界
```

关键文件说明：取消结构从原运行模块唯一分离，records 延迟调用同一 schema，不能将它当作 ready、取消许可或完整物理退出证明。错误分类复用 `contracts/error_taxonomy.py` 的准确不可自动重放条目，不用 UNKNOWN 兜底替代协议拒绝。

B2 启动模式组件（不是实际 C/W ready 证明）：

```text
|-- agent_py_agent/agent/common/executor_registration.py # 受管执行启动上下文与拒绝接口，默认无I/O
|-- agent_py_agent/agent/common/global_write_guard.py # 仅C可写事实的角色拒绝门，不签发写入权
|-- agent_py_agent/agent/gateway_parts/multiprocess_mode.py # 原内部启动信封与同进程模式拒绝门
|-- agent_py_agent/agent/gateway_parts/multiprocess_bootstrap.py # 冻结WorkerLaunch并复核根、分区与代次
|-- agent_py_agent/tests/test_gateway_multiprocess_bootstrap.py # 启动快照缺失及不可变事实漂移
`-- agent_py_agent/tests/test_gateway_worker_bootstrap_scope.py # mode/scope拒绝、恢复及默认无I/O
```

只读委派和遥测接缝的直接运行回归（仍须实际 C/W 组合验收）：

```text
|-- agent_py_agent/tests/test_im_dispatch_readonly.py # 规范owner、原构造配置路径、无写读取及跨日冻结快照
|-- agent_py_agent/tests/test_telemetry_transport_uncertain.py # 原执行未知穿透统计/上下文/独立压缩结算，普通显示故障仍降级
|-- agent_py_agent/tests/test_bounded_call_lifetime.py # 有界调用出生未知、真实退出及worker与cleanup共同期限
`-- agent_py_agent/tests/test_bounded_call_observation_identity.py # 原观察缺失、复制、替换及固定线程锚不能假报已退出
```

2026-10-17 BG 原 claim 运输定位补缝：

```text
|-- agent_py_agent/tests/test_supervisor_first_start.py # 公开监护首启私有设施、危险父链与预核零副作用回归。
|-- agent_py_agent/tests/test_host_notification_scope.py # 宿主通知真实热轮、词法隔离、取消及未结责任回归。
|-- agent_py_agent/tests/test_host_notification_transport.py # 通知阶段沿原C/W、T/fence与claim CAS运输回归。
`-- agent_py_agent/tests/test_background_claim_transport_reference.py # 原C/W、会话claim、core callback与同T发布的普通本地回归。
```

2026-10-16 F4/F5 组合候选新增的重要模块与直接测试如下；这是本轮结构增量，后续原文件树与历史阶段保留。未列出的同组回归仍以测试目录为准，运行时资料不逐项列入。

```text
|-- agent_py_agent/
|   |-- agent/
|   |   |-- agent_core/
|   |   |   `-- self_map_bootstrap.py # 在原 Agent 装配阶段接入只读自我目录，不创建服务或触发运行维护。
|   |   |-- capability/
|   |   |   |-- _agents_contract_fields.py # 提供三个冻结接口共同的结构化校验错误。
|   |   |   |-- agents_control_ledger.py # 安全持久保存预览、直接确认原授权和一次性消费状态。
|   |   |   |-- agents_learning_contract.py # 冻结可学习来源、自动化排除和子代理标签，不接候选库或curator。
|   |   |   |-- agents_source_cache.py # 拆平HTTPS成功提交、last-good降级和冻结元数据复核，保持原控制文件与锁。
|   |   |   |-- agents_source_configuration.py # 有界严格读回owner配置并精确验证唯一记录。
|   |   |   |-- agents_source_contract.py # 冻结来源加载、缓存、配置 CAS 与前缀交接使用的数据结构。
|   |   |   |-- agents_source_create.py # 防止默认文件缺失时绕过来源CAS、覆盖用户抢先创建的文件或误删外部正文。
|   |   |   |-- agents_source_files.py # 固定每段目录/最终文件身份，从同一FD读写，并安全提交小型owner配置。
|   |   |   |-- agents_source_host.py # 将唯一AGENTS来源服务接进现有宿主身份、参数和权限裁决。
|   |   |   |-- agents_source_limits.py # 拦住超过注入预算的新增，同时让历史超限正文仍能受控缩小。
|   |   |   |-- agents_source_load_contract.py # 给来源正文与元数据一个不可变、可校验的交接包。
|   |   |   |-- agents_source_local.py # 拆平同FD本地写回，不改变CAS/摘要/权限/硬帽检查顺序。
|   |   |   |-- agents_source_network.py # 安全读取 HTTPS 来源，并返回供缓存逐跳重新授权的不可变原字节结果。
|   |   |   |-- agents_source_options.py # 用一个不可变依赖集合绑定开关、URL许可、严格传输和活path policy。
|   |   |   |-- agents_source_processes.py # 用结构化来源版本给宿主进程绑定边界并在提交前请求原注册表退役，不检查stdin文本。
|   |   |   |-- agents_source_publish.py # 把完整正文一次发布到缺失的默认来源，并保留可验证的回滚FD。
|   |   |   |-- agents_source_read_lock.py # 协调同进程来源读写并保留无副作用读取。
|   |   |   |-- agents_source_repository.py # 管理默认和显式本地来源的安全加载及受授权写入；URL由独立传输模块加载，不授予额外权限。
|   |   |   |-- agents_source_rollback.py # 消除校验到unlink之间的公开路径替换竞态，冲突时保留外部数据。
|   |   |   |-- agents_source_switch.py # 拆平配置提交前后校验和资源退役，保持唯一来源仓库的小步事务。
|   |   |   |-- agents_write_contract.py # 冻结用户本人明确写入请求与宿主核对上下文。
|   |   |   |-- agents_write_service.py # TUI/IM共享AGENTS预览→确认→一次提交，身份由宿主绑定。
|   |   |   |-- memory_tool_recovery.py # 为记忆工具接续删除遗留清理并投影准确提交事实。
|   |   |   |-- persona_body_transaction.py # 提交普通人格正文并在失败时只撤回本事务拥有的文件。
|   |   |   |-- persona_snapshot_transaction.py # 准备人格版本快照并提供条件回滚材料。
|   |   |   |-- persona_storage_io.py # 安全读写人格版本与快照的底层文件。
|   |   |   |-- persona_version_ledger.py # 原子发布人格版本记录并保留可恢复的事务边界。
|   |   |   |-- self_map_memory.py # 登记记忆、Persona和学习组件的安全元信息、数量未知原因及原读入口。
|   |   |   `-- self_map_skills.py # 登记普通方法、数量和管理员权威位置，保留stable_id及合法正文入口。
|   |   |-- conversation/
|   |   |   |-- learning_source.py # 构造、解码学习来源投影，并从线程与消息推导当次有效来源事实。
|   |   |   |-- self_map_context.py # 登记上下文和 Compact 的只读元数据。
|   |   |   `-- self_map_work.py # 登记本 owner 与当前代理的工作元信息，正文和唤醒/维护副作用不进入目录。
|   |   |-- gateway_parts/
|   |   |   |-- agents_control_service.py # 沿现有公共控制链提供同owner的规则来源、预览与一次确认，保留原HostActor和群配置租约。
|   |   |   |-- memory_candidate_control.py # 让本人在IM或终端查看并准确确认记忆候选，普通/memory搜索仍走原入口。
|   |   |   `-- memory_candidate_review.py # 绑定候选完整版本和目标CAS，先耐久消费再准确审核提交；未知永不重放。
|   |   |-- memory_store/
|   |   |   |-- _jsonl_maintenance.py # 验证维护源并携带是否真正读到文件的事实，保护破坏性派生重建。
|   |   |   |-- candidate_commit_lock.py # 协调候选提交和正式删除后的同步清理。
|   |   |   |-- candidate_source_coverage.py # 让多次候选合并可补齐同引用来源，同时永久保留显式来源否决。
|   |   |   |-- candidate_target_binding.py # 防止旧消息或工具证据借改写候选、切换动作或入口重新取得正式目标版本。
|   |   |   |-- curator_learning_source.py # 为策展候选绑定完整引用集合的学习资格。
|   |   |   |-- curator_promotion_receipts.py # 为策展运行保留不含私密正文的晋升摘要。
|   |   |   |-- curator_rejected_bindings.py # 阻止同一被拒证据通过重放刷新正式目标版本。
|   |   |   |-- evidence_admission.py # 拦住失败、未知或身份不完整的工具事实来源。
|   |   |   |-- learning_source_authority.py # 为直接记忆与策展路径取得一致的宿主学习来源。
|   |   |   `-- promotion_remove_recovery.py # 在重启和故障后收拾已删除记忆的候选与派生残留。
|   |   |-- plugin_self_map.py # 分开能力包安装/构建/普通方法及插件库存，提供权威位置，不复制授权状态。
|   |   |-- settings/
|   |   |   `-- self_map_settings.py # 登记模型库存和有效配置元数据，普通用户只开放原 decision-only 面。
|   |   |-- tooling/
|   |   |   |-- pty_process_lifecycle.py # 保留真实PTY组长的内核退出身份供原注册表清退，不用根退出推断整树死亡。
|   |   |   |-- self_map_availability.py # 以同一纯判据决定本次模型输入是否可提示self_map入口。
|   |   |   |-- self_map_display.py # 给自我目录提供短行、共享精确详情模板及数量/完整性说明，不读盘或保存状态。
|   |   |   |-- self_map_mcp.py # 登记 MCP 配置、连接和发布状态；配置与本机 transport 观测分别计数。
|   |   |   |-- self_map_registry.py # 提供自我目录登记、只读上下文和分页来源合同，新领域只需登记 reader。
|   |   |   |-- self_map_tool.py # 以短总览、分块分页和精确条目回答关于代理自己的问题。
|   |   |   `-- self_map_tools.py # 为自我目录登记当前授权工具的用途、来源、展开及不可用原因。
|   |   `-- user_space/
|   |       `-- self_map_paths.py # 登记目录与权限分块，区分模型直写、宿主专用入口与未知观测。
|   `-- tests/
|       |-- memory_confirmation_fixtures.py # 仅测试临时owner和原人工确认链的隔离helper
|       |-- test_agents_control_transport.py # 当前用户封印、真实控制回执与作用域还原
|       |-- test_agents_source_lifecycle_review39.py # 来源切换、资源退役与未知状态
|       |-- test_agents_source_mount_order.py # 人格祖先与原hidden/拒写/精确授权的纯argv组合合同
|       |-- test_agents_source_repository.py # 唯一来源配置与读写权限及缓存身份
|       |-- test_agents_source_closed_owner_host.py # closed宿主重建与来源活许可、修复和再次撤销
|       |-- test_agents_source_sandbox_cli.py # 模块定义顺序与fake binary的真实CLI子进程回归
|       |-- test_agents_write_contract.py # 精确意图、owner/actor/有效期及正文版本
|       |-- test_memory_candidate_control.py # 本人候选控制真实临时认证、CAS、unknown、并发与完整显示
|       |-- test_memory_remove_recovery_guards.py # 删除墓碑恢复和派生坏库不假成功
|       |-- test_memory_user_confirmation.py # 来源资格不代替正式内容确认
|       |-- test_self_map_contract.py # 领域、分页、未知事实、路径与原权限负例
|       |-- test_self_map_read_authority.py # 真实内置读门对照、插件范围区分、普通/群/子代理与冻结cwd
|       |-- test_self_map_model_surface.py # 普通/Compact资格、关闭开关与main媒体保留
|       `-- test_self_map_runtime.py # 正式工具执行、撤回、cursor与fake LLM/replay
`-- docs/
    `-- design/
        `-- SELF_MAP.md # 自我目录公开读口、权限、完整性与导航合同
```

## 关键文件说明

### 2026-10-25 七项能力的新增入口与直接测试

- `agent_py_agent/runtime_sdk.py`：公开 `get_datasource` / `query_sql` / `DatasourceHandle`，只重导出唯一工作流SDK；不据设计简称宣称存在 `my_agent_runtime` 兼容模块。
- `agent_py_agent/agent/assets/browser_recorder.py`：持久化安全动作序列并编译参数化脚本及可选工作流/Skill；创建记录不启动浏览器，真实采集/回放和部署另验。
- `agent_py_agent/agent/assets/datasource_store.py`：owner/workspace 规格与 owner 私有秘密分离、公开摘要脱敏、SQLite 建库/探测/迁移及版本 CAS；秘密是 `data/datasource_secrets/<ref>.json` 0600 私有明文，不是加密 `vault.json`。
- `agent_py_agent/agent/assets/turn_extractor.py`：聚合选定回合中的实际工具证据，生成脱敏持久化 DRAFT，并按选择与依赖发布 script/workflow/skill；不自动重放历史命令，不将草稿生成说成零落盘。
- `agent_py_agent/agent/assets/workspace_assets.py`：`.agent/` 四类资产按 `workspace > owner > builtin` 合并，只读发现不创建目录；显式晋升与批量保存沿 CAS 和两阶段事务。
- `agent_py_agent/agent/conversation/background_process_view.py`：只读原受管记录和有限日志，保留 unknown；默认 owner 聚合、可显式按线程过滤，不恢复事务或消费通知。
- `agent_py_agent/agent/conversation/thread_fork_service.py`：只分叉主会话，按完整工具边界选前缀，重建检查点身份及复制冻结人格，最后发表新线程；不改源或继承运行授权。
- `agent_py_agent/agent/conversation/workspace_context.py`：统一目录存在性、管理员/普通用户及宿主控制面边界，提供 `/dir` / `/fork` 领域控制和只读项目上下文；工作目录不替代 owner home。
- `agent_py_agent/agent/plugin_host/action_specs_v2_ext.py`：本轮 20 个新动作的唯一静态规格来源，准确名称见 [设计台账](DESIGN_LEDGER.md)，不是整个 Host API 动作总量。
- `agent_py_agent/agent/plugin_host/capabilities_v2_handlers.py`：授权后调用目录/分叉/数据源/提炼/录制/后台领域，复用原控制回执和写幂等，不把 payload 身份当可信 owner。
- `agent_py_agent/agent/plugin_host/observations.py`：工作区资产与上下文只读投影、后台进程安装线程及 task 范围过滤；不代表新暴露了 `observations.recent` 动作。
- `agent_py_agent/agent/tooling/datasources_tool.py`：受信 owner/workspace 下的数据源管理工具，公开结果和错误不暴露秘密；连接测试不是 SDK 已支持全部驱动的证明。
- `agent_py_agent/agent/workflows/datasource_runtime.py`：将脚本声明解析为 run 级冻结绑定，生成有界私有清单并清理，按真实秘密值清洗输出；不能用输入覆盖扩展数据源授权。
- `agent_py_agent/agent/workflows/runtime_sdk.py`：只允许合同声明的逻辑代号，SQLite 查询使用参数绑定及原生 authorizer 限制只读/表范围；非 SQLite SQL 当前明确不支持。
- `agent_py_agent/tests/test_browser_recorder_compiler.py`：动作生命周期、参数化/秘密默认值、非法导航、生成脚本与真实存储入口；fake Playwright 不代表真实浏览器验收。
- `agent_py_agent/tests/test_cli_dir_and_workspace_assets.py`：目录参数、显式路径安全、owner 双根、四类覆盖/晋升与批量保存回滚边界。
- `agent_py_agent/tests/test_conversation_thread_fork.py`：目录绑定/CAS、工具回合安全前缀、Compact/冻结人格继承、消息新身份及源文件不变。
- `agent_py_agent/tests/test_datasource_keyring_and_script_binding.py`：凭据分离/脱敏、SQL 声明门、只读/表范围、子进程清单清理、真实 SQLite 及迁移恢复；不证明远端迁移已完成。
- `agent_py_agent/tests/test_plugin_host_v2_five_capabilities.py`：新增 20 动作矩阵、闭合参数、能力 AND 门、写入幂等、安装撤销及 owner/线程隔离。
- `agent_py_agent/tests/test_tui_turn_completion_and_bg_watch.py`：完成记录不替换 Footer、历史回放、后台聚合/导航/按键、只读日志路径和坏记录隔离。
- `agent_py_agent/tests/test_turn_asset_extractor.py`：检索与回合打包、分析前后脱敏、DRAFT 不发布、选择性提交/重放、依赖重绑与部分发布回滚。

- `docs/design/GATEWAY_ROLE_INCIDENT_ACCEPTANCE_2026-10-20.md`：区分报告线索与本机证据，记录启动、身份、状态出口、去重、故障恢复和真实长跑验收；不授权清除未知责任或绕过owner权限。

- `agent_py_agent/tests/test_gateway_pid_record_identity.py`：共享PID发现者的producer/readiness/启动caller运行合同；fake OS与临时文件不替代真实服务验收。

- `agent_py_agent/agent/gateway_parts/native_diagnostic_projection.py`：只验证原safe producer的有限schema与公开literal，不读文件、不授权限，返回六项固定诊断字段。
- `agent_py_agent/tests/test_gateway_safe_native_diagnostics.py`、`test_admin_self_gateway_diagnostics.py`：分别验证有界无正文投影和完整HomePaths／当前main attempt／claim／ToolExecutor链；新增普通回归须双平台包内实际执行。


- `agent_py_agent/tests/test_tui_decision_menu_sink_lifecycle.py`：验证真实测试TUI helper退出后归还进入前的全局输出sink，覆盖嵌套及工厂、setup、正文、exit、await异常；9节点属于普通双平台运行义务。

- `agent_py_agent/agent/backends/assistant_messages.py`：逐消息来源保存在原正文块，阶段不授执行或续跑权；完整当前正文和整段净化上下文成立时才分段显示。Responses收集器的真实索引只在内部归一化，不伪造缺失索引或泄入其它协议。
- `agent_py_agent/agent/agent_core/model/visible_response.py`：只在原工具开始或最终采纳边界核同次sealed调用，再提交可见正文；生成中增量、被拒正文或历史块不能成为公开回复。
- `test_assistant_message_phases.py`、`test_committed_model_messages.py`、`test_pending_input_public_boundary.py`、`test_progress_message_revision.py`：分别维护阶段/历史、显示采纳、失效上限、真实索引和TUI活动到canonical唯一冻结合同。
- `test_credential_card_validation.py`：使用临时owner、原签名/输入账及实际回环核Count0/正Count和adapter；表单错误可改正，原身份/期限/重放门、密文保存和单Wake不变。
- `test_final_response_projection_roles.py`、`test_tui_local_candidate_commit_boundary.py`、`test_committed_stream_public_boundary.py`：以原内部正文、完整安全投影及sealed-call提交验证CLI／TUI／Gateway／IM真实公开出口，覆盖未提交片段、取消、失败关闭、旧草稿重放及合法工具／心跳；新增133节点为普通双平台运行义务。

- `agent_py_agent/tests/test_mcp_approval_sink_reuse.py`：验证原Agent有界缓存跨sink复用同范围approved_session，保新attempt/权限/插件与失源边界。

- `agent_py_agent/tests/test_admin_full_access_mcp.py`：离线供应商验证当前管理员Full Access仅免普通MCP确认，不扩大子代理、插件或宿主执行权限。

- `agent_py_agent/tests/test_curator_dispatch_eligibility.py`：区分待确认候选资格与新策展工作，验证完整字节稳定和原pending/lease/输入/日终入口。

- `persona_storage_io`只选择原本地IO策略，固定close不发行恢复权；原人格/版本/配额仍各守唯一算法。`coordinator_owner_management`复用原factory command/source/receipt，ExitStack维持原借用、global scope和seal至父complete/event结束，不增加第二调度或授权账。

- 两个固定bootstrap沿原main加载同包受信委托，launcher只发现固定模块origin；模块发现与无参数125不代表后台执行已验，正式包必须另跑原业务路径。

- `coordinator_memory_candidates`不持久source或actor；`memory_preview_transaction`仅原预览账固定末叶，工厂真人seal寿命延续至父收口，默认host审核仍共用唯一算法。

- `coordinator_client_memory`复用唯一owner holder、同quota/promotion引用及每次实际来源；JsonlMemory依赖DTO只组织原对象，不携权限或替代原八参构造，详见冷记忆闭包合同。

- `agent_py_agent/agent/gateway_parts/coordinator_control_transport.py`：cold None正count普通POST的固定producer；原HTTP同对象、原pin/lifetime、完整输入与资源预算只约束原receipt运输，不构造Agent或授予管理权限。
- `agent_py_agent/tests/test_coordinator_control_forward.py`：实际HTTP→原回执→原W真实池和领域的普通验收入口；不替代正式Count2/native、管理/记忆或物理停止验收。

- `gateway_parts/multiprocess_background_input.py`只从原主后台当前claim/binder捕获opaque来源，文字输入经原安全点append/reserve/submit/ACK，原claim.finish fullraw CAS后结算完整队列。旧无transport pending与G0仍未闭，不新增输入状态权威或关闭整个Gateway。
- `gateway_parts/private_approval_control.py`消费原签名和准确原执行引用，把批准/拒绝交给当前真实等待线程；负向退休只改原permission/JSON，资源忙保留typed未结，不新建审批或执行权威。原Future与恢复链另验，不能以卡片回执假定业务完成。
- `conversation/guidance_injection.py`与`conversation/input_resource_retry.py`分别维护原内存注入集合和固定安全点资源预算；runtime与Gateway直接共用实际实现，不保留反向依赖或旧转发入口。注入不等于提交或ACK，预算不授执行权。
- `common/runner_thread_origin.py`只捕获和只读核验原Thread/Context/PID/异常同对象，原runner仍负责stop/join，Gateway仍核完整claim和持久未知；来源观察不运行旧Context或释放P3。
- `conversation/input_dependencies.input_dependency_owner_resources`组装原配额、canonical child loader和existing-only RuntimeDB，原私卡事务逐次commit门、owner/current和单Wake算法保持，不初始化业务Agent或新库。
- F3 `multiprocess_records` 是唯一记录codec/FSM入口，取消格式只在 `multiprocess_stop_schema` 实现，真实 stop 必须复用；纯格式校验不能冒充 actual ready、死亡或物理排空。
- F3 `multiprocess_route` 只投影原 `host_request_owner.v1` 与主线指纹；进程内模式、角色标记及 callback 存在均不签发执行权。真实启动仍须原 C lease 与 source/ports 活性，具体拒绝沿 `contracts/error_taxonomy.py`，不降成 UNKNOWN 或自动重放。
- `im_dispatch_policy` 的冷读必须沿宿主提供的原构造 root 和准确覆盖路径，不把 owner home 当同一概念；`context_usage`、`model_metrics` 与独立辅助调用结算沿 common 唯一 typed 未知原对象上抛，显示失败不能洗掉执行责任。
- `backends/bounded_call.py` 的原 `_CALLS/_Call` 与 `concurrency/interrupt.py` 的原取消句柄分别保留 worker/cleanup；只读快照不清表，退出等待共用原 deadline，未知由独立类型交原请求核实，不扩权限或自动重放。

- `test_background_claim_transport_reference.py` 经原领取、模型尝试和 core task guard 发布两侧身份，覆盖同T重入、Compact、Count0、取消及原typed未结责任；定位投影不授执行权。
- `agent_py_agent/tests/test_agents_source_closed_owner_host.py`：以真实本机与普通 owner 验证来源构造不签权、坏权限不读正文或缓存，修复后按活策略恢复。

- `agents_source_repository` 是本人规则来源读写组合入口；configuration 只给配置元信息，load/缓存与实时权限各有原合同。`agents_write_service` 与原用户控制回执负责精确预览和一次消费，不能从模型文字重建授权。
- `memory_candidate_control`/`memory_candidate_review` 组合原认证、owner/HostActor、候选与 promotion 服务；nonce 沿原 Gateway preview receipt，无第二正式库或审批账。来源覆盖、目标绑定和删除恢复继续落原唯一候选/正式记录。
- `self_map_bootstrap` 只装配领域 reader；`self_map_registry/tool/display` 提供原范围内的聚合、完整性与分页，实际权限、库存、状态和正文读取各归原模块。F5 不代替 list_tools/tool_search，不新增网络、执行或授权入口。
- F5 的 `registry_invoke` 仅冻结原内置读取门的 mode/exact roots/墙外范围事实；`self_map_paths` 分别说明内置路径裁决与插件工作区读取上界，workspace 取同次 cwd，Skill 位置仍取本人原快照来源。`self_context` 只补来源标签和数量，不新增根路径披露；真实文件门不变。
- 本轮详细边界见[规则归属](docs/design/PERSONA_AGENTS_OWNERSHIP.md)、[自我目录](docs/design/SELF_MAP.md)和[集成设计](docs/design/SEVEN_FEATURE_INTEGRATION.md)。最终组合 gate、真实入口和发布未由文件已存在推断为完成。


2026-10-13 安全追加的新入口（只复用原配置、权限和凭据账）：

```text
|-- agent_py_agent/agent/auth/configuration.py # CLI/HTTP 共用原配置认证构造与启动拒绝
|-- agent_py_agent/agent/ingestion/source_credentials.py # 原来源 fetch 的当前 owner/TaskCredential 授权
|-- agent_py_agent/tests/test_gateway_auth_configuration.py # 实际生命周期零凭据及非法 IM 共享档拒绝
|-- agent_py_agent/tests/test_gateway_header_ambiguity.py # 重复认证/身份头真实 HTTP 拒绝
|-- agent_py_agent/tests/test_host_secret_environment_boundary.py # 准确 SecretRef 名冻结与真实命令环境
|-- agent_py_agent/tests/test_owner_network_policy_integrity.py # 策略损坏及活实例主子孙网络封顶
|-- agent_py_agent/tests/test_ingestion_secret_scope.py # 宿主秘密、同源跳转和回显原入口拒绝
|-- agent_py_agent/tests/test_ingestion_task_credential_scope.py # 私人卡、任务/监控/子孙/恢复精确授权
|-- docs/design/OWNER_NETWORK_POLICY_INTEGRITY.md # 当前文件与创建期权限交集边界
|-- docs/design/WATCH_SOURCE_SECRET_AUTHORITY.md # 无新秘密账的来源凭据注入及恢复限制
`-- docs/tasks/IM_SOURCE_SECRET_SECURITY_HANDOFF.md # 合成攻击与合法私人凭据回归证据
```

关键职责：configuration 在监听或恢复前校验结构化部署事实；source_credentials 只在原请求出口解引用原授权，不把秘密或授权对象写回来源状态；权限文件仍是原唯一策略。


Gateway 安全初始化：`agent/auth/configuration.py` 是 CLI 与 HTTP 服务共用的配置→鉴权唯一入口，拒绝关闭鉴权的多 owner 组合及任何 IM 未隔离组合，配置缺中间件不能静默放行。


宿主环境与运输安全交接：[环境来源边界](docs/design/HOST_CREDENTIAL_ENVIRONMENT_BOUNDARY.md)、[owner 权限验收](docs/tasks/IM_OWNER_PERMISSION_SECURITY_HANDOFF.md)、[飞书运输安全](docs/design/FEISHU_OUTBOUND_SECURITY.md)、[独立投递复核](docs/tasks/FEISHU_SECURITY_AUDIT_HANDOFF.md)。原模块/账本保持唯一；最终同源源码门已通过；包内验收及发布身份以同包发行清单为准。


安全追加：`common/authenticated_http.py` 统一固定认证接口不可重定向运输；`delivery/attachment_content.py` 只在一次 DeliveryAttempt 冻结 owner/hash/size 一致字节；`adapter/feishu_attachment.py` 上传同一冻结字节。域授权仍属于原 owner/context/artifact，详见 [安全设计](docs/design/IM_SECURITY_AUDIT_20261013.md) 与 [会话交接](docs/tasks/IM_TASK_SCHEDULER_SECURITY_HANDOFF.md)。


群引用资料入口：`agent_py_agent/agent/adapter/feishu_quoted_media.py` 核验准确父消息 ID、同群/话题和未撤回事实；文字作为有界外部资料，资源交原 owner 下载器，不引入历史缓存或父发送者权限。`agent_py_agent/tests/test_feishu_quoted_media.py` 保留附件边界，`agent_py_agent/tests/test_feishu_quoted_text.py` 验证人/机器人文字、当前 actor、截断、混合失败与冷重投；[设计说明](docs/design/FEISHU_QUOTED_MEDIA.md) 记录用户步骤、飞书权限和失败事实。

```text
|-- agent_py_agent/tests/test_feishu_quoted_text.py # 群引用文字的来源、权限、混合准备与冻结重投
|-- agent_py_agent/tests/test_feishu_quoted_content.py # 可见/隐藏/未知正文和有界解析正反例
|-- agent_py_agent/tests/test_feishu_quoted_members.py # 转发根、准确关系、重复/撤回/深度成员限制
|-- agent_py_agent/tests/test_feishu_quoted_inputs.py  # 各类引用资料、逐源下载和原入站安全整合
```

2026-10-13 IM 追加修复入口（沿原授权、调度及用量账）：

```text
|-- agent_py_agent/agent/gateway_parts/schedule_diagnostics.py # 监控参数与无正文错误诊断
|-- agent_py_agent/agent/scheduler/store_snapshot.py # 原账本完整原子只读快照
|-- agent_py_agent/agent/scheduler/recovery.py # 原 claim 死亡恢复与终态历史
|-- agent_py_agent/agent/scheduler/creator_migration.py # 冻结来源的旧群创建人迁移
|-- agent_py_agent/agent/scheduler/creator_access.py # 宿主创建者显示与有界实时离群代清理证明
|-- agent_py_agent/tests/test_schedule_owner_scope.py # 当前owner监控、同群代清理及原累计指标矩阵
|-- docs/tasks/SCHEDULE_OWNER_SCOPE_HANDOFF.md # S01–S05独立开发线交接
|-- agent_py_agent/agent/user_space/feishu_cloud_authorization.py # 真实个人/群协作者权限验证
|-- agent_py_agent/agent/user_space/feishu_sheet_authorization.py # 表格保护范围与准确主体编辑权限
|-- agent_py_agent/agent/contracts/model_call_latency.py # 原模型调用账的匿名单调耗时
|-- agent_py_agent/agent/agent_core/model/im_wait_activity.py # 当前物理模型调用的 IM 等待观察
|-- agent_py_agent/agent/gateway_parts/stream_liveness.py # 原 chunk 出口的工具与模型等待接口
|-- agent_py_agent/tests/test_im_control_repair.py # 后置提及、时区、诊断、审批与计时运输
|-- agent_py_agent/tests/test_scheduler_repair.py # 无写锁快照、死亡恢复和旧群归属
|-- agent_py_agent/tests/test_feishu_cloud_collaborator_authorization.py # 自助授权、撤权、wiki 与保护范围
|-- agent_py_agent/tests/test_feishu_sheet_cloud_authorization.py # A1 保护范围、编辑者与追加位置不确定
|-- agent_py_agent/tests/test_model_latency_accounting.py # 物理调用耗时、用途分区与旧数据未知
|-- agent_py_agent/tests/test_model_wait_progress.py # 同消息等待展示、停止、超时及思考隐私
|-- docs/design/IM_CLOUD_SCHEDULE_LATENCY_REVIEW.md # 本轮 14 项定位及修复验收
|-- docs/tasks/IM_SCHEDULER_REPAIR_HANDOFF.md # 调度修复交接
|-- docs/tasks/IM_CLOUD_REPAIR_HANDOFF.md # 云授权修复交接
`-- docs/tasks/IM_LATENCY_REPAIR_HANDOFF.md # 耗时与按任务规模收敛交接
```

关键职责：store_snapshot 只读取原 scheduler store 的完整代次；recovery 只结算原 claim/run/history；creator_migration 不借当前群发言者猜旧身份；云授权只使用当前准确主体的供应商权限证据；model_call_latency 不建立第二份用量账。im_wait_activity 只在实际模型调用边界观察等待，stream_liveness 仍写原 chunk，不创建第二个消息或执行状态源。实际验收与发行状态见本轮核查报告。

2026-10-13 新权威入口与验证（凭据/owner/运行账均复用原模块）：

```text
|-- agent_py_agent/agent/user_space/resource_grants.py # 原 tool_policy 的准确外部资源许可
|-- agent_py_agent/agent/tooling/feishu_cloud_tool.py # 原注册表的受管云文档工具
|-- agent_py_agent/agent/adapter/feishu_cloud.py # 固定官方 API、原租户 token 与秘密净化
|-- agent_py_agent/agent/gateway_parts/request_owner_binding.py # 原入队前冻结 owner
|-- agent_py_agent/agent/agent_core/runner/slice_closeout.py # 退出等待片结清原 exact attempt
|-- agent_py_agent/agent/gateway_parts/session_control.py # 绑定管理员管理本人原会话与审批
|-- agent_py_agent/tests/test_feishu_cloud_tool.py # 三类 owner、授权与实际合成 HTTP
|-- agent_py_agent/tests/test_channel_owner_isolation.py # 原群发送与宿主队列隐私
|-- agent_py_agent/tests/test_runner_slice_closeout.py # 可恢复等待与未知副作用门
|-- agent_py_agent/tests/test_session_control.py # 跨入口审批和嵌套秘密不落盘
|-- agent_py_agent/tests/test_background_im_progress.py # 同条短窗口与终态
|-- agent_py_agent/tests/test_im_subagent_permission_matrix.py # 三类身份主子孙真实文件/网络
`-- docs/design/HOST_CLOUD_RESOURCE_ACCESS.md # 云资源与通道隔离边界
```

关键文件说明：resource_grants 只管理原用户工具策略的精确许可；feishu_cloud 只运输原宿主凭据请求；request_owner_binding 只冻结原请求身份；slice_closeout 只沿原数据库结束真实退出的工作片，不新建任务或恢复账。

2026-10-13 新维护入口（沿原模块，无新任务账）：

```text
|-- agent_py_agent/agent/gateway_parts/schedule_control.py # 本人监控查询与原 CAS 控制
|-- agent_py_agent/agent/scheduler/rules.py # 多规则定义、JSON 条件和 DAG 校验
|-- agent_py_agent/agent/scheduler/monitor_state.py # 原 job/rule/run 的纯状态变换
|-- agent_py_agent/agent/scheduler/tool_work.py # NO-LLM 原执行器与内部/外部交付
|-- agent_py_agent/agent/tooling/task_credential_tool.py # 私人卡请求工具，只返回引用
|-- agent_py_agent/tests/test_scheduler_rules.py # 多规则合同校验
|-- agent_py_agent/tests/test_scheduler_monitors.py # 规则并发和真实执行器回归
|-- agent_py_agent/tests/test_schedule_control.py # 群创建人、本人跨入口与通知控制
|-- agent_py_agent/tests/test_task_credentials.py # 原卡、锁、密文和注入回归
|-- agent_py_agent/tests/test_task_credential_lifetime.py # 持续保存、有限期限与原scope撤销边界
|-- agent_py_agent/tests/test_task_credential_http_failures.py # 登录/请求失败分类与认证拒绝单次转换
|-- agent_py_agent/tests/test_inbound_media_ownership.py # 两用户附件落地、读取及交叉拒绝
```

关键职责：附件沿原 owner resolver 和 media/input；监控沿原仓库/索引/执行器；凭据沿原私人交互和网络工具。实施/验收状态见 docs/tasks/IM_MONITOR_PERMISSIONS_IMPLEMENTATION.md。

权限评估入口：[OWNER_HOME_PERMISSION_REVIEW.md](docs/design/OWNER_HOME_PERMISSION_REVIEW.md) 记录主子家目录、父 full-access、文件/Shell 裁决差异与审批换代边界；仅评估，未修改权限实现。

本轮验证新增入口：

```text
|-- agent_py_agent/tests/test_feishu_first_contact_lock.py  # 连续未设密码入站、设置回调和锁定告警合同
`-- docs/design/IM_CREDENTIAL_COLLECTION.md  # 群任务私聊凭据方案，尚未实现的独立功能
```

关键职责：首设密码门沿原 session_lock 与 adapter 入站共用；API 凭据设计不代表当前包已有通用秘密收集工具。

本轮整合新入口：`agent_py_agent/tests/test_scheduler_im_l_integration.py` 验证 IM/调度策略共用；`docs/tasks/L_T_IM_INTEGRATION_HANDOFF.md` 保存来源、冲突处理、证据和未发布边界。生产新模块沿 L 源分支的现有文件树条目，不再重复列第二套入口。

IM 多任务和公共包的新长期入口及关键文件职责（均复用原 Store，不增加第二套任务账）：

```text
|-- agent_py_agent/agent/conversation/session_task_dispatch.py  # 模型工具与宿主共用的耐久派发、幂等、链深和配额
|-- agent_py_agent/agent/conversation/im_task_status.py  # IM 当日设置和后台/孩子状态的同源只读投影
|-- agent_py_agent/agent/conversation/execution_threads.py  # 独立执行线程和准确来源引用
|-- agent_py_agent/agent/conversation/host_actor.py  # 原入口、主根和子孙共用的宿主发起人校验
|-- agent_py_agent/agent/conversation/background_receipt_contract.py  # 后台公开回执策略和原交付信封接线
|-- agent_py_agent/agent/contracts/background_public_receipt.py  # 显式公开完成协议的共享标识
|-- agent_py_agent/agent/agent_core/model/response_provenance.py  # 原模型调用出口签发与核对响应来源
|-- agent_py_agent/agent/agent_core/tool_loop/background_public_receipt.py  # 显式后台完成协议、内部结果与公开内容分离
|-- agent_py_agent/agent/conversation/im_delegation.py  # 宿主真实派工、等待和主模型汇总
|-- agent_py_agent/agent/conversation/im_delegation_delivery.py # 原claim/runner/owner与主sender单发话只读判据
|-- agent_py_agent/agent/scheduler/execution_policy.py  # 新 IM 计划隔离线程策略
|-- agent_py_agent/agent/settings/im_task_preferences.py  # 每发送者当日偏好及到期解析
|-- agent_py_agent/agent/gateway_parts/im_dispatch_policy.py  # 入站回执冻结执行方式
|-- agent_py_agent/agent/gateway_parts/im_task_routing.py  # 辅助模型结构路由和原任务派发
|-- agent_py_agent/agent/gateway_parts/delegation_control.py  # 当日委派显式命令
|-- agent_py_agent/tests/test_im_delegation_ingress.py  # 真实 HTTP 排队、重送和关闭快照
|-- agent_py_agent/tests/test_im_delegation_runtime.py  # 原创建服务、G1、恢复及收口组合
|-- agent_py_agent/tests/test_im_delegation_single_speaker.py # 完整离线worker/child/主sender、原W回报与终态重放
|-- agent_py_agent/tests/test_im_delegation_preferences.py  # 零点、DST、重启和发送者隔离
|-- agent_py_agent/tests/test_im_execution_threads.py  # 调度、线程和投递来源隔离
|-- agent_py_agent/tests/test_public_capability_packages.py  # 公共发现、授权、升级与撤销
`-- docs/tasks/IM_MULTI_TASK_PUBLIC_PACK_HANDOFF.md  # 本轮证据与交接
```

临时更新关键文件：`agent/gateway_parts/status_projection.py` 只读 TUI 同代上下文、角色与权限；`cli/chat_parts/tui_application.py` 接管单调重绘计时；`adapter/progress_draft.py` 沿原 pending 保存编辑消息身份。新回归 `tests/test_tui_im_preview.py` 与 `tests/test_im_progress_draft.py` 验证时间跳变、管理员绑定、群聊 mention、退出清理与进度恢复。方案及后续 13 项需求总表见 [TUI/IM 临时更新](docs/design/TUI_IM_PREVIEW.md)。

## AGENTS 第 6 轮（mc2-agentsr6，2026-10-09）

```text
|-- agent_py_agent/agent/memory_store/agents_migration_reconcile.py # completed 旧精确租约收尾；state→record、原记录历史
|-- agent_py_agent/agent/memory_store/curator_lease.py # stop/join 保护原异常与停止未确认
|-- agent_py_agent/agent/memory_store/curator.py # 所有失败收尾拒绝清未确认停止的租约
|-- agent_py_agent/tests/test_agents_cleanup_review33b.py # mc4 原九项公开入口/崩溃/收尾
|-- agent_py_agent/tests/test_agents_cleanup_review33b_locks.py # 两个独立校准的完整 quota 锁探针
|-- agent_py_agent/tests/test_agents_cleanup_reconcile.py # 精确身份、历史、I/O恢复、真实死进程与业务不重做
|-- agent_py_agent/tests/test_curator_heartbeat_stop.py # 真实存活心跳线程的 stop/join 中断
```

- AGENTS 快照字节合同：`agent/memory_store/agents_migration.py` 的复制入口复用 `agent/common/nofollow_fs.py` 私有原子字节写；`tests/test_agents_snapshot_bytes.py` 验证公开迁移、续作、清单兼容和篡改拒绝。路径均相对 `agent_py_agent/`，不新增恢复执行权。

## AGENTS 第 5 轮（mc2-agentsr5，2026-10-09）

```text
|-- agent_py_agent/agent/memory_store/promotion.py # quota→policy→目标；最终目标同源解析
|-- agent_py_agent/agent/memory_store/agents_migration.py # 原 record 收尾 pending、精确退役与结构化诊断
|-- agent_py_agent/tests/test_agents_cleanup_review33.py # 真 flock、收尾组合、重放/二次中断合同
```

## AGENTS 第 4 轮（mc2-agents0r4，2026-10-09）

```text
|-- agent_py_agent/agent/memory_store/agents_migration.py # quota 前置；policy→AGENTS 全程外锁
|-- agent_py_agent/agent/memory_store/curator_lease.py # 精确 ID 心跳与晋升后独立释放
|-- agent_py_agent/tests/test_agents_cleanup_critical_section.py # 一次删除/全程持锁/续作/晋升租约合同
```

## 子代理自身工作目录写边界（w3，2026-10-09）

```text
|-- agent_py_agent/agent/agent_core/tool_runtime_ledger.py # 合并 owner 范围时保留宿主绑定且已授权的自身窄根
`-- agent_py_agent/tests/test_child_own_workspace_write.py # 真实文件写链的自身目录放行、其它受保护位置拒绝及两处变异靶点
```

关键文件说明：`test_child_own_workspace_write.py` 通过真实 runner 上下文构造、runtime 边界合并和 ToolExecutor→WriteFileTool 写入/读回核对自身草稿授权；不替代真实生产子代理或 OS 沙箱验收。详细边界见子代理模块 04-structure。

## 空回复诊断（wsempty4，2026-10-09）

```text
|-- agent_py_agent/agent/backends/reply_diagnostics.py # 原始Responses无正文形状与诊断白名单，不参与决策
|-- agent_py_agent/agent/agent_core/tool_loop/reply_diagnostic.py # 仅读取循环计数及权威上限，随最终结果带出
|-- agent_py_agent/tests/test_user_reply_unavailable_diagnostic.py # 工具循环、传输投影与Gateway失败落账回归
```

## 工具索引统一流式预筛（toolrefs / E11e，2026-10-08）

```text
|-- agent_py_agent/agent/common/tool_index_stream.py # 唯一权威逐行严格UTF-8/换行与安全字节预筛
|-- agent_py_agent/agent/memory_archive/control_plane.py # E11a与通用控制面调用共享流式读取器
|-- agent_py_agent/agent/memory_archive/compact_tool_output_refs.py # 原scope同源匹配，两类引用单扫描，投影不变
|-- agent_py_agent/agent/memory_archive/artifact/reader.py # C逐行解析后保留ref/path候选，规范POSIX路径快路，最终权限/正文验证不变
|-- agent_py_agent/agent/memory_store/promotion.py # 原证据回退条件与整文件失效，不导入archive层
|-- agent_py_agent/tests/helper_tool_index_baseline.py # 冻结9e843077d整读内存oracle，保留E11a原断言
|-- agent_py_agent/tests/helper_tool_ref_isolation.py # 每个测量参数在全新pytest子进程执行原体，隔离前序intern表/trace污染
|-- agent_py_agent/tests/test_tool_ref_isolation.py # 子用例三阶段成功回执、skip/xfail/未执行拒绝和子环境边界守卫
|-- agent_py_agent/tests/test_tool_index_validation.py # C解析语法对照、资源限制、分块UTF-8与换行/异常顺序
|-- agent_py_agent/tests/test_tool_ref_hot_paths.py # artifact/晋升/控制面旧新等价与错误合同
|-- agent_py_agent/tests/test_tool_ref_hot_measurement.py # 高频真实入口合成索引计量
|-- agent_py_agent/tests/test_tool_ref_scope_stream.py # 冻结旧口径、多根多形状等价与解析/枚举/打开计数
|-- agent_py_agent/tests/test_tool_ref_timing_measurement.py # 同8入口分开测峰值与无探针三次耗时，供真基线复跑
`-- agent_py_agent/tests/test_tool_ref_scope_measurement.py # 1000根同字节合成数据，压缩来源及收尾产物更新真实入口计量
```

关键职责：预筛不是归属判断；只优化索引，不改变正文/权限/schema/归档写入。scope carried同步复用，已知两源的request carried保留原流式及替换解码合同；无持久缓存。

## Curator消息/审计增量定位（mc2-e11c，2026-10-08）

```text
|-- agent_py_agent/agent/io/cursor_cache.py # 有界短锁LRU、dev/ino指纹、保守首ID过滤器；仅元数据
|-- agent_py_agent/agent/conversation/message_cursor.py # after_report/byte_offset_after唯一定位与原尾读合同
|-- agent_py_agent/agent/memory_store/curator_audit_cursor.py # 已校验旧分片/锚行跳过，错误/预算/缺cursor保持
|-- agent_py_agent/tests/support/curator_scan_cases.py # 合成owner与实际文件读取计数
|-- agent_py_agent/tests/support/curator_scan_legacy.py # 725d读取器独立对照，仅测试，不进产品
|-- agent_py_agent/tests/test_curator_cursor_scans.py # 132格等价矩阵与扫描量/推进/重复ID
|-- agent_py_agent/tests/test_curator_cursor_cache.py # 身份失效、隔离/LRU、多线程预算及变异靶点
`-- agent_py_agent/tests/bench_curator_cursor_scans.py # 800线程/100分片CPU与分配计量，自动清理自有合成数据
```

## 线程人格前缀冻结（personafreeze，2026-10-08）

```text
|-- agent_py_agent/agent/prompting_parts/thread_persona.py # 唯一渲染快照、已提交epoch、按行有界增量
`-- agent_py_agent/tests/test_thread_persona_freeze.py # 临时owner、实际builder/纯投影/Compact同源合同；不代替真实渠道验收
```

## 首次选包测量（w2，2026-10-07；开发工具，不进入 wheel）

```text
|-- scripts/eval/pack_pick_bench.py          # setup/run、安全隔离 home、原安装启用、题目与输入指纹
|-- scripts/eval/pack_pick_runtime.py        # 原 Gateway 首请求、隔离文件开关与原子字节/权限恢复、预算缓存覆盖、模型层捕获与零工具执行
|-- scripts/eval/pack_pick_results.py        # 最小工具身份、原账本已报 token、分组分语言汇总
|-- agent_py_agent/tests/test_pack_pick_bench.py # 六个自测；假传输校准，不代表自然召回
|-- agent_py_agent/tests/test_package_selection_candidates.py # 选包预算与推荐限数解耦、稳定排序、原测量器 C 组校准
`-- agent_py_agent/tests/fixtures/pack_pick_queries.json # 四句中/英/法小样本（不是正式 39 句）
```

实际用法、接缝与验证边界见 [SKILL_PACK_SELECTION.md 第 5 节](docs/design/SKILL_PACK_SELECTION.md#5-第-3-步第一次挑中三组对比按数据定)。
`test_package_selection_candidates.py` 是 selfix 候选合同和原 C 测量入口的长期回归；它不评自然召回，也不进入 wheel。

## M1 B9（原 M5）插件作者入口

```text
|-- agent_py_agent/skills/builtin/plugins/write-my-agent-plugin/
|   |-- SKILL.md                          # 工具/面板、v8 观察/收紧指引；她自己做的 v6 文件插件可经 package_build/package_install 按开关装，其余仍由用户确认
|   |-- references/
|   |   |-- author-contract.md            # v8 订阅、握手、审批及 B7 前关闭/安全前置合同
|   |   `-- pack-and-verify.md             # 文件构建器、真实 v8 读回和包内 stdio 验证
|   `-- templates/
|       |-- python/
|       |   |-- declaration.json           # v8 entry/files、合法观察/精确 full 收紧、默认断网
|       |   `-- src/server.py              # 标准库 MCP：count_text、观察空回执、字面命令收紧
|       `-- node/
|           |-- declaration.json           # 与 Python 相同的 v8 订阅及网络权限合同
|           `-- src/server.js              # 无 npm 依赖，同样的三个方法及双能力握手
|-- agent_py_agent/skills/builtin/plugins/learn-external-agent/
|   |-- SKILL.md                          # learnpack 学外部 agent：学习笔记、核许可证、读原版、查同领域、默认做能力包、打包、按开关装或给确认行
|   |-- references/
|   |   |-- tell-the-user.md              # 照 package_install 回执原文提醒用户（确认行、管理命令、开关命令、退回两段式）
|   |   |-- pack-layout.md                # 能力包目录与声明怎么写（版本、文字、来源许可证的规则）
|   |   `-- merge.md                      # 同领域合并成新版本、并错了怎么退回
|   `-- templates/
|       |-- CAPABILITY.md                 # 能力包入口文档模板（适合、不适合、怎么用、方法目录）
|       `-- PROVENANCE.md                 # 来源、提交号、许可证、改动记录模板
|-- agent_py_agent/tests/test_write_my_agent_plugin_skill.py # 真包/stdio、纯订阅、模型边界及旧新字节回归
|-- agent_py_agent/tests/test_learn_external_agent_skill.py # learnpack 学习技能：名片短、要点齐、参考链接在、要的工具管理员都有
|-- scripts/build_plugin_files_package.py  # B1 同源 v8 文件包构建，不执行入口
`-- scripts/build_plugin_package.py        # 原受信 wheel 构建路径，保持 v1–v5 回归
```

关键职责：作者入口只交付代码、包和证据，不提供宿主管理通道；
模板、参考随内置 Skill 镜像/发布，详细合同见 `docs/design/PLUGIN_PACKAGES.md` 的 M5 节及
`docs/design/PLUGIN_EVENT_HOOKS.md`；B9 独立 stdio 不证明 B7 宿主隔离或生产启用。

## 发布与开发入口

```text
|-- scripts/private_release/             # 临时离线发行构建、校验、原子更新和本地模型配置辅助
|   |-- build.py                         # 同源展示快照、业务字节码、逐文件清单和自解压载荷
|   |-- installer.py                     # 显式安装路径、数据保留、活动进程检查和原子版本切换
|   |-- configure.py                     # 不回显密钥的 OpenAI 兼容模型配置
|   |-- prepare_runtime.sh               # 构建期从原 wheel 安装自包含 headless 依赖
|   `-- prepare_bwrap.sh                 # Ubuntu 20.04 重建同源 bubblewrap 与离线运行库
|-- agent_py_agent/tests/test_private_release.py # 离线安装/更新、坏包、数据保留、UTF-8 和展示快照回归
|-- docs/guides/PRIVATE_OFFLINE_RELEASE.md # 无桌面 Ubuntu 内网安装、固定数据位置和后续更新说明
|-- agent_py_agent/agent/gateway_parts/event_points.py # B4 Gateway 提示/回合/控制回执的最小事件投影装配；关闭零安装读取
|-- agent_py_agent/tests/test_plugin_event_gateway.py # B4 真实队列/控制回执/Gateway 请求观察与发布故障隔离
|-- agent_py_agent/tests/test_plugin_event_runtime.py # B4 生产 Registry/宿主自动执行与真实子代理父链的三身份观察
|-- agent_py_agent/tests/test_plugin_event_e2e.py # B4 假模型、真 Gateway/Agent/工具链与握手假插件六事件；拒绝组合待核对
|-- agent_py_agent/tests/test_plugin_event_points.py # B4 事件点投影：字段白名单与默认不带正文、提示脱敏与截断、只授权信封才带 content、actors、关闭不投影不发布、发布异常不外泄、真实 handler 前后成对
|-- agent_py_agent/tests/test_plugin_event_display.py # B6 事件账本与展示合同：plugin_gate.decided 只读查询、观察计数、/plugins info 四段、TUI/HTTP/IM 同文渲染与跨 owner 隔离
|-- LICENSE                              # 主项目许可证
|-- NOTICE                               # 适用的第三方版权与许可说明，随包发布
|-- STATUS.md                            # 当前能力、开放问题与证据边界
|-- DESIGN_LEDGER.md                     # 当前架构决策及模块设计导航
|-- TESTS.md                             # 开发测试、真实 TUI 与发布 gate
|-- agent_py_agent/agent/attempt/process_run.py # 沙箱同步命令通用取消/超时：临时回调、TERM→宽限→KILL 整组回收及管道释放
|-- agent_py_agent/agent/attempt/landlock_launcher.py # G5：Linux Landlock 端口拒绝启动器（exec bwrap 前按端口拒绝连本机 Gateway；自包含、fail-closed）
|-- agent_py_agent/tests/test_pack_verification_cancellation.py # 块 6a：预取消零启动、真实父子组回收、取消入账和不返工，保留平台外复跑
|-- agent_py_agent/tests/test_gateway_client_credentials.py # G3：随机端口假服务验证三类客户端、插件流、故障拒绝、单token与不泄漏
|-- agent_py_agent/tests/test_gateway_local_trust_enforcement.py # G2b：强制档回环无凭据匿名、启动 fail-closed、插件令牌豁免、未知对端不可信、TUI 预检与五项变异
|-- agent_py_agent/tests/test_plugin_tool_gate_logic.py # B5 第一段七组合、严格回复、安全参数投影与稳定最严合并；未替代执行链验收
|-- agent_py_agent/tests/test_plugin_tool_gate_execution.py # B5 执行器/共用池、预算和引用到Gateway审批隔离联测；不启用真实插件
|-- agent_py_agent/tests/test_plugin_gate_approval.py # B5 消费端四审批入口与自主提供者反绕过；隔离标记请求，不代替完整执行链
|-- agent_py_agent/tests/test_plugin_gate_consumers.py # B5真实隔离主/子消费者与I4恢复重问，旧子审批不可跨attempt；不启停真实Gateway
|-- agent_py_agent/tests/test_plugin_gate_cross_bfail.py # B5×B7跨件：冷启动撞预算/沙箱起不来/进程被杀都收紧成ask；只替插件进程
|-- agent_py_agent/tests/test_plugin_gate_reapproval.py # B5六身份精确批准重跑、主子实际批准/再问/拒绝及鲜活换代，不读真实owner
|-- agent_py_agent/tests/test_plugin_gate_event_combination.py # 17j B5/B4/B8组合：拒绝零handler/零工具事件，原归档/唯一写账→真实临时库→B6 info；三项转正
|-- agent_py_agent/tests/test_plugin_m1_joint_e2e.py # jb6：真实样例stdio、B3/B2与只读展示，队列手工推进，不代宿主启用/B7
|-- agent_py_agent/tests/test_plugin_m1_joint_tool_gate.py # jb6/jb6b：目标插件非空观察、实际B5拒绝链；ask 批准/不可交互/消费者答 unavailable 三出口账本与最近决定排序
|-- agent_py_agent/tests/test_plugin_tool_gate_decision_ledger.py # B5决定字段/全部review/批准排除/单门多门与canonical信封组件反证
|-- agent_py_agent/tests/fixtures/plugin_tool_gate_mutation_probe.py # B5 第一段五个真实内存变异，保留 pytest 退出码，不写产品源码
|-- agent_py_agent/agent/capability_package_manifest.py # 独立能力内容声明与资源路径校验
|-- agent_py_agent/agent/capability_verification_manifest.py # 能力包 v2 可选核验声明：交付物识别、钉住的检查程序、输入策略
|-- agent_py_agent/agent/capability_verifier_consent.py # 检查程序启用前确认、确认码与写入内容激活的同意摘要
|-- agent_py_agent/agent/capability/pack_verifier_runner.py # 宿主在唯一沙箱入口里跑钉住的包内检查程序（断网、整根只读），返回结构化检查事实
|-- agent_py_agent/agent/capability/pack_verifier_redaction.py # 检查程序 location 转给模型前脱敏宿主路径（目标/输入换相对路径，临时目录、解释器换占位）
|-- agent_py_agent/agent/capability/pack_verification_service.py # 宿主核验主流程：记基线、写完就查、收尾再查、输入解析、返工 1 次
|-- agent_py_agent/agent/capability/pack_verification_hooks.py # 宿主核验接进工具执行缝隙和收尾通道的钩子（不新增模型工具）
|-- agent_py_agent/agent/capability/pack_verification_scope.py # 宿主核验生效范围：开关、owner、钉住的带检查程序的包、基线模式
|-- agent_py_agent/agent/capability/self_install_switches.py # learnpack 两个“自动装”开关（能力包、插件）的唯一读取入口：按文件现读、读不到按关，给回执用的开关事实与命令原文
|-- agent_py_agent/agent/capability/method_carry.py # 主会话成功 get 的隐藏登记、逐 run 冻结目录和压缩后入口/资料清单带回；不存第二份正文、不扩权
|-- agent_py_agent/tests/test_conversation_method_carry.py # 原线程锁内成功读取登记、主会话边界、容量、隐藏字段和立即生效开关
|-- agent_py_agent/tests/test_conversation_method_directory.py # 首次使用显示顺序、required 必显、逐 run 冻结及关闭/失效的旧提示字节
|-- agent_py_agent/tests/test_conversation_method_restore.py # 压缩代次、完整投递预算、当前版本资料参数、取消/失败不消费资格
|-- agent_py_agent/tests/test_conversation_method_reference.py # 引用底线预留、完整get参数与资料过滤、中断原样传播、成功消费代次
|-- agent_py_agent/tests/test_conversation_method_native_compact.py # 真实checkpoint/CAS后补带和下一build预算回收，完整工具对与同代去重
|-- agent_py_agent/tests/test_conversation_method_prefix.py # 真实Chat序列化正文/引用startup前缀延伸及模拟命中量开关对照
|-- agent_py_agent/tests/test_conversation_method_commands.py # using/remove 的认证线程范围、TUI 文本往返和 Gateway 共用入口
|-- agent_py_agent/tests/test_conversation_method_gateway.py # 三轮真实 get 与原 Compact 检查点提交、第二轮带回及同代不重复（仅模型传输替身）
|-- agent_py_agent/agent/capability/package_build.py # 能力包（v7）与文件型插件包（v6/v8）唯一的打包实现：清单、摘要、成员顺序与权限位、复验；两个打包脚本只是薄壳
|-- agent_py_agent/agent/capability/learnpack_store.py # learnpack 宿主存储 <owner home>/data/learnpack/：她打的包（内容寻址、读回验摘要）、待确认安装单（只执行一次）、安装记录、她装过的包名
|-- agent_py_agent/agent/capability/learnpack_build_notes.py # learnpack 打包回执的版本事实与软提醒：现在装着哪一版、她做过哪些版本，同号重用、版本倒退、比上一版少了文件（不挡打包）
|-- agent_py_agent/agent/capability/learnpack_notices.py # learnpack 给用户的宿主提示：她自己装上包时说装了什么、怎么删、怎么关自动装；她开安装单时列出现在全部还有效的确认行（以这条为准）
|-- agent_py_agent/agent/capability/learnpack_domain.py # learnpack“同领域”与“她做的”的唯一判断：打包提醒、安装与确认回执里给用户的提醒、/plugins# 列表的同领域行、skill_search 的 made_by_me 都调它
|-- agent_py_agent/agent/capability/learnpack_installer.py # learnpack 安装驱动：把“装她打的包”变成管理员同一串 /plugins 命令（安装/停用+更新→启用→必要时代确认运行她自己的程序），联网与读写目录授权永不代给
|-- agent_py_agent/agent/capability/pack_commands.py # 能力包命令 /plugins#[包名] [查看|启用|停用|删除|退回|安装 <单号>] 的唯一处理处（TUI、IM 同一入口），列表标出她做的
|-- agent_py_agent/agent/capability/learnpack_service.py # learnpack 三条安装入口：开关开着自动装（专用 learnpack 线程）、用户发 /plugins confirm 执行待确认安装单（只执行一次）、/plugins revert 退回她做的上一版；包名归属检查
|-- agent_py_agent/agent/capability/pack_verification_matching.py # 按“路径模式 + 字段匹配”认文件，及有界工作区快照
|-- agent_py_agent/agent/capability/pack_verification_ledger.py # 每 run 一本的宿主核验账本（基线、结果、收尾、返工，权限 600）
|-- agent_py_agent/agent/capability/pack_verification_report.py # 从核验账本生成最终交付事实和宿主提示文字
|-- agent_py_agent/agent/capability/pack_verification_originals.py # 任务输入原件清单与副本（规范任务根 data/pack_verification，一个任务只记一次）
|-- agent_py_agent/agent/capability/pack_verification_inputs.py # 输入保护：task_input 候选、就地修改判定与返工提示
|-- agent_py_agent/agent/capability/pack_verification_deliverables.py # 交付存在：本回合改过工作区时查必需交付物缺失或打不开，返工提示
|-- agent_py_agent/agent/plugin_content_activation.py # 内容包无进程激活身份
|-- agent_py_agent/agent/plugin_permissions/ # 老格式 R/W/N/E 静态授权、路径身份、原激活记录与一次性兼容、同源文字、B7 构造器接缝（WIP，非 OS 规则）
|   |-- confirmation.py # 完整四维确认/授权身份/确认命令、拒启动原因的同源文字
|   |-- enable.py # 单一模式来源、原确认身份、撤旧代预测/回执守卫与执行边界权限漂移复验
|   |-- rollback.py # 严格 v4→v3 独立导出及授权损失报告，不覆盖原表
|-- scripts/export_plugin_installations_v3.py # 新运行时回滚两步预览/确认，排他创建0600独立v3文件
|-- agent_py_agent/tests/test_plugin_legacy_permissions.py # 授权形状/有限根/确认变化/开关不降级/B7 接缝合同
|-- agent_py_agent/tests/test_plugin_legacy_config.py # 默认与参数边界、认证 settings 作用域
|-- agent_py_agent/tests/test_plugin_legacy_state.py # 显式 v3→v4 一次性兼容、无缺字段旁路、撤销结束豁免
|-- agent_py_agent/tests/test_plugin_legacy_records.py # 新授权随原激活摘要持久化、同代不可变、同源文字和普通身份不泄路径
|-- agent_py_agent/tests/test_plugin_legacy_management.py # 原管理/HostCommand/执行器、完整参数/确认、撤旧未知与YAML真消费（外部进程替身）
|-- agent_py_agent/tests/test_plugin_legacy_transitions.py # 兼容/restricted/wide 更新重装与重新授权矩阵，旧授权/旧码不复活
|-- agent_py_agent/tests/test_plugin_legacy_rollback.py # 固定17i原读取函数、v4 fail-closed、导出保安装/启用、CLI损失/确认/拒覆盖
|-- agent_py_agent/tests/test_plugin_legacy_drift.py # 真实权限维度/根inode/程序内容/身份漂移、授权绑定与撤旧提交事实（外部进程替身）
|-- agent_py_agent/tests/test_plugin_legacy_launch_guard.py # 四启动入口的固定授权事实复核（构造/start/reconnect）、restricted 经 B7 包装与 wide 显式模式（替身就绪）
|-- agent_py_agent/tests/plugin_enable_fixtures.py # 真实启用回归的完整预览/授权命令与显式测试wide模式，不替换准备或MCP
|-- agent_py_agent/tests/test_plugin_legacy_candidate.py # 候选异常/退出未知、各候选边界漂移、并发配置与解释器替换拒发布
|-- agent_py_agent/tests/test_plugin_legacy_transport.py # 原客户端/IM/HTTP handler长完整预览、回填确认/重送与UNKNOWN有限观察；非真实网络
|-- agent_py_agent/agent/plugin_content_lifecycle.py # 原安装库中的内容激活迁移
|-- agent_py_agent/agent/capability/package_snapshot.py # 包级快照及绑定内容与激活代次的读取参数
|-- agent_py_agent/agent/capability/package_provider.py # 从原安装表发现并复核内容包
|-- agent_py_agent/agent/capability/task_references.py # 主子任务与调度的准确包版本引用
|-- agent_py_agent/agent/capability/package_resources.py # 原写文件工具的私有资源引用解析
|-- agent_py_agent/agent/capability/package_read.py # 原工具与宿主共用的准确正文分页和任务pin
|-- agent_py_agent/agent/capability/package_selection.py # 有界包名卡及一次结构化选择
|-- agent_py_agent/agent/capability/package_selection_failure.py # 选择失败的无正文结构化原因（回执 failure 与宿主日志）
|-- agent_py_agent/agent/capability/package_selection_scope.py # 新主任务的元数据资格与pending初始化
|-- agent_py_agent/agent/capability/package_selection_authority.py # 原只读准入与精确执行权复核
|-- agent_py_agent/agent/capability/package_selection_context.py # 已读入口的总预算和准确续页
|-- agent_py_agent/agent/capability/package_selection_runtime.py # 首个主业务请求前的一次领取和装配
|-- agent_py_agent/tests/test_wire_contract.py # 出站协议合同：逐形状修整与校验、三个出口集成、修整被改坏时本地拦截、随机历史性质测试
|-- agent_py_agent/tests/test_backends_provider_history_reload_identical.py # 磁盘重载 vs 同进程的 provider payload 逐字一致：工具参数稳定键序、reasoning_content 保留、Responses 路径同口径（DeepSeek 前缀缓存底座）
|-- agent_py_agent/tests/test_backend_signature_guardrails.py # 后端传输签名守卫：类关系发现四个传输方法的覆盖签名一致性 + ast 调用点关键字扫描，含比较器自检
|-- agent_py_agent/tests/test_responses_empty_recovery.py # 纯空工具响应失败分类；fake WS重连、真实工具与恢复载荷、commentary原投影
|-- agent_py_agent/tests/test_responses_cache_key.py # Responses 请求体会话级缓存键：绑定会话带 prompt_cache_key、未绑定不带、同线程稳定、跨线程不同、不含凭据；订阅登录另带同值 session-id 头
|-- agent_py_agent/tests/test_provider_retry_final_sink.py # 修法 B 收口提示走结构化通道：typed final 事件、异常/旧签名回退、TUI 与 IM 同一句已登记文案
|-- agent_py_agent/agent/retrieval/embedding_usage.py # 嵌入用量与召回方式的进程内计数（按用途：请求、条数、失败、供应商回报的 token；semantic/keyword/none），只给 /model vector 展示
|-- agent_py_agent/tests/test_embedding_usage.py # S7：各用途计数、未回报 token、失败、真实记忆接线、召回方式、/model vector 的 TUI/IM 同一份且不含正文
|-- agent_py_agent/tests/test_embedding_selection.py # 向量模型选择：同主机直设、异主机不写、共享/普通 owner 拒绝、关闭还原、TUI/IM/Gateway 入口与工具回执
|-- agent_py_agent/tests/test_semantic_memory_storage.py # 向量文件 600/目录 700（替换后仍是）、第一次召回复用 memory_vectors.json 不重嵌
|-- agent_py_agent/tests/test_memory_file_permissions.py # 候选、日事件、lesson/INDEX/HOT、Curator 事务目标与前镜像替换后 600/目录 700（S2）；迁移备份私有复制
|-- agent_py_agent/tests/test_memory_archive_permissions.py # memory_archive 写入 600/目录 700、已有文件收紧（不跟随符号链接）、失败计数与 owner 维护回执
|-- agent_py_agent/tests/test_private_lock_permissions.py # 三套锁写法统一私有：json/jsonl/派工锁 600、父目录 700、存量 644 自愈、符号链接被拒、blocking=False 不变
|-- agent_py_agent/tests/test_global_index_permissions.py # global_index 数据文件与目录私有：新建 600/700、预置 644 下次追加收紧且内容逐字节不变
|-- agent_py_agent/tests/test_private_dirs_policy.py # pdp：_ensure_private_dir 缺失按 0700 建、已存在目录（含符号链接）一位不动、可配置路径指向用户目录时只新增 0600 文件、建目录点 umask 022 下仍 0700
|-- agent_py_agent/tests/test_private_writes_conversation.py # 会话/用量宿主数据私有写入：0600/0700、存量收紧、内容逐字节不变
|-- agent_py_agent/tests/test_private_writes_audit_events.py # 审计/事件/Gateway 历史私有写入与第四处锁写法：0600/700、存量锁自愈
|-- agent_py_agent/tests/test_private_writes_collaboration.py # 协作账与 run workspace 私有写入：0600/0700、存量收紧、内容不变
|-- agent_py_agent/tests/test_private_writes_subagents.py # 子代理账与工作日志私有写入：0600/0700、存量收紧、内容不变
|-- agent_py_agent/tests/test_private_writes_memory.py # retention 审计私有写入：0600/0700、存量收紧、内容不变
|-- agent_py_agent/tests/test_private_writes_batch2.py # 第二批私有写入（pw2）：append 家族、gateway_parts/io 写函数、subagents 工作区文件
|-- agent_py_agent/tests/test_capability_selection_state.py # 原TaskLink严格标记、损坏隔离和CAS
|-- agent_py_agent/tests/test_capability_selection_scope.py # 默认关闭、主子权限和配置资格
|-- agent_py_agent/tests/test_capability_config_missing_defaults.py # 缺配置文件给默认实例且不缓存、坏文件仍 None、决策默认值读 capability 文件
|-- agent_py_agent/tests/test_capability_config_single_default_source.py # 各调用点缺文件/坏文件都取 dataclass 默认、文件值生效
|-- agent_py_agent/tests/test_session_task_real_chain.py # 会话互通真实链路门禁：真实 Gateway ask + 后台调度器 + 只替换供应商传输；含 list_owner_sessions 列会话后按清单派活/发消息
|-- agent_py_agent/tests/test_capability_package_selection.py # 结构化选择、原调用账与响应隔离
|-- agent_py_agent/tests/test_package_selection_failure.py # 选择失败原因落回执且不含正文、选择 schema 只用严格模式关键字
|-- agent_py_agent/tests/test_capability_package_read.py # 原页回执保持及读取和pin取消边界
|-- agent_py_agent/tests/test_capability_selection_authority.py # 原执行权和只读准入组合验证
|-- agent_py_agent/tests/test_capability_package_entry_context.py # 多入口总预算、分页和失败隔离
|-- agent_py_agent/tests/test_capability_package_selection_runtime.py # 主请求一次准备及停止续跑组合
|-- agent_py_agent/tests/test_capability_package_drama_text_duration.py # A包逐场镜头与来源目标时长对账
|-- agent_py_agent/tests/test_capability_package_drama_text_basis.py # A包v3镜头来源、改编与未知声明及报告版本
|-- agent_py_agent/tests/test_capability_package_drama_text_visibility.py # A包人物字面覆盖、画外声明及有界诊断
|-- agent_py_agent/tests/test_capability_package_drama_text_lines.py # A包0.4.0可选台词、逐字引用、道具状态接续与检查器身份
|-- agent_py_agent/tests/test_capability_package_drama_text_v05.py # A包0.5.0新增7项检查（占位、内嵌引文、改编原文、道具状态/来源、画外点名、过短镜头）与--host-json宿主核验输出
|-- agent_py_agent/tests/test_capability_package_drama_text_source_marks.py # A包0.5.3来源与改编标注三条规则（道具来源标错、未标改编、说明与原文矛盾）与hint
|-- agent_py_agent/tests/test_capability_package_drama_text_hint_guard.py # A包hint通用守卫：无未替换占位符、≤200字、无控制字符，清单从脚本现取
|-- agent_py_agent/tests/test_capability_package_drama_text_review_fixes.py # A包0.5.4：台词逐字必须在段落引号内、镜内道具交接提醒补prop_handoffs
|-- agent_py_agent/tests/test_capability_package_drama_workflow_duration.py # B包分集镜头与明确目标对账
|-- agent_py_agent/tests/test_capability_package_b_template.py # B完整模板、制作交接及原派工授权接口
|-- agent_py_agent/tests/test_capability_package_drama_workflow_handoff.py # B交接绑定、原字节摘要、对象地址及路径别名缓存边界
|-- agent_py_agent/tests/test_capability_package_drama_workflow_baseline.py # B包0.2.0基线逐ID对比、交接覆盖与虚假新增/省略、检查器身份
|-- agent_py_agent/tests/test_capability_package_host_mode_robustness.py # A/B 检查器遇到列表、对象等坏形状时宿主模式仍输出合格 v1、不崩溃
|-- agent_py_agent/tests/test_capability_package_verification_blocks.py # A/B 两包 verification 块经真实构建器往返、启用确认列出输入、按声明参数驱动检查器
|-- agent_py_agent/tests/test_capability_package_drama_workflow_v03.py # B包0.3.0新增检查（缺表/外键、编造参考ID、节拍角色、出镜参考、未列出基线改动、虚假改动声明）与--host-json按摘要对应交接
|-- agent_py_agent/tests/test_subagent_package_entries.py # 子代理同代包入口的首请求、显式选模、权限和恢复组合
|-- agent_py_agent/tests/test_file_syntax_diagnostics.py # 有界语法观察、反馈与诊断异常隔离
|-- agent_py_agent/tests/test_filesystem_syntax_feedback.py # 三文件入口的真实发布、部分提交与取消回归
|-- agent_py_agent/tests/test_file_syntax_diagnostics_runtime.py # 配置到模型可见回执及修复选择的替身验证
|-- scripts/build_capability_package.py   # 不执行代码的可重复内容包构建入口
|-- examples/capability-packages/         # 来源独立的短剧与证据报告样包源码
|   |-- drama-text-a/methods/review-source.md # 原文与下游事实的成对证据审阅
|   |-- drama-text-a/methods/review-continuity.md # 同镜、相邻镜与跨场接续的定向阅读
|   |-- drama-text-a/methods/review-delivery.md # 实际合并产物、检查回执与拟对外陈述核对
|   |-- drama-text-a/methods/visible-characters.md # 从成稿回填可见与画外人物，解释字面诊断边界
|   |-- drama-text-a/templates/review.md # 人与模型使用的审阅反馈形状，不生成机器状态
|   `-- drama-workflow-b/templates/handoff.json # 制作阶段输入与对象转换的待填写业务资料
|-- docs/design/IM_TASK_EXECUTION_POLICY.md # IM 定时后台、当天委派开关与零点失效的待实施合同
|-- docs/design/PUBLIC_CAPABILITY_PACKS.md # 管理员公共发布、普通用户只读消费与原执行权限隔离的评估
|-- docs/design/OWNER_HOME_PERMISSION_REVIEW.md # 家目录授权误拦、Shell 锁保护与子代理审批换代的证据及待实施方案
|-- docs/design/CAPABILITY_PACKS.md       # 能力包协议、生命周期、隔离召回和任务版本合同
|-- docs/design/CAPABILITY_MIGRATION.md   # 从固定来源到候选、安装、验收、迭代和回退的操作流程
|-- docs/design/CAPABILITY_SOURCE_COVERAGE.md # 固定上游入口、脚本、依赖与样包迁移缺口
|-- docs/design/CAPABILITY_UPSTREAM_MIGRATION.md # 上游未迁内容的许可核对、迁移清单与不迁原因（C14）
|-- docs/design/FILE_SYNTAX_DIAGNOSTICS.md # 文件原子发布的有界语法观察及工具反馈边界
|-- docs/design/PERSONA_AGENTS_OWNERSHIP.md # AGENTS.md 只由用户写、指向配置、自动学习另存、来源标记与清理迁移
|-- docs/design/SLEEP_RESUME.md # 睡眠/长暂停后的 claim、副作用不确定态与恢复故障注入设计（待 3a 定）
|-- docs/guides/MODEL_GUIDE.md        # 给用户的模型管理使用说明：新增/切换/默认/effort/删除/共享/派子代理选模
|-- docs/guides/CAPABILITY_PACK_GUIDE.md # 给用户的能力包使用说明：装/启用/停用/更新/回退/卸载/发现使用/派子代理授权/自制包/安全边界
|-- docs/guides/PLUGIN_GUIDE.md       # 给用户的插件与安全开关使用说明：命令清单、/plugins info 四段、样例插件与写插件技能、屏幕观察只看档、G2b 本机凭据强制、锁与私有文件口径
|-- docs/guides/FEISHU_QUICK_CHECK.md # 给用户的飞书 5 分钟手测清单：普通回复、系统命令、后台完成主动送达、Goal 熔断提示、私聊闲置锁
|-- agent_py_agent/tests/test_capability_package_native_pipeline.py # 模型替身沿原生循环读取和精确落盘
|-- agent_py_agent/tests/test_capability_package_prompt_guidance.py # 包采用与资源复用软规则、空选择和旧提示不变
|-- agent_py_agent/tests/test_capability_package_selector_recovery.py # 准确读取参数、错误恢复、受限隔离及换代拒绝
|-- agent_py_agent/tests/test_capability_package_recommendations.py # 当轮候选软展示、配置开关及原生请求边界
|-- agent_py_agent/tests/test_capability_package_resource_scope.py # 包成员地址空间、同代导航与大搜索完整卡片预览
|-- agent_py_agent/tests/test_capability_resource_input_schema.py # 完整来源声明、原执行器缺字段门与同代原字节复制
|-- agent_py_agent/tests/test_capability_package_compact.py # 原快照与安装事实的 Compact 续读及换代拒绝
|-- agent_py_agent/tests/test_capability_package_catalog_scale.py # 包目录预算与大量私有成员不展开
|-- agent_py_agent/tests/test_capability_package_main_scope.py # 失效包隔离、保留原版本与主任务正常收口
|-- agent_py_agent/tests/test_write_file_sources.py # 来源物化复用原权限、审批和操作回执
|-- agent_py_agent/tests/test_prompt_scope_failure.py # 准入失败和嵌套并发的线程上下文恢复
|-- docs/tasks/CAPABILITY_INTERNALIZATION_GOAL.md # 能力内化的唯一 TODO、并行归属和收口要求
|-- docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md # 能力迁移的来源、许可、覆盖矩阵和真实验收计划
|-- docs/tasks/DECISION_MODEL_TAKEOVER_HANDOFF.md # 决策模型完整Goal、暂停状态、实际证据和接手范围
|-- docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md # 决策容量、完整请求投影与 Compact 的源码证据和分片交接
|-- docs/tasks/DECISION_MODEL_CHILD_COMPACT_HANDOFF.md # child完整恢复共享实现、验收与后台后续边界交接
|-- docs/tasks/DECISION_MODEL_BACKGROUND_PREPARATION_HANDOFF.md # 后台一次准备、范围冻结与原Compact材料丢失复现交接
|-- docs/tasks/DECISION_MODEL_CHILD_LIVE_HANDOFF.md # Jev 自动建议到真实异模子代理首轮/工具轮的隔离验收
|-- docs/tasks/DECISION_MODEL_P5B_HANDOFF.md # 来源—正式记忆关系建议的第一片、原权威边界与离线验收
|-- docs/tasks/DECISION_MODEL_EXTERNAL_MATERIAL_ORDER_HANDOFF.md # 已归档网页阅读提示的首片、原来源边界与离线验收
|-- docs/tasks/DECISION_MODEL_MAIN_MODEL_AUDIT.md # 主会话自动选模型的真实工作片顺序、历史兼容与实施边界审计
|-- docs/tasks/DECISION_MODEL_MAIN_MODEL_SELECTION_HANDOFF.md # 主会话选择版本、显式覆盖和旧线程迁移的本地交接
|-- docs/tasks/DECISION_MODEL_MAIN_MODEL_OBSERVE_HANDOFF.md # Gateway 准确工作片后的只观察建议与关闭等价验收
|-- docs/tasks/DECISION_MODEL_MAIN_MODEL_ADOPTION_HANDOFF.md # 主会话实际首请求发送前的模型采用、失败保留与测试交接
|-- docs/tasks/DECISION_MODEL_MAIN_MODEL_LIVE_HANDOFF.md # 隔离真实主会话的超时、缺资料与保留原模型样本
|-- docs/tasks/DECISION_MODEL_SELF_EXPERIMENT_AUDIT.md # 自动对照、授权、输入预算及精确恢复缺口审计
|-- docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md # 仅授权/预算原语、实际实验联网失败关闭与验证边界交接
|-- docs/tasks/DECISION_MODEL_SETTINGS_RESTORE_HANDOFF.md # 原设置事务内 set/unset 恢复与 CAS 验证交接
|-- docs/tasks/DECISION_MODEL_PRE_RECALL_AUDIT.md # 召回前建议的结构化意图、检索副作用与权限边界审计
|-- docs/tasks/DECISION_MODEL_SELF_LEARNING_AUDIT.md # 自学习候选、Skill 提案与确认缺口的源码审计
|-- docs/tasks/DECISION_MODEL_PLANNING_AUDIT.md # 规划 Todo 精确 ID 建议与原派工权限边界审计
|-- docs/tasks/DECISION_MODEL_PLANNING_HANDOFF.md # 原 Todo read 上的精确优先级软建议、设置与失败保留交接
|-- docs/tasks/DECISION_MODEL_DELIVERY_QUALITY_AUDIT.md # 交付复核提示的结构化证据与原收口权威审计
|-- docs/tasks/DECISION_MODEL_P4G_REPORT.md # 决策模型真实验收汇总：延迟分布、各点采用与判断质量、输入 token 与遗漏清单
|-- docs/tasks/DECISION_MODEL_FINAL_HANDOFF.md # 决策模型 P1—P5 最终交接：18 项状态、合入清单、证据位置、缺口与建议下一步
|-- docs/tasks/MY_AGENT_WRAPUP_HANDOFF.md # my-agent 停用后的收尾交接：生产现状与上线链、暂停与恢复预案、已上线批次、会话互通禁令解除、待用户决定、清理计划与真实测试规矩
|-- docs/tasks/DECISION_MODEL_ACTION_CANDIDATE_AUDIT.md # DOM/OCR/工具动作候选的观察来源与原审批边界审计
|-- docs/tasks/DECISION_MODEL_NATURAL_CONFIG_AUDIT.md # 普通中文配置要求到原 user_config 工具及真实读回的证据边界
|-- docs/tasks/DECISION_MODEL_NATURAL_CONFIG_LIVE_HANDOFF.md # 普通 user owner 真实中文设置失败的工具快照、原因与隔离收尾
|-- docs/tasks/DECISION_MODEL_NATURAL_CONFIG_FIX_HANDOFF.md # 普通 owner 配置工具可信线程/CAS 预览修复及真实读写验收
|-- docs/tasks/DECISION_MODEL_CATALOG_GENERATION_HANDOFF.md # 原私有/共享模型目录持久代次及原锁 guard 交接
|-- docs/tasks/TUI_RESOURCE_HANDOFF.md  # 资源性能线的归属、验收、限制和主线整合交接
|-- agent_py_agent/agent/gateway_parts/execution_target.py  # 从原 claim 与 core 身份读取实际执行目标
|-- agent_py_agent/agent/gateway_parts/execution_control.py  # 控制入口共用精确绑定和原锁内复核
|-- agent_py_agent/agent/conversation/claim_execution_binding.py  # 后台 core 身份投影到原 claim 的 CAS 发布
|-- agent_py_agent/tests/test_gateway_execution_target.py  # 等待请求、后台执行与过期控制组合
|-- agent_py_agent/tests/test_tui_input_ordering.py  # FIFO、重复回执及接回失败重试
|-- agent_py_agent/tests/test_background_input_recovery.py  # 异常尝试的预留释放与未知提交隔离
|-- agent_py_agent/tests/test_background_steer_gateway.py  # 真实后台 core 与隔离 HTTP 的连续插话
|-- docs/tasks/TUI_STEER_CONTROL_HANDOFF.md  # 插话与停止修复的独立实施、依赖和验收记录
|-- docs/tasks/TUI_INPUT_LATENCY_HANDOFF.md # 输入延迟、终端对照和临时发行验证边界
|-- docs/tasks/IM_MONITOR_PERMISSIONS_IMPLEMENTATION.md # 十五项实施进度、复用落点与最终发布验收
|-- docs/design/IM_CLOUD_SCHEDULE_LATENCY_REVIEW.md # 云文档授权、监控查询恢复和任务耗时的只读核查与待实施清单
|-- agent_py_agent/tests/test_im_command_access.py # 机器人提及、角色帮助与控制身份组合
|-- agent_py_agent/tests/test_im_tool_liveness.py # 静默长工具、取消及原 Registry/流出口心跳回归
|-- agent_py_agent/tests/test_im_interaction_refs.py # 私聊交互引用的签名、身份和受保护存储回归
|-- agent_py_agent/tests/test_scheduler_delivery_policy.py # IM 巡检内部结果、通知更新与原 run 冻结策略
|-- agent_py_agent/tests/test_im_control_cards.py # 原生卡片运输、真实消息身份与 SDK/webhook 回调归一
|-- agent_py_agent/tests/test_im_tool_approval.py # 无 TUI 主子孙私人审批、解锁、防转发及真实 HTTP 接线
`-- docs/design/
    |-- ADMIN_CHANNEL_IDENTITY.md       # IM 管理员身份：管理员密码、私聊精确绑定为 local/main、聊天内 /approve /deny 审批
    |-- FEISHU_CLI_INTEGRATION_ASSESSMENT.md # 普通用户与群助手 CLI 的官方接口、原生运行证据和待实施接线
    |-- IM_UNATTENDED_EXECUTION_REVIEW.md # 企业 IM 自动执行、停止、无审批等待资源与凭据/CLI 接缝评估
    |-- IM_CAPABILITY_DISCOVERY_AUDIT.md # 助手查询自身能力/权限的角色差异、披露缺口与只读验证
    |-- PARAMETER_CENTER.md             # 参数中心：一个参数一个权威定义、安全等级、my-agent 自助修改与回滚、分阶段迁移
    |-- STORAGE_RETENTION.md            # 产品持久数据盘点：写入点、实测大小、现有保留机制与缺口、通用保留方向（未落地）
    |-- WAKE_POISON_PILL.md             # 唤醒毒丸处理：同因连续失败持久计数、瞬时排除、结案与人工重放（方案已审，分步实现中）
    |-- PROVIDER_WIRE_CONTRACT.md       # 出站协议合同：三个后端出口修整坏历史并按协议校验，MiniMax 实测校准与已知边界
    |-- SKILL_AUTO_SUMMARY.md           # 自学习 S3：完成任务后自动总结 Skill，自动闸门代替人工确认、登记表所有权、账本与回滚
    |-- REASONING_EFFORT.md             # 智能程度：各服务商实测、档位与控制方式、会话/子代理档位来源、/effort 与边界
    |-- HOST_NOTICES.md                 # 宿主提示：现有通道查证、线程待送达字段、提交即已读、飞书/TUI/历史显示与边界
    |-- MAINTAINABILITY_AND_JEV_REVIEW.md # 可维护性评估、渐进重构建议及 Computer Use/Jev 能力边界
    |-- DECISION_MODEL_INTEGRATION.md    # 可选决策模型的短期限、失败隔离、接入点、缓存与并行实施计划
    |-- DECISION_AUDIT_AND_ADMIN_CONTROLS.md # 决策点开关、超时自调上下限、选模型输入精简、统计行、统一审计与管理员管控
    |-- TUI_INPUT_MEDIA.md               # TUI 图片视频输入、owner 原件与发送预算合同
    |-- COMPACT_MEDIA_POLICY.md          # 媒体会话压缩策略：归档引用主链、视觉摘要按结构化能力事实开启
    |-- TOOL_LOOP_DEPENDENCY_SPLIT.md   # 第8步模型响应、工具轮与Compact职责边界及参考核对
    |-- PLUGIN_LIFECYCLE.md              # 可装卸插件、动态命令、版本切换与故障回收的待实施方案
    |-- PLUGIN_PACKAGES.md               # 本地包静态校验与待接线的安装事实、隔离和撤销边界
    |-- PLUGIN_WORKSPACE_CONTEXT.md      # 逐次只读工作区协议、路径裁决与轻量 SDK 构建边界
    |-- PLUGIN_WORKSPACE_WRITE.md        # 逐次写入工作区协议，与内置写工具同一裁决且只可能更严
    |-- PLUGIN_HOST_API.md               # 界面型插件的宿主只读 API：v4 声明、按激活发令牌、主题白名单
    |-- PLUGIN_OBSERVATION_CANDIDATES.md # 插件观察候选结构设计稿：manifest 声明、宿主铸 ID、两层执行前复核与动作候选决策点
    |-- CAPABILITY_PACKS_V2.md           # 能力包 v2：宿主跑钉住的原版检查程序、输入原件保护、交付存在（已确认、实施中）
    |-- LEARN_TO_PACK.md                 # 学外部 agent 做成能力包或插件：两个自动装开关、一行确认、/plugins# 命令（第一期开发中）
    |-- SKILL_PACK_SELECTION.md          # skill 与能力包的挑选：开关现读、会话沿用（压缩后带回）、首次挑中三组对比、"不比原版差"验收（提议中）
    |-- J16_SCREEN_OBSERVATION.md        # J16 屏幕识别设计（已确认、未实施）：自家适配器结构化窗口观察、两层复核、自动执行默认关
    |-- PLUGIN_ANY_LANGUAGE.md           # 任意语言插件（v6）：随包可执行文件/系统解释器、启用前用户确认、解释器固定与跨语言读取检查用例
    |-- PLUGIN_PROCESS_SANDBOX.md        # 插件进程 OS 沙箱试点：开关语义、失败拒绝、已知限制与验证
    |-- PLUGIN_EVENT_HOOKS.md            # M 线第一期设计稿：插件只读事件订阅与只收紧的工具调用钩子、清单 v8、安全底座、拆块
    |-- PLUGIN_EVENT_HOOKS_ACCEPTANCE.md # M 线真实验收手册：17j 联合冒烟、隔离 TUI/飞书矩阵、结构化证据与安全收尾
    |-- WORKSPACE_PEEK.md                # 首个自有只读插件的预览、分页、安全打开与构建边界
    |-- PLUGIN_ACTIVATION.md             # 唯一安装表的激活 CAS、撤销、显式迁移及待接线资源边界
    |-- MANAGED_PROCESS_STDIO.md         # 原 host 字节管道、激活资源归属及旧版本恢复边界
    |-- HOST_COMMAND_EXECUTION.md        # 显式命令复用原运行链的请求身份、重送和结果回读合同
    |-- PLUGIN_SAMPLE_ACCEPTANCE.md      # 10 个自有简易插件的来源、功能范围及真实 TUI 验收计划
    |-- TUI_RESOURCE_LIFETIME.md         # TUI、HTTP 和空闲 owner 的资源寿命与规模验收边界
    |-- TUI_DESIGN.md                    # 终端布局、事件、输入与生命周期规范
    |-- SUBAGENT_PARALLEL_EXECUTION.md   # 父子独立工作、逐项交付与慢任务诊断边界
    `-- TUI_BEHAVIOR_CHECKLIST.md        # 不依赖历史流水的 TUI 验收场景
```

独立资源线交接：`docs/tasks/TUI_RESOURCE_HANDOFF.md`，记录文件归属、容量证据及默认环境切换边界。

十步重构的执行状态和逐批验收入口：`docs/tasks/REFACTOR_PLUGIN_GOAL.md`。
决策模型 P1—P5 独立执行清单：`docs/tasks/DECISION_MODEL_GOAL.md`。
P1-A 本地配置交接：`docs/tasks/DECISION_MODEL_P1A_HANDOFF.md`。
P1-B 本地协议与传输交接：`docs/tasks/DECISION_MODEL_P1B_HANDOFF.md`。
P1-C/D 有界调用组件交接：`docs/tasks/DECISION_MODEL_P1CD_HANDOFF.md`。
P1-E 共用设置交接：`docs/tasks/DECISION_MODEL_P1E_HANDOFF.md`。
P1-F/G 实际服务与用量组合交接：`docs/tasks/DECISION_MODEL_P1FG_HANDOFF.md`。
P2 Curator 前置标注交接：`docs/tasks/DECISION_MODEL_P2_CURATOR_HANDOFF.md`。
P5-B 正式条目关系建议交接：`docs/tasks/DECISION_MODEL_P5B_HANDOFF.md`。
子代理自动选模真实隔离验收：`docs/tasks/DECISION_MODEL_CHILD_LIVE_HANDOFF.md`。
P5-C 已归档网页阅读建议交接：`docs/tasks/DECISION_MODEL_EXTERNAL_MATERIAL_ORDER_HANDOFF.md`。
P5-D 主会话选模型合同审计：`docs/tasks/DECISION_MODEL_MAIN_MODEL_AUDIT.md`。
P5-D 原线程模型选择版本交接：`docs/tasks/DECISION_MODEL_MAIN_MODEL_SELECTION_HANDOFF.md`。
P5-D Gateway 观察首片交接：`docs/tasks/DECISION_MODEL_MAIN_MODEL_OBSERVE_HANDOFF.md`。
P5-D Gateway 首请求自动采用交接：`docs/tasks/DECISION_MODEL_MAIN_MODEL_ADOPTION_HANDOFF.md`。
P5-D 主会话隔离真实样本：`docs/tasks/DECISION_MODEL_MAIN_MODEL_LIVE_HANDOFF.md`。
P5-E/F/G/H 自动对照与恢复合同审计：`docs/tasks/DECISION_MODEL_SELF_EXPERIMENT_AUDIT.md`。
P5-E1 授权/预算原语交接：`docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md`。
P5-G 设置恢复基础原语交接：`docs/tasks/DECISION_MODEL_SETTINGS_RESTORE_HANDOFF.md`。
P5-A 召回前安全接缝审计：`docs/tasks/DECISION_MODEL_PRE_RECALL_AUDIT.md`。
P5-C 自学习候选筛选审计：`docs/tasks/DECISION_MODEL_SELF_LEARNING_AUDIT.md`。
P5-C 规划派工建议审计：`docs/tasks/DECISION_MODEL_PLANNING_AUDIT.md`。
P5-C 原 Todo 优先级建议首片：`docs/tasks/DECISION_MODEL_PLANNING_HANDOFF.md`。
P5-C 交付质量软提示审计：`docs/tasks/DECISION_MODEL_DELIVERY_QUALITY_AUDIT.md`。
P5-C 动作候选观察来源审计：`docs/tasks/DECISION_MODEL_ACTION_CANDIDATE_AUDIT.md`。
P4-B 普通中文代操作审计：`docs/tasks/DECISION_MODEL_NATURAL_CONFIG_AUDIT.md`。
P4-B 首次真实失败与注册边界：`docs/tasks/DECISION_MODEL_NATURAL_CONFIG_LIVE_HANDOFF.md`。
P4-B 普通 owner 工具修复与真实复测：`docs/tasks/DECISION_MODEL_NATURAL_CONFIG_FIX_HANDOFF.md`。
子代理模型目录持久代次交接：`docs/tasks/DECISION_MODEL_CATALOG_GENERATION_HANDOFF.md`。

这份树只描述当前主链路。旧迁移入口、过渡计划和已删除模块不在这里保留。

```text
.dockerignore                           # Docker 生产源码允许列表，排除本地运行状态和测试产物
install.sh                              # 默认一键容器安装；生成透明 my-agent CLI，--host 为开发模式
TUI_EXTREME_TEST_MATRIX.md              # 五路 TUI 持续轮转、边界/组合/fuzz/soak 用例与客观证据权威账本
agent_py_agent/
|-- __main__.py                         # python -m agent_py_agent CLI 入口
|-- config/                             # 默认 YAML 配置与常数目录（constants_catalog.json 由 scripts/build_constants_catalog.py 生成）
|-- vendor/bubblewrap/                  # 随 Linux 离线二进制携带的许可、对应源码 RPM、构建及来源说明
|-- cli/                                # 命令行、chat/TUI、gateway 管理、诊断维护命令
|   |-- chat.py                         # 本地 chat 入口
|   |-- chat_client_context.py          # 轻量 Gateway TUI 客户端、活动输入三态与 input-status 查询
|   |-- gateway_client_headers.py       # 仓库HTTP验收脚本复用原部署配置与G3凭据，不增传输入口
|   |-- scenario.py                     # 隔离诊断场景注册、命令参数来源及 suite 调度
|   |-- scenario_cases/                 # 各类诊断场景及其专用替身
|   |   |-- runner_retry_case.py        # runner 重试场景，保留现有收口断言及其失败证据
|   |   `-- runner_retry_backend.py     # 只供该场景注入的离线失败后端
|   |-- memory_admin_parser.py          # Memory v2 唯一管理员命令树与中文参数帮助
|   |-- memory_admin_commands.py        # Candidate/Curator/Retention/Doctor/Migration 共用正式 Service 的 CLI 适配
|   |-- skill_proposal_commands.py      # `skills proposals list/show/confirm/reject`：子代理经验提案的用户入口；也挂 learned 子树
|   |-- skill_learning_commands.py      # `skills learned list/show/revert/remove`：自动总结 Skill 的查看、回滚与删除
|   |-- host_state_guard.py             # H3：模型命令沙箱里宿主状态只读、CLI 起不来时输出结构化错误码 CLI_HOST_STATE_READ_ONLY
|   |-- chat_parts/                     # TUI、gateway client、stream/render worker
|   |   |-- chat_prompt_queue.py        # 可按 request identity 原子回取且保持 FIFO/task_done 账的聊天任务队列
|   |   |-- tui_agent_navigation.py     # TUI 精确子代理选择栈、详情游标与父子视图切换状态
|   |   |-- tui_goal_editor.py          # 主子代理共用 Goal 草稿编辑、明确保存、版本冲突与放弃退出
|   |   |-- tui.py                      # chat TUI 生命周期、唯一 runtime/worker/preflight 接线与返回码
|   |   |-- tui_block_renderer.py       # typed snapshot 到欢迎/消息/思考/工具/权限/队列/footer formatted lines
|   |   |-- tui_complete_detail.py      # 完整原文有界分页、长行分片与稀疏页索引
|   |   |-- tui_reading.py              # 普通/详细/原文共用阅读锚点与连续跨页定位
|   |   |-- tui_display_archive.py      # 异步读取原文归档页，缓存限额与失败重试
|   |   |-- slash_commands.py          # CLI 命令分派与公共声明的帮助投影
|   |   |-- plugin_command_client.py   # 显式宿主模式、会话目录缓存与原版本提交
|   |   |-- command_interaction.py     # 单次命令的编号、审批消费者及独立取消引用
|   |   |-- plugin_command_stream.py   # 持续读原命令流并异步回写完整审批绑定
|   |   |-- tui_plugin_commands.py     # 输入候选版本绑定与不阻塞输入的插件命令分派；面板动作本地切换
|   |   |-- tui_plugin_panels.py       # 插件面板本地显示偏好、有界后台刷新与纯排版，不运行插件代码
|   |   |-- slash_command_types.py     # CLI 命令处理器的可信上下文，不另设命令目录
|   |   |-- tui_input.py                # 真实 slash/path 补全、菜单、history suggest 与排队占位投影
|   |   |-- tui_media_clipboard.py      # 显式截图剪贴板读取及临时文件清理
|   |   |-- tui_media.py                # 文件拖入/附件命令与草稿引用
|   |   |-- tui_decision_menu.py        # 原模型菜单的决策范围、模式、秒数、恢复继承和显式原生连接测试
|   |   |-- tui_effort_menu.py          # 单独 /effort 的档位菜单：选中后以 /effort <值> 走控制出站箱
|   |   |-- tui_model_menu.py           # /model 顶层五项（新增/选择/管理/连接测试/默认与共享）与弹窗、焦点守卫
|   |   |-- tui_model_add.py            # 「新增模型」五类：填连接→拉列表→勾选一次保存，拉不到可手动填，OpenCode 模板与高级请求头
|   |   |-- tui_model_auth.py           # 新增登录账号、已有账号的登录/添加模型/退出/删除，设备码与通用参数
|   |   |-- tui_browser_login.py        # ChatGPT 订阅浏览器登录：本机临时回调、自动打开浏览器、登录完自动回到 TUI
|   |   |-- tui_subscription_models.py  # 模型勾选框（可多选）：订阅账号与新连接共用，经 add_models 一次保存
|   |   |-- tui_model_metrics.py        # Context 下方本轮模型轮、当轮工具、总缓存（两位小数）、累计会话、决策开关与成败、输出、速度的一行统计
|   |   |-- tui_shared_model_menu.py    # 管理员逐模型显式共享/撤销、指定其他用户的初始模型
|   |   |-- tui_permissions_menu.py     # /permissions 与 F4 三档权限菜单、保存/取消及管理员确认
|   |   |-- tui_provider_menu.py        # 「管理已有模型」（改连接、编辑/删除模型、从连接再勾选添加）与连接测试
|   |   |-- tui_media.py                # 文件拖入/附件命令与草稿引用
|   |   |-- tui_media_clipboard.py      # 显式截图剪贴板读取及临时文件清理
|   |   |-- tui_input_delivery.py       # 活动回合输入的持久 outbox、同 ID 对账与排队接管
|   |   |-- tui_control_delivery.py     # slash 控制命令的持久 outbox、稳定操作 ID 与只读状态对账
|   |   |-- tui_interaction.py          # stash、Ctrl-R、paste 与 `?` help 的线程安全输入状态机
|   |   |-- tui_markdown.py             # CommonMark/table token 到 Unicode 宽度换行、语法色和 prompt_toolkit fragments
|   |   |-- tui_paste.py                # 大文本粘贴显示引用与提交时精确展开合同
|   |   |-- tui_preflight.py            # alternate screen 内真实 Gateway readiness 等待、typed 连接事件与 worker 启动门
|   |   |-- tui_upgrade_follow.py       # Gateway 换安装后空闲原地切换到同版客户端：runtime_prefix 比对、空闲判定、UI 线程 execv、会话与 termios 交接
|   |   |-- tui_permission_queue.py     # 主代理与多个 child 的工具审批统一 FIFO、原 request 回写和展示去重
|   |   |-- tui_runtime.py              # session/queue/turn/model/tool/Gateway typed rows 到稳定 TuiEvent 的唯一 adapter
|   |   |-- tui_application.py          # 单调时间重绘合并，避免校时后输入可读但屏幕不刷
|   |   |-- tui_terminal.py             # OSC 终端标题、活动帧与退出清理
|   |   |-- tui_transcript.py           # 详细 transcript 冻结视图、全文搜索与命中导航状态
|   |   |-- tui_view.py                 # prompt_toolkit typed transcript control、frame/block cache、scroll anchor 与 overlay/footer
|   |   |-- tui_history.py              # 上翻异步读取更早页、同页面响应校验和冻结视口衔接
|   |   |-- tui_safe_lines.py           # 不可变已净化展示行，复用稳定正文并保留新文本过滤
|   |   |-- tui_identity_window.py      # 客户端有界近期身份索引，不替代 canonical 幂等
|   |   |-- tui_events.py               # TUI 唯一 versioned event 信封、单调 sequencer 与幂等有界 journal
|   |   |-- tui_view_model.py           # typed event reducer：稳定/活动 block、权限 overlay、输入队列与状态快照
|   |   |-- tui_ui_setup.py             # alternate-screen prompt_toolkit 布局、控件、style 与 focus 接线
|   |   |-- tui_keybindings.py          # 按键注册与处理：输入、帮助、权限、队列、滚动、transcript、中断与退出 typed key intents
|   |   |-- tui_actions.py              # 按键触发的副作用边界：Gateway 提交/控制对账、后台线程、子代理插话与中断
|   |   |-- tui_clipboard.py            # 单应用有界复制顺序与分通道结果，及本机/tmux 复制子进程 helper
|   |   `-- control_runtime.py          # CLI 对共享会话控制协议及窗口级精确中断的运行适配
|   |-- home_runtime_commands.py        # owner home 状态、daily/task workspace/index 维护命令
|   |-- admin_identity_commands.py      # 本机 admin-password set/status/clear 与 admin-identities list/remove（仅 local/main 配置）
|   |-- gateway_process.py              # gateway 进程入口
|   |-- gateway_host_guard.py           # 托管标记：Gateway 托管的工具进程不能停止或重启托管自己的 Gateway
|   |-- gateway_restart_handover.py     # 安全重启进程接线：服务循环排空、接班进程或退出码 75、启动时续跑与通知
|   |-- gateway_lane_retry.py           # 后台 owner/thread 配置等待、环境故障与额度用完暂停（指纹变化或 60→900 秒探测放行）与普通冷却；有界、线程安全、不另存任务状态
|   |-- gateway_loop_backoff.py         # 派发循环与后台主循环共用的连续出错退避（0.2s 翻倍封顶 30s）与打印限流（同种错误第 1 次、之后每 10 次）
|   `-- _*.py                           # CLI 子命令实现
|-- skills/builtin/<category>/<name>/   # 内置知识型 skill 树：目录即分类（research/documents/…），递归扫描，类目索引常驻 prompt，skill_search 工具按需检索（千级地基）
|-- agent/
|   |-- command_catalog.py             # 核心命令声明、别名、会话词法、保留命名空间与帮助/补全事实
|   |-- command_arguments.py           # 参数不可变声明、类型校验与保留原文范围的公共词法
|   |-- command_binding.py             # 同源参数绑定、缺值位置、帮助与选项分界
|   |-- command_declarations.py        # 包和宿主目录共用的严格 JSON 声明读取器
|   |-- plugin_commands.py             # 插件命名空间、宿主描述解析与无副作用的静态回执；/plugins list 末尾 MCP 服务段的文字投影
|   |-- plugin_command_catalog.py      # 可验证的不可变目录快照、JSON 合同及内容版本
|   |-- plugin_command_service.py      # 宿主作用域目录与旧版本拒绝，不拥有安装和执行权
|   |-- plugin_completion.py           # 用公共词法和绑定事实生成只编辑输入的候选
|   |-- plugin_manifest.py             # 静态包描述与不可变 schema：v2 面板、v3 Skill、v4 宿主 API、v5 观察，v8 文件入口订阅；旧字节保持
|   |-- plugin_channel/                # 插件共用通道（M 线 B2）：面板与事件共用的连接管理
|   |   |-- __init__.py                # 只导出通道池与异常，不反向依赖面板或事件模块
|   |   `-- pool.py                    # 每个（owner, 激活代次）一条 MCP 连接：单在途、代次复核、启动计时超时、退避、空闲关闭
|   |-- plugin_events/                 # M1 插件事件域：B1 静态声明与确认、B3 观察投递（protocol/hub）
|   |   |-- __init__.py                # 事件域命名空间，不在导入时启动服务
|   |   |-- declarations.py            # v8 事件、工具收紧门、网络权限的不可变声明与严格校验
|   |   |-- confirmation.py            # 同源四项确认事实和中文正文/参数/网络/强制沙箱需求预览
|   |   |-- protocol.py                # 事件公共字段、握手能力门与线上 payload 组装，纯协议无 IO
|   |   |-- hub.py                     # 事件中心：按 owner 分区、只留最新、单在途、回收与计数；投递走 plugin_channel
|   |   |-- points.py                  # B4 事件白名单投影、提示脱敏、会话哈希与开关/异常短路；不读安装表
|   |   |-- tool_gate.py               # B5 纯收紧合同：精确匹配、安全请求投影、严格回复和稳定最严合并
|   |   |-- decision_ledger.py         # B5决定白名单与原runtime_events唯一写入；读canonical信封，排除虚构门/正文
|   |   `-- tool_gate_review.py        # B5共用池总预算/握手/安装追新，精确批准跳门仍复读，不另建池或批准账
|   |-- plugin_observation.py          # 观察候选宿主合同（插件与 MCP 共用）：声明类型与配对规则、载荷整份校验（含几何 frame/region）、宿主铸 ID、模型投影、按 runtime_events 序判定新鲜度与候选复核
|   |-- plugin_display/                # 插件声明式面板与只读订阅（第 9 步）
|   |   |-- protocol.py                # 面板声明、公开主题与展示描述校验/截断，纯协议无 IO
|   |   `-- service.py                 # Gateway 进程内展示服务：面板待发合并、结果缓存与退避；连接与在途走 plugin_channel
|   |-- plugin_package.py              # 有界 ZIP 读取、成员与摘要核对，不安装或导入插件
|   |-- plugin_entry.py                # v6 非 Python 入口、随包文件与平台声明的形状校验及本机平台标记
|   |-- plugin_wheels.py               # wheel 标准元数据、RECORD、平台与本地依赖闭包预检
|   |-- plugin_wheel_layout.py         # 环境内文件计划、引导文件保护与宿主只读核对
|   |-- plugin_environment.py          # 固定地址的 owner 独立 venv 准备，尚不发布激活
|   |-- plugin_files_environment.py    # v6 包的环境准备：按摘要排他解包随包文件并设权限，不执行任何文件
|   |-- plugin_environment_plan.py     # 原 operation 领取前冻结包、配置版本、解释器与候选身份
|   |-- plugin_environment_process.py  # 原 claim 精确复查、托管准备命令与有界取消清理
|   |-- plugin_installation.py         # 安装请求、私有配置事实、通用提交回执与安装准入
|   |-- plugin_installation_state.py   # 唯一安装表 v3 编解码及 v1/v2 明确迁移来源
|   |-- plugin_configuration.py        # 完整配置替换、原请求重放与版本 CAS 纯裁决
|   |-- plugin_activation_record.py    # 固定环境计划、同代准备/发布/撤销身份及严格读回
|   |-- plugin_activation_ref.py       # 可信 owner 与原代次引用，跨进程复查唯一安装表
|   |-- plugin_activation.py           # 原安装版本上的激活迁移、阶段重放与旧代拒绝
|   |-- plugin_runtime.py              # 固定代次的 MCP 服务、完整目录校验与原工具代理；观察结果改写与动作候选发送前复核
|   |-- plugin_runtime_facts.py        # 非 Python 插件的本机运行事实：解释器解析与固定、启动前复核、用户确认回执与确认码
|   |-- plugin_sandbox.py              # 插件进程 OS 沙箱试点：经平台沙箱网关启动，只写插件数据目录，不可用则拒绝
|   |-- plugin_skills.py               # 已启用插件自带 Skill 目录的唯一定位规则（来源 plugin:<ID>，最低优先级）
|   |-- plugin_host_api.py             # 插件宿主只读 API：令牌发放与复核、/plugin-host/query 主题投影
|   |-- workspace_read_context.py      # 宿主与插件共用的冻结读取协议及逐项路径检查
|   |-- workspace_write_context.py     # 宿主与插件共用的冻结写入协议及逐项写入裁决
|   |-- plugin_invocation.py           # 显式业务选择摘要、原执行器组装及单次 MCP 连接收尾
|   |-- plugin_deactivation.py         # 撤销原代、关闭准备执行权并清理两类精确资源
|   |-- plugin_release.py              # 原准备执行器退出与完整资源证据核验，不改写 UNKNOWN
|   |-- plugin_cleanup.py              # 原结果成功读回后消费退出引用、回收无人引用包，重送不重跑
|   |-- plugin_removal.py              # 固定安装删除 CAS 与卸载回执，不新增墓碑或持久历史
|   |-- plugin_install_store.py        # owner 唯一安装表、包内容保存及锁内 CAS/提交裁决
|   |-- plugin_install_tool.py         # 经原执行器读取授权包快照并保存默认停用记录
|   |-- plugin_update.py               # 版本更新纯计划：同 ID 新包替换已停用安装，install+可选 configure 两步回执保留配置
|   |-- plugin_update_tool.py          # 模型不可见的更新管理工具：读新包、按当前版本 CAS 提交替换，回执带配置是否保留
|   |-- plugin_configure_tool.py       # 经原执行器读取和验证私有配置，结果不含配置值
|   |-- plugin_enable_tool.py          # B7 前确认之前拒绝 v8；旧版原操作内准备、核对目录、候选退出后发布
|   |-- plugin_disable_tool.py         # 原宿主链中的隐藏停用工具，区分撤销与资源清理结果
|   |-- plugin_remove_tool.py          # 原宿主链中先停用释放再卸载，保留产物及确定/未知提交事实
|   |-- plugin_sources.py              # 安装包与配置共用的有界授权文件读取
|   |-- plugin_management.py           # 原权限、线程与目录的组合，管理/业务分派及原结果查询
|   |-- core.py                         # SimpleAgent 组合入口
|   |-- turn_end.py                     # 主/子代理共用的结束原因及技术续跑判据
|   |-- model_guidance.py               # 完整 Prompt 与有副作用工具共用的验证/授权软提示唯一正文
|   |-- runtime_context.py              # Tooling、Conversation 与 core 共用的线程本地 runner 身份和任务属性
|   |-- model_request_selection.py      # 主请求渲染与发送前的可选宿主回调，不持有目录或执行权
|   |-- gateway_model_observation.py    # Gateway 车道后主会话的一次决策观察与来源冻结
|   |-- gateway_model_adoption.py       # 跨 Gateway/core 的完整候选验证、临时依赖与线程发送前 CAS
|   |-- gateway_compact_context.py      # 跨 Gateway/core 的同 turn Compact 重载展示和身份冻结
|   |-- gateway_compact_recovery.py     # 完整恢复请求准备后执行原 transcript CAS，并用获选材料继续同次生成
|   |-- task_progress_guidance.py        # Todo exact-id 最终回复前核对的可配置软合同；不自动判定或打勾
|   |-- agent_core/                     # 无副作用包入口；主代理运行时、工具循环、编排与自然回合收口实现
|   |   |-- decision_planning.py        # 原 Todo read 的当前未完成项精确优先级软建议
|   |   |-- cli_run_conversation.py     # 一次性 CLI 的权威 user/assistant transcript、幂等身份与失败分级
|   |   |-- tool_request_capture.py    # 从真实运行参数共用捕获完整请求，不重复目录或上下文准备
|   |   |-- host_turn_scope.py         # 宿主工具和有界表达共用的原core轮生命周期与未结责任。
|   |   |-- tool_only_execution.py     # 显式工具工作和零工具表达，沿原身份、权限、取消令牌和收口
|   |   |-- compact_request_recovery.py # 三宿主一次准备、完整候选计量和原CAS后同次发送
|   |   |-- compact_transcript_prefix.py # 同次主请求连续历史前缀与受保护事实边界验证
|   |   |-- compact_active_projection.py # 精确保留IR与归档交接投影，保留原工具账和媒体插话
|   |   |-- compact_tool_partition.py # 原生工具完整往返与归档同源分区，不完整和未知材料保留
|   |   |-- subagent/compact_recovery.py # child独立历史与固定注入位置到公共完整恢复器的适配
|   |   |-- tool_request_projection.py # 冻结完整 prompt/schema/IR 的纯请求投影，缺事实返回 typed unknown
|   |   |-- subagent/model_selection.py # 首个真实子代理模型请求的宿主资格、持久选择及失败保留协调
|   |   |-- runtime/                    # 单 child guidance、active-turn compact carrier、sleep 闹钟与 loop support
|   |   |   |-- conversation_state.py  # 主/子代理当前 Compact 代次的结构化模型事实投影
|   |   |   |-- sleep_tool.py           # clock.sleep 工具：模型主动定时等待，写 wake_queue 字条、事件提前醒取消
|   |   |   |-- record_lesson_tool.py   # 子代理专属 record_lesson：身份取 runner 上下文，把一条可复用做法记进本 run 的 lessons.jsonl
|   |   |   |-- turn_trigger.py         # 回合触发类型：生命周期唤醒以“# Host Event”开头、固定推荐短名单、本片注入不进后续重放
|   |   |-- model/                      # 主工具循环的统一模型调用账、动态超时、上下文压力与成本统计
|   |   |   |-- selection_refresh.py   # 请求安全点采用最新显式模型选择，保持原执行和工具身份
|   |   |   |-- selection_history.py   # 换协议时保留工具/媒体并从原 Compact 来源恢复兼容历史
|   |   |   |-- context_calibration_carry.py # owner 级按分词身份的校准比值缓存（O/data/context/calibration.json），线程观测对不上时回落
|   |   |-- tool_loop/                  # 工具轮次执行、恢复与自然结束
|   |   |-- tool_loop/closeout.py       # 绑定原收口操作，按结构化副作用事实生成未完成交接
|   |   |-- tool_loop/model_turn.py     # 绑定模型请求／超限恢复、用量与输入确认，保持原顺序
|   |   |-- tool_loop/display_archive.py # 执行当时的公开工具原文归档与轻量预览引用
|   |   |-- tool_loop/deferred_schema_reload.py # 收起工具被盲调且参数校验失败时，下一次请求带上它的完整定义（默认收起开关打开时）
|   |   |-- tool_context/               # 工具结果上下文：reducer、窗口、microcompact、PTL 单轮重试
|   |   |   |-- decision_delivery_quality.py # 新验证或成功写入后多个 stale 焦点的可选提示，共用逐记录一次，只追加宿主事实
|   |   |   |-- decision_action_candidate.py # 插件观察归档后的可选动作候选提示，只选宿主铸的候选；材料带几何派生的粗位置，采用后可计划宿主自动执行
|   |   |   |-- decision_action_execute.py # action_candidate 自动执行规划与记账：能力开关、属主、唯一可执行动作、幂等事实，计划进 params.host_actions
|   |   |   `-- external_material_order.py # 原已归档页的可选阅读顺序提示，保留正式结果与 refs
|   |   |-- orchestration/              # 创建、只读状态、消息、取消、授权五个递归直属工具与内部自动启动/恢复引擎；无兄弟 goal 广播，进展事件由宿主写入
|   |   |   |-- decision_subagent.py # 原批次创建前模型建议，锁外请求、锁内复核，不另建任务账
|   |   |   |-- create_context.py     # 显式资料与合同装配；已移除 work_scope.py 的自动 IO 身份，引用不合并派工
|   |   |   |-- create_read_scope.py  # 创建前显式输入路径可见性预检：复用子代理运行时同一判定链，看不到整批 not_started
|   |   |   |-- input_media_refs.py   # 派子代理时 input_media_refs 的解析与校验：只认父会话里的 sha256，重验后写进 child 的 input_media（默认关）
|   |   |   |-- capacity.py           # 根/子/孙代理共用的会话树与 owner 容量事实；超限整批拒绝
|   |   |   |-- coordinator_policy.py # 主代理/多层 coordinator 共用的 会话运行时 式派工后职责软合同
|   |   |   |-- tools/list_agents.py  # 会话运行时 式只读代理树查询；复用 canonical 投影，不推进或取消下级
|   |   |   |-- tools/list_owner_sessions.py # 管理员只读列本 owner 会话（id/状态/最近活动/可用发送类型），不含正文、不列别的 owner
|   |   |   `-- planned_delegation.py # 已有 Todo 时，创建前原子校验 active exact covers 与父级 workspace 上界
|   |   |-- agent_tree/status.py        # `/status`、TUI、恢复与诊断共用的内部代理树投影（不是模型工具）
|   |   |-- agent_tree/model_view.py    # 同一授权快照的模型状态/结果视图；超长摘要保留逻辑归档入口
|   |   |-- _finalization_service.py   # 保留模型最终正文并记录 turn_end.reason
|   |   |-- tool_loop/natural_user_reply.py # 派工/续跑/完成共用的无工具 LLM 用户回复出口
|   |   |-- tool_loop/completion.py     # 工具上限、截断与递归父级自然让出后的依赖等待
|   |   |-- tool_loop/deliverable_closeout.py # 子代理声明了交付物却缺产物时的有界收口门（只认宿主声明的清单）
|   |   `-- runner/                     # 子代理 runner prompt/worker/session/timeout；context.py 也隔离共享 Agent 的 thread-local 运行态
|   |       `-- activity_diagnostics.py # 复用心跳的 exact attempt 阶段提醒，不强杀慢模型或自动重派
|   |-- subagents/
|   |   |-- manager.py                  # 子代理 root manager：初始化、基础生命周期、服务组合
|   |   |-- coordination.py             # 原创建文件锁的同线程重入，统一创建与执行轮短事务
|   |   |-- runner_start.py             # 准确 pending 接纳与原身份核对，共用创建锁
|   |   |-- file_runner_start.py        # 显式文件模式的一次性启动与撤销，整任务保存保留原接纳事实
|   |   |-- cancellation.py             # 原子树权限关闭和资源冻结，锁外仅清理固定批次
|   |   |-- cancellation_hosts.py       # 原执行轮协作中断与宿主退出只读观察，不杀共享或接续进程
|   |   |-- runner_control.py           # 原 RuntimeDB/canonical 取消判据，供心跳和执行前复核
|   |   |-- kernel.py                   # 子代理树快照
|   |   |-- manager_work_orders.py      # 工单路径、默认文件、校验
|   |   |-- models.py                   # 子代理数据模型
|   |   |-- process_control.py          # 子代理宿主存活/启动事实，复用公共进程树终止并保留核对回执；后台启动记录过期判定的唯一权威
|   |   |-- direct_parent_lifecycle.py # 直属父子等待、事件唤醒、同批合并与结果上下文
|   |   |-- runner_completion_payload.py # 完成正文、产物引用与父级交接信封的只读投影
|   |   |-- runner_display_projection.py # 已裁决子代理状态到 TUI 活动标签的纯投影
|   |   |-- runner_result_admission.py # exact run／attempt 的迟到与终态冲突准入、拒绝诊断
|   |   |-- tool_failure_ledger.py      # 系统级工具失败账本：archive ok=False 摘要 -> attributes/对账投影；最近同码连续失败段（父级视图与授权收口共用）
|   |   |-- result_registered_artifacts.py # 自然/结构化收口共用的 exact run 工具产物投影
|   |   |-- lesson_ledger.py            # lessons.jsonl 账本唯一合同：四字段边界、固定渲染模板、幂等追加、每 run 上限与读回复核
|   |   |-- services/                   # 子代理业务服务
|   |   |   |-- base.py                 # create_run/split/owner/runtime config scope
|   |   |   |-- output_alignment.py    # 声明按可信 cwd 解析，不重定位、不搬运、不增权
|   |   |   |-- persistence/            # canonical state、projection、index 同步
|   |   |   |-- dispatch/               # dispatch/watch/parent planner 报告
|   |   |   |-- runner_context_service.py # 执行上下文和边界文件
|   |   |   |-- runner_result_service.py # runner result 写回和副作用
|   |   |   |-- runner_result_commit.py # 已落盘 runner 结果的 WAL、run 结算及父级通知初次编排
|   |   |   |-- runtime_closeout.py    # 显式存储/通知能力推进原 WAL、运行收口与恢复扫描
|   |   |   |-- executor_recovery.py   # 执行器确证退出后的失败/未知副作用投影与父级通知
|   |   |   |-- board/                  # board、due-check、action-plan
|   |   |   |-- actions/                # action-plan 应用、取消/接管动作
|   |   |   |-- takeover/               # 接管 run、接管就绪包；record.py 是接管/接替边的唯一落账入口（已结束来源只记 superseded_by）
|   |   |   |-- hierarchy/              # 多层调度和恢复包
|   |   |   |-- patch_apply/            # patch review/apply/report
|   |   |   |-- capability_service.py   # 能力请求、grant、gap、路由
|   |   |   `-- memory_candidates.py   # 子代理 lessons/findings 只经父级写入 owner 唯一候选账本
|   |   |-- patch/                     # patch review/apply 底层实现
|   |   |-- execution/                 # 测试执行和记录
|   |   `-- static_site/               # 静态站点检查
|   |-- user_space/                    # owner home、task workspace、policy、可选 quota、doctor、自动 retention
|   |   |-- owner_access.py            # 完整代理与冷管理共用的 owner 目录墙及权限裁决
|   |   |-- approval_mode.py           # owner 显式审批模式与权限快照映射，子代理同源读取
|   |   |-- admin_password.py          # 管理员密码 scrypt 记录与按渠道身份的失败节流（config/，0600）
|   |   |-- admin_channel_identity.py  # 已绑定为管理员的 IM 私聊 (channel, user_id) 精确绑定表
|   |   |-- task_credentials.py        # 私人任务凭据的加密和 task/job/origin 校验
|   |   |-- interaction_refs.py         # 群转私聊的 owner/actor/channel/expiry 签名引用；不授予工具权限
|   |   |-- owner_admin_controls.py    # 管理员对每个 owner 的 Jev/审计开关与本人跨用户审计许可（tool_policy.json 的 admin_controls，失败关闭）
|   |   |-- owner_quota.py             # 显式非零磁盘上限的跨进程配额锁；0 时退出热路径
|   |   |-- home_retention.py          # 结构化终态/时间清理、二次校验、trash tombstone 与 legal hold
|   |   `-- owner_maintenance.py       # owner 维护间隔、状态记录与自动执行控制
|   |-- memory_store/                  # owner 长期事实、候选、每日经历、策展、晋升与维护的唯一主链
|   |   |-- candidate_models.py       # Candidate v2 Schema、来源/scope/状态枚举与稳定 ID
|   |   |-- candidates.py             # owner candidates.jsonl 唯一候选账本和唯一状态机
|   |   |-- curator.py                # 后台策展统一 Service、reason、lease、增量 cursor 与有界重试
|   |   |-- curator_due.py            # consumer与发现共用本地日终、间隔和DST时间边界的纯判据
|   |   |-- curator_backend.py        # 无工具辅助模型调用适配；只返回严格结构化结果
|   |   |-- decision_recall.py        # 原预算后重排长期事实或追加同范围补充查询，复核来源并复用本轮上下文
|   |   |-- decision_curator.py       # 可选用户后台分类/优先级标注，失败保留原批次且不拥有记忆写权限
|   |   |-- decision_curator_relation.py # 同阶段的完整来源—正式条目关系建议，精确版本复查且不直接合并或晋升
|   |   |-- curator_commit.py         # Daily/Candidate/state/run audit 提交与同临界区恢复接管
|   |   |-- curator_routing.py        # 按会话主代理模型分组：挑本次处理的那组、审计事件只跟默认组、按组投影熔断游标、汇总多组结果
|   |   |-- curator_*.py              # Curator 输入、Schema、正式记忆快照、状态与运行审计辅助模块
|   |   |-- daily.py                  # v2 DailyMemoryEvent 稳定序列、幂等合并与按天账本
|   |   |-- jsonl.py                  # 正式 long_term 稳定 ID CRUD/batch、冲突、hard delete 与召回
|   |   |-- lessons.py                # 正式 lesson/HOT 唯一正文、阈值验证与确定性 routing index
|   |   |-- lifecycle.py              # close/reset/task-complete/pre-compact 等统一策展请求入口
|   |   |-- migration.py              # Memory v1→v2 只读计划、完整备份、幂等迁移和失败回滚
|   |   |-- operations.py             # ops.jsonl 无正文正式 Memory 操作审计
|   |   |-- promotion.py              # Candidate 证据、冲突、Persona、lesson/HOT 的唯一晋升服务
|   |   |-- recall.py                 # active long-term/正式 lesson/HOT 的 owner+scope 安全召回
|   |   |-- retention.py              # 统一 Retention service facade
|   |   |-- retention_*.py            # 策略/计划/重验证/可恢复删除和 hard-delete 辅助模块
|   |   `-- security.py               # Memory/Persona 共用持久内容威胁扫描
|   |-- memory_api.py                  # CLI/外层包使用的公开 Memory façade；只转发正式 Service/DTO，不建第二权威
|   |-- memory_push.py                 # planner/runner 决策点只召回正式 Lesson/HOT，并复用唯一 memory-context 信封
|   |-- memory_archive/                # compact、audit、tool output artifact、task workspace refs
|   |-- local_storage/                 # SQLite/FTS/文件事实源；ledger_redaction.py 精确擦除已删事实但保留幂等身份
|   |-- runtime_db/                     # SQLite 运行事实源：Task 身份、TaskRun/AgentRun/Attempt 生命周期、wake 与投递账本
|   |   |-- input_lifecycle.py          # 原wake整行CAS、效果验权与输入静默退休SQL收口
|   |   |-- run_creation.py             # 普通代理及宿主命令共用的同连接运行树创建
|   |   |-- host_commands.py            # 显式宿主请求与原始 pending 运行的唯一绑定及严格回读
|   |   |-- host_command_execution.py   # 原 pending 精确执行、原运行收口和不重跑的只读查询
|   |   |-- host_command_approval.py    # 同一宿主执行区间的精确审批与拒绝，不复用会话批准
|   |   |-- operation_resources.py      # 同次只读核对原操作、holder/代数/epoch 与已领取资源锁
|   |   |-- run_cancellation.py         # 精确 task/run/attempt 的共用取消权限合同，UNKNOWN 保留锁与恢复障碍
|   |   |-- run_takeover.py             # 被接替（TAKEN_OVER）的子代理运行收口为 cancelled：只在执行轮已静止时，CAS 核对 attempt 状态
|   |   |-- run_closeout_backfill.py    # 历史悬挂 run 一次性补收口迁移（rcb）：结构化判定（attempt 静止/无未结算操作/无活跃锁/6h 宽限）分族补终态，迁移记录写 metadata、可只读预览
|   |   |-- user_interrupt.py           # 用户 Esc／/interrupt 中断事实：记到被中断的执行代次，账本自愈据此在续接窗口内不收口
|   |   |-- child_recovery.py           # /recover 线程分支：列出 unknown 子代理；带编号时事务内复核根/子目标范围后恢复
|   |   |-- owner_recovery.py           # /recover owner：空 thread 历史 unknown 只读投影、集合确认码、批量共享 CAS 与幂等回执
|   |   `-- executor_liveness.py        # exact attempt 执行区间和 OS 退出事实；慢模型不按时长判死
|   |-- gateway_parts/                 # gateway request/worker/lease/http/renderer
|   |   |-- process_completion_source.py # 原受管命令终态的C只读来源与W原Wake/notice交接
|   |   |-- runtime_wake_source.py       # C只读DB行快照进入原P3/P5，不复制SQLite/WAL作来源
|   |   |-- execution_target.py        # T 候选：原 claim/request/task 的实际执行投影与代次复核
|   |   |-- execution_control.py       # T 候选：前后台共用的插话和停止目标解析
|   |   |-- local_client_token.py       # G1：本机客户端持久凭据，缺则私有生成、宿主严格读取，不进模型环境
|   |   |-- client_credentials.py       # G3：原请求头共用凭据选择，配置token优先；读不到时按 gateway_require_local_credential 降级或拒绝
|   |   |-- http_routes.py              # G2a：实际HTTP分发和观察唯一同源，路由模板及四档属性
|   |   |-- private_interaction_service.py # 私人卡片签名、宿主凭据/密码/管理员绑定核对及原审批决定
|   |   |-- plugin_command_service.py  # TUI HTTP 与 IM 会话共用插件服务、可信 owner 目录和管理员授权
|   |   |-- plugin_panels_http.py      # /client/plugin-panels：作用域解析、只读活动投影与展示服务调用
|   |   |-- command_stream_protocol.py # 命令有界消息、规范 owner 握手与原审批路径
|   |   |-- command_stream.py          # 原 HTTP 线程执行、审批运输和断连取消
|   |   |-- owner_conversation_store.py # 模型配置与插件管理共用的原 owner 会话 Store 组装
|   |   |-- request_execution.py        # 单个已领取请求的租约、模型执行、超窗恢复与收尾编排
|   |   |-- request_context.py          # 原车道内的会话快照、Compact 与工作目录准备
|   |   |-- request_binding.py          # 精确请求与运行身份绑定、执行车道和原子更新
|   |   |-- request_experiment.py       # /experiment 冻结参数在主轮绑定后、首个模型调用前的单次实验授权与回执
|   |   |-- request_experiment_records.py # 实验对照记录写入请求记录、回合收尾补写实际工具用量、授权指针证据链与只读评估
|   |   |-- request_audit_records.py   # 审计只读：窗口内请求记录的决策观察与请求结果（owner_id 优先、会话归属兜底），白名单、有界、不写文件
|   |   |-- request_experiment_promotion.py # /experiment apply 授权内：证据满足规则时经原设置 CAS 晋升 skill_tool 并写回执
|   |   |-- request_experiment_notice.py # 新晋升回执生成中文宿主提示，复用原队列、当轮流与 IM final，旧回执不补投
|   |   |-- request_pack_verification_notice.py # 回合结束时把能力包宿主核验结论排成宿主提示，和当轮提示一起发布
|   |   |-- steer_closeout_replay.py  # 收口时给没有入口回执的插话定两级出口：可重放就排备用下一轮，否则排“请重新发送”宿主提示
|   |   |-- request_history.py          # 公开正文、canonical 历史提交、去重与延迟补交
|   |   |-- request_prompt.py           # 已准备会话投影的模型输入渲染与历史种子
|   |   |-- stream_writer.py            # 请求级文本缓冲、typed 流事件和显示投影的有序出口
|   |   |-- stream_events.py            # 公开事件白名单、插话/重试投影和原思考归档载荷
|   |   |-- stream_approval.py          # 精确作用域审批缓存绑定与真实客户端交互
|   |   |-- display_archive_service.py # 按可信 owner 与主子会话归属读取一页完整原文
|   |   |-- approval_mode_service.py   # /client/permissions 认证与冷 owner 控制入口
|   |   |-- main_activity.py            # 前台 typed chunk 到共用 main 数字/阶段的只读投影，不复制正文
|   |   |-- foreground_transcript.py    # 前台公开过程复用会话 mapper，候选与 canonical final 精确交接
|   |   |-- approval_session.py        # owner/thread/cwd/权限精确作用域的有界进程内工具审批缓存
|   |   |-- owner_retention.py          # 有精确在途保护的空闲 owner 缓存回收
|   |   |-- bounded_http_server.py     # 单 Gateway 固定 daemon worker、128 在途上限与过载 503 背压
|   |   |-- loop_health.py             # 派发线程起止/每次 tick/逃逸异常/错误打印失败的进程内账本，心跳与 /status 共用一份快照
|   |   |-- control_service.py         # owner/thread 持久根任务的即时状态、纠偏和中断
|   |   |-- control_operation_service.py # slash 控制副作用前置回执、幂等重放与 unknown 对账
|   |   |-- workspace_scope.py         # 普通请求和控制入口共用的宿主目录与 owner 权限校验
|   |   |-- input_delivery_service.py  # 普通消息 active/queued 去向的唯一持久回执与后台对账
|   |   |-- request_client.py          # 薄客户端 ask 载荷和不可变执行选项合同
|   |   |-- channel_health.py          # adapter PID/heartbeat/逐通道状态的 fail-closed 健康投影
|   |   |-- permission_bridge.py       # TUI/Gateway 工具审批 request binding 的原子决定文件桥
|   |   |-- turn_recovery_control.py   # /recover：查看并按用户处置解除当前会话的未知执行轮阻塞（主链优先，其次本会话子代理）
|   |   |-- restart_service.py         # 安全重启唯一状态源：请求/合并、两段排空、完成标记、冷却防循环与发起方通知
|   |   |-- restart_control.py         # /restart：管理员在 TUI/IM 里安排 Gateway 安全重启
|   |   |-- end_task_control.py        # /endtask：管理员结束卡在等待中的定时会话任务，并结算它堵住的定时执行
|   |   |-- wake_ops_control.py        # /wakes：管理员列出已结案的后台唤醒、预览并确认重放
|   |   |-- admin_control_service.py   # /admin 绑定 IM 管理员身份，/approve、/deny 决定本会话唯一待决审批
|   |   |-- skill_control_service.py   # 聊天 /skills：TUI 与 IM 里查看、确认、拒绝技能提案，查看、回滚、删除自动总结的 Skill
|   |   |-- settings_control_service.py # 聊天 /settings（管理员）：TUI 与 IM 里查找、查看、修改、恢复默认、回滚参数
|   |   `-- goal_control_service.py    # 同 thread 持续目标的创建/修改/暂停/恢复/清除
|   |-- conversation/                  # 通道会话账本、权威 transcript、结构化任务关联/续接
|   |   |-- store.py                    # 同源领域组件组装、跨领域上下文与账本维护
|   |   |-- store_io.py                 # 无 Store 依赖的 JSONL 读取、错误报告、路径与文件归档原语
|   |   |-- store_layout.py             # 统一持久目录和路径、只读打开及扫描上下文组装
|   |   |-- store_threads.py            # 线程身份、通道绑定、默认模型解析与 Compact 原子更新
|   |   |-- message_scan.py             # 同 canonical 文件的固定尾界分页、字节限额与流式幂等扫描
|   |   |-- store_messages.py           # 消息幂等追加、展示检查点及字节游标读取
|   |   |-- message_selection.py        # 固定EOF两遍来源校验与逐行范围/覆盖筛选，无第二消息存储
|   |   |-- message_replay.py           # 固定原文件身份及行地址/hash的只读消息重放，切片不驻留正文
|   |   |-- store_tasks.py              # 任务关联、活动索引、工作区状态投影及终态进度关闭
|   |   |-- task_run_closeout.py        # TaskRun唯一领域收口；准确输入退休仅cancelled执行账，未知和活动树保持
|   |   |-- runtime_wake.py             # Count0/W共用原DB单行到期效果，quota与claim窗口保护
|   |   |-- capability_selection_state.py # 原任务内的一次选包标记校验，不另建账
|   |   |-- store_audits.py             # Audit 准备、发布修订、终态重开与运行代提交
|   |   |-- store_guidance.py           # 插话入队、精确认领、权威回执查询与组件组装
|   |   |-- session_messaging.py        # 会话间消息与派活的权限判定（纯函数，只读结构化身份与开关）
|   |   |-- wake_poison.py              # 唤醒毒丸判定（纯函数）：尝试结果分类、同因连续段、总上限、两类退避与结案判定
|   |   |-- wake_attempt_tracking.py    # 唤醒毒丸接线：批次消费处按尝试记账、跳过阶段读持久退避、结案、进程内在途登记
|   |   |-- wake_domain_closeout.py     # 唤醒毒丸领域收尾：结案/来源已放弃时派活收 failed 并回报、宿主提示、领域终态判定
|   |   |-- session_tasks.py            # 会话间派活的唯一权威存储（状态机单一写入方，正文只存 guidance id）
|   |   |-- session_task_dispatch.py    # 已授权会话任务的唯一耐久派发流程，模型工具与宿主 IM 共用
|   |   |-- im_task_status.py           # IM 当日偏好、任务和子代理占用的只读控制投影
|   |   |-- session_pair_rate.py        # 每对会话每小时条数计数（消息/派活/回报/取消通知都计入）
|   |   |-- store_guidance_records.py   # 插话回执格式、迁移构造与身份校验
|   |   |-- store_guidance_ledger.py    # 插话回执读取、回合锁及队列与索引修复
|   |   |-- store_guidance_submission.py # 模型提交批次、执行前拒绝及回执投影修复
|   |   |-- store_guidance_acknowledgements.py # 模型消费确认批次与幂等消息投影
|   |   |-- store_guidance_recovery.py  # 插话终态结算、释放、失效改绑与网络重试准确预留；续跑上限收口时死提交插话定终态（settle_dead_turn）
|   |   |-- store_index.py              # 有界扫描投影、惰性注册、文件指纹与权威读回
|   |   |-- store_usage.py              # 模型用量领域、累计增量去重及线程数字显示的原子更新
|   |   |-- store_claims.py             # 执行租约的领取、续租、精确终态、恢复归属与旧账归档
|   |   |-- store_goals.py              # 目标集合、内容版本 CAS、预算结算及共享时钟依赖
|   |   |-- goal_transition.py          # 原 Goal 最近变更来源、版本、验证引用及进度元数据
|   |   |-- unbound_thread_store.py     # 在原 ThreadStore 幂等物化无通道绑定的独立执行上下文
|   |   |-- claim_execution_binding.py # T 候选：core 实际 run/attempt CAS 发布到原后台 claim
|   |   |-- store_observations.py       # 观察事件构造、追加、待处理扫描与原子确认
|   |   |-- store_wakes.py              # 唤醒入口、观察链接、投递冻结与消费确认
|   |   |-- store_wake_publication.py   # 原去重记录冻结完整发布、固定身份恢复与纯读回执
|   |   |-- store_wake_attempts.py      # 唤醒毒丸持久层：尝试账、结案 failed_permanently、人工重放与留档（store.wakes.attempts）
|   |   |-- store_wake_quarantine_archive.py # 唤醒结案留档满 14 天移进 quarantine/archive/（只移不删，发布语义不变）
|   |   |-- store_progress.py           # 进度策略、到期投影、失败退避落账与旧策略归档
|   |   |-- process_events.py           # 受管后台命令终态到原会话 wake 的去重交接
|   |   |-- task_follow_up.py           # 会话任务是否还有后续工作的结构化判定（定时执行收口据此决定 waiting）
|   |   |-- compact_progress.py       # transcript/live-tool/turn-local Compact 来源与提交权的唯一公开进度协议
|   |   |-- compact_calibration.py    # 压缩候选/压缩前计量的唯一校准口径：按宿主冻结的供应商观测折算，无观测原样返回
|   |   |-- compact_carry.py           # 同宿主溢出原生IR携带、精确插话释放与归档归并
|   |   |-- context_usage.py          # 主/子 preflight 数字的 canonical thread 保存、代次检查与无正文展示
|   |   |-- model_metrics.py          # 主/子模型调用账与历史用量的只读显示投影，不回灌模型上下文
|   |   |-- agent_activity.py          # active task link + canonical child run 到 TUI/Web 共用有界活动投影
|   |   |-- agent_transcript.py        # 子代理跨进程公开过程事件的 owner 存储、游标和有界裁剪
|   |   |-- agent_control.py           # owner 树内代理详情、运行中 guidance 与精确停止的通道中立控制面
|   |   |-- agent_tool_approval.py     # main/child exact ToolApprovalRequest 的唯一耐久记录、consumer 租约与决定等待
|   |   |-- background_im_progress.py # 原委派 claim/attempt 的同条短进度
|   |   |-- im_tool_approval.py        # 原 pending 内的私人卡回执、冻结发起人与原 claim/attempt 决定
|   |   |-- tool_approval_scope.py     # 从canonical任务/线程/main claim/child attempt读取审批归属，隔离恢复换轮
|   |   |-- background_transcript.py  # 后台 main/child 的有界 typed 过程事件环与共用工具审批 sink
|   |   |-- background_history.py     # 后台完整展示块快照，随 canonical final 保存并用于恢复重基
|   |   |-- display_checkpoint.py     # canonical逐块公开过程校验/写入、恢复未知占位与显示去重
|   |   |-- display_archive.py        # 不可变显示原文、无路径引用与有界页文件，不进入模型上下文
|   |   |-- auxiliary_model_call.py   # 会话辅助模型调用的统一账、退避、并发闸；独立压缩用量持久结算
|   |   |-- decision_service.py       # 可选决策阶段预算、设置/身份复核和建议返回，不执行业务动作
|   |   |-- decision_policy.py        # 有界连接/点位冷却与同进程设置取消通知，不拥有 worker 或持久状态
|   |   |-- decision_selection_cadence.py # 选模型询问节奏：structure_change 时按（压缩代数, 候选目录版本, 当前模型）指纹只在结构变化时问，成功后记指纹
|   |   |-- decision_outcome_log.py   # 决策结果日志：每次 decide 的点位/状态/原因/耗时与供应商实际版本（无正文），有界落 owner data/decision，审计按点位汇总并按版本计次
|   |   |-- decision_observe_nonblocking.py # observe 不挡主链路的唯一后台执行器：单 worker、有界排队、捕获身份执行、写结果行与独立用量范围
|   |   |-- decision_point_limits.py  # 决策点数量界限（焦点/候选/网页/待办/提案/记忆的上下限）：点位判定与诊断大白话共用的唯一定义
|   |   |-- decision_reach_counts.py  # 决策点到达/未触发原因计数：进程内按小时累加，节流合并落 owner data/decision/reach_counts.json，原因码配大白话；Gateway 正常停止时补写尾巴
|   |   |-- decision_model_call.py    # 实际决策 worker 复用原准入、身份头、HTTP 观察和唯一调用账；实验先算经验上界再预留
|   |   |-- decision_experiment.py    # 实验准入/路由/在途复核：只读原授权、账本代次与 v2 上界口径
|   |   |-- decision_experiment_evaluation.py # 只读证据评估：召回/节省/结算三项事实，决定 skill_tool off→apply 建议
|   |   |-- decision_audit.py         # 决策审计汇总：设置摘要与 model_usage 决策分区计数（时间窗、按会话明细），只读
|   |   |-- decision_send_permit.py   # 实验单次发送许可：连接前复核绑定、撤销/期限、设置与连接后在原账锁内消费
|   |   |-- tool_context_window.py    # text/native 共用的有界工具历史窗口与稳定前缀投影
|   |   |-- tool_input_progress.py     # provider 大工具参数生成期的脱敏临时展示合同
|   |   |-- agent_thread.py            # child/grandchild 独立 thread、逐 attempt transcript 与统一 Compact 适配
|   |   |-- agent_thread_store.py      # agent thread 精确 ID 物化、身份冲突与运行目录校验
|   |   |-- closeout.py                # 收口状态机 decide_closeout(四改之 2): 终态 done/cancelled/wait_human/wait_handoff/resume_round
|   |   |-- compact.py                  # 唯一 thread compact：候选验证、一次 CAS 提交与近期 raw tail
|   |   |-- compact_projection.py       # 原 Compact 的只读来源、完整请求投影和提交后临时材料合同
|   |   |-- compact_provider_surface.py # transcript Compact 复用普通轮 stable prompt/system/tools/messages 的缓存面
|   |   |-- tool_discovery.py          # typed搜索名称的线程原子并集及新回合恢复，不授予权限
|   |   |-- compact_media_digest.py     # 随图摘要的发送实现：含图回合分组、按预算打包看图小请求、要点标签与部分成功/失败合成
|   |   |-- compact_media_policy.py     # 媒体压缩策略：归档引用/随图摘要决策、视觉能力事实、字节与预算准入、checkpoint 媒体事实
|   |   |-- compact_text_source.py      # 只读两遍编码校验与当前字符窗口，消费后释放，不拥有覆盖
|   |   |-- compact_message_source.py   # 可重放原生摘要消息与完整JSON数组编码，复用唯一token估算
|   |   |-- compact_request_budget.py   # 按当前模型窗口顺序分段摘要，完整覆盖历史且失败不推进游标
|   |   |-- compact_remote.py            # 服务端压缩（Codex 路线）共享助手：触发请求、预算与瘦身、provider_compaction 记录与兼容性判断
|   |   |-- compact_landmarks.py        # 压缩摘要末尾原话备份：按 token 随窗口放大、带消息编号、放不下保留头尾、省略列编号与回查说明
|   |   |-- compact_tool_refs.py        # 从匹配原生工具往返保留原样路径线索，不靠模型摘要记忆目录
|   |   |-- compact_guard.py            # 结构化完整回合选择、连续失败冷却与 typed compact 错误
|   |   |-- compact_checkpoint_scan.py  # 同次固定EOF临时行地址与hash读取，不新增持久索引
|   |   |-- compact_checkpoint.py       # 原提交链v3检查点、版本封印与scope/base精确来源
|   |   |-- compact_scope.py            # 全线程、任务和活动轮的结构化摘要适用范围
|   |   |-- compact_summary_view.py     # 沿实际摘要基础链解析覆盖，旧版本显式读取
|   |   |-- compact_tool_summary.py     # 将所选工具的完整模型可见投影送入原摘要分段器
|   |   |-- compact_tool_identity.py    # 原调用四元身份与精确覆盖键，旧未知不伪造
|   |   |-- active_turn_compact.py      # 跨工作片工具 archive 到同一 checkpoint/CAS 的恢复压缩与模型投影
|   |   |-- live_tool_compact.py        # 运行中原生工具历史到同一 thread checkpoint/CAS 的适配层
|   |   |-- native_history.py           # 完成回合的 provider 原生消息信封、校验与按请求替换式恢复
|   |   |-- native_history_replay.py    # 同次冻结来源的轻量native位置计划，不缓存正文，回放仍校验原行
|   |   |-- history_projection.py       # 前后台共用完整历史行选择、范围过滤和原生 metadata 保留
|   |   |-- history_seed.py             # 会话种子只读来源：冻结行加原单行投影，只在 native/text 准备边界解析
|   |   |-- history_display.py          # 从 canonical 消息投影只读恢复事件，不把问答预览代替正文
|   |   |-- host_notices.py             # 宿主提示：线程待送达提示的存取（同来源替换、按编号取走、按来源清除）与 IM 正文前渲染
|   |   |-- turn_resume_notice.py       # 被打断回合“已自动续跑”和“打断太多次、已停止自动续跑”（按插话结算分三句）提示的共用文案表（TUI 与 IM 共用）
|   |   |-- input_media.py              # owner 内容寻址原件、验证、发送编码和媒体预算
|   |   |-- history_order.py            # 按源记录恢复跨工作片顺序并去重插话显示副本
|   |   |-- input_media.py              # owner 内容寻址原件、验证、发送编码和媒体预算
|   |   |-- history_page.py             # canonical 字节边界向前分页和完整工作片分组
|   |   |-- message_stream.py           # 同账本正文与显式协商的过程检查点投影，共用 ID/字节游标
|   |   |-- task_runtime_state.py      # 后台续轮读取精确任务进度的结构化运行事实
|   |   |-- runtime.py                  # 后台主代理调度热循环：wake_queue 到期消费、三源对账(5min)、事件提前醒取消闹钟
|   |   |-- background_progress_policy.py # 无副作用的进度策略计数与确定性失败退避
|   |   |-- background_supply_backoff.py  # 会话供应冷却的进程内状态、消费守卫及恢复日志
|   |   |-- background_goal.py          # 后台 Goal 异常结算与续跑裁决，只持有精确领域和回调能力
|   |   |-- goal_progress_fuse.py       # Goal 续跑只按工具账、Goal/任务结构快照累计连续空片并复用 host notice 提示
|   |   |-- background_routing.py       # 线程及 owner 投递地址的只读选择，不消费来源或执行发送
|   |   |-- background_shutdown.py      # 原后台片的准确claim中断与物理退出/只读收口观察
|   |   |-- background_claim.py         # 后台执行领取、最终准入、共享心跳与精确 claim 结算
|   |   |-- background_recovery.py      # 按次查询权威恢复阻断，只去重日志、不缓存执行资格
|   |   |-- background_tool_policy.py   # 无副作用的后台工具目录、owner/task 收紧及展示投影
|   |   |-- background_context.py       # 后台一次性事实准备与纯渲染，保留任务范围和原预算
|   |   |-- background_history_seed.py  # 后台范围与应用视图冻结、精确消息覆盖、种子及压缩来源
|   |   |-- background_compact_context.py # 后台范围选择与同视图上下文投影
|   |   |-- background_compact_recovery.py # 后台完整候选纯投影及公共恢复器接线
|   |   |-- host_notification_scope.py # 原claim内通知的活身份、词法复用与后台运输接线。
|   |   |-- background_execution.py     # 后台单片执行、Compact 重试、原生历史保存与具名结果
|   |   |-- lifecycle_wake_event.py     # 子代理生命周期唤醒的宿主事件：从唤醒信封字段确定性投影成回合触发类型
|   |   |-- background_delivery.py      # 后台投递、canonical 回复提交、整封冻结与耐久去重
|   |   |-- control_commands.py        # CLI/IM 共用 typed slash dispatcher、task command 与状态渲染
|   |   |-- task_resources.py          # 从正式主链绑定关闭权限并组合固定子树清单，清理不再重新查树
|   |   |-- local_run_control.py       # direct 单次调用的身份发布与中断门，沿原任务锁冻结主资源
|   |   |-- goal_tools.py              # 默认可见的持续目标创建、读取与精确收口
|   |   |-- goal_binding.py            # 当前代理及直属下级的精确目标身份解析，拒绝借用父目标
|   |   |-- goal_delegation.py         # 显式子目标初始化、正常轮续接及授权停止/恢复的状态同步
|   |   |-- goal_editing.py            # 目标草稿版本比较保存，保持身份、状态、用量并投递引导
|   |   |-- goal_control.py            # 复用用户会话树授权的主子目标完整读取和编辑 API
|   |   |-- goal_recovery.py           # 用户显式恢复时修复旧目标共享任务冲突，保留源记录与任务现场
|   |   |-- goal_clock.py              # 同一会话存储的前后台共享单调时钟，避免重复累计目标耗时
|   |   |-- authority.py               # 标记会话 transcript 为当前多轮对话唯一事实源
|   |   `-- task_promotion.py          # 任务工具触发提升、完成/中断候选选择与关闭
|   |-- scheduler/                     # owner 持久 at/every/cron、原 job/run/claim 与来源或独立上下文执行
|   |   |-- execution.py               # 实际开工、独立线程、模型来源和冻结原会话交付
|   |   |-- execution_policy.py        # 执行与预检配置的唯一规范及工具 schema
|   |   |-- delivery_policy.py         # 原 job/run 通知策略规范，来源与静默开关分开
|   |   |-- policy.py                  # 错过周期、队列期限、未来周期失败退避和暂停
|   |   |-- precheck.py                # 原只读工具观察、变化判定和失败/无变化收口
|   |   `-- run_preparation.py         # 原 run 上的来源冻结、claim 复核与预检观察 CAS
|   |-- verification/                  # owner 被动验证事件、targeted/full 投影与文件写后 stale
|   |   |-- runtime.py                 # 公共工具出口读取结构化执行结果，记录验证或文件修改
|   |   |-- repository.py              # 按 owner/thread/task 保存事件和过期状态，不升级验证范围
|   |   `-- project_facts.py           # 从项目显式声明识别验证命令，不按报告文字猜测已验证
|   |-- delivery/                      # 多 IM 统一投递：registry、可信 context、reply envelope、receipt
|   |   |-- registry.py                # adapter/配置/健康/绑定/capabilities/target validator 唯一注册表
|   |   |-- attachment_content.py     # 有界 no-follow 安全读原附件，单次投递冻结 bytes，不新增持久账
|   |   |-- progress.py                # 原生可编辑进度的真实消息 ID 与结构化结果
|   |   |-- control_cards.py           # 原生私人控制卡的真实 message/chat 身份；不授予权限
|   |   `-- service.py                 # 普通回复、主动消息、可编辑进度及附件的统一发送出口
|   |-- adapter/
|   |   |-- private_controls.py        # 已验证 SDK actor/卡片身份到原 Gateway 的私人决定运输
|   |   |-- feishu_ws_observation.py   # SDK帧头及连接摘要、固定卡片阶段；不读取payload或改变ACK
|   |   |-- feishu_mentions.py         # WS/webhook 共用直接 @机器人判据，不按名称或正文猜
|   |   |-- feishu_attachment.py      # 飞书上传已核验的同一不可变字节，不重新打开路径
|   |   |-- feishu_quoted_media.py     # 准确根与同次转发成员的文字/资源证明，当前 owner/actor 不变
|   |   |-- feishu_quoted_content.py     # 引用可见文字、代码、链接与媒体的纯投影，隐藏树不读取
|   |   |-- feishu_quoted_members.py     # 同次准确转发成员闭包与来源限制，不改变主体
|   |   |-- progress_draft.py           # 单条草稿在原 pending 内合并、限频、恢复与独立最终回复
|   |   |-- delivery.py                # 原 pending/sent 外发意图、死亡核验、消息身份与 epoch CAS 回送
|   |   `-- ingress.py                 # POST 前 durable ingress、冲突隔离与单线程全链恢复
|   |-- settings/                      # AgentConfig、加载、来源账本、runtime scope config
|   |   |-- user_config_capability.py  # 用户配置路径（当前加载的配置文件）、显式白名单校验、边界名单与生效时机说明
|   |   |-- parameter_registry.py     # 参数中心登记表：每个配置字段的说明（取自 YAML 注释）、类型、分类、安全等级、生效时机
|   |   |-- constants_catalog.py      # 代码常数目录的运行时只读入口（/settings internal 与 user_config search 共用），只读随包 JSON

|   |   |-- parameter_registry.py     # 参数中心登记表：每个配置字段的说明（取自 YAML 注释）、类型、分类、安全等级、生效时机、来源文件（agent/capability/runtime_guard）
|   |   |-- parameter_metadata.py    # 参数登记表元数据自动推导（单位/范围/归属模块/读取方），只读纯函数
|   |   |-- parameter_changes.py      # 参数中心唯一写入口：按来源写对应文件、按类型写入、正式加载回读核对、修改记录、恢复默认与回滚
|   |   |-- model_profiles.py           # owner 私有模型配置唯一文件源、脱敏列表及子代理创建时引用
|   |   |-- curator_profile.py          # 固定 Curator 档案整组解析（本 owner 解析不到时改用其默认模型并留原因）；留空时只读解析各会话的主代理模型（不可用带原因退回默认），失效诊断与设置型号展示
|   |   |-- embedding_profile.py        # 嵌入档案引用解析（capability=embedding）、失效诊断、向量库身份（P13/P14）
|   |   |-- embedding_selection.py      # 语义记忆“向量模型”唯一选择入口：TUI/IM 管理员菜单与 manage_models 自配（同主机直设、异主机请用户选）
|   |   |-- decision_probe.py           # 显式原生连接测试，共用后端/worker/账本，不改开关或聊天选择
|   |   |-- decision_settings.py        # 原 owner/thread 决策覆盖共用读取、字段修改、恢复继承与双版本 CAS
|   |   |-- decision_settings_schema.py # 决策字段/范围校验及旧会话覆盖迁移，不持有默认值
|   |   |-- decision_settings_defaults.py # 投影原 Agent/能力/记忆配置默认值和继承来源
|   |   |-- decision_settings_projection.py # 原模型引用可用性与有效设置读回，不发请求、不暴露凭据
|   |   |-- decision_experiment_schema.py # 准确线程实验授权的来源/范围/期限/有限预算严格信封
|   |   |-- decision_experiment.py    # 宿主显式授权保存与撤销原语；普通模型不能建立许可
|   |   |-- model_oauth.py              # owner 登录代次、取消与刷新并发控制，复用私有配置源
|   |   |-- model_oauth_schema.py       # 授权配置、凭据目的地、私有状态与绑定校验
|   |   |-- model_oauth_wire.py         # 设备码、兑换和刷新协议的有界无重定向 HTTP
|   |   |-- thread_model_selection.py   # canonical 会话模型编号、默认初始化与逐工作片解析
|   |   |-- reasoning_effort.py         # 会话/子代理智能程度档位：线程现读、全局默认、/effort 写入、子代理继承与请求选项
|   |   |-- reasoning_probe.py          # 智能程度自动检测：/effort 自动与手动检测、后台 9 次短请求、按档案记录、确认支持后经参数中心写档案
|   |   |-- reasoning_probe_judge.py    # 智能程度检测的判定规则（纯函数）：只读 usage 的 token 字段，按 09-26 实测标定
|   |   |-- shared_model_catalog.py     # 管理员显式共享引用目录与其他用户初始模型，不复制私有连接凭证
|   |   |-- model_provider_schema.py    # provider/model v2 校验、v1 显式迁移与单份连接快照解析
|   |   |-- model_provider_operations.py # 锁内服务商/模型管理，密钥保留与显式清除
|   |   |-- model_provider_network.py   # 用户主动目录 GET/短问候（可带未保存连接），不执行工具或创建任务
|   |   |-- model_connections.py        # 按连接一次加多个模型（add_models）：连接去重、重复跳过、整批落盘
|   |   |-- model_scope.py              # 逐请求依赖绑定与选模版本；不热改已经开始的响应
|   |   |-- model_selection_status.py   # 原调用账的无正文模型、采用、阶段和缓存投影
|   |-- common/                        # 跨域小权威：safe_id、path_normalize、json_io、日志脱敏、结构化输出批处理
|   |   |-- authenticated_http.py    # 带宿主认证的固定 HTTP 请求拒绝跳转，保留原状态/超时
|   |   |-- cache_freshness.py          # 文件代次指纹与粗 mtime 窗口判断的唯一实现，供 json_io/响应轮询/子代理缓存共用
|   |   |-- cancellation.py             # UI、Gateway 和工具共用的唯一进程内取消令牌与上下文绑定
|   |   |-- config_paths.py             # CLI与worker共用默认配置路径，调用时读MY_AGENT_CONFIG且不加载配置或运行时
|   |   |-- directory_lock.py           # 原后台与安装 Store 共用的永久目录系统锁，不降级为仅线程互斥
|   |   |-- nofollow_fs.py              # 受信根内的文本/二进制读写及锁文件打开，拒绝链接路径
|   |   |-- nofollow_tree.py            # 固定目录树递归删除，拒绝顶层链接且不沿树内链接越界
|   |   |-- strict_json.py              # 包与安装表共用的唯一键、有限数及严格 UTF-8 JSON 读取
|   |   |-- text_file_window.py          # 普通/按行/字符读取共用的有界编码索引、seek 游标和版本失效
|   |   |-- file_version.py              # 观察版本与显式写前置条件；不建立任务锁或冒充内核 CAS
|   |   |-- audit_activation.py        # 显式 `/audit` 前缀 -> guarantee/window 结构化激活
|   |   `-- tool_output_paths.py       # Memory/工具共用的 owner/task 输出归档与索引路径权威
|   |-- concurrency/                   # 重试/退避（jittered backoff）、锁、per-thread 协作中断
|   |   `-- restart_gate.py            # 安全重启工具关口：排空时新副作用工具停在领取前，统计执行中工具数
|   |-- owner_object_store.py          # scale owner PG/RLS manifest + versioned S3，Pod 盘只作缓存
|   |-- scale_runtime.py               # scale role/release channel/S3 配置 fail-closed
|   |-- continuous_monitor_entry.py    # 真实 wall-clock 异构来源 proof 长守入口
|   |-- contracts/                     # 安全/协议/错误分类与格式校验合同；无旧任务质量 acceptance 判官
|   |   |-- model_call_budget.py       # 原模型调用账复用的显式实验输入预留、保守结算与同代拒绝
|   |   |-- subagent_completion.py     # Gateway/后台续片共用的中立 child 完成信封与直属结果投影
|   |   |-- tool_approval.py           # 工具审批 request/decision/binding 与跨层调用身份协议
|   |   `-- tool_input_schema.py       # 工具参数有限 JSON Schema 纠正/完整校验与脱敏问题路径
|   |-- tooling/                       # 唯一 ToolRuntime/ActionPolicy/ToolExecutor、写入边界与结果投影
|   |   |-- mcp_client.py             # MCP 配置、握手、当前连接及永久关闭；目录发布核对同一连接
|   |   |-- mcp_declarations.py       # MCP 逐工具声明表：审批只能更严、观察声明解析、按发现到的工具核对、发布结果事实
|   |   |-- mcp_transport.py          # 固定进程与出生身份、请求队列、读写线程和原进程树清理
|   |   |-- mcp_managed_process.py    # 插件 MCP 接原托管管道、资源锁准入和原生清理回执
|   |   |-- mcp_protocol.py           # 每连接独立响应箱、有界诊断、期限与结构化协议错误
|   |   |-- observation_binding.py    # 观察三件套的来源无关实现：发送前候选复核、结果铸 ID、提供方拒绝提升，插件与 MCP 共用
|   |   |-- plugin_registration.py    # 新运行中同步原插件连接，各权限视图分别投影已验工具
|   |   |-- process_output_capture.py   # stdout/stderr 有界保留与持续排空，公开截断和完整性
|   |   |-- computer_use_profile.py   # MIT 开源桌面执行器的 local/main + Full Access MCP 薄装配与 effect 边界
|   |   |-- computer_use_server.py    # 用公开 MCP 接口组合上游桌面工具、滚轮及文本输入；底层 Server 单一运行路径，按环境标记装观察接管层
|   |   |-- computer_use_observation_tools.py # J16 屏幕观察工具的接入胶水：tools/list 声明（type_into 按后端能力注册）、底层 tools/call 接管、_meta 读取与 isError+structuredContent 编码
|   |   |-- computer_use_x11.py       # Linux X11 真后端：叠放列窗、上层矩形（含 override-redirect 与边框）、mss 区域截屏（screen_region）、点击；真实库只经 load_real_x11_libraries
|   |   |-- computer_use_macos.py     # macOS 真后端（片 E）：Quartz 列窗与遮挡、权限预检、ScreenCaptureKit 单窗口截图与 mss 回退（带原因）、辅助功能预检与点击；片 G 的控件、全选、输入、删除键入口
|   |   |-- computer_use_macos_ax.py  # macOS 无障碍读取（片 G）：只查不弹窗的授权、AX 窗口匹配、广度优先限深限量读树、密码框不读值、AX 全选
|   |   |-- computer_use_backends.py  # 屏幕观察后端唯一选择入口：darwin → MacBackend，其余 → X11Backend
|   |   |-- screen_ocr.py             # 两个桌面后端共用的 RapidOCR 识别器（文字区域 → TextRegion）
|   |   |-- screen_observation.py     # 屏幕观察核心（来源无关）：选窗、采样、候选合并、快照、动作前复核、点击与输入（片 G）
|   |   |-- screen_observation_store.py # 窗口实例登记（boot + 实例号）与每窗最近 4 代快照环（只存几何、可见事实、候选 region/grid 或控件结构化事实）
|   |   |-- screen_region_digest.py   # 候选区域像素摘要口径：灰度 → 16×8 面积平均 → 16 级量化 → sha；容差判定
|   |   |-- screen_ui_candidates.py   # 候选来源无关部分（片 G）：动作名、label 清洗、控件 + OCR 合并去重与动作判定
|   |   |-- computer_text_input.py    # 按平台提交可靠文本，校验 Unicode 并保留后置验证边界
|   |   |-- models.py                 # ToolModelSpec、ToolRuntimePolicy、ToolRuntime/Snapshot 与 handler outcome
|   |   |-- segment_planning.py       # 执行轮与嵌套派发共用的纯并发段和批上限规划，不依赖agent_core
|   |   |-- runtime_contracts.py      # 唯一 canonical ToolCall/ToolResult、ToolChoice、协议与 operation 合同
|   |   |-- runtime_facts.py          # 工具回复、归档索引及恢复共用的有界执行事实（含停机关门原因的唯一清洗口），不依赖 Agent
|   |   |-- tool_search_state.py      # 主/子/Gateway 共用的纯工具发现归档投影，不导入执行循环
|   |   |-- input_schema.py           # 唯一 input_schema 规范化、强类型纠正和完整执行前校验
|   |   |-- action_policy.py          # 副作用前唯一 allow/ask/deny 聚合决策
|   |   |-- executor.py               # approval、sandbox、handler、账本、核对、持久化与投影状态机
|   |   |-- event_observation.py      # B4 单次工具真实 handler 观察状态；拒绝/重放不发，重试只发一对
|   |   |-- liveness.py               # 真实 handler 的单调运行时间心跳；不调用模型，不拥有执行权
|   |   |-- plugin_gate_policy.py     # B5宿主收紧、原批准源精确引用、同源确认/拒绝/无人审批回执，不改参数或授予新批准
|   |   |-- runtime_boundary.py       # task 相对路径归一与精确读边界检查
|   |   |-- workspace_read_scope.py   # 沿原 exact 与墙外授权生成本次 cwd 内的读取上界
|   |   |-- workspace_write_scope.py  # 按原写入边界解析生成本次写入范围，无范围时只允许 cwd
|   |   |-- capabilities_tool.py      # 从真实工具目录与唯一 channel registry 投影模型能力
|   |   |-- _filesystem_display.py   # 文件工具共用的有界 diff/write 富终端展示事实构造器
|   |   |-- file_syntax_diagnostics.py # 原子候选同源的有界语法观察及成功写入后的软反馈
|   |   |-- _persona_write_guard.py   # SOUL/USER/AGENTS 统一强制走 update_persona
|   |   |-- background_process_launch.py # v3 预留、原权威复查及 v4 日志/stdio 启动交接
|   |   |-- background_process_host.py # 独立绑定 child、直接继承管道、检查寿命并提交真实终态
|   |   |-- process_scope.py          # 访问、任务执行与共享激活的独立身份合同
|   |   |-- process_registry.py       # 受保护记录的进程缓存、水合、PID 身份核对与完整后代树终止
|   |   |-- process_session_store.py  # 原 session 地址的互斥读写、版本 CAS、精确停止清单与裁剪
|   |   |-- process_cleanup_evidence.py # 原退出证明与固定身份摘要，原锁下消费前核验
|   |   |-- process_session_records.py # v1/v2 原版本读取及 v3 任务/激活归属、实例与单调状态合同
|   |   |-- process_session_commit.py # 原版本 v2/v3 批次的 redo 预检、安装恢复及提交回执
|   |   |-- process_session_cleanup.py # 冻结实例的精确清理、原账完整退出证明与提交异常回执
|   |   |-- process_resource_stop.py   # 主控制冻结后台清单和 PTY 请求，锁外清理只消费原回执并保留部分错误
|   |   |-- process_network_status.py # exact 受管进程树监听、防火墙显式规则与外部探针边界的只读投影
|   |   |-- process_sessions.py       # owner+TUI 会话隔离的后台命令查询、等待与停止工具
|   |   |-- gateway_status.py         # 本机管理员读取唯一 Gateway 身份、端点、队列和本生命周期日志摘要
|   |   |-- gateway_restart_tool.py   # restart_gateway：管理员主代理安排 Gateway 安全重启，只写请求立即返回
|   |   |-- user_config_tool.py        # main_agent 专用：读生效值/来源，写白名单项并报告生效时机
|   |   |-- audit_records_tool.py      # 统一只读审计工具：topic 枚举（decision、requests）、本人范围，管理员明确许可才跨用户
|   |   |-- audit_requests_topic.py    # audit_records 的 requests 主题：请求成败、错误码与处理建议、归属 owner，管理员附带身份事实
|   |   |-- admin_controls_tool.py     # 管理员专用：list/set 各用户 Jev/审计开关与跨用户审计许可，每次都要本人确认
|   |   |-- package_build_tool.py      # learnpack package_build：本机管理员把整理好的目录打成能力包/插件包（只打包不安装，默认收起）
|   |   |-- skill_summarize_tool.py    # learnpack "让她总结一下"：按 target 参数选落点，current 按会话任务打标记、收尾时总结，previous 当场总结这个会话刚做完的那次活（默认收起）
|   |   |-- package_install_tool.py    # learnpack package_install：按自动装开关装她打的包，关着只开待确认安装单并给用户一行确认（默认收起）
|   |   |-- shell.py                  # 非交互 run_command、独立 stdin、超时/中断与有界 pipe drain
|   |   |-- shell_syntax.py           # 外层及字面 Shell -c 的后台语法检查，不解释普通字符串或 heredoc 正文
|   |   |-- tool_input_completion.py # 明示安全默认值、可信上下文补参与脱敏 source/source_ref
|   |   `-- sandbox.py                # bwrap 唯一策略、自检、worker/K8s readiness 硬门；含插件进程用的整根只读形态
|   |-- capability/                    # 单一 SkillsService、逐轮 snapshot、能力路由与 capability tools
|   |   |-- package_read.py            # 原工具与宿主共用正文分页、页验收和任务pin
|   |   |-- package_selection.py       # 有界元数据与原后端的一次结构化选择
|   |   |-- package_selection_failure.py # 选择辅助调用失败时的结构化原因，不含正文
|   |   |-- package_selection_scope.py # 新主任务选择资格和typed pending初值
|   |   |-- package_selection_authority.py # 原策略门和精确执行轮复查
|   |   |-- package_selection_context.py # 入口总预算、来源与续页装配
|   |   |-- package_selection_runtime.py # 原主业务首轮的一次选择准备
|   |   |-- subagent_package_entries.py # 子代理原首请求内的已授权同代包入口准备
|   |   |-- subagent_entry_authority.py # 子入口新建材料及当前run/attempt的只读授权复核
|   |   |-- decision_candidates.py     # 原能力快照候选、独立适用性题及必要引用，不读取Skill正文
|   |   |-- decision_recommendation.py # 每工作片可选推荐及采用前复核，只改变展示不改授权
|   |   |-- decision_experiment_sample.py # 只观察实验的对照条目：基线/候选名单、配置版本与原账结算视图
|   |   |-- skill_service.py           # bounded builtin/shared/owner/workspace discovery、policy 与缓存
|   |   |-- skill_snapshot.py          # 不可变稳定引用、正文 hash/guard 校验与子代理收窄
|   |   |-- skill_proposals.py         # 自学习 S1：子代理 lesson 候选→Skill 提案→确认链（用户 CLI 或自学习开启时自动）经 guard 原子安装
|   |   |-- skill_learning.py          # 自学习 S3 服务：收口入队、后台运行锁/前台让路/每日上限、无工具结构化总结、重试与账本
|   |   |-- skill_learning_request.py  # S3 触发判据（结构化）与有界脱敏请求材料、本轮用过的 skill_id
|   |   |-- skill_learning_prompt.py   # S3 提示词、严格 JSON schema 与输出解析（frontmatter 无损规范化）
|   |   |-- skill_learning_publish.py  # S3 自动闸门与发布：重名/删过的名字/上限/所有权 hash/脱敏/解析/guard，回滚与删除
|   |   |-- skill_learning_store.py    # S3 唯一落盘权威：请求队列、registry.json、ledger.jsonl、版本全文、removed 归档与锁
|   |   |-- skill_usage_ledger.py       # owner使用明细与汇总：区分搜索展示、正文读取、宿主注入和采用；有效使用仅正文读取与注入
|   |   |-- skill_learning_report.py   # 自动总结 Skill 的状态推导与用户回滚/删除，CLI 与聊天 /skills 共用
|   |   |-- decision_skill_proposal_review.py # 自学习 S2：`skills proposals list` 的可选审核顺序点，只重排展示并加宿主标签，从不确认/拒绝/写入
|   |   |-- persona_repository.py      # owner SOUL/USER/AGENTS 受控加载、版本/CAS/回滚唯一入口
|   |   |-- model_profile_tool.py      # manage_models：主会话代理自助增删改切 owner 模型目录，复用唯一配置服务，回执不含密钥
|   |   |-- channel_message_target.py  # 当前 owner 与本机管理员绑定目标的唯一外发解析
|   |   |-- channel_message_tool.py    # 当前 owner 的统一 send_message；登记产物经原生通道发送
|   |   |-- memory_search_tool.py      # memory_search：主模型只读检索本人正式长期记忆（默认关），与自动召回同范围、不写访问信号
|   |   |-- session_search_tool.py     # session_search：本地历史检索/翻看/浏览，外加按 message_id 读原文与当前会话检索
|   |   `-- session_history_read.py    # session_search 的会话原文读取：按消息编号分段读回、当前会话按页浏览，只读 canonical 消息文件
|   |-- prompting_parts/               # prompt 构造
|   |   |-- builder.py                 # 完整 prompt 与 native 三段追加式缓存布局构造
|   |   |-- cache_layout.py            # typed 稳定 system/user、动态尾部与完整字符串投影
|   |   |-- thread_persona.py          # 线程唯一安全渲染快照、结构化断点与人格变化动态尾巴
|   |   `-- memory_context.py          # 非权威、可转义且可统一剥离的召回记忆信封
|   |-- scale_downstream.py            # scale worker 复用普通 gateway 会话执行主链
|   `-- backends/                      # 模型后端适配、run 固定协议/tool_choice、原生工具历史与结构化生成
|       |-- oauth.py                   # 原三种模型协议的 OAuth 认证层，不新建执行循环
|       |-- oauth_transport.py         # 带凭据请求的重定向拒绝，登录和模型共用
|       |-- provider_headers.py        # 自定义头保护、owner/thread 稳定会话头及 endpoint 拼接
|       |-- request_content.py         # 摘要分段前的文字完整性判断，非文本引用不冒充正文
|       |-- request_scope.py           # 前台模型端点占用与后台单次预算；不保存正文和持久状态
|       |-- gateway_request_limits.py  # 原 HTTP 请求的绝对期限、有限读取与严格 JSON 校验；许可请求须零重试/禁重定向/有期限
|       |-- provider_send_gate.py      # 传输层发送许可接口：最终请求事实与 ProviderSendRefused，不读设置或账本
|       |-- transport_timing.py        # HTTP 尝试分段计时（建连/代理隧道/TLS/发送/首字节/读正文），观察者显式开启才计，只量不改发送；复用连接记跳过建连
|       |-- keepalive_transport.py     # 严格请求（决策调用）的长连接池与保活打开：与 urllib 同口径的代理/超时/守卫，只有干净读完才归还
|       |-- bounded_call.py            # 从 Curator 迁出的唯一有界调用，保留未退出 worker/清理资源
|       |-- decision_protocol.py       # 决策输入快照、宿主来源/版本绑定及逐题响应，不拥有业务执行权
|       |-- typesafe_decision.py       # Jev 原生 decide 适配（无网络 prepare + 必须持许可的 send），不接聊天生成接口
|       |-- typesafe_decision_wire.py  # TypeSafe 题目/结果校验，未知用量保留缺失；jev_wire_bytes.v1 经验输入上界
|       |-- cache_diagnostics.py       # 出站请求摘要及前缀变化诊断，不存正文或改变缓存布局
|       |-- sampling.py                # top_p 校验与精确端点 V4 Flash 采样默认，不改变身份或重试
|       |-- reasoning_control.py       # 智能程度档位与模型思考控制方式（effort/budget/none）的唯一换算点，只对实测供应商给默认
|       |-- structured_output_mode.py  # 结构化输出方式（native/json_object）的唯一换算点；只对实测不接受 json_schema 的供应商改用 json_object
|       |-- responses.py               # Responses 协议生成入口，复用正式 HTTP/取消/超时主链
|       |-- responses_wire.py          # typed SSE/items 与既有工具历史映射、加密 reasoning 回放
|       |-- responses_websocket.py     # ChatGPT 订阅 Responses 的 WebSocket 传输（长回复不断线），事件交 responses_wire
|       |-- responses_failure.py       # Responses 失败事件按服务商错误码分类（上下文超限／额度／限流／临时故障／其余）
|       |-- anthropic_prompt_cache.py  # Anthropic tools/system/最新 history 断点与追加式 user 投影
|       |-- base.py                    # 模型响应、冻结选项和公共后端接口；本地 echo/缺配置实现
|       |-- http.py                    # HTTP 传输、请求局部控制、工具探针与模型目录读取
|       |-- openai_chat.py             # Chat Completions 请求、原生历史、思考和流式结果转换
|       |-- request_content.py         # 原始内容的文字计量适用性，未知模态保留原请求
|       |-- vision_capability.py       # 视觉能力事实：8×8 纯色图结构化探针、进程级缓存（端点+模型+api_base，同键单飞）
|       |-- anthropic.py               # Messages 请求对象、缓存布局、思考及工具结果转换
|       |-- wire_contract.py           # 出站协议合同：三个后端出口先修整历史副本（空块、结果紧跟调用、孤儿结果），再按协议校验，不合规本地报错不发送
|       |-- factory.py                 # 显式配置构造唯一后端，缺配置判据与调度共享
|       `-- tool_protocol_adapter.py   # native 事件或显式完整 text 帧到 canonical ToolCall 的唯一适配口
|-- tests/                             # 单元、集成、真实链路回归
|   |-- test_background_input_recovery.py # T 候选：失败 attempt 的提交前输入精确释放
|   |-- test_background_steer_gateway.py # T 候选：真实后台工作片的 Gateway 插话与停止
|   |-- test_gateway_execution_target.py # T 候选：前后台活动目标、旧快照与 claim/attempt 归属
|   |-- test_gateway_input_target_lifecycle.py # T 候选：输入绑定生命周期与同名旧请求隔离
|   |-- test_tui_input_ordering.py      # T 候选：outbox 顺序、未知首条与迟到回执
|   |-- test_runtime_model_selection_refresh.py # 真实 Gateway/工具配假 HTTP 的安全点选模及配置竞态
|   |-- test_model_selection_history.py # Compact 原来源恢复、媒体保留和工具不重放
|   |-- test_scheduler_execution.py    # 独立历史/模型、实际开工与原会话冻结交付
|   |-- test_scheduler_policy.py       # 可控时钟的错过周期、排队期限及失败策略
|   |-- test_scheduler_precheck.py     # 真实工具执行器的变化、失败、取消、恢复及检查点
|   |-- fixtures/decision/jev_capability_rounding.json # 合成材料真实Jev响应的脱敏概率舍入replay，不含凭据
|   |-- test_skill_proposals.py         # 自学习 S1：默认关闭、幂等提案、迁移不碰、确认拒绝矩阵、快照可见、runner 自动确认与 CLI 往返
|   |-- test_parameter_registry.py     # 参数中心登记表：覆盖全部字段、YAML 说明（上方注释优先，其次行尾注释）、空说明基线、安全等级、凭据脱敏、搜索排序、派生实际效果、常用层级守卫
|   |-- test_constants_catalog.py      # 常数目录守卫（P10）：目录与源码一致、待整改白名单只短不长、协议类排除、单位/类别/扫描样例、运行时搜索
|   |-- fixtures/constants_catalog_pending_fixes.json # 常数目录待整改白名单：无单位后缀或无上方中文说明的历史遗留（只短不长）
|   |-- fixtures/parameter_description_baseline.json # 说明为空的参数基线名单（按原因分组）；名单外新增或名单里已有说明/已删除都让测试失败
|   |-- test_structured_masking.py     # 字典/列表参数结构脱敏：请求头与环境变量只留键名，任意开关后的“名字: 值/名字=值”只留名字，凭据开关、连接串与网址密码遮值，各回显与记账出口无明文
|   |-- test_value_display_parity.py   # 回显值文字一致：8 种值（False/0/True/None/空串/空列表/空映射/凭据）在 user_config 查看/搜索/改参回执、/settings show、config get 下显示相同，聊天空值显示“（空）”，走不到的格子核对原因
|   |-- test_parameter_changes.py      # 参数中心写入：按类型写入并真正生效、拒绝不改文件、回读不一致回滚、记录与回滚链、user_config 工具
|   |-- test_config_write_permissions.py # 配置/账本写回保留原权限（600 不被放宽）、参数中心新建文件 0600、配置写回不用固定 .tmp 名
|   |-- test_parameter_sources.py      # 参数中心来源维度 P17：四来源 search/view、运行值真实读文件与 owner 覆盖值、capability 写运行时文件且运行时入口读到新值、文件缺失时新建、只读来源 PARAMETER_SOURCE_READ_ONLY 拒绝、回读失败恢复/删除、边界拒绝、user_config 工具链路
|   |-- test_settings_chat_control.py  # 聊天 /settings：解析、Gateway 分派、TUI 还原与本地拒绝、仅管理员、常用/全部两种视图、完整修改与回滚流程
|   |-- test_plugins_chat_control.py   # IM 插件公共解析、共享服务、权限错误码、启用确认和持久控制重放
|   |-- test_config_field_readers.py   # 每个 AgentConfig 字段都必须有读取方（属性访问、字符串键或决策设置映射），防止死配置
|   |-- test_merged_config_knobs.py    # 参数减量第 2 批：11 组合并后的单一旋钮语义（空/0/数字）、旧键只告警不转值、随包 YAML 与默认值一致
|   |-- test_param_reduction_e_group.py # 参数减量第 3 批 E 组：14 个内部参数降为常量（值不变）、旧键只告警、注册表用常量、模型可见提示不再指向已删的键
|   |-- test_embedding_service.py      # 嵌入服务合并：记忆与工具共用 embedding_*（模型/端点/key 链同一条），两个开关各管各的，旧 tool_/memory_embedding_* 只告警不转值，登记表归“模型请求”
|   |-- test_vector_identity.py        # P14 向量空间身份：端点/协议决定空间、凭据不进身份、接线一次解析且身份缺失关通道、读写裁决、重建入口只认本机管理员（拒绝时不嵌入不改文件）
|   |-- test_vector_snapshot_consistency.py # P14 单文件快照：换代重载、锁内重读、交错与跨进程锁、重建全有或全无且计数如实、维度以实际向量为准
|   |-- test_model_output_cap.py       # 输出上限统一 64K：常量/YAML/dataclass 同值、按已知窗口一处夹取、默认模型同规则、vision 死配置已删
|   |-- test_constant_names_unique.py  # 同一数值常数名只在一个模块定义；确属不同含义的列白名单写原因，过期条目也失败
|   |-- test_ci_workflow_paths.py      # CI 工作流里点名的测试文件必须存在，删除或改名测试时同批改工作流
|   |-- test_runtime_write_guards.py   # 正在运行的安装目录写保护：按进程事实认定、开关、文件工具与 Shell 只读、Full Access 仍可写外部目录
|   |-- test_skill_chat_control.py     # 聊天 /skills：解析校验、Gateway 分派不落入 stop、TUI 文本还原与本地拒绝、提案确认版本、自动 Skill 回滚删除
|   |-- test_skill_learning.py          # 自学习 S3：触发判据、请求有界脱敏、create/update/skip、各闸门拒绝码、上限、重试、忙时顺延、回滚删除
|   |-- test_reasoning_effort.py        # 智能程度：换算与优先级、两种协议真实组包、线程档位、投影一致、子代理继承、/effort、档案字段与配置
|   |-- test_anthropic_reasoning_budget.py # 空预算区间：工厂边界、非流/流式出站与完整投影、控制服务及参数中心回执同源、正常大上限、HTTP 默认禁外发
|   |-- test_reasoning_probe.py         # 智能程度检测：回放标定与阈值、自动触发条件、出站字段、检测请求带输出上限与回执按事实、截断下判定仍正确、写档案与撤销、管理员门槛、记录失效、凭据不外泄
|   |-- test_probe_tool_capability_metering.py # 工具能力探测计量：purpose=probe:tool_capability 独立成桶、每次真实 generate 记账、缓存命中不重复、无 scope 不记账、失败记 failed
|   |-- test_host_notices.py            # 宿主提示：存取与清洗、模型看不到、Gateway 发布与提交即已读、失败保留、飞书正文前、TUI 灰行、历史回放、检测结论
|   |-- test_steer_delivery_recovery.py # TUI 插话：失败调用按编号退回并重提交、超时与瞬断重试只见一次、隔离 Gateway 端到端、定时任务目标终态后排到下一轮
|   |-- test_structured_output_mode.py  # 结构化输出方式：方式解析、DeepSeek 用 json_object 且 schema 进提示、其余仍 json_schema、档案字段、缓存键、TUI 与配置
|   |-- test_skill_learning_integration.py # 自学习 S3 接线：组合根装配、收口入队、Gateway 策展车道准入、learned CLI、S1 自动确认与配置
|   |-- test_subagent_lesson_ledger.py  # record_lesson：身份与 Schema、字段/条数/字节上限、幂等、账本复核、结果合并、候选与 S1 提案、暴露面与提示
|   |-- test_decision_skill_proposal_review.py # 自学习 S2 审核顺序点：资格边界、别名与脱敏、逐题校验、采用前复核、取消传播、零写入
|   |-- test_decision_cooldown_persistence.py # 冷却持久化：快照恢复/落盘、过期丢弃、坏文件回退、S2 入口落盘 .cooldown.json
|   |-- test_decision_skill_proposal_review_integration.py # 自学习 S2 经真实 CLI/设置/决策服务（只替换 HTTP）：输出字节、observe 记账、冷却超时、菜单与默认值
|   |-- test_decision_model_profiles.py # 决策用途隔离、旧目录迁移、共享撤销与生成选择不退化
|   |-- test_model_usage_tags.py       # 模型用途标签：规范化与拒绝、决策模型不收、不进运行时配置、两处决策候选与 /model 表单
|   |-- test_decision_model_operations.py # 原模型操作中的决策设置、原生HTTP测试和用量结算
|   |-- test_decision_gateway_transport.py # 原菜单运输到本地Gateway服务和原生HTTP的组合
|   |-- test_decision_settings_scope.py  # owner后台范围、前台覆盖隔离和历史覆盖清理
|   |-- test_tui_decision_menu.py        # 真实按键的决策设置、CAS、取消和显式测试
|   |-- test_user_config_decision_operations.py # 主代理读取已保存决策目录及显式测试，拒绝伪造身份
|   |-- test_user_config_decision_patch.py # decision_patch 经真实执行器落盘、多带字段仍拒并写明 unknown_fields、假模型自然语言调等待时间
|   |-- test_decision_settings.py       # 决策覆盖继承、双版本 CAS、权限隔离及原配置迁移
|   |-- test_decision_settings_reason.py # decision_patch/reset 接受可选 reason：记录与展示、200 字截断、多余字段仍拒、机器逻辑不受影响
|   |-- test_decision_settings_notifications.py # 设置逆序通知、覆盖恢复继承及精准取消
|   |-- test_decision_service.py        # 决策阶段预算、冷却、设置复核、关闭与旧请求隔离
|   |-- test_decision_background_deadline.py # 后台点位各自完整计时、前台保留阶段上限、调用方期限取更小（假时钟）
|   |-- test_decision_service_http.py   # 原配置到真实本地 HTTP、账本与活动用量行的组合
|   |-- test_decision_observe_nonblocking.py # observe 转后台：主链路先返回、apply 仍同步、有界队列、停止/撤销/停机、独立用量、选模型补记
|   |-- test_decision_audit_controls.py # 管理员控制存取/失败关闭/只许管理员写、审计工具范围与时间窗、观察白名单与跨用户许可
|   |-- test_decision_fault_matrix.py   # 决策故障矩阵：断网/DNS/TLS/额度/计费/5xx/慢响应的冷却与恢复、同 owner 多会话并发
|   |-- test_decision_selection_cadence.py # 选模型询问节奏：字段与默认关、结构没变跳过、换模型/目录变化/新会话重问、失败不记指纹、会话层覆盖、文件有界
|   |-- test_decision_outcome_log.py    # 决策结果日志：只记结构化字段、有界、按窗口汇总，decide 的成功/超时/点位冷却落日志，审计按点位报告，成功行记请求名与实际版本
|   |-- test_decision_transport_timing.py # 假代理/假 TLS/假服务端逐段注入延迟：分段计时与超时阶段落结果日志、估算输入入 model_usage、零重试不补发
|   |-- test_keepalive_transport.py     # 决策长连接复用：一条隧道/一次握手、复用记 0 毫秒建连、错误与中止不归还、过期与对端关闭丢弃、开关
|   |-- test_decision_stats_display.py  # 决策统计口径：TUI 已报/估算（未完成）/缺报分开、未发出单列，结果日志与审计把没发出去的失败单列
|   |-- test_compact_remote_provider.py  # 服务端压缩：触发项/压缩项线层、远端助手回退、检查点与视图兼容性、live 摘要走远端
|   |-- test_midturn_compact_prefix.py  # 回合中途密文/文本摘要两回合前缀延伸与辅助压缩同源投影
|   |-- test_compactcall_websocket.py  # 订阅WS完整头体、自动preflight/live缓存路由和粘性工具边界
|   |-- test_transcript_compact_prefix.py # 同源连续覆盖、工具配对与当前宿主媒体保护
|   |-- test_compact_prefix_fallback.py # 容量优化可解除与原小窗口分段可用性
|   |-- test_compact_prefix_lifetime.py # 前缀容量下界、校准边界与发布前旧历史释放
|   |-- test_compact_single_empty_repair.py # 完整空摘要有界一次重试及异常不重试
|   |-- test_context_usage_binding.py # 真实采样绑定、迟到CAS与跨store/thread隔离
|   |-- test_f7_request_surface_contract.py # 三协议缓存表面、宿主图与当前工具权限
|   |-- test_f7_usage_accounting_contract.py # 主摘要分账、未知缓存与物理尝试和耗时
|   |-- test_f7_queued_compact_contract.py # F6媒体摘要经F2公平队列到原checkpoint同链
|   |-- test_thread_tool_discovery.py  # 线程累计加载的兼容序列化、身份隔离和原子恢复
|   |-- test_tui_status_line_display.py  # 状态行布局（2026-10-08）：总缓存两位小数、累计会话只算输入、决策（关闭/观察/实际）、速度按整次调用时长、Context 行模型名与思考档位
|   |-- test_compact_cache_surface_fit.py  # 压缩请求装进窗口：输出预留同源、超预算先瘦身再单次、Responses configuration_update 降档、会话累计命中率
|   |-- test_decision_reach_counts.py   # 到达计数：进程内累加、节流合并不覆盖、7 天修剪、开关与写失败、阶段原因与大白话；导出各点位测试共用的 reach_counter
|   |-- test_capability_presentation_observation.py # 能力推荐观测进 Gateway 请求记录：一回合一条、采用/保留原因、失败码、写入上限与失败语义
|   |-- test_decision_cooldown_backoff.py # 决策连接连续失败的冷却翻倍、并发同次故障不加级、成功/显式重试复位
|   |-- test_decision_owner_scope.py    # 用户后台run/空thread、原身份冲突、后台期限与配置隔离
|   |-- test_decision_subagent.py      # 原根/递归创建、有工具真实快照、逐项模型选择及幂等
|   |-- test_subagent_first_request_selection.py # 首发送模型资格、原子采用、撤销竞争与失败保留
|   |-- test_model_scope_dependencies.py # 候选依赖视图、作用域生命周期与后续工具轮协议一致性
|   |-- test_thread_model_selection_revision.py # 原线程选择版本、显式同值覆盖及旧记录迁移
|   |-- test_model_profile_catalog_generation.py # 原模型目录/共享发布代次、迁移、跨进程与锁竞争
|   |-- test_decision_recall.py        # 预算先行、固定保护槽位、来源撤销及原完整准备入口复用
|   |-- test_decision_pre_recall.py    # 补充查询的关闭、非选择、预算、撤销、取消与原阶段共用
|   |-- test_decision_planning.py      # Todo 优先建议、精确 ID/版本/取消、设置与原 read 回执的回归
|   |-- test_gateway_model_observation.py # Gateway 主模型观察的零副作用、请求身份与恢复边界
|   |-- test_gateway_model_adoption.py # 原 Gateway/PromptBuilder/provider builder 的采用、容量、竞态与零 HTTP 回退
|   |-- test_invalid_tool_arguments_e2e.py # 参数无效有界纠正端到端：坏参数回灌纠正续跑、合法调用执行一次、连续超限按 MODEL_TOOL_ARGUMENTS_INVALID 失败
|   |-- test_gateway_compact_deferred_source.py # 只读压缩来源、坏原文及当前未完成后缀排除
|   |-- test_subagent_compact_recovery.py # child真实runner到HTTP载荷等价及取消/冲突/摘要故障隔离
|   |-- test_subagent_compact_recovery_continuation.py # child恢复后真实工具轮、活动归档提交与其它child隔离
|   |-- test_gateway_compact_recovery.py # 完整恢复请求与真实HTTP对照、CAS/取消/摘要故障不发送业务
|   |-- test_gateway_compact_recovery_continuation.py # 新历史沿后续工具轮保留，活动轮先完整准备和计量再CAS
|   |-- test_compact_request_projection.py # 原候选回退携带对应材料，未知完整输入不降级粗估
|   |-- test_decision_external_material_order.py # 已归档页安全投影、来源复核、取消与 text/native 提示一致
|   |-- test_external_material_order_integration.py # 原页面生产归档、决策 worker、设置工具与 TUI 接线
|   |-- test_decision_delivery_quality.py # 交付复核焦点资格、脱敏输入、非选择、来源复核、取消与 text/native 同段提示
|   |-- test_decision_delivery_quality_integration.py # 真实验证账到 _record_tool_call、原设置默认与 TUI 入口的组合
|   |-- test_decision_delivery_stale.py # 成功写入 stale、单焦点不请求、共享去重、失效、observe 和归档/worker 离线接线
|   |-- test_decision_curator_plugin_concurrency.py # 后台 curator 与前台 skill_tool 同连接并发：互不拖住、撤销命中对应点、超时只冷却本点而连接故障冷却全部、关闭一起取消
|   |-- test_gateway_decision_shutdown_cancel.py # Gateway 停止时主动取消在途决策：回原方案、不进冷却、关闭后不再联网、收尾顺序与失败隔离
|   |-- test_gateway_background_slice_shutdown.py # 后台片停止、物理晚退/UNKNOWN与独立资源不误停回归
|   |-- test_gateway_model_call_shutdown_settlement.py # Gateway 停止排空后把仍在途的模型调用记为被停机中断、未结算，并写结构化停机事件
|   |-- test_late_response_tool_fence.py # 停机关门后迟到的模型响应里的工具不启动，记 HOST_SHUTDOWN_TOOL_NOT_STARTED，原因码与账本同源；关门后不再弹审批卡，档案/索引共用清洗（I3）
|   |-- test_gateway_background_sessions_shutdown.py # Gateway 停机时只读列出仍存活的受管后台会话并写事件/state 计数，不停进程
|   |-- test_gateway_host_identity.py # 主机名变化后 SIGTERM 仍能停网关：信号用启动身份、host_id 进程内缓存、macOS 硬件 UUID、跨进程核验表现
|   |-- test_tool_operation_host_identity.py # 工具操作持有者同主机判定用 process_host_id：主机名变化后本机死进程可接管、老主机名记录等租约到期
|   |-- test_plugin_catalog_digest_stability.py # 激活目录摘要跨声明版本稳定：可选字段为空不改变旧安装的 catalog_sha256
|   |-- test_background_listen_scope.py        # 后台服务默认只监听回环：授权键、approved_owner 面板与 owner 授权存储、host 按 socket 表回收越界服务
|   |-- test_decision_action_candidate.py # 动作候选资格、隐私、非选择、新鲜度与来源复核、取消、text/native 同段与设置入口
|   |-- test_decision_action_execute.py # J16 片 D：粗位置、假 Jev 四档、自动执行各生效条件、两本账 actor/decision_ref、审批/失败/中断只记不重试
|   |-- test_decision_capability_consumer.py # 原设置/worker/循环接线到实际prompt/schema减量及失效原输入
|   |-- test_decision_capability_provider_grouping.py # 能力推荐按结构化provider_id按插件出题、选中展开与整体延迟
|   |-- test_jev_candidate_projection.py # 发给 Jev 的能力候选白名单投影、按转义字节截断与计数、53 题最坏情况落进经验上界
|   |-- test_decision_capability_http.py # 能力消费者经本地原生HTTP的成功/期限/在途设置变化
|   |-- test_decision_skill_projection.py # Skill名卡动态投影、原搜索可达及None旧字节等价
|   |-- test_tool_presentation_projection.py # 工具schema收起、原搜索恢复及真实插件撤销
|   |-- test_tool_default_deferral.py # 工具自己声明的默认收起：开关、索引、搜索找回、保护条件与决策投影并集
|   |-- test_ctxopt_prompt_persona.py # 核心系统提示词去重、版本化AGENTS种子迁移与人格全段保护回归
|   |-- test_ctxopt_self_map.py # 动态能力族计数、多租户隔离与未知技能目录状态回归
|   |-- test_ctxopt_background_context.py # 后台上下文总预算与各节配额上限回归
|   |-- test_ctxopt_goal_prompt_revisions.py # Goal与Gateway请求提示按版本指纹去重回归
|   |-- test_ctxopt_loop_support.py # 正式记忆全信封预算与后台续跑工具按需加载回归
|   |-- test_ctxopt_router_method_carry.py # 技能目录全信封预算与任务内方法指引跨轮去重回归
|   |-- test_ctxopt_runner_prompts.py # 子代理提示去重与恢复提示精简回归
|   |-- test_ctxopt_c7_domain_reducer.py # 领域感知工具输出摘要器（命令/进程/代理树）回归
|   |-- test_ctxopt_c7_schema_contracts.py # 重型工具说明收紧与单一动作Schema合同回归
|   |-- test_ctxopt_c7_shared_helpers.py # 跨模块共享纯函数抽取等价性回归
|   |-- test_ctxopt_c7_tail_reader.py # 有界反向块读取器大文件尾行读取回归
|   |-- test_ctxopt_pr11_outer.py # 便携Python根、飞书云动作单一事实源与分级创作快路径回归
|   |-- test_ctxopt_authoring_fastpaths.py # 内置创作与评审Skill风险分级快路径文案合同回归
|   |-- test_ctxopt_feishu_operations.py # 飞书云操作读写表与工具Schema同源回归
|   |-- test_ctxopt_script_contracts.py # 脚本描述符必填提取键、非空输出校验与AccessTarget对齐回归
|   |-- test_ctxopt_skill_catalog.py # 统一Skill目录根、安全Frontmatter解码与逻辑引用解析回归
|   |-- test_ctxopt_skill_snapshot_refresh.py # 任务内新建Skill受控快照代次刷新回归
|   |-- test_ctxopt_store_commit_contract.py # 脚本与工作流Store提交前预检与提交后读回错误分类回归
|   |-- test_ctxopt_workflow_host.py # 工作流宿主真实分析器注入、步骤输入透传与暂停续跑回归
|   |-- test_context_tool_discovery_order.py # 跨轮与Compact工具发现顺序稳定保持回归
|   |-- test_tool_default_deferral_chain.py # 默认收起的真实链路：tool_search 后下一次请求带上并执行（本机与飞书 owner）
|   |-- test_tool_description_trim.py # 工具说明只删重复：create_subagents/remember/user_config 结构不变
|   |-- test_tool_request_projection.py # 完整冻结请求与实际原生出站等价、未知输入及无副作用
|   |-- test_gateway_capability_compact.py # 同片能力展示沿 Gateway/子代理 Compact 和续跑保留
|   |-- test_subagent_capability_compact.py # child 同轮展示沿真实 Compact 重试保留、失效清除与新轮重置
|   |-- test_background_prepared_context.py # 后台重复纯渲染无写账、冻结输入、任务范围与窄审计隔离
|   |-- test_native_history_projection_memory.py # 原生历史嵌套复制峰值、canonical及输出别名隔离
|   |-- test_compact_checkpoint_stream.py # 旧摘要读取内存、JSONL兼容、改写及文件关闭回归
|   |-- test_compact_scoped_checkpoint.py # 交错作用域、摘要基础、局部CAS、版本篡改与精确覆盖
|   |-- test_compact_scoped_transcript.py # 局部历史来源、交错摘要基础与竞争CAS
|   |-- test_applied_compact_context.py # 同一应用视图的参数传递、工具过滤和摘要注入
|   |-- test_background_scoped_compact.py # 独立任务与窄事件原Store压缩及实际模型材料回归
|   |-- test_background_context_budget.py # 后台上下文预算只估算将渲染的节、有种子时不计最近消息及节集合合同
|   |-- test_background_compact_recovery.py # 后台transcript及活动归档候选和实际HTTP材料对照、失败不恢复发送
|   |-- test_background_active_turn_carry.py # 生命周期唤醒片按精确请求编号续接前台轮工具事实：owner记录只进运行时状态、同内容派工判重复、预算不缩水
|   |-- test_background_child_control_tools.py # 后台整合档与goal子代理档从直属管理面唯一定义派生（含list_agents）、原顺序不变、收紧只做减法
|   |-- test_background_reply_after_foreground_failure.py # 前台 USER_REPLY_UNAVAILABLE 后，子代理结束的后台收尾回复仍落 canonical 并进 TUI 后台消息页
|   |-- test_lifecycle_wake_host_event.py # 唤醒片记为宿主事件：确定性事实投影、原生与文本渲染、固定推荐、历史不重放注入、脚本化假模型不再重复派工、历史不完整时接替已完成子代理
|   |-- test_subagent_done_supersede.py # 已结束子代理被接替：终态不改写只记 superseded_by、回执按落盘结果、防重复接替、持久化白名单、父级视图 replaced_by、保存事件带接替关系
|   |-- test_compact_active_projection.py # 原生交接纯替换、媒体插话与guidance保留、未知IR拒绝
|   |-- test_active_turn_compact_projection.py # 活动归档完整容量、取消、CAS及局部证据继承
|   |-- test_gateway_child_compact_scope_application.py # Gateway和child共用view、交错游标及Audit范围隔离
|   |-- test_compact_tool_source.py      # 同一纯来源分区、完整身份与未知保留、快照隔离
|   |-- test_compact_tool_partition.py   # 原生完整配对、跨轮同名调用、未知/媒体/孤儿保留
|   |-- test_compact_tool_ref_archive_chain.py # 真实归档、回执投影、原生IR与carry的引用来源组合回归
|   |-- test_compact_native_ir_recovery.py # 真实读文件原生回执、恢复安全点、原CAS及候选HTTP对等
|   |-- test_compact_text_source.py   # 顺序完整覆盖、源改写/取消、纠正预算与序列化峰值回归
|   |-- test_compact_message_source.py # 可重放摘要消息的编码/估算等价及迭代关闭验证
|   |-- test_message_tail_streaming.py # 消息尾部流式读取与原整块读取逐项等价（跨块、CRLF、NEL/U+2028、坏行、停止边界）
|   |-- test_compact_source_lifetime.py # 全选真实JSONL到摘要提交的正文驻留、完整覆盖及峰值验证
|   |-- test_compact_work_measurement.py # 合成大线程索引/压缩的解析、连接、正文水合及峰值有界测量
|   |-- test_compact_work_contracts.py # 线程批次连接及异常、精确索引比较、原生位置计划等值回归
|   |-- test_model_selection_isolation.py # 双会话选模故障隔离、近窗口完整材料和连接校准失效组合
|   |-- test_compact_output_reserve.py # 三宿主完整输入和原输出cap、容量拒绝无提交及Responses字段对照
|   |-- test_compact_capacity_facts.py # 候选过大失败带出最小候选/上限/摘要占比/保留条数/候选数，白名单与TUI失败行
|   |-- test_compact_capacity_host_chain.py # 真实恢复宿主两入口：固定开销经只计量入口只在失败时量一次，保留IR按实际发送材料
|   |-- test_host_managed_store_access.py # H2：宿主托管存储（插件安装库、包库）对文件工具和 shell 不开放：路径策略、遍历工具、Seatbelt/bwrap 隐藏与真实沙箱
|   |-- test_pack_verification_post_write_feedback.py # 块 8 试点后的写后反馈修正：回执列全错误、一次改完的软提示、同一检查对象连续失败 6 次后只记账
|   |-- test_pack_verification_protection.py # 能力包块 4：核验账本防伪造（主代理和子代理 × 文件工具和真实 Shell 写不进 data/pack_verification，读照常）
|   |-- test_host_files_access.py # H3：宿主配置、运行状态、任务核验记录对模型只读，凭据对文件工具不可读：逐项文件工具与真实 Shell、A/B 两类、上级目录改名、大小写
|   |-- test_task_run_settle_quiescent_children.py # TaskRun 收口：静止但未终态的子 run（attempt 终态且无锁）不再拦住父 TaskRun，closed 带证据、reopened 可逆、发现扫描能关存量
|   |-- test_owner_wake_discovery_task_run_no_link.py # 发现层补关没有会话任务的 TaskRun（C12c）：关联文件确实不存在才按代理树关，读坏/active/根在跑/目录列不全都不关
|   |-- test_run_closeout_settlement.py # 回合收口统一推进 agent_run/task_run（rco）：Gateway 真实收口链路四条结束路径（完成/blocked/取消/错误）+ 等用户族与可续跑族保留 + 子代理失败族映射与端到端
|   |-- test_run_closeout_backfill.py # 历史悬挂 run 一次性补收口迁移（rcb）：分族补收口/六类不动/一次性/预览只读/分族对拍/维护集成
|   |-- _postgres_test_schema.py       # 测试专用：每个 pytest 进程一个 PostgreSQL schema（URL options 设 search_path），并行分片不互删表
|   |-- test_compact_calibrated_candidate_gate.py # 候选接受门按预检校准口径：纯函数、宿主冻结、两条门、触发来源、两回合假LLM复现
|   |-- test_compact_trigger_cap.py # 触发线绝对上限：0/非法不变、封顶后触发线/尾部/recovery、配置解析、请求前预检、后台定时回合先压缩、finalization token 触发线、/context 说明
|   |-- test_compact_retained_history.py # 三宿主完整保留行、媒体/工具回放和超容量不丢来源
|   |-- test_conversation_history_seed.py # 具体种子与只读来源两边界逐项等价、冻结时刻与追加、互斥及改写/截短/替换/删除失败
|   |-- test_host_history_seed_lifetime.py # 三宿主4.2M字符种子准备只驻留地址、解析后完整hash不变
|   |-- test_host_summary_phase_lifetime.py # 三宿主4.2M字符全链：摘要期不驻留旧请求历史、覆盖完整
|   |-- test_compact_recovery_release.py # 恢复宿主解绑旧历史：失败/取消/超限收尾不读、noop保留、tool_context共享合同、解绑前量压缩前大小
|   |-- test_compact_landmarks.py   # 原话备份：预算随窗口、编号、头尾裁剪、省略编号、跨代继承、只重读选中行、候选超目标时收缩
|   |-- test_compact_landmark_remeasure.py # 原话备份收缩：完整投影实测、容量原门、不变小不采用与中断传播
|   |-- test_session_history_read.py # session_search 读原文：长消息分段拼回、可信会话身份、当前会话浏览/检索不混入其它会话
|   |-- test_compact_media_recovery.py  # 两协议媒体工具轮及溢出后原文保留、无摘要和无CAS
|   |-- test_compact_transcript_media_partition.py # 文字前缀覆盖与媒体完整后缀、分段拒绝
|   |-- test_media_compact_preflight.py # 媒体会话越过压缩点：off 只守窗口/越窗 NON_TEXT，auto 归档引用后按压缩点压缩
|   |-- test_compact_media_policy.py    # 媒体压缩策略片 A：分类、投影、后缀保护按策略与 checkpoint 媒体事实
|   |-- test_compact_media_digest.py    # 媒体压缩策略片 C：含图回合分组、按预算/次数打包看图小请求、部分成功双计数、typed 失败同次回落
|   |-- test_context_pressure_media_reserve.py  # 已知图块按 INPUT_MEDIA_TOKEN_RESERVE_TOKENS 常量折进预检/自动压缩估算，分类加总不变
|   |-- test_tool_output_headroom_externalize.py # 余量不足的工具输出立刻外置并登记溢出，下一次预检走统一压缩
|   |-- test_compact_media_vision.py    # 媒体压缩策略片 B/C：输入模态声明、视觉探针缓存、B/A 决策与准入、看图小请求 + 文字摘要两步、typed 失败回落
|   |-- test_request_content_capacity.py # 当前思考与跨模型内容边界、child保留原模型
|   |-- test_input_media.py             # 媒体归属、字节、预算和原生后端投影
|   |-- test_native_compact_carry.py   # 同回合携带身份、深复制、精确释放和工具轮标记隔离
|   |-- test_native_user_input_identity.py # 原生UserTurn绑定原插话包ID，不按正文推断消费
|   |-- test_mixed_compact_contract.py   # 双来源机械回退、整包候选回退与取消零提交
|   |-- test_mixed_compact_recovery.py   # 两协议混合候选和实际HTTP对照、双覆盖及失败不发送
|   |-- test_compact_tool_provenance.py  # 原归档索引四元身份、同名调用保留与产物不覆盖
|   |-- test_model_turn_identity.py     # 同run/attempt跨真实模型轮的身份碰撞及错误隐藏回归
|   |-- test_background_capability_compact.py # 后台同工作片展示复用、清除与下一片重新评估
|   |-- test_decision_skill_tool_settings.py # 展示策略/开放类别列表的设置、CAS及原菜单编辑
|   |-- test_user_config_owner_scope.py # 普通 owner 决策工具可见性、可信线程与本机全局配置拒绝
|   |-- test_decision_curator.py        # 原Curator临时建议、完整材料、非选择结果和lease头寸对照
|   |-- test_decision_curator_relation.py # 正式版本/完整性、独立后台设置、关系注释与原提取提交边界
|   |-- test_decision_curator_relation_selection.py # 关系对按 BM25 词面相关度挑选：越界对可达、确定性、空材料边界与覆盖声明
|   |-- test_decision_model_call.py     # 实际 worker 账本保留、HTTP 尝试、身份、准入与取消
|   |-- test_decision_experiment_authorization.py # 设置授权来源、CAS/撤销、v2 上界口径与普通模式互斥
|   |-- test_decision_experiment_send_gate.py # 本地 HTTP 统计 TCP accept：拒绝/绕过零连接、单次发送结算、超上界与挂起
|   |-- test_decision_experiment_command.py # /experiment 解析、HTTP 入口重推 system_task、单次授权与重放/重启/Compact
|   |-- test_decision_experiment_records.py # E2 对照记录：结算等于原账、基线/候选、写入边界、回合收尾实际用量
|   |-- test_decision_experiment_evaluation.py # F1a 规则矩阵、外来记录忽略、授权指针证据链与 decision_read 只读暴露
|   |-- test_decision_experiment_promotion.py # F1b apply 授权内 CAS 晋升、回执幂等、用户后改/撤销/到期跳过
|   |-- test_decision_experiment_notice.py # J6：晋升回执到当轮 TUI、同会话 IM final、消费后重启去重及无晋升不提示
|   |-- test_decision_experiment_gateway_turn.py # 真实 Gateway 回合：本地 HTTP 实验、真实工具账实际用量与链上晋升
|   |-- test_model_call_input_budget.py # 原账带标签上界预留、单次发送许可、拒绝/绕过结算与代次/LRU 隔离
|   |-- test_model_call_ledger_partitions.py # 原账本用途、字段真值、单调终态及 worker 精确保留
|   |-- test_model_call_ledger_open_calls.py # 账本裁剪不丢进行中的调用：长压缩/主回合遇另一会话 129 次调用不 KeyError、请求计数完整、停机结清可见、6 小时无活动算失联可裁
|   |-- test_decision_usage_metrics.py  # 决策用途增量、迟到补账、未知输入与原 TUI 一行展示
|   |-- test_decision_protocol.py       # 决策快照、复杂度上限、逐题失败与用量未知合同
|   |-- test_typesafe_decision.py       # 原生请求、绝对期限及本地 HTTP 组合验收
|   |-- test_gateway_strict_request.py  # 原 HTTP 严格请求的零重试、期限与正文上限回归
|   |-- test_bounded_call.py            # 启动/取消/超时竞态、未退出资源与进程容量保护
|   |-- test_browser_lite_launcher.py  # browser-lite 关闭时先等本 profile 子进程再清 profile：假 /proc 与 stat、晚退出网络服务、超时只 SIGKILL 同进程组的精确匹配；启动参数关闭 macOS MacAppCodeSignClone
|   |-- _desktop_open_guard.py  # 测试防线：会话级 shim 顶替 open/xdg-open/osascript/通知/剪贴板程序并记录（令牌打码），conftest 据此让违规测试失败
|   |-- test_desktop_open_guard.py  # 防线自检：shim 只记录不执行、剥离环境后照样记录、PATH 前置与 webbrowser 替身、real_desktop_programs 放行
|   |-- _repo_tree_guard.py  # 测试防线：起跑目录下的 MagicMock/ 前后指纹比对（MagicMock 被当路径写进仓库根），conftest 据此让违规测试报错
|   |-- test_repo_tree_guard.py  # 仓库树防线自检：MagicMock 当路径被识别并清理、残留不动不算违规、残留里新增条目算违规、报错文字
|   |-- test_decision_call_resources.py # 有界调用与原模型准入的组合、普通模型保留名额
|   |-- test_subagent_process_control.py # 公共进程树终止覆盖后代、升级、宿主保留及未确认回执
|   |-- test_subagent_reserved_start_restart_pickup.py # 已预留未启动的子代理：停机后 worker 入口不启动、冻住的启动记录不复用、重启后被拉起、复用与复活分开计数
|   |-- test_subagent_resource_stop.py  # 固定原子树、终态资源、恢复隔离及 Goal/creation 锁序
|   |-- test_runner_stop_relay.py       # 独立 Python 宿主心跳转交精确取消、共享隔离及旧配置投影
|   |-- test_runtime_module_boundaries.py # 公共后端合同和纯策略不加载执行器/HTTP 的导入边界回归
|   |-- test_computer_text_input.py     # 文本事件 UTF-16、显式替换与不支持字符零副作用回归
|   |-- test_screen_observation_core.py # J16 片 B 核心：实例登记与快照环、摘要口径与容差、观察载荷过宿主校验、五项复核放行与各项 stale
|   |-- test_computer_use_observation_tools.py # J16 片 B 接入：开关并入声明与环境标记、属主范围、工具调用三元组、底层处理器（假 mcp.types）、X11 后端假 Xlib
|   |-- test_computer_use_observe_only.py # J16 只看档（vho）：四档位装配、只看档只交出 observe_window、上游导入被拦时仍可装配、适配器顶层无上游 import
|   |-- test_computer_use_xvfb_lane.py   # J16 车道 Xvfb 冒烟（默认跳过）：真适配器观察 → 点击 → 再观察、移动后过期、开关关着只有上游工具
|   |-- test_computer_use_xvfb_cases.py  # J16 片 C 车道集成（默认跳过）：关掉再开、改内容、/stop 中断慢 OCR、闪动光标误判统计
|   |-- test_computer_use_macos.py # J16 片 E：假 Quartz/ScreenCaptureKit/mss 上的列窗、可见、遮挡、多显示器缩放、回退原因、权限与晚到回调
|   |-- test_computer_use_macos_ax.py # J16 片 G：假 AX 上的授权、窗口匹配、限深限量读树、密码框不读值、端到端输入与控件变化 stale
|   |-- test_screen_ui_candidates.py # J16 片 G 核心：合并去重与动作判定、控件结构化复核、type_into 顺序、文字边界与取消时点
|   |-- _fake_macos_ax.py         # 测试共用的假 ApplicationServices：记录读写，绝不弹授权框、不执行 AX 动作
|   |-- test_screen_capture_guard.py # 真实屏幕防线自检：macOS / X11 真实库入口一调就失败且吞不掉（绊线兜底）；递归扫描“开观察 + 拉子进程”的文件必须带车道标记
|   |-- _screen_capture_guard.py # 测试侧真实屏幕防线：RealScreenAccessForbidden 与车道标记扫描规则
|   |-- _sandbox_capabilities.py # 沙箱能力探测（/bin/ps、嵌套 sandbox-exec、后台启动器身份）：会话内缓存，缺能力时按环境跳过、强制开关下失败
|   |-- test_sandbox_capabilities.py # 能力探测与判定自检：缓存、skip/fail 两种模式、强制开关只认 1；真起子进程验证标记生效
|   |-- _gateway_stub.py          # G3 插件命令回归：本机假 Gateway（随机端口、真实 HTTP），让 TUI 提交用例覆盖真实传输与凭据降级
|   |-- test_subagent_activity_diagnostics.py # 阶段提醒、慢流不误杀、执行代与消息去重回归
|   |-- test_subagent_takeover_hint.py   # 执行器退出后给父级的结构化接替提示：合同形状、开关开关两种回执、各消费方投影、只属一份结果（C4）
|   |-- test_r223_audit_regressions.py   # 外部审计的编码、版本、并发、MCP、输出、网络和恢复故障注入
|   |-- test_timeout_recovery_delivery.py  # 门槛5 探针的无损交付：两枪合法正文按序保留、归属同一轮、截断/预算/零工具轮语义不变
|   |-- test_store_scan_indexes.py      # wake/观察/策略读取侧索引：条目+记录双预算有界、删名清理、目录不可读不误清、枚举与 glob 同口径
|   |-- test_tui_injected_input_states.py  # 插话三段状态(排队/已提交/已确认)：提交边界不消费、已提交即入历史、慢流/失败/重连不丢不重
|   |-- test_slow_model_liveness.py     # 慢模型长任务活性：流式不按总时长判死、客户端只按机器活动续期、租约心跳与长工具续租
|   |-- test_background_claim_execution.py # 后台领取后竞态、异常收尾顺序及运行中真实续租
|   |-- test_background_recovery.py     # 恢复阻断每次重读、不可读判据与日志去重边界
|   |-- test_runtime_db_stable_mutation_generation.py # 正常换代、活动接管和升级调和保留已确认资源
|   |-- test_host_command_registration.py # 并发请求唯一登记、输入冲突、事务回滚与原链损坏检查
|   |-- test_host_command_operation_replay.py # 原执行器终态回读、身份隔离及线程退出 UNKNOWN 保留
|   |-- test_host_command_execution.py # 同请求并发、规范输入守门、终态收口故障及只读查询
|   |-- test_host_command_approval.py  # 明确批准、拒绝、取消、迟到决定与并发重复请求
|   |-- test_host_command_stream.py    # 临时 HTTP/MCP 与 TUI 审批往返、身份映射和取消隔离
|   |-- test_host_command_resource_reference.py # 原操作反查、坏链拒绝及资源集合回读
|   |-- test_gateway_admission_wait.py  # 合法排队等准入的结构化等待信号：只写等待事实、有节流与总预算、客户端持续收到且停写/取消/终态收口
|   |-- test_gateway_dispatcher_resilience.py # 派发线程不被 tick 异常与打印失败杀死、退出事实与假死自检（tick_wedged/pending_starved）进心跳与 /status、扫描门扫描前取样 mtime
|   |-- test_hang_diagnostics.py        # 网关 hang_diagnostics 启动接线、派发器假死一次性全栈快照落盘与跨平台信号注册单测
|   |-- test_adapter_state_write_resilience.py # 适配器状态写入失败不杀进程并恢复、异常退出写 failed 且删 pid、/status 按进程存活判适配器
|   |-- test_gateway_loop_backoff_coverage.py # 维护/调度器到期/孤儿恢复/心跳循环持续出错时退避（不快于自身间隔）、限流、按循环记账；包装异常按原因链归 io
|   |-- test_guidance_receipt_digest_compat.py # 插话幂等回执指纹 v1/v2 钉死、旧回执可读可重试、对账自然收敛、坏账不重写并计数
|   |-- test_scheduler_scan_costs.py    # waiting 投影缓存三重校验、runtime_snapshot 锁外解析与旧实现逐字一致、owner 事实缓存失效回归
|   |-- fixtures/tui/                   # 固定尺寸/时间线的非敏感 TUI PTY 动作 fixture
|   |-- test_adapter_ingress.py         # adapter POST 前落盘、幂等/隔离、响应丢失与崩溃恢复回归
|   |-- test_adapter_delivery_resume.py # 外发意图、保守接管、同键窗口恢复、unknown 与迟到回执
|   |-- test_agent_transcript.py        # 子代理公开过程事件的增量游标、隔离和有界裁剪回归
|   |-- test_chat_prompt_queue.py       # canonical chat Queue 精确回取、FIFO 与 unfinished-task 对账
|   |-- test_owner_home_workspace.py    # 主/子代理家目录范围、跨 owner 拒绝、运行记录分离与软整理指南
|   |-- test_shell_stdin.py             # 普通/受控/attempt 命令不抢读宿主 stdin，显式管道仍可传入
|   |-- test_shell_foreground_cleanup.py # 前台自然退出后代回收、出生身份与清理未知分账回归
|   |-- test_tui_ansi_snapshot.py       # ANSI offset 重放、样式/背景、Unicode、resize 和坏账 fail-closed 回归
|   |-- test_tui_agent_navigation.py    # 子代理选中/进入/返回、详情过程、只读终态与 footer 回归
|   |-- test_tui_superseded_marker.py   # “已被 X 接替”：Gateway 行摊平 kernel replaced_by、名册与子代理页头部标注（含窄屏）、渲染缓存键
|   |-- test_tui_shutdown_failure_label.py # “宿主停机中断”：权威标签、Gateway 名册 failure_label、TUI 名册/子代理页头部、IM /status 异常细分
|   |-- test_agent_goals.py            # 单代理单 Goal、主子隔离、版本冲突、停止和同执行轮持续工作回归
|   |-- test_goal_progress_fuse.py     # Goal 连续无进展熔断的结构化判据、计数重置、用户消息与通知幂等
|   |-- test_gateway_goal_fuse_reset.py # Gateway 前台用户消息清零 Goal 空片计数（保留暂停原因、写入失败不清），后台续跑片仍 3 片熔断；第 3 片排队的用户消息先处理
|   |-- test_goal_fuse_user_turn_first.py # 车道“用户回合在场”登记（排队到退出车道）与空片不计入规则（C5/O4）
|   |-- test_run_claim_probe_paths.py   # 登记/车道闸/撤销在失败与取消路径下的行为（领取失败、执行抛错、收尾抛错、心跳启动失败、双等待者取消、跨会话与跨 owner 隔离）
|   |-- test_goal_idle_delivery.py     # Goal 空转片不外发外部通道、熔断提示主动推送一次并去重、纯 TUI 行为不变（C11 修复）
|   |-- test_tui_resource_lifetime.py  # 缓存净化、异常退出、事件和冻结阅读预算回归
|   |-- test_gateway_owner_retention.py # 空闲回收、配置、在途及新消息竞态回归
|   |-- test_tui_events.py              # TUI event 信封、sequencer、cursor、重复/冲突/乱序与有界重放
|   |-- test_tui_markdown.py            # CommonMark 标题/列表/引用/代码/表格、样式角色与 Unicode 宽度换行
|   |-- test_tui_runtime.py             # 本地/Gateway 流式、工具、queue、终态和全局事件顺序 adapter 回归
|   |-- test_tui_view.py                # UIContent formatted lines、frame/block cache、follow anchor 和 resize 重排
|   |-- test_tui_history_paging.py      # 更早页渲染顺序、锚点、冻结页、子页/过期响应和单在途读取
|   |-- test_conversation_history_paging.py # 中文长行字节边界、完整工作片、坏游标与追加竞态
|   |-- test_tui_input.py               # slash/path 补全、菜单选择、queue 回取与 bracketed paste 输入回归
|   |-- test_tui_input_responsiveness.py # 慢历史建议与候选读取期间的事件循环响应回归
|   |-- test_gateway_input_target_lifecycle.py  # 请求与持久任务同名时的插话生命周期、目标隔离回归
|   |-- test_plugin_command_catalog.py # 声明跨进程往返、版本变化及损坏载荷拒绝
|   |-- test_plugin_observation.py     # 插件观察候选宿主合同：形状码、ID 稳定、投影、事件序新鲜度、动作候选复核
|   |-- test_plugin_package.py         # 静态包篡改、归档预算、危险成员及不执行代码的合同检查；v5 观察声明往返与非法声明
|   |-- test_plugin_manifest_v8.py     # M1 B1：严格静态声明、旧 v1–v7 字节、订阅确认、构建与原确认组件
|   |-- fixtures/plugin_manifest_v1_v7.json # 固定设计基线的七版本序列化字节，不在测试中重建
|   |-- test_plugin_any_language.py    # v6 非 Python 插件：包描述、可复现打包、确认回执、解包准备、解释器固定与真实 MCP 启停
|   |-- test_plugin_any_language_samples.py # 跨语言读取检查一致性用例（Python 参考 + Node 移植）与 hello-node/hello-go 宿主链路
|   |-- test_shuohao_novel_gates.py     # C14 M-B1：五阶段真实 Node MCP、74+20 读取向量、原样许可/NUL、自检、无写入与宿主链（环境失败保留）
|   |-- fixtures/shuohao_skills/        # 固定上游 B 的五阶段自检/样例、三张镜头卡与 report 测试闭包；不进插件 ZIP
|   |-- test_plugin_sandbox.py         # 插件进程沙箱：开关默认值、bwrap 整根只读布局、包装与 TMPDIR、不可用拒绝、真实平台写边界
|   |-- plugin_wheel_fixtures.py       # 合成标准 wheel 与导入陷阱，仅用于开发组件检查
|   |-- test_plugin_wheels.py          # 固定依赖、extras、平台、摘要与归档预算检查
|   |-- test_plugin_wheel_layout.py    # 跨 wheel 与引导文件冲突、安装布局和入口脚本检查
|   |-- test_plugin_environment.py     # 临时真实 venv/pip、导入陷阱、原配额及候选排他检查
|   |-- test_plugin_environment_process.py # 托管准备取消、正常退出交错与未知状态替身
|   |-- test_plugin_preparation_operation.py # 原 claim、资源锁故障与完整临时宿主执行回读
|   |-- plugin_environment_fixtures.py  # 临时 RuntimeDB 原操作与计划的开发测试组装
|   |-- test_plugin_install_store.py   # 默认停用、原请求重放、失败裁决与独立进程安装竞争
|   |-- test_plugin_configuration.py   # 私有配置 CAS、显式迁移、矛盾状态和提交异常裁决
|   |-- test_plugin_activation.py      # 激活/撤销竞争、配额隔离、跨进程 CAS、迁移及坏记录拒绝
|   |-- test_plugin_activation_ref.py  # 规范用户引用、独立进程复查和 host 创建前撤销拒绝
|   |-- test_plugin_mcp_transport.py   # 托管协议、队列撤销、调用隔离和原操作账结果集成
|   |-- test_mcp_operation_outcomes.py # MCP 完整业务失败落账、原结果重放及传输未知边界
|   |-- plugin_deactivation_fixtures.py # 原执行器内的假启用夹具与真实两类资源，精确 finally 清理
|   |-- test_plugin_deactivation.py   # 管理停用、原请求重放、准备取消、权限和未知清理保留
|   |-- test_plugin_deactivation_races.py # 阻塞业务停用、另一插件/任务隔离与独立 host 创建交错
|   |-- plugin_activation_fixtures.py # 实际临时 wheel/MCP、原管理链和业务工具执行夹具
|   |-- plugin_enable_fixtures.py # 真实启用回归按完整确认命令/授权请求运行，测试wide不改变产品默认
|   |-- test_plugin_enable.py         # 实际启用、坏目录、原执行器调用与旧快照停用验证
|   |-- test_tool_call_precheck.py    # 审批前/批准后执行前复核：代理 opt-in、TOOL_UNAVAILABLE 提前拦下、内置工具不复核
|   |-- test_background_extension_tools.py # 后台续跑白名单按注册表代理类型并入插件/MCP 工具；显式配置/任务名单不并入；真实插件启停跟随
|   |-- test_process_session_retry_settles_unknown.py # 重试停止按 PID 出生标识结清实例已消失的旧 unknown 记录；首次停止不凭空确认
|   |-- test_plugin_proxy_observation.py # 插件代理观察路径：结果改写与归档信封、整份拒绝、动作候选 _meta、未知/过期不发送
|   |-- test_mcp_observation_binding.py # MCP 逐工具声明与共用观察绑定：审批只能更严、整服务拒绝带原因码、未发现只提醒、代理铸 ID 与复核、always 真的审批
|   |-- test_plugin_proxy_revoked_call.py # 插件代理发送前复核激活：撤销固定 TOOL_UNAVAILABLE/not_started 且不发送，激活有效沿原 MCP 链
|   |-- test_plugin_tool_approval_recheck.py # C12e：真实插件进程+真实激活撤销+真实执行器审批流：批准后/审批前/发送前/下一轮四处复核都不让停用插件收到调用
|   |-- test_plugin_release.py        # 原 handler 退出、环境删除、结果落账与重送消费边界
|   |-- test_plugin_removal.py        # 管理卸载、权限、旧请求重放、准备未退与持久成功后包回收
|   |-- test_plugin_source_errors.py  # 插件来源结构化原因：相对路径按会话工作区解析、不存在/越权/格式无效分别回执
|   |-- test_plugin_update.py         # /plugins update：替换包并保留兼容配置、不兼容清空并报告、已启用/ID 不一致/同包拒绝或不变、计划纯函数
|   |-- test_plugin_skills.py         # 随包 Skill：v3 描述往返与校验、只取已启用插件、最低优先级、停用即消失
|   |-- test_plugin_host_api.py       # 宿主只读 API：令牌随激活失效、主题白名单、线程公开字段、v4 描述
|   |-- test_harness_console_package.py # harness-console 实际包：v4 描述、假宿主 API、网页令牌/cookie、失效与不可用、桌面窗口回收
|   |-- test_plugin_removal_store.py  # 安装删除 CAS、提交故障、符号链接与并发重新安装隔离
|   |-- test_plugin_registry.py       # 共享视图、可信 owner 注入、关闭登记交错与未知保留
|   |-- test_process_cleanup_evidence.py # 完整清理证明、自然终态保持、单调合并与 redo 恢复
|   |-- test_process_cleanup_consumption.py # 精确引用消费、部分删除、同 ID 换代及原记录保留
|   |-- test_nofollow_tree.py          # 环境目录与父链链接拒绝、树内链接不越界
|   |-- test_directory_lock_wait.py   # 原线程/系统目录锁等待的取消与释放验证
|   |-- test_plugin_configure_management.py # 原配置执行链、值不外泄、过期请求与 UNKNOWN 重放
|   |-- test_plugin_management.py      # 真实原执行链、来源消失、配置关闭、路径权限和配额检查
|   |-- test_plugin_list_mcp_section.py # /plugins list 的 MCP 服务段：发布状态投影、TUI 直连注册表、Gateway 冷 owner 不初始化、已加载 owner 借注册表
|   |-- test_plugin_invocation.py      # 原生 MCP 显式调用、单次审批、撤销交错、取消及原结果重放
|   |-- test_nofollow_binary_io.py      # 二进制预算、私有原子写入、锁链接和 portable 创建竞争
|   |-- test_gateway_plugin_commands.py # 冷 owner、可信身份、HTTP 插件命令分流与过期拒绝
|   |-- test_gateway_plugin_management.py # 原认证管理角色、冷用户拒绝及安装结果回读
|   |-- test_plugin_command_client.py  # 三模式、异步响应隔离、原候选版本及显式 Tab 读取
|   |-- test_tui_plugin_directory_pipe.py # 完整键盘链的 Tab 读取、候选接受、暂存恢复和原版本提交
|   |-- test_tui_interaction.py         # stash、Ctrl-R、help 与 paste refs 状态机回归
|   |-- test_tui_paste.py               # 大小 paste 的折叠/展开和占位符安全回归
|   |-- test_tui_preflight.py           # Gateway readiness 瞬态成功、typed 失败与 worker 只启动一次回归
|   |-- test_tui_upgrade_follow.py      # TUI 随 Gateway 升级原地切换：目标判定、空闲事实、UI 线程两段式、交接载荷、终端兜底
|   |-- test_turn_recovery_control.py   # /recover 普通/编号/owner 语法、恢复后可再挂载、Gateway 分派与 TUI 原文序列化
|   |-- test_turn_recovery_child_unknown.py # /recover 子代理分支：只读列出、按编号逐条处置、接替收口、TaskRun 关闭、范围与事务复核
|   |-- test_owner_unknown_recovery.py  # /recover owner：临时库只读投影、管理员边界、集合确认、共享 CAS、事件与重复确认幂等
|   |-- test_end_task_control.py      # /endtask 解析、只读列表与预览、确认结束后同一定时任务恢复派发、拒绝不改状态、Gateway 分派与 TUI 序列化
|   |-- test_wake_ops_control.py      # /wakes 解析、仅管理员、列表与预览只读、确认重放与事件、各类拒绝不改文件、状态计数、Gateway 分派与 TUI 序列化
|   |-- test_wake_quarantine_archive.py # 唤醒结案留档 14 天归档：判断时间、坏账随记录、max(mtime,ctime)、发布语义不变、重放拒绝、会话删除清单
|   |-- test_admin_identity_store.py    # 管理员密码私有存储、5 次失败锁定、绑定精确匹配与损坏 fail-closed、本机 CLI
|   |-- test_admin_identity_gateway.py  # 绑定私聊解析为 local/main、/admin 回执脱敏、服务端审批开关、/approve 与 /deny 精确决定
|   |-- test_admin_identity_clients.py  # 适配器敏感命令不进持久队列、Gateway 不可达回复、TUI 本地拒绝且不写输入历史
|   |-- test_gateway_host_guard.py      # 托管标记继承与擦洗保留、只拦托管自己的 Gateway、stop/restart/start --force 拒绝
|   |-- test_restart_gate.py            # 工具关口：关闭时停在领取前、重开放行、中断按未启动收口、执行中计数与等待
|   |-- test_gateway_safe_restart.py    # 安全重启：合并/冷却/防循环、两段排空与超时取消、标记、续跑优先、接班与 /restart
|   |-- test_shutdown_turn_resume.py    # 停机准入拒绝的回合留给重启续跑（I4）：不写终态、用户停止优先、TUI/IM 同一句提示、真实链路续跑完成
|   |-- test_provider_transient_turn_resume.py # tresume：供应商故障回合级自动续跑（白名单/开关/上限/跨重启计数、重排与失败收尾边界）
|   |-- test_turn_resume_limit.py       # 非计划重启续跑最多 3 次：计数跨重启累计、安全重启/租约过期不计、用完收成 failed 并提示、真实链路“继续”是新回合
|   |-- test_turn_resume_limit_steer.py # 续跑上限收口时插话的终态：已取走被杀/未取走→备用下一轮、已在历史→不送两遍、封存后补交、提示分句
|   |-- test_gateway_restart_tool.py    # restart_gateway：只在 Gateway 内、立即返回、冷却拒绝、只注册给管理员主代理
|   |-- test_self_install_switches.py   # learnpack 两个自动装开关：默认关、仅管理员 /settings 改、按文件现读马上生效
|   |-- test_package_build.py           # learnpack 唯一打包实现：样例包固定摘要与旧脚本逐字节一致、坏输入错误码
|   |-- test_learnpack_store.py         # learnpack 宿主存储：内容寻址验摘要、安装单只执行一次、安装记录
|   |-- test_package_build_tool.py      # package_build 工具：管理员专用且收起、读权限与密钥拦截、回执开关事实
|   |-- test_pack_commands.py           # learnpack /plugins#：命名空间互不串、帮助与补全、每个动作 TUI 与 IM 真实链路、重发回放、非管理员只读
|   |-- test_learnpack_flow.py          # learnpack 整条链路：假模型 + 真实回合，学 → 打包 → 开关关给确认行；技能能被 skill_search 找到读到
|   |-- test_pack_recommendation_threshold.py    # learnpack 能力包推荐门槛：评测 4 个包离线打分，领域请求照样命中、只撞描述常用词或关键词片段的不推荐
|   |-- test_learnpack_background.py    # learnpack 后台续跑：子代理写完后的续跑目录里有打包、安装（第 8 步修正），非管理员不授予，始终直出
|   |-- test_learnpack_e2e.py    # learnpack 整条链路：能力包开关关/开、插件两种开关位置、TUI 与飞书、确认重发回放、重启后照常
|   |-- test_learnpack_merge.py    # learnpack 同领域合并：查到已装同领域包先问、并进去出新版本、两个方向都在、退回合并前；版本号重用提醒
|   |-- test_learnpack_notices.py    # learnpack 宿主提示：自己装包与开单时直接告诉用户（不经她转告）、只列还有效的确认行、真实回合进同一会话
|   |-- test_learnpack_same_domain.py    # learnpack 同领域早提醒：安装回执的 same_domain_installed 与 user_notice、确认后宿主回执带提醒（TUI/IM）、/plugins# 同领域行、skill_search made_by_me
|   |-- test_skill_summarize_tool.py    # learnpack 让她总结一下：管理员专用、自学习关着如实说、target 两种落点、同一次活只入队一次、过闸门发布、能回退
|   |-- test_learnpack_install.py       # learnpack 安装：命令顺序与代确认边界（假管理服务）、开关开/关真实链路、确认单只执行一次、检查程序跟插件开关
|   |-- test_audit_requests_topic.py    # 审计 requests 主题：owner_id/会话归属、去重、时间窗、不含正文、跨用户两道门、管理员身份事实
|   |-- test_model_text_control.py      # 聊天 /model：解析、无模型引导、选择共享模型不泄密钥、按 owner 隔离、TUI 菜单保留
|   |-- test_tui_terminal.py            # OSC 标题、活动动画、去重与清理回归
|   |-- test_tui_transcript.py          # 详细 transcript、全文搜索、命中导航和 resize 回归
|   |-- test_tui_reading_position.py    # 原地展开、立即滚动、双向跨页和插话身份回归
|   |-- test_tui_renderer.py            # 欢迎/消息/spinner/tool/permission/queue/help 的固定时钟 golden
|   |-- test_tui_pty.py                 # PTY 动作时间线、ANSI 录制、resize、超时回收和 manifest 脱敏回归
|   |-- test_tui_reference_fixture_server.py # loopback Anthropic 参考场景协议与审计脱敏回归
|   |-- test_tui_view_model.py          # active→stable、工具权限、队列、状态和未知事件 fail-closed reducer 回归
|   |-- test_gateway_agent_control_service.py # owner 树内详情、幂等插话、停止和终态拒绝回归
|   |-- test_gateway_bounded_http_server.py # 固定 HTTP worker 复用、容量 503 与停机排队回收回归
|   |-- test_current_turn_execution.py # 当前轮成功/失败副作用事实投影回归
|   |-- test_cli_run_conversation.py   # CLI transcript、Memory 消息证据、任务链接与 workspace 收口回归
|   |-- test_closeout_machine.py      # 收口状态机 truth table 穷举测试(全组合+场景)
|   |-- test_wake_queue.py             # wake_queue 字条 CRUD、到期弹出、跨进程唤醒、对账与清理边界
|   |-- test_scheduler_wake_tick.py    # 调度器 tick 新契约: 到期字条→wake_queue_due、对账分频、EXEC-39 门、audit 跳过
|   |-- test_scheduler_waiting_deadlock.py # 定时执行没做完：无后续工作→受阻+提示+下一周期照常派发，有后续工作才 waiting，存量 waiting 出口
|   |-- test_scheduler_quota_settle.py # 定时任务撞额度用完：每次到期一次模型调用、一条通知，run 记 failed 带额度码，唤醒同拍确认；通知没送达只重投
|   |-- test_compact_quota_wake.py     # 压缩调用撞额度：按错误码或异常链认成额度用完，Goal 记 usage_limited 并发额度通知，不看文案
|   |-- test_tool_unknown_reason_preservation.py # 工具结果未知时 unknown_reason、重放回执、受管账本保留原始结论
|   |-- test_sleep_tool.py             # clock.sleep 参数边界、字条落盘、唤醒取消与错误码回归
|   |-- test_memory_hardening.py       # 来源证据、候选、并发去重、hard delete 与信封安全回归
|   |-- test_memory_candidate_daily_v2.py # Candidate/Daily v2 身份、状态、顺序、并发与大输出边界
|   |-- test_memory_curator_v2.py      # Curator 触发、模型配置、权限、失败恢复与整批提交
|   |-- test_curator_dispatch_eligibility.py # 待确认候选不单独运输/策展，保pending、旧lease、新输入与日终
|   |-- test_curator_recovery_race.py  # Curator 双进程发现/清理交错与唯一恢复执行
|   |-- test_curator_recovery_critical_section.py # Curator manifest重验、清理审计领取持锁与异常释放
|   |-- test_curator_thread_model_routing.py # 记忆整理按会话主代理模型分组：各调各的、失败不重复落账、坏 JSON 用默认补跑、按组熔断、路由只读
|   |-- test_curator_input_budget.py   # Curator 输入预算缩批：按最终提示实测长度截尾、尾部重放不丢、标注前缩批、预算失败不复用旧尝试形状
|   |-- test_curator_model_not_configured.py # 没配模型的 owner：永久配置错误不原地重试、独立失败码、发现层与 Curator 同源一小时退避
|   |-- test_curator_model_profile.py   # 固定连接/空值沿用/失效原因/旧键告警，用户专属写边界与 TUI/IM 档案编号展示
|   |-- test_curator_failure_attribution.py # Curator 失败归因：解析失败带响应形状（长度/截断/结束原因/出错位置），包装异常带根因类名与 errno，诊断有界
|   |-- test_provider_request_scope.py # 同端点前台优先、后台预算传递、取消和不重叠重试
|   |-- test_provider_quota_window.py  # 429 按供应商声明的限额窗口分额度用完/临时限流：长窗口不重试，短窗口和认不出的写法保持瞬时
|   |-- test_memory_promotion_v2.py    # 证据/冲突/Persona/lesson/HOT 晋升边界
|   |-- test_memory_recall_v2.py       # 正式来源、scope、陈旧索引、owner 隔离与信封安全
|   |-- test_memory_migration_v2.py    # v1→v2 dry-run、备份、回滚、幂等与坏数据关闭式失败
|   |-- test_memory_retention_v2.py    # 保留期、legal hold、终态保护、重验证与 hard delete
|   |-- test_memory_admin_cli_v2.py    # 统一 Memory 管理命令复用正式服务
|   |-- test_tool_input_completion_provenance.py # 有限补参、来源账目、伪造拒绝和旧旁路删除回归
|   |-- test_user_visible_text_markers.py # 回复正文净化：方括号下标（行内代码/代码块/普通文字）不被误判成内部标记，真标记仍剥掉
|   |-- test_tool_input_schema.py      # 强类型纠正、嵌套/组合/边界规则与显式 Schema fail-closed
|   |-- test_process_sessions.py       # 后台命令有界等待、进程树停止与 owner/TUI 会话隔离回归
|   |-- test_process_unknown_reason_codes.py # 后台进程/终端会话结果未知时带出具体原因码，工具操作账 unknown_reason 可归因
|   |-- test_process_activation_scope.py # 共享激活归属、旧 v2 恢复、任务隔离及退出证据保留
|   |-- test_background_stdio.py       # 实际字节管道、EOF、交接失败及精确资源隔离组件验证
|   |-- test_process_completion_receipt_install.py # 原receipt安装器的typed硬错/actual同对象回归
|   |-- test_multiprocess_process_completion.py # 原C/W完成源、收件身份、去重与故障窗
|   |-- test_process_completion_events.py # 后台完成通知的重启补发、去重和停止边界
|   |-- test_cache_diagnostics.py       # 请求前缀诊断、无正文存储与线程隔离
|   |-- test_gateway_status_tool.py    # Gateway 权威身份、端点与生命周期日志诊断回归
|   |-- test_sandbox.py                # bwrap argv、自检协议、owner-scoped fail-closed
|   |-- test_shell_hide_user_home.py   # macOS 非管理员 owner 拒读用户家目录开关、HOME 改指向与真实 Seatbelt 验证
|   |-- test_shell_sandbox_boundary_facts.py # 隔离 Shell 碰到沙箱边界时的结构化事实：允许目录、可能越界提示、开关与真实 Seatbelt
|   |-- test_container_install.py      # 假 runtime 验证一键 build/probe/透明包装器
|   `-- test_check_clean_package.py    # untracked、运行目录和 tar/wheel 制品门
scripts/
|-- build_constants_catalog.py              # 模块级数值常数只读目录生成器（ast 静态扫描，不 import 产品代码；--check 校验）
|-- build_plugin_api.py                # 固定共用源码原字节投影，标准构建零运行依赖 SDK wheel
|-- build_plugin_package.py            # 构建自有 Python 插件；wheel 与外层 ZIP 都固定归档元数据并经原包/依赖校验
|-- build_plugin_files_package.py      # 构建任意语言插件 v6/v8：显式新声明升 v8、生成摘要、固定时间戳、同读包器复核后发布
|-- plugin_build.py                    # 开发构建后端调用、wheel 元数据读取与独占产物写入
|-- bench/                             # GW-03/慢模型配对基准：锁内解析成本、owner 事实缓存各路径（配对交替，比值只在组内）；决策模型中文质量基准
|   |-- decision_quality_bench.py      # 决策质量基准运行器：离线核对、真实打分、阈值比对、登记成绩、“点位默认打开”前提检查
|   |-- decision_quality_adapters.py   # 点位适配：中文用例 → 各点位真实材料构造代码 → 请求材料与每题可接受答案
|   `-- decision_quality/              # thresholds.json（12 点位阈值）、results.json（登记成绩）、cases/<点位>.json（12 点位都有用例）、README
|-- live_lab/                          # 真实链路 harness；真实 preflight、main-artifact、tool-recovery
|-- tui_ansi_snapshot.py               # pyte 开发工具：从 raw ANSI/offset 账还原文本、样式、光标和标题快照
|-- tui_reference_fixture_server.py    # loopback 确定性 Anthropic 服务：驱动 TUI Markdown/思考/权限/错误黑盒场景
|-- tui_pty_recorder.py                # argv-only PTY 黑盒录制器：按键/粘贴/resize、原始 ANSI、事件索引与脱敏 manifest
`-- check_clean_package.py             # 工作树与真实发布制品的结构化干净度检查
|-- cache_hit_report.py                # 只读缓存命中运维报告：按天×用途汇总 provider/estimated 用量与命中率（表格/JSON/前后对比）
`-- reproject_model_usage.py           # 历史用量账本的只读重算投影（exact/partial/incomplete，不覆盖原账本）
plugins/
|-- sdk/pyproject.toml                  # 独立 SDK 的唯一发行版本及标准构建声明
|-- sdk/conformance/workspace_read_check.json # 工作区读取检查的跨语言一致性用例（期望值由宿主参考实现裁决）
|-- shuohao-novel-gates/                # C14 M-B1：v6 Node 五阶段只读结构校验与质量门；待外部复验/集成
|   |-- README.md                      # 构建、本人确认、五工具用法、回执与未验证边界
|   |-- declaration.json               # 安装清单、动作与五工具 schema 的同源声明
|   |-- LICENSE                        # 上游 Apache-2.0 原文
|   |-- NOTICE                         # 上游版权通知原文，旧样例路径不改
|   |-- PROVENANCE.md                   # 固定来源、逐文件摘要、未暴露写入口与排除项
|   |-- UPSTREAM.json                  # 上游路径、本仓库目标、SHA-256 与改动的机器清单
|   |-- conformance.js                 # 仓库内读取向量验证，不进插件 ZIP
|   |-- src/
|   |   |-- server.js                  # 串行 MCP stdio 入口，只协商读取上下文 v1
|   |   |-- arguments.js               # 同源 schema 参数检查，拒绝未知字段和写操作
|   |   |-- errors.js                  # 稳定公开错误码，不回显输入、私有路径或栈
|   |   |-- workspace_read.js          # hello-node 读取权限移植，算法保持
|   |   |-- workspace_files.js         # 普通文件完整 UTF-8 读取、no-follow 与 dev/ino 复核
|   |   |-- cast.js                    # 补齐角色 CLI 顶层与原文逐字核对
|   |   `-- gates.js                   # 同进程调用导出函数；门/跳过计数与已有日志统计，不写门日志
|   `-- upstream/skills/               # 五份固定原样 ESM CLI，characters 的 NUL 按字节保留
|-- activity-line/                     # 自有纯展示插件：面板显示运行状态、活动与耗时，无工具、无依赖
|   |-- README.md                      # 构建与面板用法
|   |-- pyproject.toml                 # 插件发行身份，无运行依赖
|   `-- src/activity_line/
|       |-- declaration.json           # 展示动作与 text 面板（activity、run_state 主题）的唯一声明
|       |-- __init__.py                #
|       |-- __main__.py                # python -m activity_line 启动 stdio 服务
|       `-- server.py                  # 握手声明展示能力，只实现只读 my-agent/display.render
|-- browser-lite/                     # 自有浏览器插件：标准库最小 CDP 客户端驱动本机 Chrome，打开/读取/点击/填写受控页面
|   |-- README.md                      # 构建、依赖准备、五个动作用法与安全边界
|   |-- pyproject.toml                 # 插件发行身份及精确 SDK 依赖，不含浏览器
|   `-- src/browser_lite/
|       |-- declaration.json           # 动作、工具（read 只读，其余 mutating）与设置的唯一声明
|       |-- __init__.py                # 独立插件包入口
|       |-- __main__.py                # python -m browser_lite 启动 stdio 服务
|       |-- server.py                  # MCP 握手声明读取上下文扩展，逐次分派；EOF/SIGTERM 时关闭浏览器
|       |-- declarations.py            # 读取同源声明并验证参数和设置（含字符串数组）
|       |-- errors.py                  # 带稳定错误码的中文业务错误
|       |-- access.py                  # 地址守卫：工作区 file:// 经读取上下文裁决，http(s) 只放行 allowed_hosts
|       |-- launcher.py                # 浏览器探测、专属 profile 启动、调试端口读取与进程回收（Linux 先等本 profile 子进程退出再清 profile）
|       |-- websocket.py               # 最小 WebSocket 客户端：握手、掩码帧编解码、分片、ping/pong、close
|       |-- cdp.py                     # 页面级 CDP 命令/事件收发、请求拦截与崩溃检测
|       |-- page.py                    # open/read/click/fill 页面脚本与结果结构
|       `-- session.py                 # 单浏览器会话：启动复用、空闲回收、致命错误后关闭
|-- context-inspector/                # 自有纯展示插件：面板查看上下文用量、组成与压缩次数，无工具、无设置、无依赖
|   |-- README.md                      # 构建与面板用法
|   |-- pyproject.toml                 # 插件发行身份，无运行依赖
|   `-- src/context_inspector/
|       |-- declaration.json           # 展示动作与 status 面板（context 主题）的唯一声明
|       |-- __init__.py                #
|       |-- __main__.py                # python -m context_inspector 启动 stdio 服务
|       `-- server.py                  # 握手声明展示能力，把 context 主题数字渲染成状态字段
|-- status-pet/                        # 自有纯展示插件：文字小宠物显示工作/等待审批/空闲，外观与名字可配置，无工具、无依赖
|   |-- README.md                      # 构建、面板用法与改外观步骤
|   |-- pyproject.toml                 # 插件发行身份，无运行依赖
|   `-- src/status_pet/
|       |-- declaration.json           # 展示动作、text 面板（run_state、activity 主题）与 style/name 设置的唯一声明
|       |-- __init__.py                #
|       |-- __main__.py                # python -m status_pet 启动 stdio 服务
|       |-- art.py                     # 三种外观 × 三种状态的自绘字符小图
|       `-- server.py                  # 启动时校验设置，只实现只读 my-agent/display.render
|-- desktop-lite/                      # 自有桌面小工具插件：系统通知、默认程序打开工作区文档、写剪贴板，只调用本机系统程序
|   |-- README.md                      # 构建、三个动作用法、平台支持表与安全边界
|   |-- pyproject.toml                 # 插件发行身份及精确 SDK 依赖
|   `-- src/desktop_lite/
|       |-- declaration.json           # notify/open/clipboard 动作与 mutating 工具、程序路径与超时设置的唯一声明
|       |-- __init__.py                # 独立插件包入口
|       |-- __main__.py                # python -m desktop_lite 启动 stdio 服务
|       |-- server.py                  # MCP 握手、逐次读取上下文接入与三个工具分派，错误带实际程序与退出码
|       |-- declarations.py            # 读取同源声明并验证平面参数和设置
|       |-- commands.py                # 业务错误、按平台定位系统程序、组装 argv/stdin 命令并以超时运行
|       `-- opening.py                 # open 的授权、no-follow 普通文件校验与可执行/脚本类扩展名拒绝清单
|-- drama-media-shell/                 # A/drama-skills 离线媒体外壳：四个提示词检查器 + fixture-only 生产作业
|   |-- README.md                      # 十个工具、夹具边界、作业流程、构建与测试说明
|   |-- pyproject.toml                 # 插件发行身份、精确 SDK 0.2.0 依赖及许可/来源资源打包
|   `-- src/drama_media_shell/
|       |-- declaration.json           # 10 个工具的名称、说明、effect 与 inputSchema 唯一声明
|       |-- __init__.py                # 独立插件包入口
|       |-- __main__.py                # python -m drama_media_shell 启动 stdio 服务
|       |-- server.py                  # MCP 握手、逐次读写上下文恢复、固定工具分派与结构化错误
|       |-- declarations.py            # 读取同源声明并验证工具参数
|       |-- errors.py                  # 稳定错误码与中文安全消息
|       |-- workspace_io.py            # SDK 读取上下文 + no-follow 有界 JSON/JSONL 读取
|       |-- image_prompt_check.py      # 图片提示词资产、引用、字段及负面词检查
|       |-- container_check.py         # 视频容器与镜头所有权、时长和覆盖核对
|       |-- motion_timing_check.py     # 运动窗口、镜头时长及所有权核对
|       |-- music_spec_check.py        # 音乐提示词字段与负面词检查
|       |-- job_contract.py            # 作业清单规范化、输入摘要与固定指纹
|       |-- production_state.py        # prepare/confirm/status 的确认消费状态机
|       |-- production.py              # fixture-only run/collect 与供应商拒绝边界
|       |-- production_audit.py        # 当前指纹运行证据和工作区输出摘要审计
|       |-- fixture_adapter.py         # 内置离线夹具字节；明确不是真实媒体生成
|       |-- private_store.py           # 只在绝对插件私有数据目录保存作业 JSON
|       |-- LICENSE.drama-skills       # 上游 MIT 原文与版权行
|       `-- PROVENANCE.md              # 固定提交、逐文件 sha256、改动和排除项
|-- genui-lite/                        # 自有数据展示插件：JSON 渲染为表格/文字条形图，可导出独立 HTML，随包带 Skill
|   |-- README.md                      # 构建、两个动作用法、数据格式与中文示例
|   |-- pyproject.toml                 # 插件发行身份、精确 SDK 依赖，package-data 打入声明与 SKILL.md
|   `-- src/genui_lite/
|       |-- declaration.json           # 动作、工具（table 只读、export 写）、设置与随包 Skill 名单的唯一声明
|       |-- __init__.py                # 独立插件包入口
|       |-- __main__.py                # python -m genui_lite 启动 stdio 服务
|       |-- server.py                  # MCP 握手声明读/写上下文扩展，逐次调用分派与中文错误
|       |-- declarations.py            # 读取同源声明并验证平面参数和设置（含布尔）
|       |-- tables.py                  # 工作区 no-follow 有界读取、两种 JSON 格式解析、Markdown 表格与文字条形图
|       |-- html_page.py               # 独立 HTML 渲染：内联 CSS、内联 SVG 条形图、全部文本转义、无脚本
|       |-- operations.py              # table 渲染与 export 导出流程；导出经写入上下文 check/anchor 后原子写出
|       `-- skills/genui-table/SKILL.md # 随包 Skill：何时用两个工具、数据格式、导出路径与不越界约束
|-- image-text/                        # 自有图片本地 OCR 插件：系统 tesseract 提取文字 + 标准库图片元数据，不做视觉理解
|   |-- README.md                      # 构建、依赖准备（tesseract）、用法与"视觉理解走 /attach"边界
|   |-- pyproject.toml                 # 插件发行身份及精确 SDK 依赖
|   `-- src/image_text/
|       |-- declaration.json           # read 动作/只读工具与设置（tesseract_path、default_lang 等）的唯一声明
|       |-- __init__.py                # 独立插件包入口
|       |-- __main__.py                # python -m image_text 启动 stdio 服务
|       |-- server.py                  # MCP 握手、逐次读取上下文接入与 read 流程，错误附带图片元数据
|       |-- declarations.py            # 读取同源声明并验证平面参数、pattern 和设置
|       |-- reading.py                 # 业务错误类型与授权后的 no-follow 有界图片读取
|       |-- image_info.py              # 按魔数识别 PNG/JPEG/GIF/WebP 并解析宽高
|       `-- ocr.py                     # 定位并以超时运行 tesseract、临时文件清理、TSV 解析与语言包检查
|-- design-lite/                       # 自有 HTML 设计文件插件：按模板生成、只改标题/副标题/颜色，随包带 Skill
|   |-- README.md                      # 构建、两个动作用法、模板说明与中文示例
|   |-- pyproject.toml                 # 插件发行身份、精确 SDK 依赖及 Skill 打包
|   `-- src/design_lite/
|       |-- declaration.json           # 动作、工具（create/edit 都是写）与随包 Skill 名单的唯一声明
|       |-- __init__.py                # 独立插件包入口
|       |-- __main__.py                # python -m design_lite 启动 stdio 服务
|       |-- server.py                  # MCP 握手声明读/写上下文扩展，逐次调用分派与中文错误
|       |-- declarations.py            # 读取同源声明并验证平面参数
|       |-- operations.py              # 生成与修改流程：no-follow 读回，经写入上下文 check/anchor 原子写回
|       |-- templates.py               # 三个内联 CSS 模板、转义渲染与 data-dl-field 字段定位替换
|       `-- skills/design-card/SKILL.md # 随包 Skill：何时用、模板差别、先 create 再 edit
|-- savepoint-lite/                    # 自有文件快照插件：保存/列出/恢复，快照只存插件数据目录
|   |-- README.md                      # 构建、三个动作用法与中文示例
|   |-- pyproject.toml                 # 插件发行身份及精确 SDK 依赖
|   `-- src/savepoint_lite/
|       |-- declaration.json           # 动作、工具（save/list 只读、restore 写）与设置的唯一声明
|       |-- __init__.py                # 独立插件包入口
|       |-- __main__.py                # python -m savepoint_lite 启动 stdio 服务
|       |-- server.py                  # MCP 握手声明读/写上下文扩展，逐次调用分派与中文错误
|       |-- declarations.py            # 读取同源声明并验证平面参数和设置
|       |-- operations.py              # 保存、列出、恢复流程；恢复先核对 --expect 再经写入上下文写回
|       `-- snapshots.py               # 工作区 no-follow 读取与哈希，插件数据目录内的快照存取
|-- harness-console/                   # 自有界面型插件：agent 工作台网页/桌面窗口，经宿主只读 API（v4 host_api=read）展示运行状态
|   |-- README.md                      # 构建、open/desktop/stop 中文用法与安全边界（宿主令牌只在服务端、回环、会话令牌）
|   |-- pyproject.toml                 # 插件发行身份，无 SDK 与第三方依赖
|   `-- src/harness_console/
|       |-- declaration.json           # 动作、工具（三个写类）、设置与 host_api 名单的唯一声明
|       |-- __init__.py                # 独立插件包入口
|       |-- __main__.py                # python -m harness_console 启动 stdio 服务
|       |-- server.py                  # MCP 握手与分派，服务复用、桌面窗口启动/回退、进程退出时关闭服务与窗口
|       |-- declarations.py            # 读取同源声明、验证平面参数和设置、业务错误
|       |-- host.py                    # 宿主只读 API 客户端（令牌头、1 秒缓存、403 失效记忆）与 /api/state 白名单整理
|       |-- console.py                 # ThreadingHTTPServer 服务：会话令牌/cookie、只 GET、首页与状态 API、空闲自停
|       |-- page.py                    # 工作台单页：内联 CSS/JS、textContent 渲染、轮询与失效停止
|       `-- window.py                  # Chrome/Chromium 探测、--app 独立窗口子进程、默认浏览器打开与窗口回收
|-- web-board/                         # 自有网页界面型插件：插件进程内起只绑 127.0.0.1 的只读网页浏览指定目录
|   |-- README.md                      # 构建、serve/status/stop 用法与安全边界（回环、令牌、只读、限定目录）
|   |-- pyproject.toml                 # 插件发行身份及精确 SDK 依赖
|   `-- src/web_board/
|       |-- declaration.json           # 动作、工具（serve/stop 写类、status 只读）与设置的唯一声明
|       |-- __init__.py                # 独立插件包入口
|       |-- __main__.py                # python -m web_board 启动 stdio 服务
|       |-- server.py                  # MCP 握手与分派，单实例服务生命周期，进程退出时关闭服务
|       |-- declarations.py            # 读取同源声明并验证平面参数和设置
|       |-- board.py                   # ThreadingHTTPServer 服务：令牌/cookie、GET/HEAD 路由、计数、空闲自停、关闭释放端口
|       |-- reading.py                 # 业务错误、根目录与请求路径授权、no-follow 目录枚举与有界读取
|       `-- pages.py                   # 目录列表/预览/错误页 HTML 渲染，全部转义、内联 CSS、HTML 走 sandbox iframe
|-- hello-go/                          # Go 示例插件（v6 随包可执行文件）：交叉编译成单文件，提供问候工具
|   |-- README.md                      # 构建、打包、安装与确认步骤
|   |-- go.mod                         # Go 模块声明，只用标准库
|   |-- declaration.json               # 包声明（打包时生成摘要），同时编译嵌入作工具目录
|   `-- main.go                        # stdio MCP 服务：握手、工具目录与 hello
|-- hello-node/                        # Node.js 示例插件（v6 系统解释器）：问候与按宿主读取上下文读文本
|   |-- README.md                      # 构建、安装、确认与读取检查说明
|   |-- declaration.json               # 包声明，同时随包供服务读取工具目录
|   |-- conformance.js                 # 开发用：跑工作区读取一致性用例（不打进包）
|   `-- src/
|       |-- server.js                  # stdio MCP 服务：握手声明读取扩展、工具分派、打开后复核的有界读取
|       `-- workspace_read.js          # 工作区读取上下文的 Node 移植：逐段解析链接、owner/凭据/危险目录裁决
|-- event-watch/                       # M1 B8 事件观察样例（v8）：订阅全部 6 类事件，只读 table 面板显示收到与合并丢弃计数
|   |-- README.md                      # 构建、确认启用与计数说明
|   |-- declaration.json               # 6 类事件（content none）、watch 面板与默认断网的唯一声明
|   `-- src/
|       `-- server.py                  # stdio MCP 服务：握手声明事件与展示能力，只回观察回执与只读计数表
|-- rm-guard/                          # M1 B8 工具收紧样例（v8，Python）：rm 加 -r 和 -f 要求确认、delete_file 直接拒绝
|   |-- README.md                      # 构建、确认启用与命中写法说明
|   |-- declaration.json               # guard-rm（run_command，full 参数）与 guard-delete（delete_file）收紧门声明
|   `-- src/
|       `-- server.py                  # stdio MCP 服务：握手声明收紧能力，只按结构化 command 裁决
|-- rm-guard-node/                     # M1 B8 工具收紧样例（v8，Node）：与 rm-guard 行为一致的 Node.js 实现
|   |-- README.md                      # 构建、确认启用与跨语言一致性说明
|   |-- declaration.json               # 与 rm-guard 相同的两个收紧门声明
|   `-- src/
|       `-- server.js                  # stdio MCP 服务：与 Python 版逐条对齐的裁决逻辑
|-- workspace-peek/                    # 自有文件预览插件；不依赖完整宿主运行包
|   |-- README.md                      # 离线构建、命令示例与当前验收边界
|   |-- pyproject.toml                 # 插件发行身份及精确 SDK 依赖
|   `-- src/workspace_peek/
|       |-- declaration.json           # 命令、工具、输入与设置的唯一声明
|       |-- __init__.py                # 独立插件包入口
|       |-- __main__.py                # 标准模块执行入口
|       |-- server.py                  # 有界 stdio MCP 请求与逐次读取上下文接入
|       |-- declarations.py            # 读取同源声明并验证样本平面输入和配置
|       |-- reading.py                 # 路径授权、对象身份与进度游标
|       |-- preview.py                 # 固定 fd 上的 UTF-8 文件分页
|       `-- tree.py                    # 预算内目录枚举、变化检测与稳定分页
`-- worktable-lite/                    # 自有纯展示插件：面板查看本用户最近会话并提示 resume 命令，无工具、无依赖，只有显示偏好设置
    |-- README.md                      # 构建、显示偏好设置与面板用法
    |-- pyproject.toml                 # 插件发行身份，无运行依赖
    `-- src/worktable_lite/
        |-- declaration.json           # 展示动作、text 面板（sessions 主题）与 max_rows/hide_current 设置的唯一声明
        |-- __init__.py                #
        |-- __main__.py                # python -m worktable_lite 启动 stdio 服务
        `-- server.py                  # 握手声明展示能力，把 sessions 主题渲染成带相对时间的会话行
deploy/
|-- Dockerfile                         # 内置系统 bubblewrap+tini，构建期 binary probe
|-- seccomp-bwrap.json                 # 固定 Moby 默认 profile，仅放行 bwrap namespace/mount 系统调用
|-- seccomp-bwrap.PROVENANCE.md        # 上游 commit、许可、本地增量和升级验收说明
`-- k8s/                              # stable/canary、Gateway route、migration、monitor 与 DR 清单
docs/
|-- PRODUCT_FACTS.md                    # 当前功能与边界说明；发布风险统一指向 STATUS
|-- audits/r223-report/                 # R223 中文只读 HTML 报告的独立公开目录，不放配置或任务产物
|-- design/SUBAGENT_TOOL_APPROVAL_BRIDGE.md # child→owner 具体工具审批的身份、租约、FIFO 与失败语义
|-- design/LONG_TASK_RUNTIME_REVIEW.md # 长任务问题、模块边界及 L 线实施验收
|-- design/SCHEDULER_EXECUTION_POLICIES.md # 原调度链的上下文、模型、失败策略和预检合同
|-- tasks/TUI_STEER_CONTROL_HANDOFF.md # T 核心候选的原交接与未完成矩阵
|-- tasks/LONG_TASK_RUNTIME_HANDOFF.md # L/T 组合候选的源码、测试、真实验收与接手边界
|-- design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md # 后台命令 host 所有权、跨进程记录与安全回收设计
|-- design/HOST_CONFIG_WRITE_GUARD.md # 宿主托管文件（配置、运行状态、任务核验记录）对模型只读、凭据对文件工具不可读（H3）的拦截层与已知边界
|-- design/GATEWAY_LOCAL_TRUST.md # Gateway 本机来源信任收紧：回环不再自带信任、特权入口认宿主客户端凭据、模型命令沙箱按端口连不到 Gateway（定稿，实施中）
|-- design/CLI_READONLY_STARTUP.md # 只读 CLI 现状与裁定：3a 定乙不在沙箱支持只读启动，保留 H3 错误并列出内置工具覆盖与缺口
|-- design/TUI_DESIGN.md # 终端交互 TUI Python 原生复刻的用户行为、事件架构与验收规格
|-- design/TUI_BEHAVIOR_CHECKLIST.md # 启动、消息、输入、权限、生命周期和命令映射逐项证据账
|-- tasks/completed/TASK-20260818-终端交互-tui-parity.md # 已完成 TUI 复刻实施、测试机边界和验收记录
|-- tasks/REFACTOR_PLUGIN_GOAL.md       # 同版发布部署、十步重构状态与逐步多 TUI 验收
|-- tasks/TUI_READING_HANDOFF.md        # 阅读锚点、连续滚动、插话顺序与真实终端验收交接
|-- tasks/HANDOFF_STEP7_SUBAGENT_LIFECYCLE.md # 第 7 步子代理结果链的代码、验证和集成交接
|-- tasks/HANDOFF_STEP7_WAKE_PUBLICATION.md # 第 7 步唤醒配对发布半写修复与恢复边界交接
|-- tasks/HANDOFF_STEP8_MODEL_TURN.md # 第8.2模型采纳边界、验证和集成交接
|-- tasks/HANDOFF_18A_DECISION_MERGE_MAIN.md # 决策线吸收 main 的来源身份统一、原场景对照与受影响测试清单
|-- tasks/HANDOFF_STEP7_CLOSEOUT_RECOVERY.md # 第 7 步恢复扫描显式依赖、回归与装配交接
|-- design/FEATURE-20260804-tool-runtime-unification.md # 工具唯一主链的用户行为、需求与验收规格
|-- design/tool-runtime-unification.md  # 工具参考证据、架构、迁移删除表与并行边界
|-- design/LONG_RUNNING_EXECUTION.md    # 慢模型、后台长等待与缓存诊断统一合同及验收矩阵
|-- design/P2_SCALE_ROLLOUT_DR_OWNER_STORE.md # 灰度/灾备/Owner store/24h proof 事实
|-- architecture/BOUNDARY_RULES.md      # 分层和写入边界
|-- architecture/MODEL_CATALOG_SNAPSHOT.md # 模型目录、配置覆盖及元数据维护边界
|-- architecture/MODULE_OWNERSHIP.md    # 当前模块归属
|-- architecture/MY_AGENT_HOME_LAYOUT.md# owner home 布局
|-- modules/                            # 分模块结构和进展
`-- development/                        # 开发规则、review、写文件规则
```

## Current Storage Roots

### 关键文件说明

- `docs/tasks/IM_CREDENTIAL_LIVE_ENTRY_HANDOFF.md`：首次普通 Gateway 请求的 TaskLink/前台 claim 根因、原 promotion 与 typed scope 修复、独立 343 项回归及真实租户待验边界；两份新增入口与 scope 测试由此导航。
- `conversation/host_actor.py`：认证入口专有字段、原主任务/子任务记录及不可变执行来源共用的发起人解析；普通模型属性不能授权。
- `conversation/background_receipt_contract.py`、`agent_core/tool_loop/background_public_receipt.py`：仅任务显式选择时启用受管公开收口；内部结果和公开通知仍沿原执行与投递账本保存。
- `contracts/background_public_receipt.py`：模型循环和会话投递共同使用的 schema 标识，避免会话领域反向依赖模型实现。
- `agent_core/model/response_provenance.py`：由原调用账签发当前响应的用途与身份，公开字典不能恢复宿主签发权；来源正确不等于正文语义已验证。

- `docs/design/PUBLIC_CAPABILITY_PACKS.md`：公共能力包的待实施发布范围、管理/使用权限矩阵、主子版本与消费用户隔离，记录当前内容包与插件总闸的耦合；不表示当前包已开放公共发布。
- `docs/design/IM_TASK_EXECUTION_POLICY.md`：普通 IM 多任务的待实施策略，区分定时后台与当天自动委派，记录执行归属、主代理后台汇总、容量与跨零点验收；不表示当前包已具备该能力。

- `docs/tasks/TUI_INPUT_LATENCY_HANDOFF.md`：输入辅助工作的线程边界、原生 TUI/PuTTY 证据及发行交接。
- `docs/tasks/IM_MONITOR_PERMISSIONS_IMPLEMENTATION.md`：本轮 R01～R13 实施状态与最终合并推送验收记录。
- `docs/design/IM_CLOUD_SCHEDULE_LATENCY_REVIEW.md`：14 项追加问题的源码、隔离复现、报告纠正与待实施边界，不表示产品已修复。

- `scripts/private_release/`：临时离线发行唯一构建/安装入口，产物放工作树外；不代替原源码 wheel 发布，不进行 main 合并。
- `docs/guides/PRIVATE_OFFLINE_RELEASE.md`：版本、程序目录、数据目录、源码可见范围与原目录更新约定。

实际执行读取与复核入口为 `gateway_parts/execution_target.py`、`execution_control.py`；后台发布入口为 `conversation/claim_execution_binding.py`。TUI 和 IM 都使用原 claim/run/attempt，不把回执或展示状态变成运行权威。测试与实施记录见 `docs/tasks/TUI_STEER_CONTROL_HANDOFF.md`。

- `agent_py_agent/tests/test_plugin_m1_joint_e2e.py`：迁入sol2联合观察点，真实样例进程、Hub合并计数和只读展示共用原单池，临时各插件激活独立；手工processing、合成安装与启动适配不代产品启用/B7；缺Node event-watch样例时单独skip。
- `agent_py_agent/tests/test_plugin_m1_joint_tool_gate.py`：强断言17j必备B5字段，四联合严格核真实征询、用户拒绝、零handler及目标event-watch非空三事件；两直接deny唯一行/info通过；jb6b 起两条 ask 账本用例转正（删掉空账本豁免），另补批准出口、不可交互、消费者答 unavailable 与同 owner 多行排序 4 条。无真实删除，非整期验收。
- `plugin_events/tool_gate_review.py`：共用 B2 池、共享单次预算、握手和安装快照追新；不创建第二池或批准账。
- `tooling/plugin_gate_policy.py`：将插件纯协议事实接到唯一执行器，来源只取宿主请求，模型参数不能获得豁免。

- `docs/guides/MODEL_GUIDE.md`：给用户的模型管理使用说明，覆盖新增（含 ChatGPT 订阅登录勾选）、切换与默认、`/effort` 智能程度、删除、管理员共享与初始模型、派子代理选模与 `manage_models` 对话式管理。
- `docs/guides/FEISHU_QUICK_CHECK.md`：给用户本人在真实飞书上 5 分钟内做完的手测清单，每项写明发什么、看到什么算通过、出问题提供什么；与 C11 自动化验收互补，不挡收口。
- `docs/guides/CAPABILITY_PACK_GUIDE.md`：给用户的能力包使用说明，覆盖装/启用/停用/更新/回退/卸载（TUI、IM 的 `/plugins`，管理仅管理员）、按任务发现与使用、版本固定、派子代理授权、自制包与安全边界。
- `docs/guides/PLUGIN_GUIDE.md`：给用户的插件与安全开关使用说明，覆盖插件命令（TUI 与飞书共用入口）、`/plugins info` 四段（事件订阅、收紧工具、网络与沙箱、最近决定与观察计数）、随包样例插件与写插件内置技能、屏幕观察只看档的开关组合与锁屏行为、G2b 本机凭据强制开关（默认关、打开前先看 `/status` 计数）、锁与私有文件"只动自己建的东西"的口径。
- `agent_py_agent/tests/test_plugins_chat_control.py`：IM 与 TUI 的共享插件服务回归；用假渠道及真实持久回执核对路由、原参数、管理员权限、文本错误码和非 Python 启用预览，不替代真实 IM 收发验收。
- `docs/tasks/C10_IM_PLUGINS_HANDOFF.md`：C10 的文件范围、开发测试、独立变异、未验证边界与集成复查入口。
- `docs/design/COMPACT_GENERATION_FACTS.md`：摘要来源与请求关联的交接设计，明确旧检查点未知和诊断不参与恢复权威。
- `agent_py_agent/agent/capability/task_references.py`：连接原任务 pins、授权 grants 和调度引用，不另建状态库。
- `agent_py_agent/agent/capability/package_read.py`：原工具与宿主共用准确原字节、分页、来源与任务pin。
- `agent_py_agent/agent/capability/package_selection_runtime.py`：原主业务准备的唯一选包接缝；scope、authority、context和纯selector分别负责资格、原门复核、总预算与模型选择。
- `agent_py_agent/agent/capability/subagent_package_entries.py`：只消费子代理原首次请求资格与显式授权的同代引用，不再次选包或扩权。
- `agent_py_agent/agent/capability/subagent_entry_authority.py`：首请求入口的宿主新建材料与当前执行身份检查，不形成第二份状态或授权。
- `agent_py_agent/agent/tooling/file_syntax_diagnostics.py`：文件修改的有界纯语法观察；写入成功、当前语法与任务质量分开，禁止重新读取目标猜测已提交内容。
- `agent_py_agent/agent/conversation/capability_selection_state.py`：原TaskLink的可选值类型和结果摘要，CAS与持久化仍归store.tasks。
- `agent_py_agent/agent/capability/package_provider.py`：只从当前 owner 的原安装表取得已启用包，每次读取复查代次与摘要。
- `agent_py_agent/agent/capability/self_install_switches.py`：learnpack 自动装开关的唯一读取口，每次按文件现读（不吃 agent 缓存快照），参数中心据它把两个开关的生效时机报成“马上生效”。
- `agent_py_agent/agent/capability/package_build.py`：打包规则的唯一位置；不执行、不导入随包文件，同一输入同一字节（固定摘要测试守着）。
- `agent_py_agent/agent/capability/learnpack_store.py`：learnpack 的宿主存储；“是不是她做的”只认这里且读回重算 sha256，安装单靠独占创建只执行一次。
- `agent_py_agent/agent/capability/learnpack_notices.py`：learnpack 的宿主提示（她自己装包、开安装单）只在这里拼文字，文字只来自结构化事实，经 `host_notices.queue_turn_host_notice` 随当轮回复显示给用户。
- `agent_py_agent/agent/capability/learnpack_domain.py`：“同领域”（她做的、装着的、包名不同的能力包，声明关键词 casefold 整词重合）和“她做的”（装着的字节在 learnpack 存储里有打包记录）只在这里判断；只读结构化字段，给用户的同领域提醒也在这里拼。
- `agent_py_agent/agent/tooling/package_build_tool.py`：模型工具 package_build（本机管理员、默认收起），读文件过 read_file 同一裁决、拦密钥，产物进 learnpack 存储，回执带开关事实与下一步。
- `agent_py_agent/agent/capability/learnpack_installer.py`、`learnpack_service.py`：learnpack 安装只经 `PluginManagement.command` 执行管理员同一串 /plugins 命令；代确认只覆盖运行她自己做的程序。
- `agent_py_agent/agent/capability/pack_commands.py`：`/plugins#` 唯一处理处；启用/停用/删除转成管理员同一条 `/plugins` 命令并沿用用户的请求编号与目录版本，退回与安装复用 learnpack_service。
- `agent_py_agent/agent/tooling/skill_summarize_tool.py`："让她总结一下"按结构化参数 `target` 选落点（`current` → `SkillLearningService.request_learning` 按会话任务打标记，`previous` → `summarize_recent` 当场入队这个会话最近完成的那次活；同一次活对用户只入队一次），入队、闸门、版本、回退全走现有自学习流水线。
- `agent_py_agent/agent/tooling/package_install_tool.py`：模型工具 package_install（本机管理员、默认收起），包身份只认 learnpack 存储，能不能直接装只看对应开关现读值。
- `scripts/build_capability_package.py`：开发者命令行薄壳，读文件后调 `capability/package_build` 构建 v7 内容包，不导入或执行包内脚本。
- `examples/capability-packages/`：三个独立迁移切片与来源许可，组件测试不代表真实 TUI 通过。
- `examples/capability-packages/drama-text-a/methods/review-source.md`、`review-continuity.md`、`review-delivery.md`：A0.2.1 的来源、接续、交付三份私有审阅分表；沿入口按需读取，不进入全局 Skill。
- `examples/capability-packages/drama-text-a/templates/review.md`：可带回作者处理的 Markdown 证据反馈模板，不是第二份任务或验收状态。
- `agent_py_agent/tests/test_capability_package_drama_text_duration.py` 与 `test_capability_package_drama_workflow_duration.py`：合成输入下验证样包原脚本的时长聚合、目标缺失/非法、浮点和溢出边界，不执行真实任务产物。
- `agent_py_agent/tests/test_capability_package_drama_text_basis.py`：验证 A 包 v3 镜头声明、本场来源、失败回执版本和结构不代表语义的边界。
- `agent_py_agent/tests/test_capability_package_drama_text_visibility.py`：以公开合成资料通过原 CLI 验证代称/退出、可见/画外、歧义与长短词、原位置及预算/裁剪计数；不修改真实产物。
- `agent_py_agent/tests/test_capability_package_drama_text_v05.py`：A 包 0.5.0（能力包 v2 块 7）的 7 项新检查各一个正例一个反例，以及 `--host-json` 只输出 `pack_verifier_result.v1` 结构化字段、写出即退 0；不读真实产物。
- `agent_py_agent/tests/test_capability_package_drama_workflow_v03.py`：B 包 0.3.0（能力包 v2 块 7）的新检查各一个正例一个反例；带基线时未在交接列出的改动报错、列出的放行（宿主按摘要对应和 `--input-file` 两种模式）；`--host-json` 只输出 v1 结构化字段。
- `agent_py_agent/tests/test_capability_package_drama_workflow_v031.py`：B 包 0.3.1（能力包 v2 块 8 归因）的动作节拍字段名收紧——缺 `character_ids` 且写了非空单数 `character_id` 报 `beat_character_id_singular` 错误；纯缺失、空值和对白字段保持原语义；只读公开合成项目。
- `agent_py_agent/tests/test_capability_package_drama_text_lines.py`：A 包 0.4.0 可选字段——台词结构与说话人声明、逐字引用只按子串（不归一化）、道具持有人/相邻状态接续与 `continuity_break`、报告 `checker` 身份与声明版本一致。
- `agent_py_agent/tests/test_capability_package_drama_text_source_marks.py`：A 包 0.5.3（能力包 v2 块 8 重跑归因）来源与改编标注三条规则——道具名在原文里却标成新增、台词字幕既非原文也没标改编（含极短语气词/纯标点/称呼/占位的字面豁免）、说明声称"原文未出现"而其实在原文里；每条各写正例、反例、合法退出，并断言新错误都带 ≤200 字 `hint`。
- `agent_py_agent/tests/test_capability_package_drama_text_hint_guard.py`：A 包 hint 的通用守卫（来源是 ds3 在 B 包踩到的"模板占位符没被替换就漏给模型"）。hint 清单从脚本模块现取——扫所有 `*_HINT` 常数，另真实触发一遍走 `host_result` 投影覆盖 f-string 拼接的两条；逐条断言无 `{占位符}`、≤200 字、无控制字符，并自带一条"坏样本"用例证明守卫会红。
- `agent_py_agent/tests/test_capability_package_drama_text_review_fixes.py`：A 包 0.5.4（9 例业务审阅归因）两条新检查——标了 `verbatim_source_id` 的台词必须落在段落引号内（段落无引号时不报，避免误伤）、镜内道具持有人变化提醒补 `prop_handoffs`（一端为 `null` 不算交接）。样例形状按审阅失败形状自己构造，不拷产物正文。
- `agent_py_agent/tests/test_capability_package_drama_workflow_baseline.py`：B 包 0.2.0——`--baseline-project` 的换说话人（含重编号）、节拍类型改变、对象删除提醒，交接覆盖真实改动（根地址不算）、虚假新增/省略报错、检查器身份与项目摘要。
- `examples/capability-packages/drama-text-a/methods/visible-characters.md`：A0.3.0 人物依据回填的私有方法与v3诊断合同；只说明字面覆盖，不生成宿主完成状态。
- `agent_py_agent/tests/test_capability_package_b_template.py`：用完整占位填写公开合成资料，核对 B 的原检查器、私有交接模板及真实隔离派工授权。
- `examples/capability-packages/drama-workflow-b/templates/handoff.json`：制作阶段、输入字节摘要、对象转换、省略与新增的业务清单；不作为宿主状态或已验证证据。

- `docs/design/CAPABILITY_PACKS.md`：包级发现、私有方法、原安装与任务授权的边界。
- `docs/design/CAPABILITY_MIGRATION.md`：固定来源、完整覆盖表、候选构建和原命令发布／回退的可执行步骤。
- `docs/design/CAPABILITY_SOURCE_COVERAGE.md`：两个固定短剧来源的全部入口、阅读深度、样包映射及未迁移项；索引覆盖不等于能力全部迁移。
- `docs/design/CAPABILITY_UPSTREAM_MIGRATION.md`：上游未迁内容按许可证分成可迁（B 的 `.mjs` 门做 Node 插件、A 的图像/视频工具只做外壳）与不迁（Toonflow、Remotion、限用途样例），给出分批、验收与审查要点。
- `docs/design/FILE_SYNTAX_DIAGNOSTICS.md`：原生文件修改的语法反馈设计，区分写入成功、观察结果与任务质量。
- `docs/design/PERSONA_AGENTS_OWNERSHIP.md`：AGENTS.md 归属与多 owner 隔离、指向本地或网络文件的权限边界、自动学习另存与合并、学习来源标记、写入上限、误加条目的清理迁移。
- `docs/design/SLEEP_RESUME.md`：睡眠/长暂停后的 lease、执行者接管、外部副作用不确定状态、前五风险和可派工测试切片。
- `agent_py_agent/tests/test_capability_package_resource_scope.py`：验证包成员和业务路径的区分、同代搜索导航、受限隔离，以及归档后原生消息保留完整资源引用。
- `agent_py_agent/tests/test_capability_resource_input_schema.py`：验证模型完整来源schema、原执行器在handler前拒绝缺字段/类型错误，以及原身份/代次/摘要边界不变。
- `docs/tasks/CAPABILITY_INTERNALIZATION_GOAL.md`：能力内化执行 Goal 与当前唯一 TODO。
- `docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md`：固定来源版本与迁移范围、许可和测试证据矩阵。

- `agent_py_agent/agent/capability/skill_proposals.py`：自学习 Skill 提案唯一权威；只收 `subagent_lesson` 且带 task/run 来源的 Candidate，固定模板渲染、O_EXCL 幂等写 `<owner_home>/data/skill_proposals/`；confirm 在 owner 锁内复核版本、草稿 hash、来源 Candidate 与目标不存在，经 frontmatter 解析和 `agent_generated` guard（不 force）后 `os.replace` 安装，失败不写目标。
- `agent_py_agent/cli/skill_proposal_commands.py`：`my-agent skills proposals list/show/confirm/reject` 的注册与输出，只委托上面的服务；确认必须带 `--expected-revision`，不提供模型工具。自学习开启时 runner 结果链以 `actor=auto` 调同一 confirm。
- `agent_py_agent/agent/capability/skill_learning.py` 与 `skill_learning_{request,prompt,publish,store}.py`：自学习 S3 自动总结 Skill（设计见 `docs/design/SKILL_AUTO_SUMMARY.md`）。收口按结构化判据写有界请求；Gateway 记忆整理车道逐条处理：运行锁、前台让路、每日上限、复用 Curator backend 的无工具结构化调用；自动闸门代替人工确认后发布到 `<owner_home>/skills/learned/<name>/`，登记表 `registry.json` 是自学归属唯一权威，账本只记结构化字段。
- `agent_py_agent/cli/skill_learning_commands.py`：`my-agent skills learned list/show/revert/remove`，只读写 `SkillLearningStore` 与 `skills/learned/`，不构造 Agent、不调模型。
- `agent_py_agent/tests/test_skill_learning.py`、`test_skill_learning_integration.py`：S3 的离线合同（假 backend）与接线（组合根、收口 helper、Gateway 车道、CLI、S1 自动确认、配置）。
- `agent_py_agent/agent/common/config_paths.py`：默认配置路径的依赖轻权威；CLI bootstrap 与 Gateway worker 直接引用同一函数，显式配置优先、环境值调用时读取，不打开配置或创建目录。
- `agent/settings/parameter_registry.py` 与 `parameter_changes.py`：参数中心。登记表是“有哪些参数、什么意思、谁能改”的唯一来源；写入口是 my-agent 的 `user_config` 工具与聊天 `/settings` 共用的唯一修改路径（记录与回滚）。
- `scripts/build_constants_catalog.py` 与 `agent/settings/constants_catalog.py`：代码常数只读目录（P10）。生成器用 ast 静态扫描产品代码生成随包 `config/constants_catalog.json`，运行时模块只读该 JSON 供 `/settings internal` 与 user_config search 查找；常数权威位置仍是读取它的那行源码，目录只是投影。
- `docs/design/PARAMETER_CENTER.md`：参数中心的唯一模块设计：来源、目标、安全等级、修改与回滚、my-agent 开发工作树、阶段与验收。
- `docs/design/SKILL_AUTO_SUMMARY.md`：自学习 S3 的唯一模块设计：用户决定、上游参考取舍、触发、材料、后台执行、输出合同、自动闸门、存储所有权、用户入口与边界。
- `agent_py_agent/agent/subagents/lesson_ledger.py`：子代理经验账本 `lessons.jsonl` 的唯一合同。字段规范成单行且有界，id 取内容 hash（同 run 相同参数只记一次），每 run 最多 5 条、16 KiB，超限返回结构化结论；读回逐行复核版本、字段、id 与 run 归属。工具写入与 runner 结果收口共用，宿主从不解析模型回复正文。
- `agent_py_agent/agent/agent_core/runtime_write_guards.py`：正在运行的 my-agent 安装目录写保护的唯一实现（开关 `protect_running_runtime`，默认开）。按进程事实认定安装目录（虚拟环境整体 / site-packages 包目录 / 源码运行不保护），写入独立边界键 `runtime_install_roots`：文件工具在 `tooling/write_boundary` 无条件拒写，Shell/终端并入沙箱只读路径，Full Access 同样生效。
- `agent_py_agent/agent/agent_core/runtime/record_lesson_tool.py`：子代理专属 `record_lesson` 工具。run/attempt 取 runner 上下文、task 取任务记录，Schema 只含四个经验字段；注册表默认隐藏，随子代理 allowed_tools 下发，主线程调用返回 `TOOL_UNAVAILABLE`；所有拒绝都声明 `effect_outcome=not_started`。
- `agent_py_agent/agent/agent_core/orchestration/input_media_refs.py`：派子代理时 `input_media_refs` 的解析与校验（第 14 条，默认关）。引用只认 sha256，只在父级本轮附件与父级 transcript 的 canonical 用户媒体块里查找，再按 owner 附件根与配置上限重验，写进 child 任务属性 `input_media`；未知/重复/格式错/超限整批 `SUBAGENT_INPUT_MEDIA_INVALID`，开关关闭整批 `SUBAGENT_INPUT_MEDIA_DISABLED`，根与递归创建共用。
- `agent_py_agent/agent/agent_core/orchestration/create_read_scope.py`：创建子代理前的输入路径可见性预检。只读调用方显式输入字段（批量 item 在并入 goal 路径之前记下），经 `prepare_run` → runner 写边界 → `write_boundary_with_runtime_ledger`（task_local）→ `granted_external_work_roots` → `PathAccessPolicy.check_with_external_roots` 判定；看不到整批 `SUBAGENT_INPUT_PATH_NOT_VISIBLE` 拒绝，根与递归创建共用。
- `agent_py_agent/tests/test_subagent_tool_failure_streak.py`、`test_subagent_authorization_failure_halt.py`、`test_create_subagents_input_read_scope.py`：子代理可观测三件事的离线合同（真实 owner 权威库与假模型，零网络）：父级 `recent_tool_failure`/如实 `last_progress_summary`；授权阶段同码连续失败 → BLOCKED + 结构化生命周期事件；创建前预检与子代理真实 read_file 裁决一致。
- `agent_py_agent/tests/test_subagent_lesson_ledger.py`：record_lesson 链路的离线合同（只用假件）：身份与 Schema、字段/条数/字节上限、幂等、坏账本与符号链接、读回复核、合并进 output.json、`subagent_lesson` 候选与 S1 提案（含重放不重复）、候选失败不阻断交付、暴露面与 runner 提示。
- `agent_py_agent/tests/test_skill_proposals.py`：自学习 S1 的默认关闭、幂等、忽略非法来源、Curator 迁移不碰提案目录、确认拒绝矩阵、安装/回执失败回滚、快照可见性、runner 结果隔离与真实 CLI 入口往返验证。
- `agent_py_agent/agent/capability/decision_skill_proposal_review.py`：自学习 S2 的唯一审核顺序点 `skill_proposal_review`（owner_background，默认 off）；只读 `SkillProposalService.list`，外发别名、来源计数与经 `external_data/default` 投影的草稿摘要，采用前重读待确认提案核对版本与草稿 hash，只重排 CLI 展示并附宿主标签；关闭、observe 或任何失败都返回 None 保持原输出，取消上抛。
- `agent_py_agent/agent/capability/memory_search_tool.py`：`memory_search` 工具（J9，开关 `enable_memory_search_tool` 默认关）；主模型只读检索本人正式长期记忆，范围与自动召回共用 `memory_store/recall.runtime_long_term_scope`，只调 `JsonlMemory.search_scoped_candidates_report`（不写访问信号），返回有界摘录与检索方式（semantic/keyword）；自动召回被抑制的回合不可用。
- `agent_py_agent/tests/test_memory_search_tool.py`：J9 守卫——开关关不注册、写参数执行前被拒、跨 owner 与跨范围查不到、条数与摘录上限、语义/关键词降级事实、不写访问信号、抑制回合不可用、模型不能自己打开开关。
- `agent_py_agent/agent/settings/embedding_selection.py`：语义记忆“向量模型”唯一选择入口（用户 10-02 拍板）；`embedding_model_profile`/`memory_semantic_recall` 是全局配置，只给本机管理员；经 `user_settings_write_scope` 调参数中心写两项并记账，重启 Gateway 生效；manage_models 自配只在本人目录嵌入档案且与默认对话模型同端点主机时直写，主机不同回 `needs_user_choice`。
- `agent_py_agent/agent/capability/model_profile_tool.py`：`manage_models` 工具；把 TUI /model 的 list/add/save_provider/save_model/select/set_default/delete_model/delete_provider/probe/discover（以及向量模型 set_embedding/disable_embedding）暴露给主会话代理，唯一写入口仍是 `execute_model_profile_operation`；delete_provider 为 dangerous，子代理不可用，`select` 只读结构化 `conversation_thread_id`，回执只含 `has_key`，开关 `enable_model_profile_tool`。
- `agent_py_agent/tests/test_decision_skill_proposal_review.py`、`test_decision_skill_proposal_review_integration.py`：前者只替换决策服务边界，覆盖资格、隐私、逐题校验、并发变化与取消；后者经真实 CLI、设置、模型目录、决策服务与调用账，只替换 HTTP 发送，覆盖输出逐字节不变、observe 记账、错误/冷却/超时、中断、30 条窗口门、默认值与 TUI 菜单。
- `agent_py_agent/agent/tooling/segment_planning.py`：只接canonical调用、有效批上限、Compact及并发描述查询；审批、线程和provider顺序记账仍归原执行轮。
- `agent_py_agent/agent/subagents/services/run_authority.py`：子代理创建时 runtime.db 权威运行链（Task/TaskRun/AgentRun/pending Attempt/Delegation）的唯一写入点与链身份回存；有 home 上下文时 fail-closed，由 `SubAgentBaseService.create_run` 调用。
- `agent_py_agent/agent/agent_core/tool_guard/identical_failure.py`、`agent_py_agent/tests/test_identical_tool_failure_halt.py`、`agent_py_agent/tests/test_subagent_identical_failure_halt.py`：主代理同一回合“同一调用（工具名＋规范化参数摘要）同一错误码”连续段与硬上限（复用 `repeated_failure_halt_threshold`）；命中只结束当前回合（unfinished＋`REPEATED_IDENTICAL_TOOL_FAILURE`，宿主自写收口、不再调模型），任务与 Goal 保留且不自动续跑；子代理同样生效，按 blocked 收口并经 `tool_failure_halt` 事实通知直属父级，同一次调用授权阶段收口优先。
- `agent_py_agent/agent/agent_core/tool_loop/closeout.py`：统一请求、收口响应处理和延后结束原因读取，不持有Agent或完整回合参数，不执行工具或增加模型重试。

- `agent_py_agent/agent/agent_core/tool_loop/model_turn.py`：协调请求周期与响应采纳；实际prompt构造、Compact和执行权仍由原入口绑定。
- `agent_py_agent/cli/chat_parts/tui_safe_lines.py`、`tui_identity_window.py`：只管理显示缓存和近期身份，完整记录留在 canonical 历史。
- `agent_py_agent/agent/tooling/listen_scope.py`：后台服务监听范围（loopback/lan）的规范化、child 进程树真实监听观测（Linux /proc、其它 POSIX lsof）与越界判定；host 用它回收越界服务。
- `agent_py_agent/agent/user_space/operation_grants.py`：owner 级"长期允许某类操作"的唯一权威（tool_policy.json 的 operation_grants），审批面板选 approved_owner 后写入，自主模式据此放行。
- `agent_py_agent/agent/gateway_parts/background_sessions.py`：Gateway 停机收尾只读列出 owner 后台会话权威目录里仍未终态的受管进程（含监听范围事实），供停机事件与 status 投影；不停止、不改记录。
- `agent_py_agent/agent/gateway_parts/loop_health.py`：Gateway 派发线程健康的唯一事实源（进程内、不做 IO）——起止、每次 tick 起止与派发数、从 tick 逃逸的异常、循环错误打印本身失败的次数；`dispatcher_alive` 由登记/退出记录加线程仍在 `threading.enumerate()` 判定。心跳与 `/status` 只扁平并入 `snapshot()`，不另算存活。
- `agent_py_agent/agent/gateway_parts/local_client_token.py`：持久本机客户端凭据的宿主接口，缺则私有原子生成、严读有原因码，停机不删；插件 H2 与启动钩子同一路径。
- `agent_py_agent/agent/gateway_parts/client_credentials.py`：G3宿主请求头的唯一凭据选择；配置token优先，只读G1接口；失败时按 `gateway_require_local_credential` 分流——开关关（默认）不带凭据继续发送并只记一次原因码warning，开关开在HTTP前拒绝；不生成、不修权限、不输出秘密。
- `agent_py_agent/cli/gateway_client_headers.py`：没有Agent的仓库验收脚本复用原配置解析，不从argv或环境取凭据值，不新建HTTP通道。
- `agent_py_agent/tests/test_gateway_client_credentials.py`：tmp_path凭据与随机端口假Gateway验证原运输头、插件流、开关关降级（不带凭据照发+warning一次）、开关开结构化拒绝、IM两态收口及泄漏防线。
- `agent_py_agent/tests/test_gateway_local_client_token.py`：临时凭据生命周期/权限/坏文件、H2声明、环境与日志/status不泄露、启动根和重启复用。
- `agent_py_agent/cli/gateway_loop_backoff.py`：`LoopErrorBackoff`，派发循环与后台主循环共用的"连续出错退避 + 打印限流"小工具：只算节奏不记账不打印，成功一次清零；防止 tick 持续出错时每 0.2 秒一条错误日志把刚腾出的磁盘再写满。
- `agent_py_agent/tests/test_host_files_access.py`：H3 合同：宿主托管文件（`path_access_policy` 的 `HOST_CONFIG_*`、`HOST_STATE_*`、`HOST_CREDENTIAL_*`）对模型的文件工具与 Shell 只读（凭据对文件工具连读也拒），9b 盘点的每一项都有文件工具和真实沙箱用例；B 类任务树根留在 `tool_runtime_ledger`；宿主记忆工具照常写。
- `agent_py_agent/cli/host_state_guard.py`：H3：模型在命令沙箱里跑 my-agent CLI 起不来时（每条命令都要构造完整 SimpleAgent、写本地库，而宿主状态只读），只按宿主设的 `MY_AGENT_HOST_STATE_READ_ONLY` 标记和异常的 errno / sqlite 错误码把它换成一行 `error_code=CLI_HOST_STATE_READ_ONLY`；沙箱外行为不变。
- `agent_py_agent/tests/test_host_managed_store_access.py`：H2 合同：宿主托管存储（`path_access_policy.HOST_MANAGED_OWNER_STORE_PARTS`）对模型的文件工具与 shell 不开放，用户工作区同名目录与 zip 不受影响；含 macOS/Linux 真实沙箱用例。
- `agent_py_agent/agent/gateway_parts/background_resource_report.py`：`gateway stop`、本地 `/stop` 与 Gateway 会话级 `/stop`（C12a：无运行中回合时冻结本会话遗留进程，回执渲染 `background_resource_lines` 也在这里）共用的受管后台进程事实投影与停止入口；只读登记表（根地址与写入端同用 `process_session_store_root(workspace, owner_home)`），投影不含命令正文/cwd/输出路径，按精确执行身份冻结停止意图、等真实终态后如实报告是否停止。`session_background_processes` 只按精确 `thread_id` 筛本会话资源，不做 owner 全量。
- `agent_py_agent/tests/test_gateway_stop_background_resources.py`：gateway stop 侧合同（只列运行中、跨 task/run 不误停、空身份被拒、未终态如实报未停、按进程组回收孙进程）。
- `agent_py_agent/tests/test_session_stop_background_resources.py`：会话内 `/stop` 回收被中断任务遗留后台资源的合同（无回合时不再回"没有运行中的内容"、会话隔离精确匹配、登记表不可读时报 unknown）。
- `agent_py_agent/tests/test_user_interrupt_resume.py`：Esc／/interrupt 后过一会儿再发消息接着原任务（第 7 条）：自愈窗口内不收口、过期按 `user_interrupt_expired` 收口、事件绑定精确代次、两个入口都记、窗口内 `/stop` 照旧停止回收，以及真实 Gateway ask + 脚本化假模型的"中断→5 分钟后自愈→继续"链路。
- `agent_py_agent/tests/test_gateway_stop_session_leftovers.py`：Gateway 会话内 `/stop` 回收被中断任务遗留后台进程的合同（C12a：自愈后仍能收、空闲 `/interrupt` 不碰、别的会话与仍 active 的审计任务不碰、任务记录损坏时不停）。
- `agent_py_agent/agent/gateway_parts/owner_retention.py`：复核既有硬事实后回收空闲实例和轮询登记，不关闭持久任务或共享插件。
- `docs/design/TUI_RESOURCE_LIFETIME.md`：身份、执行槽、连接和历史规模的边界与参考源码。

- `plugins/sdk/pyproject.toml` 与 `scripts/build_plugin_api.py`：SDK 唯一发行声明和原源码字节投影；不另存共用权限实现。
- `plugins/workspace-peek/` 与 `scripts/build_plugin_package.py`：首个自有只读插件及标准安装包构建，独立 MCP 入口只消费宿主逐次上下文。
- `plugins/genui-lite/`：首个随包带 Skill（包描述 v3）的自有插件，table 只读渲染、export 经写入上下文导出独立 HTML；`agent_py_agent/tests/test_genui_lite_package.py` 为其实际包与 MCP 进程组件验收。
- `plugins/desktop-lite/`：首个影响用户桌面的工具插件，文本只经 argv/stdin 交给系统程序，open 只开授权范围内的非可执行普通文件；`agent_py_agent/tests/test_desktop_lite_package.py` 为其实际包与 MCP 进程组件验收（假程序经设置注入）。
- `plugins/image-text/`：本地 OCR 工具插件，只调用系统 tesseract、不调用模型；`agent_py_agent/tests/test_image_text_package.py` 为其实际包与 MCP 进程组件验收。
- `plugins/design-lite/`：首个带随包 Skill（包描述 v3）的自有插件，生成与修改 HTML 设计文件都走写入上下文；`agent_py_agent/tests/test_design_lite_package.py` 为其实际包、Skill 打包与 MCP 进程组件验收。
- `plugins/browser-lite/`：首个驱动外部进程的自有插件，浏览器不随包分发，专属 profile 在插件数据目录，地址经读取上下文与 allowed_hosts 双重裁决；`agent_py_agent/tests/test_browser_lite_package.py` 为其实际包、帧编解码与真实浏览器组件验收；`agent_py_agent/tests/test_browser_lite_launcher.py` 用假 /proc 进程表覆盖关闭时等本 profile 子进程、超时只对同进程组的精确匹配 SIGKILL。
- `plugins/savepoint-lite/`：首个写工作区的自有插件，快照只存宿主插件数据目录，恢复走写入上下文；`agent_py_agent/tests/test_savepoint_lite_package.py` 为其实际包与 MCP 进程组件验收。
- `plugins/drama-media-shell/`：固定 A/drama-skills 上游的四个只读检查器和 fixture-only 生产作业外壳；工作区只走 SDK 0.2.0
  逐次上下文，作业账只进插件私有数据目录，不包含付费适配器、Remotion 或限用途样例；
  `agent_py_agent/tests/test_drama_media_shell_package.py` 覆盖实际包、上游 selftest 合同与 MCP 进程组件，不替代真实 TUI/模型验收。
- `plugins/harness-console/`：首个界面型插件（宿主只读 API 样本），网页与桌面窗口共用一个只绑回环的服务，宿主令牌只留在插件服务端；`agent_py_agent/tests/test_harness_console_package.py` 为其实际包、假宿主 API 与 MCP 进程、真实 HTTP 访问的组件验收。
- `plugins/web-board/`：网页界面型插件，插件进程内只绑回环的只读网页，按 serve 时冻结的读取上下文和 no-follow 读取限定目录；`agent_py_agent/tests/test_web_board_package.py` 为其实际包与 MCP 进程、真实 HTTP 访问的组件验收。
- `plugins/hello-node/`、`plugins/hello-go/` 与 `scripts/build_plugin_files_package.py`：任意语言插件（包描述 v6）的两种启动机制样例及打包脚本；`plugins/sdk/conformance/workspace_read_check.json` 是非 Python 插件移植读取检查时必须跑通的一致性用例，`agent_py_agent/tests/test_plugin_any_language_samples.py` 为其组件验收。
- `plugins/event-watch/`、`plugins/rm-guard/`、`plugins/rm-guard-node/`：M1 v8 事件订阅与工具收紧样例（B8）——event-watch 观察全部 6 类事件并在只读 table 面板显示收到与合并丢弃计数；rm-guard / rm-guard-node 在 `run_command` 出现 rm 加 -r 和 -f 时回 `ask`（`RM_RF`）、`delete_file` 回 `deny`（`DELETE_FILE_BLOCKED`）；`agent_py_agent/tests/test_plugin_m1_b8_samples.py` 为假宿主直连插件进程的组件验收。
- `plugins/shuohao-novel-gates/`：固定 B 的五阶段只读门插件，安装/调用仍沿既有宿主链；同源声明、来源清单和字节保留由 `agent_py_agent/tests/test_shuohao_novel_gates.py` 核对。`agent_py_agent/tests/fixtures/shuohao_skills/` 只用于原样上游自检，不随插件 ZIP 分发；本机环境失败与真实入口未验证边界见 TESTS。
- `agent_py_agent/tests/test_plugin_api_build.py`、`agent_py_agent/tests/test_workspace_peek_package.py`：实际标准包、独立环境和原 MCP/宿主管理链的开发验证，不代替真实 TUI。

- `agent_py_agent/agent/runtime_db/run_cancellation.py`：在原 RuntimeDB 上核对 task/run/agent run/attempt 四个身份并关闭执行权；原 UNKNOWN 不恢复、不释放锁，旧控制不能追随新的执行轮。
- `agent_py_agent/agent/runtime_db/run_takeover.py`：子代理被接替转 TAKEN_OVER 后的运行账收口。只在当前 attempt 已静止时用 `settle_agent_run` 写 cancelled（读到的 attempt 状态作 CAS 条件），仍在运行／排队／unknown 不动；由 `services/base.record_takeover` 在接替落账后尽力调用，失败不影响接替。
- `agent_py_agent/agent/runtime_db/run_closeout_backfill.py`：历史悬挂 run 一次性补收口迁移（rcb）。候选=run 未终态+attempt 已结束超过 6 小时宽限；逐条排除 attempt 不静止（unknown 等人工恢复）/有未结算操作/有活跃执行权锁/无 `agent_attempt.completed` 结束事实（recovered 例外：人工处置过 unknown 且无未结算操作/无锁/超宽限时补 cancelled、runtime_reason=attempt_recovered）；分族与 runtime_mixin 同口径（ok→done、取消族→cancelled、等用户/可续跑族不动、其余→failed，对拍用例钉住一致）。每条用 `settle_agent_run` 精确 CAS 补收口并写 `run_closeout.backfilled` 审计事件，task_run 用树终态 CAS 补关；迁移记录写 metadata 键 `run_closeout_backfill.v1`（重复调用空操作、处理幂等）；`preview_run_closeout_backfill(db_path)` 只读预览不改字节。由 owner_maintenance 在维护循环里调用。
- `agent_py_agent/agent/runtime_db/user_interrupt.py`：用户中断的唯一结构化事实。Gateway 与本地入口中断到一轮时追加 `agent_run.user_interrupted`（绑定被中断的执行代次），发现层账本自愈在 24 小时窗口内不把任务收成 cancelled，过期按原规则收口（原因 `user_interrupt_expired`）。
- `agent_py_agent/agent/runtime_db/run_creation.py`：在调用方原事务内创建 Task→TaskRun→AgentRun→首次 Attempt 及委托/事件，普通调用与显式宿主命令共用，不能另开事务。
- `agent_py_agent/agent/runtime_db/host_commands.py`：原事件索引与 TaskRun 冻结请求共同绑定唯一运行；本身不授予权限、不启动模型或任务调度。
- `agent_py_agent/agent/runtime_db/host_command_execution.py`：只领取原 pending，强制原操作 Store，终态沿原运行收口；查询不初始化数据库，UNKNOWN 不重跑。
- `agent_py_agent/agent/runtime_db/host_command_approval.py`：只在原执行区间等待精确批准，回到原 ToolExecutor 重新核验；拒绝、取消或无消费者都不启动 handler。
- `docs/design/HOST_COMMAND_EXECUTION.md`：显式宿主请求的登记、终态只读重放、UNKNOWN 和现有执行器接线边界。
- `agent_py_agent/agent/conversation/task_run_closeout.py`：TaskRun 收口的唯一判定（D3 规则）。执行收口边经 `runtime_mixin` 委托调用，网关 `/recover` 子代理分支直接调用；普通完成只经 `settle_task_run_if_agent_tree_terminal` 写；准确输入静默退休可经同领域判据取消本次执行账，最终SQL拒未知/活动，TaskLink不被取消。
- `agent_py_agent/agent/conversation/task_resources.py`：主链身份适配与整任务固定清单；热请求带正式绑定，无热请求才读取唯一主链，组合原子树后锁外清理，不重新选择资源。
- `agent_py_agent/agent/subagents/cancellation.py`：原创建事务内选择后代、关闭原权限并冻结资源；模型和用户控制共用，业务终态与资源退出分别保留。
- `agent_py_agent/agent/subagents/cancellation_hosts.py`：精确 attempt 中断及冻结宿主身份的只读观察；PID/launch 不构成整棵进程树的取消许可。
- `agent_py_agent/agent/subagents/runner_control.py`：原执行轮持久取消的唯一读取判据；session 心跳与模型前复核共用，不创建第二份状态。
- `agent_py_agent/agent/conversation/local_run_control.py`：plain/TUI worker 每条消息的临时控制句柄；复用运行绑定和任务晋升确认接口，执行权仍归 RuntimeDB，旧句柄不覆盖下一条消息。
- `agent_py_agent/agent/tooling/process_resource_stop.py`：固定后台停止回执与 PTY 请求分开记录；提交后的 PTY 失败不丢清单，清理不重新扫任务，也不把异步请求当成全部退出。
- `agent_py_agent/agent/tooling/process_scope.py`：后台访问、PTY/任务执行及共享激活的身份类型；不从访问或工作目录补业务身份，激活引用不授予执行权，不持有资源或执行取消。
- `agent_py_agent/agent/tooling/process_session_store.py`：受保护进程记录的统一入口，读写与裁剪先恢复同一目录的未完成提交；持锁事务提供启动检查点和精确停止意图，实际进程信号仍由调用方负责。
- `agent_py_agent/agent/tooling/background_process_launch.py`：启动方先预留再交接；stdio 三路端点直接继承给 child，失败关闭未交出管道，host 保持原寿命和精确停止控制。
- `agent_py_agent/agent/tooling/process_session_cleanup.py`：只清理原 Store 冻结的 host/child 出生实例；完整证明保存到原 termination.cleanup，原命令终态不改写，未确认及待恢复回执保留。
- `agent_py_agent/agent/tooling/process_session_records.py`：纯数据校验与单调合并；`process_session_commit.py` 只安装固定记录，目录互斥归公共 `common/directory_lock.py`，原锁名不变。v1 不隐式升级或获得任务停止授权。
- `agent_py_agent/agent/command_catalog.py`：无 UI/执行依赖的公共命令声明；原控制参数仍归会话模块，插件后缀识别不等于身份校验或可执行授权。
- `agent_py_agent/agent/command_arguments.py` 与 `command_binding.py`：参数定义、字面词法及值绑定的权威实现；部分输入及帮助／使用卡用法共用同一协议，不从展示文字反推规则。
- `agent_py_agent/agent/plugin_commands.py`：接收宿主提供的动作描述，统一参数解析、静态错误和公开使用卡；安装表与实际执行由管理服务沿原链处理；`render_mcp_server_section` 把 `mcp_server_facts` 投影成 `/plugins list` 末尾的 MCP 服务段。
- `agent_py_agent/agent/plugin_command_catalog.py`：冻结及校验完整管理/插件声明，内容摘要绑定 owner 视图、版本和激活引用；不提供权限凭证。
- `agent_py_agent/agent/command_declarations.py`：命令 JSON 的唯一读取器，包和宿主目录共用，旧目录私有 decoder 已删除。
- `agent_py_agent/agent/plugin_manifest.py` 与 `plugin_package.py`：只读校验包并保留同一字节快照；不接受宿主身份，不代表已安装、已授权或已隔离。
- `agent_py_agent/agent/plugin_events/declarations.py` 与 `confirmation.py`：v8 订阅与网络需求的唯一不可变声明及同源确认投影；不含通道、投递、钩子或实际沙箱，不改变旧包确认。
- `agent_py_agent/agent/plugin_events/protocol.py` 与 `hub.py`（M 线 B3）：观察事件的公共字段与握手能力门；事件中心按 owner 分区、按类型只留最新（`dropped_before`）、单在途、发送前后由共用通道复核代次，连接回收只碰自己 acquire 过的激活；publish 永不阻塞、永不抛异常，投递与计数都在后台，不写盘。
- `agent_py_agent/agent/plugin_events/points.py` 与 `agent_py_agent/agent/gateway_parts/event_points.py`（M 线 B4）：事件点投影与 Gateway 装配。投影只按白名单给字段、默认不带正文（只有 `prompt_submitted` 在授权信封里带 content）、提示字段先脱敏再截断、会话标识只留哈希；开关关闭时既不投影也不发布，投影或发布抛异常不外泄；Gateway 侧按提示/回合/控制回执装配最小事件，关闭时零安装表读取。
- `agent_py_agent/tests/test_plugin_event_points.py` 与 `test_plugin_event_display.py`：B4 投影与 B6 账本展示的组件验收——前者钉字段白名单、脱敏截断、授权信封、关闭短路与真实 handler 前后成对；后者直接插入临时 owner 的 `runtime.db` 行，钉 `plugin_gate.decided` 只读查询、观察计数、`/plugins info` 四段与 TUI/HTTP/IM 同文渲染、跨 owner 隔离，不启动插件进程或 Gateway。
- `agent_py_agent/agent/plugin_events/tool_gate.py`：B5纯收紧协议与稳定合并，身份仍读原ToolCall，不授予执行权；第1–5段已在17j迁移树接入，待3a集成和9b终审，非生产验收。
- `agent_py_agent/agent/plugin_events/decision_ledger.py`：决定15字段白名单、canonical信封提取及唯一runtime事件写入；无message/参数，不建批准账，新增调用点前必须先加幂等键。
- `agent_py_agent/tests/test_plugin_gate_event_combination.py`：真实rm-guard/handler/归档/唯一写账/临时RuntimeRepository/B6 info组合三项转正；假激活/传输不代宿主启用与沙箱验收。
- `agent_py_agent/tests/test_plugin_tool_gate_logic.py` 与 `tests/fixtures/plugin_tool_gate_mutation_probe.py`：B5 第一段七组合/协议/投影边界和五个内存变异，未冒充后续全链路覆盖。
- `agent_py_agent/tests/test_plugin_manifest_v8.py`：B1 静态规则、旧字节、同摘要确认和构建，含真实临时安装、原命令 v8 拒绝及 v6 确认路径兼容；`tests/fixtures/plugin_manifest_v1_v7.json` 固定基线字节，变异探针仅进程内变异、不改产品文件。
- `docs/tasks/M1_B1_HANDOFF.md`：B1 分支交接、真实验证/失败/未验边界与四个变异复现方法。
- `agent_py_agent/agent/plugin_wheels.py` 与 `plugin_wheel_layout.py`：标准元数据和固定依赖集合预检、环境内目标保护及安装后宿主读回；不运行插件或替代 pip 安装器。
- `agent_py_agent/agent/plugin_environment.py`、`plugin_environment_plan.py` 与 `plugin_environment_process.py`：计划先进入原 operation，准备沿原 owner 配额与 ProcessSessionStore；只返回准备事实，不发布激活或重建进程账。
- `agent_py_agent/agent/plugin_entry.py`、`plugin_runtime_facts.py` 与 `plugin_files_environment.py`：v6 非 Python 插件的声明、本机运行事实与解包准备；启用前生成用户确认回执，解释器按真实路径与摘要固定、每次启动复核，全程不执行随包文件或解释器（启动插件进程除外）。
- `agent_py_agent/agent/runtime_db/operation_resources.py`：精确资源准入的同事务只读检查，不能领取新代次、续租或恢复 UNKNOWN。
- `agent_py_agent/agent/plugin_installation.py` 与 `plugin_install_store.py`：原 owner 插件目录中的唯一安装表和最后回执；先存包再提交，默认停用，异常读回区分提交结果，不拥有管理权限或执行历史。
- `agent_py_agent/agent/plugin_installation_state.py` 与 `plugin_configuration.py`：原表 v3 编解码、v1/v2 明确迁移来源和完整配置替换裁决；未清理激活阻止改配置，没有第二套权威。
- `agent_py_agent/agent/plugin_activation.py` 与 `plugin_activation_record.py`：同一原计划的准备/发布/撤销 CAS，旧快照只读原代；撤销状态不证明资源退出。
- `agent_py_agent/agent/plugin_activation_ref.py`：只定位原安装表的可信引用；严格核对原 owner/root/scope，启动和发送不缓存授权。
- `docs/design/PLUGIN_ACTIVATION.md`：激活身份、持久撤销、释放/证据消费及重新启用；卸载、显式业务命令及实际多 TUI 仍待完成。
- `docs/design/PLUGIN_OBSERVATION_CANDIDATES.md`：插件观察候选结构设计稿（插件线已实施，决策点 `action_candidate` 已接入并真实验收）：只读工具在 manifest 声明观察、结果给出目标代次与有限候选，宿主铸 ID 写进原归档，动作前两层复核；是第 15 项动作候选决策点的前置条件。
- `docs/design/J16_SCREEN_OBSERVATION.md`：J16 屏幕识别设计（3a 已确认、未实施）。
  - **做什么**：在自家 Computer Use 适配器里用操作系统和库的公开接口给出稳定窗口身份、代次、坐标变换的结构化观察，接插件线同一套观察合同和 `action_candidate`；两层复核防按旧建议点错；自动执行由能力开关控制，默认关。
  - **范围**：属主范围同 Computer Use。
  - **实施与测试**：按片 A→G 实施，只在 Linux 车道 Xvfb 里测试。
- `docs/design/MANAGED_PROCESS_STDIO.md`：原托管器的字节通道、v4 显式保留和激活归属，旧 v2/v3 原版本恢复边界。
- `agent_py_agent/agent/plugin_configure_tool.py` 与 `plugin_sources.py`：隐藏管理工具通过原执行链读取授权来源，配置值只进 owner 私有安装表；包与配置共用有界安全读取。
- `agent_py_agent/agent/plugin_install_tool.py` 与 `plugin_management.py`：管理服务核对原授权并走唯一执行器；安装默认停用，配置与启停同源，列表／详情投影当前安装快照，查询只读原请求。
- `agent_py_agent/agent/plugin_enable_tool.py`：B7 前在确认码之前以 `plugin_events_disabled` 拒绝 v8；旧版沿原操作完整确认/精确撤旧，执行、准备、候选连接/发现/清理后发布前复核原授权，漂移不发布，restricted 未接底座拒启动。
- `agent_py_agent/tests/test_plugin_legacy_drift.py`、`test_plugin_legacy_candidate.py`、`test_plugin_legacy_transport.py`：原管理/Store/HostCommand 的漂移、候选失败、请求绑定、提交事实和长预览回填合同；外部进程/运输为替身，不证明 OS 或真实用户接收端。
- `agent_py_agent/agent/plugin_runtime.py` 与 `tooling/plugin_registration.py`：固定激活的 MCP 适配和新运行组合；原客户端共享连接，权限视图单独生成目录，不新建激活缓存权威。
- `agent_py_agent/tests/test_mcp_operation_outcomes.py`：通过原执行器和临时 RuntimeDB 区分完整失败回执与未知结果，核对重放及逻辑资源释放。
- `agent_py_agent/agent/workspace_read_context.py` 与 `tooling/workspace_read_scope.py`：纯读取协议和宿主组装分开，插件复用唯一 `path_access_policy.py` 裁决；范围为空明确拒绝，不从隔离进程环境补权限。
- `agent_py_agent/agent/workspace_write_context.py` 与 `tooling/workspace_write_scope.py`：写入协议同样分层；只对协商且声明写效果的插件工具下发，裁决与 `write_boundary.py` 一致且只可能更严。
- `agent_py_agent/agent/plugin_invocation.py`：显式业务调用固定原选择，工具输入不混入管理字段；原审批/执行链之外只管理本次 MCP 连接的建立与准确关闭。
- `agent_py_agent/agent/plugin_deactivation.py` 与 `plugin_disable_tool.py`：先关闭原激活与准备任务权限，再冻结两类准确资源并锁外清理；保留原退出记录，不能将撤销等同清理成功。
- `agent_py_agent/agent/plugin_release.py` 与 `plugin_cleanup.py`：前者从原 enable 执行器和资源账核验退出，后者只在 disable/remove 原结果严格成功读回后消费固定引用，并回收无人引用的旧包；不改写 UNKNOWN，不重新选择当前代。
- `agent_py_agent/agent/plugin_update.py` 与 `plugin_update_tool.py`：同一插件 ID 的新版本包替换已停用安装；同一锁内 install 回执后按新声明规范化旧配置成功再追加 configure 回执，只用既有持久动作；已启用、ID 不一致、版本变化在计划阶段拒绝。
- `agent_py_agent/agent/plugin_removal.py` 与 `plugin_remove_tool.py`：原停用释放返回完整记录后，沿原锁 CAS 删除安装；删除回执只存原操作，不建墓碑，不删除用户产物。
- `agent_py_agent/agent/tooling/process_cleanup_evidence.py`：原资源记录的最小身份摘要及完整退出证明，旧引用不能删除同 ID 新实例；不另建持久状态。
- `agent_py_agent/agent/common/nofollow_tree.py`：使用已验证父目录描述符递归删除固定目录，不沿链接越界；调用方负责先确认原进程和执行器已退出。
- `agent_py_agent/agent/common/cancellation.py`：原取消令牌、异常、ContextVar 和回调的唯一实现；全部调用方直接依赖公共层，旧 tooling 入口删除，不管理持久任务或 OS 资源。
- `agent_py_agent/agent/common/logical_reference_ids.py`：逻辑引用编号前缀（gwreq-/run-/subagent- + call_）的唯一共享常量与"前缀:call_"形态判定；编号生成点与文件写守卫共用，不参与路径解析。
- `agent_py_agent/agent/common/directory_lock.py`、`nofollow_fs.py` 与 `strict_json.py`：分别维护永久互斥、受信根文件原语和严格 JSON；这些公共原语不裁决领域授权或替代操作账本。
- `agent_py_agent/agent/common/json_io.py` 的私有写与私有锁（锁收私 + 私有写入两批 + pdp）：新建文件出生 0600、缺失目录经 no-follow 原语按 0700 建（`mkdir` 的 mode 不受 umask 放宽），存量宽权限文件下次写入收紧；**已存在的目录一律不改权限**（只动自己建的东西），符号链接段被拒。锁与数据文件同口径（0600/0700、不跟随符号链接、`blocking=False` 语义不变），覆盖 `locked_json_path` / `io/jsonl` / 派工锁 / Gateway 文件锁四处写法。宿主数据写入点（审计、协作账、run workspace、子代理账与工作日志、记忆候选/日事件/lesson/HOT、global_index、会话与用量）统一走这些原语；存量宿主目录的收紧归 owner 维护，不在每次写入时做。
- `agent_py_agent/tests/test_private_lock_permissions.py`、`test_private_writes_*.py`（conversation / audit_events / collaboration / subagents / memory / batch2）与 `test_private_dirs_policy.py`：私有写与私有锁的组件验收。前几组钉「新文件 0600、新目录 0700、存量宽权限下次写入收紧且内容逐字节不变、既有 0755 目录不再被收紧」；`test_private_dirs_policy.py`（pdp）钉 `_ensure_private_dir` 缺失按 0700 建、已存在目录（含符号链接）一位不动、可配置路径指向用户目录时只新增 0600 文件、umask 022 下建目录点仍 0700。运行时生成物不逐条列，位置见同目录 Tree 段。
- `agent_py_agent/agent/plugin_command_service.py`：从原安装表生成静态命令目录，旧或缺失版本明确拒绝；显式业务动作尚未接执行，普通工具贡献归 Registry。
- `agent_py_agent/agent/plugin_display/`：插件面板只读取已有公开活动投影，经固定代次连接调用只读 `display.render`，结果按类型校验截断；停用/换代即丢弃结果并关闭连接，见 [插件展示](docs/design/PLUGIN_DISPLAY.md)。
- `agent_py_agent/agent/gateway_parts/plugin_command_service.py`：三个 HTTP 入口共用原管理员与可信 owner；只读不初始化冷用户，获授权安装才登记原独立运行。
- `agent_py_agent/cli/chat_parts/plugin_command_client.py`：TUI、plain Gateway 与 direct 共用模式、声明缓存及查询语义；传输失败保留原编号与未知，不降级或自动重送。
- `agent_py_agent/cli/chat_parts/command_interaction.py`：每次 Enter 单独绑定审批回调、取消令牌和原 Gateway 连接；并发命令不共享可变回调或借聊天身份。
- `agent_py_agent/cli/chat_parts/plugin_command_stream.py`：网络持续读流，原面板独立等待；只在本连接有效时写回完整决定，退出关闭本命令等待。
- `agent_py_agent/agent/gateway_parts/command_stream_protocol.py`：校验原编号、完整帧和固定规范 owner；在可信服务根内派生隐藏审批地址，不从 HTTP 接收路径。
- `agent_py_agent/agent/gateway_parts/command_stream.py`：原请求线程调用一次原服务；心跳只检测断连，结果和资源退出仍由原执行链裁决。
- `agent_py_agent/cli/chat_parts/tui_plugin_commands.py`：保存首次接受候选的目录版本，参数补全不升级；网络提交沿共享分派在 UI 线程外执行。
- `agent_py_agent/agent/plugin_completion.py`：仅建议能够绑定到当前参数的值；停用插件只给静态帮助，文件枚举由宿主按显式路径声明提供。

- `agent_py_agent/cli/scenario.py`：诊断场景注册与参数目录；`scenario_cases/runner_retry_case.py` 和 `runner_retry_backend.py` 只负责保留的离线重试场景，旧结果块修复场景已删除。
- `agent_py_agent/agent/verification/runtime.py`：主子代理共用的被动验证入口；它消费工具执行事实，不裁决任意报告的语义正确性。
- `scripts/check_offline_contract_matrix.py`：开发文件完整性检查，验证证据项指向实际运行模块与测试；已删除无生产调用的旧 verifier integrity 合同及其自造数据测试。

- `docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md`：热点源码与参考阅读证据、未实施的重构顺序、Computer Use 当前条件及 Jev 可选接入方案。
- `docs/design/COMPACT_GENERATION_FACTS.md`：摘要来源与请求关联的设计（Codex 交接、集成方待实施），明确旧检查点未知和诊断不参与恢复权威。
- `docs/design/DECISION_MODEL_INTEGRATION.md`：原生决策模型的实施合同，覆盖配置复用、2/4 秒预算、缺数据、记忆/派工/能力接入、缓存窗口、并行认领与验收。
- `docs/design/DECISION_AUDIT_AND_ADMIN_CONTROLS.md`：2026-09-25 六项决策/审计要求的合同——接入点开启/观察模式、my-agent 自调等待时间上下限、选模型输入精简与评估、统计行约数、统一审计 `audit_records`、管理员 `admin_controls`。
- `agent_py_agent/agent/tooling/audit_records_tool.py`、`agent_py_agent/agent/conversation/decision_audit.py`、`agent_py_agent/agent/gateway_parts/request_audit_records.py`：唯一审计入口及其决策主题的四类权威来源读取（设置、用量账本、决策结果日志 `conversation/decision_outcome_log.py`、请求记录观察），不 grep 日志、不读正文；新审计主题只加 topic，不另建工具。决策主题另附 `conversation/decision_reach_counts.py` 的每点位未触发原因诊断（`point_diagnostics`），TUI 决策菜单经 decision_read 读同一份。
- `agent_py_agent/agent/tooling/admin_controls_tool.py`、`agent_py_agent/agent/user_space/owner_admin_controls.py`：管理员管控的唯一写入口与存取；Jev 禁用在 `conversation/decision_model_call.invoke_decision_model_call` 硬拦。
- `agent_py_agent/tests/test_decision_model_profiles.py`：验证当前模型目录 v4 的用途隔离、显式迁移、凭据复用、共享撤销及主子代理选择不误用决策模型。
- `agent_py_agent/agent/settings/decision_settings.py`：原设置界面和工具共用服务；原 owner 模型目录及线程字段保存覆盖，锁序 owner→thread，版本冲突拒绝覆写。
- `agent_py_agent/agent/conversation/decision_service.py`、`decision_policy.py`、`decision_model_call.py`：分别负责建议策略、连接隔离和实际模型调用；复用原存储/准入/取消/账本，不建立第二份任务权威。
- `agent_py_agent/agent/conversation/decision_observe_nonblocking.py`：observe 不挡主链路（开关 `observe_nonblocking_enabled`）的唯一后台执行器，只执行决策服务交来的已登记调用，负责结果行、独立用量结算与完成回调，不另建决策状态。
- `agent_py_agent/agent/memory_store/curator_routing.py`：记忆整理按“消息来源会话的主代理模型”分组的纯函数。批内第一条消息所在的组先跑，工具审计事件只跟 owner 默认模型那组；熔断历史只看同一模型、且投影到本组会话的游标；一次触发里多组结果汇总。路由表由组合根 `core._curator_model_router` 经 `settings/curator_profile.curator_thread_profiles` 只读解析后注入。
- `agent_py_agent/agent/memory_store/decision_curator.py`：原用户后台批次的临时分类/优先级建议；关闭不准备，失败保留完整输入，原提取/提交仍唯一。
- `agent_py_agent/agent/memory_store/decision_curator_relation.py`：完整来源和正式 long-term 版本的可选关系注释；复用同一阶段、原仓库和账本，没有直接合并/晋升权限。消息×正式条目对数超过上限时按标准库 BM25 词面相似度挑前 32 对（同分保持原枚举顺序、无嵌入调用），并在 `state.coverage` 声明总对数、展示对数与挑选规则。
- `agent_py_agent/tests/test_decision_curator_relation.py`：验证关系输入完整性、失效、原模型账本、作用域和 Curator 提取提交，fake 模型不代表真实语义质量。
- `agent_py_agent/tests/test_decision_curator_relation_selection.py`：验证挑对规则——相关对排在顺序枚举第 33 位之后仍被挑中、同输入同输出、空材料边界、覆盖声明与无嵌入调用。
- `agent_py_agent/agent/settings/decision_settings_schema.py`、`decision_settings_defaults.py`、`decision_settings_projection.py`：分别负责严格结构、原模块默认值映射及脱敏有效值投影，不增加配置权威位置。
- `agent_py_agent/tests/test_decision_usage_metrics.py`：验证用途分区复用原增量规则，决策输入与 LLM 总量不双计、缺报保留未知、历史基数随原文件更新。
- `agent_py_agent/agent/backends/decision_protocol.py`：冻结宿主决策材料、来源和候选版本；结果只有建议权，消费者仍要复查。
- `agent_py_agent/agent/backends/typesafe_decision.py`：独立 decide 操作，复用原连接选项、请求头、HTTP 与错误协议；实验另拆无网络 `prepare` 与必须携带发送许可的 `send`，普通请求字节不变；不实现 generate。
- `agent_py_agent/agent/backends/typesafe_decision_wire.py`：TypeSafe 问题和答案解析，单题错误与顶层协议损坏分开，完整用量交给原账本。
- `agent_py_agent/agent/backends/gateway_request_limits.py`：原 HTTP 传输的有限读取、剩余期限和严格 JSON 原语，不另建执行器。
- `agent_py_agent/agent/backends/keepalive_transport.py`：严格请求的长连接复用。只有 `GatewayRequest.connection_pool` 非空（目前只有决策调用，配置 `decision_connection_reuse_enabled`）才走；打开语义与 urllib `do_open` 对齐，非 2xx 抛 HTTPError，不重试；连接只在正文读完、未中止时由响应作用域归还。
- `agent_py_agent/agent/backends/transport_timing.py`：一次 HTTP 尝试的阶段计时器与连接包裹；只有 `provider_attempt_observer(transport_timing=True)` 才创建，阶段集合封闭，进度事件由账本只替换计时字段；决策调用据此把链路分段耗时写进结果日志。
- `agent_py_agent/agent/backends/bounded_call.py`：Curator 与后续决策共用的有界 callable；及时放弃等待与真实资源退出分别记录。
- `docs/design/PLUGIN_LIFECYCLE.md`：可选 Python 插件的核心边界、命令目录、隔离依赖、版本绑定和卡死卸载；提案与现有实现明确区分。
- `docs/design/PLUGIN_PACKAGES.md`：本地包格式、读取预算、静态校验及待实现的安装提交和隔离撤销合同。
- `docs/design/PLUGIN_ANY_LANGUAGE.md`：任意语言插件的用户决定、v6 包描述、启用确认、解释器固定、给插件作者的协议要点、读取检查移植实测与 OS 沙箱试点计划。
- `docs/design/PLUGIN_PROCESS_SANDBOX.md` 与 `agent_py_agent/agent/plugin_sandbox.py`：插件进程 OS 沙箱试点（配置 `plugin_process_sandbox`，默认关）的语义、失败拒绝与限制；沙箱参数仍只由 `tooling/sandbox.py` / `attempt/sandbox.py` 构造。
- `docs/design/PLUGIN_SAMPLE_ACCEPTANCE.md`：社区候选抽样与热度快照、10 个简易插件的最小功能、分批实现顺序和组合卸载验收；不代表已实现。
- `docs/tasks/REFACTOR_PLUGIN_GOAL.md`：发布部署前置条件、十步执行状态、每步真实多 TUI 矩阵、证据与推进条件。
- `docs/tasks/DECISION_MODEL_GOAL.md`：决策模型 P1—P5 完整范围、逐项完成条件、并行认领及分层验收状态。
- `docs/tasks/DECISION_MODEL_P1A_HANDOFF.md`：用途隔离、目录迁移、原管理表单及本地验收证据；不代替决策调用验收。
- `docs/tasks/DECISION_MODEL_P1B_HANDOFF.md`：原生协议、严格 HTTP、组件联合验收及后续有界调用边界。
- `docs/tasks/DECISION_MODEL_P1CD_HANDOFF.md`：精确取消、有界资源、Curator 迁移和原准入组合证据，保留尚未接线的设置/账本边界。
- `docs/tasks/DECISION_MODEL_P1E_HANDOFF.md`、`DECISION_MODEL_P1FG_HANDOFF.md`：共用设置、实际短决策调用及原用量展示的本地交接；真实服务与业务消费者另行验收。
- `docs/tasks/DECISION_MODEL_P2_CURATOR_HANDOFF.md`：用户后台身份、临时标注、非选择结果、lease头寸和原Curator对照证据。
- `docs/tasks/DECISION_MODEL_P5B_HANDOFF.md`：来源—正式条目关系建议的完整性/版本要求、原权限边界和 293 项离线验收交接。
- `docs/tasks/DECISION_MODEL_CHILD_LIVE_HANDOFF.md`：真实 Jev 建议与官方 M3/OpenCode 子代理实际请求、工具后续轮及失败回退的隔离证据边界。
- `docs/tasks/DECISION_MODEL_EXTERNAL_MATERIAL_ORDER_HANDOFF.md`：默认关闭的已归档网页阅读提示、原生产/消费接缝及尚未验真的效果边界。
- `docs/tasks/DECISION_MODEL_MAIN_MODEL_AUDIT.md`：主会话获得准确车道后的自动选模时点、手动覆盖、请求容量与恢复缺口；只读审计，未实现采用。
- `docs/tasks/DECISION_MODEL_MAIN_MODEL_SELECTION_HANDOFF.md`：原线程单调选择版本、显式覆盖和旧记录迁移的定向验证；尚无主会话自动采用。
- `docs/tasks/DECISION_MODEL_MAIN_MODEL_OBSERVE_HANDOFF.md`：准确 Gateway 车道后的请求级只观察建议、关闭路径与恢复诊断；自动采用另验。
- `docs/tasks/DECISION_MODEL_MAIN_MODEL_ADOPTION_HANDOFF.md`：同一准确 Gateway 工作片内的完整载荷验证、发送意图和局部失败保留；真实供应商调用仍另验。
- `docs/tasks/DECISION_MODEL_MAIN_MODEL_LIVE_HANDOFF.md`：三次普通中文隔离会话的真实 Jev/官方 MiniMax 请求、原模型安全保留及异模未验边界。
- `docs/tasks/DECISION_MODEL_NATURAL_CONFIG_FIX_HANDOFF.md`：普通 user owner 决策工具注册、可信会话、可见 CAS 回执修复及隔离 TUI 成功样本。
- `docs/tasks/DECISION_MODEL_SELF_EXPERIMENT_AUDIT.md`：原任务/用量/验证/设置权威可复用点与实验授权、请求前预算、恢复资格缺口；只读审计。
- `docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md`：原设置授权信封和原账预留首片；未有可信用户入口、完整输入 proof 或发送硬门，实验联网保持关闭。
- `docs/tasks/DECISION_MODEL_SETTINGS_RESTORE_HANDOFF.md`：同一原设置事务内 set/unset、完整 CAS、通知与后改优先的本地交接。
- `docs/tasks/DECISION_MODEL_PRE_RECALL_AUDIT.md`：说明普通聊天缺可信显式查历史意图、Jev 只选有限候选，以及额外检索前需拆开访问计数的只读审计。
- `agent_py_agent/tests/test_decision_pre_recall.py`：默认关闭、建议非选择、正式预算、来源撤销、可选检索失败与取消的定向验收。
- `agent_py_agent/agent/gateway_model_observation.py`、`agent_py_agent/tests/test_gateway_model_observation.py`：主会话请求级 Jev 观察、工作片来源冻结与关闭等价的实现和验证。
- `docs/tasks/DECISION_MODEL_SELF_LEARNING_AUDIT.md`：核对现行 lesson Candidate 来源与 Skill 提案/用户确认缺口；Jev 目前不得写正式 Skill。
- `docs/tasks/DECISION_MODEL_PLANNING_AUDIT.md`：核对已有 Todo、workflow plan、Goal 和 create_subagents 的权威边界；规划建议只提供软上下文。
- `docs/tasks/DECISION_MODEL_PLANNING_HANDOFF.md`：当前主代理的 Todo read 只追加精确已有项优先提示；保留原账本、Goal 和派工权威。
- `docs/tasks/DECISION_MODEL_DELIVERY_QUALITY_AUDIT.md`：核对原 verification、ready artifact、final/Goal/子代理权威，质量判断只能给主模型软复核焦点。
- `docs/tasks/DECISION_MODEL_ACTION_CANDIDATE_AUDIT.md`：现行 Computer Use/MCP 与未接生产的 Browser/OCR 候选边界；动作建议先需要可信 observation/candidate ID。
- `docs/tasks/DECISION_MODEL_NATURAL_CONFIG_AUDIT.md`：核对原 user_config 设置 CAS 和普通中文入口，区分工具本地通过与真实主模型实际选择、读回生效。
- `docs/tasks/DECISION_MODEL_NATURAL_CONFIG_LIVE_HANDOFF.md`：普通 user owner 的原工具快照缺 user_config，真实模型搜索文件后未修改设置；记录修复边界和隔离恢复。
- `agent_py_agent/agent/agent_core/decision_planning.py`、`agent_py_agent/tests/test_decision_planning.py`：默认关闭的 Todo 软建议、版本复核、非选择与原 read 回执等价的实现和验证。
- `docs/tasks/DECISION_MODEL_CATALOG_GENERATION_HANDOFF.md`：原私有 v5/共享 v2 随机代次、旧目录自动迁移和最终配置锁复核的定向证据。
- `docs/tasks/TUI_READING_HANDOFF.md`：本轮 TUI 修复的文件所有权、候选包、真实验收证据与部署边界。
- `docs/tasks/HANDOFF_STEP7_SUBAGENT_LIFECYCLE.md`：子代理结果链隔离工作树的提交、受影响测试、并行边界和主线集成待办。
- `docs/tasks/HANDOFF_STEP7_WAKE_PUBLICATION.md`：稳定 key 唤醒发布的半写恢复、v1 迁移、故障矩阵与未覆盖边界。
- `docs/tasks/HANDOFF_STEP7_CLOSEOUT_RECOVERY.md`：原恢复扫描的 repo/load/save/list/notify 能力边界、唯一 sweep 装配和并行通知迁移协调。

- `agent_py_agent/agent/agent_core/agent_tree/model_view.py`：保留 run 身份、状态、原因与真实 read_order；不暴露恢复目录，省略内容可沿原工具归档完整读取。
- `agent_py_agent/tests/test_agent_tree_model_view.py`：模型状态投影、终态报告可达性、状态不被省略及超长归档回读合同的定向验证。
- `agent_py_agent/agent/subagents/result_registered_artifacts.py`：从 exact run 的工具产物账本投影真实文件；自然最终回复与结构化收口共用，不扫描目录或搬运文件。
- `agent_py_agent/agent/subagents/runner_display_projection.py`：仅从已裁决的状态码和失败类型计算 TUI 标签；不读取模型正文或改变生命周期。
- `agent_py_agent/agent/subagents/runner_result_admission.py`：只接收 canonical task、结果参数和原 RuntimeDB，核对当前轮，拒绝迟到／冲突结果并写原诊断事件；不保存结果或通知父级。
- `agent_py_agent/agent/subagents/runner_completion_wake.py`：终态通知器仅持任务关联、唤醒和 task 读写能力；类内实现完成／受控取消的原交接，阶段提醒与能力申请保持独立入口。
- `agent_py_agent/agent/subagents/services/runner_result_commit.py`：在结果文件与 task 投影落盘后按原 WAL→运行账→父通知→已交付→清账顺序推进；显式接收原 RuntimeDB、保存与绑定交付回调；使用既有 runtime_closeout 恢复原语，不建立第二份权威。
- `agent_py_agent/agent/subagents/services/runtime_closeout.py`：WAL、结算、事件分页还原和逐条恢复仍沿原事实；只接显式 repo/load/save/list/notify，通知 Protocol 保留本次结果与 exact attempt；唯一生产装配在既有 capability sweep。
- `agent_py_agent/agent/subagents/runner_completion_payload.py`：从已有子代理结果构造有界完成正文和规范产物引用；根通知、递归父级和直属父交接共用，既不写状态也不投递通知。
- `agent_py_agent/agent/subagents/runner_start.py`：在原创建锁内预留准确 pending 并核对原身份；启动记录实际写入归既有 lifecycle 服务，CLI 与进程内入口直接调用服务，不保留旧转发函数。宿主已死的旧启动（`process_control.background_start_record_stale`）不当现存接纳复用，锁内先收掉旧记录。
- `agent_py_agent/agent/subagents/file_runner_start.py`：显式无数据库模式在原 canonical 启动记录中预留和消费准确身份；停止可撤销，旧快照不能覆盖新预留或激活，普通保存只可回收同一身份。
- `agent_py_agent/agent/subagents/coordination.py`：复用原 owner 创建锁协调 canonical 创建、执行轮变更和插话预留；只记录本线程持锁事实，不保存任务状态，启动和退出等待不得持锁。
- `agent_py_agent/agent/agent_core/runner/activity_diagnostics.py`：复用现有心跳与调用账，阶段长等待只通知直属父级，不强杀或自动重派。
- `agent_py_agent/tests/test_subagent_activity_diagnostics.py`：慢流、阶段诊断、通知去重、旧执行代与并发进度保存的定向验证。

- `docs/design/THREAD_GOAL_LIFECYCLE.md`：Goal 可见性、单一事件续跑、状态边界与旧冲突显式恢复的开发合同。
- `docs/design/SUBAGENT_PARALLEL_EXECUTION.md`：逐项交付、递归等待与活动诊断的现行设计和验收要求。
- `agent_py_agent/agent/conversation/goal_recovery.py`：精确恢复旧共享任务 Goal，不迁移运行中的执行，不清除源记录。
- `agent_py_agent/agent/conversation/background_tool_policy.py`：后台目录计算的唯一实现，输入结构化事实，结果供 runtime 消费；不读写会话或启动执行器。
- `agent_py_agent/agent/conversation/background_progress_policy.py`：仅接收计数和策略身份，计算无进展次数与失败退避；状态读取、租约及持久写入仍由后台调度负责。
- `agent_py_agent/agent/conversation/background_supply_backoff.py`：按会话维护供应冷却，三类消费及就绪扫描共享同一实例；配置组装归 runtime，额度与持久策略分路不进入组件。
- `agent_py_agent/agent/conversation/background_goal.py`：从原 Goal、任务和时钟领域结算异常或发布后续唤醒；精确身份、事务、CAS 与写账顺序保持，不持有调度器或完整 Agent/Store。
- `agent_py_agent/agent/conversation/background_routing.py`：按原优先级惰性读取线程与 owner 路由；普通唤醒、观察批次、冻结重投及额度通知共用，不另建路由状态或投递链。
- `agent_py_agent/agent/conversation/background_claim.py`：仅持有原 claims 域及精确能力；领取后重查终态/恢复，运行中复用共享心跳，退出先停心跳再结算本 claim，普通失败最后记账。
- `agent_py_agent/agent/conversation/background_recovery.py`：每次解析并查询当前权威恢复入口；指纹只抑制重复日志，unknown/不可读阻断不消费原来源。
- `agent_py_agent/agent/conversation/background_context.py`：显式请求接口一次准备原会话事实，再纯渲染；原任务进度对账不在重复候选渲染中执行。
- `agent_py_agent/agent/conversation/background_history_seed.py`：先按原任务范围筛选canonical原文，再按实际采用摘要覆盖移除消息；原种子和Compact来源共用视图，读取失败不伪造空历史。
- `agent_py_agent/agent/conversation/background_compact_context.py`：只读选择thread/task/turn范围并对齐后台上下文摘要；覆盖解析复用compact_summary_view；不拥有持久状态或任务权限。
- `agent_py_agent/agent/conversation/background_execution.py`：显式接收执行、存储与参数准备能力，保留同片取消、Compact 和原生历史；不选择唤醒、不投递外部消息。
- `agent_py_agent/agent/conversation/background_delivery.py`：显式交付能力连接原渠道和唯一 store；外发、过程/final 提交、审计回执与整封冻结保持原顺序，不运行模型或拥有调度状态。
- `agent_py_agent/agent/gateway_parts/workspace_scope.py`：普通消息和首次 Goal 共用目录校验；只接受宿主已存在的合法路径，声明本身不增加权限。
- `agent_py_agent/agent/gateway_parts/request_context.py`：先领取车道再按 repair、索引、Compact、历史、任务顺序准备当前快照；不拥有独立任务状态。
- `agent_py_agent/agent/gateway_parts/request_binding.py`：精确请求、task/run/attempt 和 claim 的持久桥接；保持原 T 锁、原子 JSON 更新与恢复身份。
- `agent_py_agent/agent/gateway_parts/request_history.py`：正常、停止和异常共用 canonical 历史提交及原样 repair；按 request/part 去重，索引仅作投影。
- `agent_py_agent/agent/gateway_parts/request_prompt.py`：纯渲染已有上下文和历史种子，不重新读盘或通过文字裁决权限。
- `agent_py_agent/agent/conversation/history_projection.py`：正文与原生回放共用行选择；Compact已选来源完整投影，普通展示沿原窗口，后台无需导入Gateway执行器。
- `agent_py_agent/agent/conversation/history_seed.py`：会话历史种子的只读来源与两个边界唯一解析入口（`seed_provider_history_messages`/`seed_text_messages`）；与具体历史互斥，不新增持久状态。
- `agent_py_agent/agent/gateway_parts/stream_writer.py`：维护请求级缓冲与事件顺序，组合 `stream_events.py` 的公开投影及 `stream_approval.py` 的审批交互；不拥有 canonical 历史或执行权。
- `agent_py_agent/agent/conversation/compact_carry.py`：三宿主共享完整工具归档替换和插话合并；真实循环溢出边界沿原mailbox释放，并携带完整原生IR及准确释放ID，不恢复权限或新建持久状态。
- `agent_py_agent/agent/conversation/store_usage.py`：显式接收原用量目录和线程读取/原子更新能力，持有用量事件、累计增量及数字显示，不继承消息、任务或 Goal 存储。
- `agent_py_agent/agent/conversation/store_io.py`：各领域与 transcript 共用 JSONL 读取、结构化错误和文件归档；不导入 Store，不吞坏行，不新增持久数据源。
- `agent_py_agent/agent/conversation/store_layout.py`：`store.storage` 的唯一目录和路径上下文；初始化可只读，路径方法不授权业务操作、不改变原文件名。
- `agent_py_agent/agent/conversation/store_threads.py`：`store.threads` 保存唯一线程元数据和通道索引，提供同一线程锁内的 CAS；新会话模型解析器由本领域持有。
- `agent_py_agent/agent/conversation/message_selection.py`：先冻结身份/锚点再筛正文，复用原文件，两遍原字节一致后才返回选中来源。
- `agent_py_agent/agent/conversation/message_replay.py`：保存临时行地址而非完整正文，每次重放核对原文件身份及行hash，不新增持久索引。
- `agent_py_agent/agent/conversation/native_history_replay.py`：同次摘要多次计量只准备一次最后信封位置；保持原转换与匿名语义，原地址校验不变。
- `agent_py_agent/tests/test_compact_work_measurement.py` / `test_compact_work_contracts.py`：合成大线程及精确批次边界验证，不代替生产大库/真实 Gateway RSS 验收。
- `agent_py_agent/agent/conversation/compact_message_source.py`：原摘要链的可重放原生消息视图，完整数组编码和估算共用原规则，普通可发送请求仍物化原消息。
- `agent_py_agent/tests/test_compact_source_lifetime.py`：从真实canonical加载前开始测量摘要链内存，并核对完整字符、精确ID及原CAS结果，不代替三宿主峰值验收。
- `agent_py_agent/tests/test_compact_message_source.py`：完整JSON标点和结构开销等价、不同Unicode与native消息及取消关闭的窄回归。
- `agent_py_agent/tests/test_conversation_message_selection.py`：锚点先于覆盖、缺锚点隔离、迟到追加/改写/坏行和大范围外正文低内存回归。
- `agent_py_agent/agent/conversation/message_scan.py`：固定完整行尾界和显式页字节限额，流式幂等检查沿原消息锁，无第二账本。
- `agent_py_agent/tests/test_conversation_message_scan.py`：冻结尾界、页预算、迟到追加、坏行回滚与大历史低内存幂等验证。
- `agent_py_agent/agent/conversation/store_messages.py`：`store.messages` 保持原 append-only 账本、幂等锁和字节游标；只通过显式能力校验线程及更新活动时间。
- `agent_py_agent/agent/conversation/store_tasks.py`：`store.tasks` 保存原任务关联和线程活动索引，沿原顺序更新工作区投影并关闭终态进度；不建立第二套任务状态。
- `agent_py_agent/agent/conversation/store_audits.py`：`store.audits` 直接实现 Audit 准备、发布和重启操作，共用 `tasks` 的命名锁、任务锁和索引更新；无旧方法转发。
- `agent_py_agent/agent/conversation/store_guidance.py`：`store.guidance` 组装插话领域并提供入队、认领和查询；唯一外部写能力是注入消息幂等追加。
- `agent_py_agent/agent/conversation/store_guidance_records.py`：插话回执及校验的唯一格式定义，保留显式旧数据迁移规则，不负责落盘。
- `agent_py_agent/agent/conversation/store_guidance_ledger.py`：`guidance.ledger` 共用原目录与精确回合锁，读取回执并修复队列、回合及输入反查投影。
- `agent_py_agent/agent/conversation/store_guidance_submission.py`：`guidance.submissions` 管理模型调用提交批次及明确拒绝后的恢复，保留先批次后回执的顺序。
- `agent_py_agent/agent/conversation/store_guidance_acknowledgements.py`：`guidance.acknowledgements` 提交消费确认，幂等修复回执和原消息账本。
- `agent_py_agent/agent/conversation/store_guidance_recovery.py`：`guidance.recovery` 先修批次再结算或恢复；网络重试按旧/新 turn 排序及原回执锁，只为最新 pending 预留候选，逐条改绑共用原持久写入顺序。
- `agent_py_agent/agent/conversation/store_index.py`：观察、唤醒和策略共用有界扫描缓存；失效回读权威文件，目录不可读不能被当成空目录。
- `agent_py_agent/agent/conversation/store_claims.py`：`store.claims` 复用同一 storage 和原子文件更新；按结构化宿主、TTL、claim/task ID 领取与释放执行权，已结束旧租约由原维护周期归档。
- `agent_py_agent/agent/conversation/store_goals.py`：`store.goals` 持有原目标集合和 CAS，显式接收线程校验、任务读取及共享时钟；不建立新的执行或恢复状态。
- `agent_py_agent/agent/conversation/store_observations.py`：`store.observations` 管理观察账与确认回执；同一事件构造供唤醒联合发布复用，线程活动沿原子回调更新。
- `agent_py_agent/agent/conversation/store_wakes.py`：`store.wakes` 组装唤醒和配对观察、更新线程活动，保留投递冻结与处理回执；只通过注入能力确认观察。
- `agent_py_agent/agent/conversation/store_wake_publication.py`：只接原 storage，在原 dedupe 锁内冻结完整发布并安装原 wake／观察，按固定 ID 恢复、显式迁移 v1；查询只读，通用新代与保留 handled 分别由结构化策略决定。已结案（`failed_permanently`）是第三个安装位置，同键再发布返回原结案信号、不新开一代。
- `agent_py_agent/agent/conversation/store_wake_attempts.py`：`store.wakes.attempts`，每条唤醒一份尝试账（失败计数唯一权威）、结案记录与重放留档；判定委托 `wake_poison.py`，结案顺序比照 `mark_handled` 并同步结掉关联观察。设计见 `docs/design/WAKE_POISON_PILL.md`，测试 `test_wake_poison.py`、`test_wake_attempt_store.py`。
- `agent_py_agent/agent/conversation/wake_attempt_tracking.py`：唤醒车道的毒丸接线。`track_wake_attempt` 包住一批唤醒的执行并在退出时逐条记账、到上限结案；作为 `background_claim` 的领取观察者（`begin`/`settled`）；`wake_attempt_deferred`/`wake_attempt_waiting` 给跳过阶段与就绪扫描用同一判据读持久退避；`inflight_attempts()` 给优雅停机找在途尝试。测试 `test_wake_attempt_wiring.py`（真实链路）、`test_background_claim_attempt_observer.py`。
- `agent_py_agent/agent/conversation/wake_domain_closeout.py`：唤醒毒丸领域收尾与"领域已是终态"判定的唯一位置。结案后派活任务收成 failed 带 `failure_code` 并回报发送方，会话消息回执不动、只留注明仍待投递的宿主提示（做法 2）；派活正文已放弃时经 `close_out_source` 收任务；长时间不计数提醒留提示；`wake_domain_status`（终态状态，没结束为空串）与 `wake_domain_terminal` 供第 4 步人工重放与拒绝提示判定。测试 `test_wake_domain_closeout.py`。
- `agent_py_agent/agent/conversation/store_wake_quarantine_archive.py`：账本整理周期里把满 14 天的唤醒结案记录、坏账留档和读不出的信封移进 `quarantine/archive/`，只移不删；归档是发布层的只读安装位置，发布语义不变。测试 `test_wake_quarantine_archive.py`。
- `agent_py_agent/agent/conversation/store_progress.py`：`store.progress` 负责策略 CRUD、到期读取及失败退避事实落账；策略/claim 跨域归档仍由 Store 原维护入口顺序协调。
- `agent_py_agent/agent/conversation/goal_clock.py`：前台、后台及控制视图共用同 owner 会话存储的目标时钟，四个计时操作直接归共享对象，整数结算保留小数余量。
- `agent_py_agent/agent/conversation/goal_binding.py`、`goal_delegation.py`：按代理自身 thread/run 归属目标和用量；显式子目标沿同一运行器续接，不另建执行通道。
- `agent_py_agent/agent/conversation/goal_control.py`、`goal_editing.py`：用户修改自己会话树的目标；内容版本防止并发覆盖，不把保存当作恢复。
- `agent_py_agent/cli/chat_parts/tui_goal_editor.py`：方向键选择 Goal、Enter 编辑、Ctrl+S 保存、Ctrl+G 放弃退出，Esc 保留停止。

- `cli/chat_parts/tui_complete_detail.py`、`tui_display_archive.py`：Ctrl+E 完整浏览的有界分页和异步读取，长行不因终端宽度丢字。
- `cli/chat_parts/tui_reading.py`：阅读锚点与内部页交换的纯显示计算，展开/收起保留当前消息位置。
- `agent/conversation/history_order.py`：复用 canonical 行次序与显示检查点，恢复插话位置，不写模型历史。
- `agent/conversation/display_archive.py`、`agent/gateway_parts/display_archive_service.py`：完整原文以 owner 私有不可变页保存，前端只有引用；跨 owner、无关会话或任意磁盘路径均拒绝。
- `agent/agent_core/tool_loop/display_archive.py`：在工具显示投影裁剪之前存真实执行快照；不读取当前文件重建历史，也不改变 LLM 工具输入。

- `agent_py_agent/cli/chat_parts/tui_clipboard.py`：串行投影显式选区，最新代次回执与退出清理，不读取或持久化系统剪贴板；本机复制命令与 tmux 缓冲子进程 helper 也在此处，均有两秒超时。
- `agent_py_agent/cli/chat_parts/tui_actions.py`：TUI 按键的副作用动作（Gateway 记忆/控制/补充消息提交与持久对账、本地排队、子代理插话/停止、Esc 中断）；运行时不导入 tui_keybindings。

- `agent/conversation/compact_tool_refs.py`：checkpoint 的历史路径投影；仅认结构化原生调用和成功回执，不解析命令/摘要或赋予权限。
- `agent/conversation/compact_text_source.py`：两遍长度/hash 校验与可释放顺序窗口；取消或来源变化时拒绝候选，不推进 canonical 游标。
- `agent/backends/request_content.py`：摘要分段的纯文字可表示性判断，不读取媒体或推断供应商能力。
- `agent/conversation/compact_request_budget.py`：当前模型窗口内的摘要请求预算和连续分段；不持有历史游标或另建状态源。
- `agent/conversation/compact_landmarks.py`：压缩摘要末尾“原话备份”的唯一实现。预算按 token 随模型窗口放大（窗口 10%，上限 `compact_landmark_max_tokens`），用户原话优先、最新优先，放不下的那条保留头尾；每条带 message_id，省略的用户消息列编号，可选回查说明。两遍处理：第一遍流式只记编号与 token，第二遍按下标只重读被选中的行。
- `agent/capability/session_history_read.py`：`session_search` 的会话原文读取。message_id 分段读回只读 canonical 消息文件；当前会话浏览/检索只认宿主可信会话，结果带 scope_resolution。
- `agent_py_agent/vendor/bubblewrap/`：离线 bwrap 的第三方许可与对应源码材料；升级二进制时同步更新并验包。
- `agent/common/text_file_window.py`：64 KiB 流式索引、有限检查点与页面 cookie；编码和字符坐标只保留一个实现。
- `agent/common/file_version.py`：read_file 返回观察版本，write/edit/patch 明确携带前置条件；外部写入者不被强制纳管。
- `agent/tooling/process_output_capture.py`：前台进程每流最多保留 4 MiB，仍持续排空并公开不完整事实，不假装完整大输出归档。
- `agent_py_agent/agent/tooling/shell_syntax.py`：后台语法的唯一判断入口，复用公共命令分析器识别嵌套 Shell；动态变量与任意脚本不做静态安全证明。
- `agent/tooling/computer_text_input.py`：文本事件的唯一适配入口；macOS 使用 Quartz Unicode，其他平台使用 PyAutoGUI 支持的字符，结果不代替应用状态验收。
- `agent_py_agent/tests/test_r223_audit_regressions.py`：本轮底层故障样本；真实 TUI 另记在 R223 审计报告，不相互冒充。
- 已删除 `agent/agent_core/_compression_service.py` 的旧记忆拼接路径；正式 Compact 仍由 ConversationStore/checkpoint/CAS 负责。

- `agent/tooling/tool_search_state.py`：只读最新结构化工具轮的未消费工具名称；不修改授权、缓存归档或运行状态。
- `agent_py_agent/tests/test_tool_search_state.py`：验证当前轮、旧尾记录、损坏信封和自然语言不能授予临时工具。

- `agent/gateway_parts/main_activity.py`：前台请求与后台共用 main 标量；绑定当前 task、隔离迟到工作片，不驱动执行。
- `agent/gateway_parts/foreground_transcript.py`：同会话公开过程、候选增量与 final 快照，不拥有运行或审批权限。
- `agent_py_agent/tests/test_gateway_foreground_transcript.py`：前台 writer、消息顺序、缺帧恢复、候选接替与隔离验证。
- `agent/gateway_parts/model_profile_service.py`：authenticated owner 的模型菜单接口，不经聊天队列或模型。
- `cli/gateway_host_guard.py`：Gateway 服务进程启动时写入托管进程号，工具子进程继承；stop/restart/start --force 据此拒绝停掉托管自己的 Gateway。
- `agent/gateway_parts/restart_service.py`：Gateway 安全重启的唯一状态源；工具、`/restart` 只写请求，排空与换进程由服务循环经 `cli/gateway_restart_handover.py` 执行。
- `agent/gateway_parts/turn_recovery_control.py`：`/recover` 不带编号时保持主链优先、唯一子代理才处置；带编号时直达同事务范围复核；`/recover owner` 仅可信 local/main 管理员查看、预览并确认空 thread 历史 unknown。所有写入复用同一个 unknown→recovered CAS，不重放旧操作。
- `agent_py_agent/agent/runtime_db/child_recovery.py`：`/recover` 线程运行库入口。只读投影按 `tasks.thread_id` 找未关 TaskRun 里 unknown 的非根代理；带编号写入口在 `BEGIN IMMEDIATE` 内复核 opaque 编号仍属该 thread/open TaskRun/current unknown，再复用主链恢复 CAS，给出不存在、越界、已变化原因。
- `agent_py_agent/agent/runtime_db/owner_recovery.py`：`/recover owner` 运行库入口。只读投影限定 owner、空 thread 和 current unknown，不读任务/会话正文；确认码绑定完整编号与 attempt 集合，锁内复核后逐条复用共享 CAS，并以批次事件提供幂等统计回执。
- `agent/gateway_parts/end_task_control.py`：`/endtask` 只处理“定时执行 waiting、会话任务 active、执行树没有未结束 attempt”的任务；列表与预览只读，确认后经 `tasks.update_status(cancelled, expected_status=active)` 与 `reconcile_waiting_run` 收口，仅管理员可用。
- `agent/gateway_parts/wake_ops_control.py`：`/wakes` 列出已结案的后台唤醒，`/wakes replay <ID>` 预览、带 confirm 经 `store.wakes.attempts.replay` 重放；来源、pending 冲突与领域终态核对不过就拒绝且不改文件，仅管理员可用。测试 `test_wake_ops_control.py`。
- `agent/gateway_parts/admin_control_service.py`：`/admin`、`/approve`、`/deny` 的唯一执行入口；只信任认证 scope 与结构化私聊类型，批准只写原 permission bridge 的精确决定文件。
- `agent/gateway_parts/skill_control_service.py`：聊天 `/skills` 的唯一执行入口，TUI 与 IM 共用；按控制范围解析 owner（与 `/model` 同一解析），提案确认/拒绝必须带用户看到的版本号并由服务端锁内复核，列提案时调用审核顺序点；自动总结 Skill 走 `skill_learning_report`；回执不含本机路径，不暴露给模型。
- `agent/capability/skill_learning_report.py`：自动总结 Skill 的状态推导（active/user_modified/missing）、列表、详情与用户回滚/删除，CLI `skills learned` 与聊天 `/skills learned` 共用。
- `agent/capability/skill_usage_ledger.py`：在原owner的 `data/skill_learning/` 写有界 `usage_ledger.jsonl` 与原子 `usage_summary.json`，仅存身份、来源与计数，不存正文。`effective_uses` 只计显式正文读取和实际宿主注入；明细序号与汇总水位支持中断后补记，裁剪不重置生命周期总数；使用失败仅告警，不阻断原工具。学习队列的热区与溢出、候选同名合并与可确认归档、`registry.daily_history` 的每日总结LLM调用另按各自口径记录，不互相冒充。候选迁移通过有界元数据意图和前后完整哈希恢复，只读查询投影一致状态，写入口按原锁与配额收敛，不放宽用户确认或发布CAS。
- `agent/user_space/admin_password.py` 与 `admin_channel_identity.py`：管理员密码（scrypt、节流）与 IM 身份绑定的唯一权威，位于 `config/`；owner 解析经 `request_worker.admin_channel_identity_for_request` 精确匹配。
- `cli/admin_identity_commands.py`：本机管理员设置密码与管理绑定的唯一 CLI 入口；设计见 `docs/design/ADMIN_CHANNEL_IDENTITY.md`。
- `agent/gateway_parts/owner_conversation_store.py`：沿原配置与 owner 路径组装模型菜单和插件管理共用的轻量会话 Store，不初始化完整 Agent。
- `agent/gateway_parts/approval_mode_service.py`：认证 owner 的权限菜单服务，不允许正文伪造管理员。
- `agent/user_space/approval_mode.py`：既有 owner 工具策略里的唯一用户审批模式读写与运行快照映射。
- `agent/user_space/owner_access.py`：原 core 路径权限裁决的唯一实现；冷管理与完整代理共用，不能替代 HTTP 管理角色认证。
- `agent/settings/model_profiles.py` 与 `model_scope.py`：用户模型存储和运行快照；敏感配置位于宿主 config/model-profiles，非业务目录。
- `agent/settings/model_oauth.py`、`model_oauth_schema.py`、`model_oauth_wire.py`：账号登录的状态、校验与协议；说明见 `docs/design/MODEL_OAUTH.md`。
- `agent/backends/oauth.py`、`oauth_transport.py` 和 `cli/chat_parts/tui_model_auth.py`：认证与原模型后端的适配及私密登录交互。
- `cli/chat_parts/tui_browser_login.py`：ChatGPT 订阅的浏览器登录（授权码 + PKCE），登录期间在本机回环端口临时收回调；说明见 `docs/design/MODEL_OAUTH.md`。
- `agent/settings/thread_model_selection.py`：按 canonical thread 固定模型，同 owner 多 TUI 不串配置，默认值只初始化新会话。
- `agent/settings/shared_model_catalog.py` 与 `cli/chat_parts/tui_shared_model_menu.py`：管理员逐模型发布共享引用；秘密留在原 provider 文件，撤销后明确提示而非换模型。
- `cli/chat_parts/tui_effort_menu.py`：TUI 单独 `/effort` 的档位菜单；只产出 `/effort <值>`，发送仍走控制出站箱。
- `cli/chat_parts/tui_model_menu.py`：真实 TUI 模型菜单，顶层五项；保存/返回与模型执行分离。
- `cli/chat_parts/tui_model_add.py` 与 `agent/settings/model_connections.py`：「新增模型」填连接→拉列表→勾选，`add_models` 一次保存并按连接去重；说明见 `docs/design/TUI_MODEL_PROFILES.md`。
- `cli/chat_parts/tui_provider_menu.py`：同一个 provider 管理多个模型；敏感字段仅表单暂存，短测试明确提示消耗。
- `agent/settings/model_provider_*.py`：v2 存储 schema/锁内修改/用户主动网络操作，配置只在 owner 私有文件存在一份。
- `agent/backends/provider_headers.py`、`responses.py`、`responses_wire.py`：统一身份与三种协议；不复制其他产品认证身份。
- `agent/backends/responses_failure.py`：Responses 流里 error／response.failed 的唯一分类点，决定走压缩恢复、额度、限流还是临时重试。
- `agent/backends/responses_websocket.py`：订阅登录的 Responses 走 WebSocket（服务商声明 prefer_websockets，SSE 长输出会断），说明见 `docs/design/MODEL_OAUTH.md`。
- `agent/backends/base.py`、`http.py`、`openai_chat.py`、`anthropic.py`、`factory.py`：公共合同、网络传输、协议适配和构造分工；包级公开导入指向唯一实现，内部调用方不依赖旧文件转发。
- `agent/backends/request_scope.py`：同 Gateway 前台端点占用和请求局部预算；外部程序及代理别名不作推断。
- `agent/backends/cache_diagnostics.py`：真实 HTTP 请求的无正文摘要，不修改模型请求或记忆。
- `agent/backends/wire_contract.py`：出站协议合同的唯一位置。Chat、Messages、Responses 三个组包入口先用 `repair_native_messages` 修整规范原生历史的副本，再用 `validate_*` 按协议复核最终请求体，违规抛 `PROVIDER_REQUEST_SHAPE_INVALID`（`ProviderRequestRejectedError` 子类）且不发送。设计见 `docs/design/PROVIDER_WIRE_CONTRACT.md`，测试 `test_wire_contract.py`。
- `agent/conversation/process_events.py`：原进程记录到原 wake 队列的耐久终态通知；不新增任务状态机。
- `agent/conversation/task_follow_up.py`：会话任务后续工作事实的唯一判定（活跃 Goal、guidance、子代理、已终态未交回的子代理完成、唤醒、后台命令、进度策略），读失败按项目报告、按记录归属限定；`agent/scheduler/active_run_closeout.py`（`close_active_run` 收口一轮没做完的定时执行、`settle_stale_waiting` 解开存量 waiting）据此决定等待还是结算成受阻。测试 `test_scheduler_waiting_deadlock.py`。
- `agent/backends/sampling.py`：YAML/profile/backend 共用 top_p 数值校验；已知 Flash 方言默认与任意端点显式覆盖分开。
- `agent/backends/reasoning_control.py::_anthropic_budget_decision`：本次上限下的合法预算或空区间原因，出站和回执共用，不抬高上限；`describe_config_reasoning_effect` 用工厂同源的常规输出 cap，具体请求仍使用请求局部 cap。测试 `test_anthropic_reasoning_budget.py`，合同见智能程度设计第 10 节。
- `agent/backends/reasoning_control.py` 与 `agent/settings/reasoning_effort.py`：智能程度的换算与解析。档位是会话线程属性（`/effort`、子代理 `effort`、全局 `model_reasoning_effort`），模型档案 `reasoning_control` 决定怎样发送；真实请求与两处自动选模投影共用 `request_reasoning_options`。设计见 `docs/design/REASONING_EFFORT.md`，测试 `test_reasoning_effort.py`。`agent/settings/reasoning_probe.py` 与 `reasoning_probe_judge.py`：宿主侧检测当前模型是否真的支持按档位调节（后台 9 次短请求，只比较 usage 的 token），确认支持后经参数中心写 `reasoning_control: effort`；设计见同文第 8 节，测试 `test_reasoning_probe.py`。`agent/conversation/host_notices.py`：宿主提示的唯一存取点（线程 `pending_host_notices`），Gateway 前台回复提交时取走并随最终回复显示；设计见 `docs/design/HOST_NOTICES.md`，测试 `test_host_notices.py`。
- `agent/backends/structured_output_mode.py`：结构化输出方式的换算。模型档案 `structured_output`（auto/native/json_object）经 `model_structured_output` 进入后端，`openai_chat.generate_structured` 按它发 `json_schema` 或 JSON 对象模式（schema 写进提示）；记忆整理和自动总结 Skill 都走这里。测试 `test_structured_output_mode.py`。
- `agent_py_agent/tests/test_gateway_main_activity.py`：前后台数值/阶段共享、任务晋升、跨会话拒绝、迟到关闭和显示故障验证。
- `agent_py_agent/tests/test_shell_stdin.py`：使用独立宿主管道验证批处理 EOF、输入不串和显式管道，不读取真实用户输入。
- `agent_py_agent/tests/test_shell_foreground_cleanup.py`：验证前台普通退出先清理后代再回收组长，身份缺失／复用拒绝发信号，原命令结果与清理 UNKNOWN 分开。
- `agent/conversation/history_page.py`：显示专用倒读页与工作片身份，双游标不回灌模型。
- `cli/chat_parts/tui_history.py`：每个 TUI 单一的按需旧页读取器，不持有后台任务或历史副本。

- `agent_py_agent/tests/test_conversation_message_stream.py`：canonical 消息分页、恢复竞态、损坏游标和实时/历史幂等验证。
- `agent_py_agent/tests/test_conversation_display_checkpoint.py`：未final主/子过程恢复、输入不变、精确覆盖与未知卡片补齐。
- `agent_py_agent/tests/test_background_history_snapshot.py`：完整后台块快照、慢客户端补帧、损坏快照拒绝重基、
  Compact 重调工具身份和 provider 历史不变验证。
- `agent_py_agent/tests/test_background_owner_delivery_commit.py`：后台答复 canonical 落账与外部投递解耦、
  未注册渠道 fail-closed、未送达正文冻结重投不重跑模型、审计回执不冒领和后台报告日志字段守卫。
- `agent/conversation/message_stream.py`：正文及显式协商检查点的唯一公开增量投影，复用 ConversationStore，不另存 notices 正文。
- `agent/conversation/display_checkpoint.py`：公开完整块的typed检查点，保存到同thread会话账本并排除模型/Compact/Memory。
- `agent/conversation/context_usage.py`：各代理最近上下文的统一数字投影；与 Working、模型输入、计费和校准分离。
- `agent/conversation/model_metrics.py`、`cli/chat_parts/tui_model_metrics.py`：按当前代理会话显示累计消耗，按工作片显示模型轮和当轮工具；缺报、重试、重放、压缩与线程隔离按统一数值协议处理。
- `agent/conversation/background_history.py`：按稳定块 ID 保存后台完整终态展示，逐 token 增量不重复存储；
  canonical final 提交前不授权 TUI 丢弃旧事件，不进入 provider history 或 Compact 输入。

- `agent_py_agent/tests/test_owner_home_workspace.py`：家目录主/子代理读写、跨 owner/符号链接拒绝、控制文件保护、普通目录名与缓存稳定整理指南。
- `agent_py_agent/tests/test_timeout_recovery_delivery.py`：门槛5 超时探针的无损交付合同——两枪已产生的合法答复按到达顺序逐字保留（不做长度二选一、不读正文做语义判定）、交付对象与 prompt/usage 同属终答那一轮、截断/零工具轮/预算用尽轮语义不变。
- `agent_py_agent/tests/test_store_scan_indexes.py`：唤醒队列/观察分片/进度策略三类台账读取侧索引的安全验收——有索引、硬关索引、索引损坏三态结果逐字一致；条目数与记录数两条预算各自独立生效（LRU）、目录删名清理不误删不可读目录、枚举成员与 `Path.glob` 同口径（断链符号链接等非普通条目照旧产生 load_error）。
- `agent_py_agent/tests/test_tui_injected_input_states.py`：插话三段的展示合同——排队(等待送入当前回合)/已提交(已进入本次提供方调用的 prompt，立即按原提交位置进入可见历史，但未获模型确认)/已确认(唯一能清标记的边界)；身份只用 client message_id/request_id/provider_call_id，重复事件与重连重放按块 ID 去重，不丢不重。
- `agent_py_agent/tests/test_slow_model_liveness.py`：慢模型/长任务活性合同——提供方层流式只看空闲(总墙钟守卫让位、非流式才显式超时)、客户端等待只由机器可观测活动续期且无总上限、服务端租约心跳在静默期持续推进、长工具滚动续租；四类(连接/首包、流式无进展、总时长、明确服务失败)分开断言。
- `agent_py_agent/tests/test_gateway_admission_wait.py`：合法排队等待的合同——被准入限流或恢复退避时worker 在请求文件写结构化 admission_wait_*（不写 status/lease/模型字段）、有节流与总预算、超预算留一次性过期事实；客户端仅凭该活动续期，停写/取消/终态/崩溃恢复仍按空闲窗口收口，含真实 worker+客户端端到端与负向对照。
- `agent_py_agent/tests/test_gateway_dispatcher_resilience.py`：派发线程永不停机合同——注入 ENOSPC 的错误打印失败与段内/段外异常都不杀线程、后续请求照常认领；tick 卡住/线程退出在心跳与 `/status` 里可见（`dispatcher_alive`、退出错误、`gateway_request_loop_exited` 事件）；扫描门对 glob 之后 rename 进来的请求下一轮必扫。
- `agent_py_agent/tests/test_adapter_state_write_resilience.py`：通道适配器常驻进程合同——周期状态写入撞 ENOSPC 不杀进程、下一轮恢复并把失败次数写进状态文件；异常退出先停适配器、删 pid 文件、状态写 failed，状态写不进去也留 ERROR 日志；`adapter_process_facts` 只按 adapter.pid 进程存活判活、只读不清理。
- `agent_py_agent/tests/test_gateway_loop_backoff_coverage.py`：Gateway 全部后台循环的退避/限流/记账覆盖——维护、调度器到期、孤儿恢复循环出错等 max(自身间隔, 退避)，心跳循环只限流不退避，派发 tick 段内错误空闲时退避、请求在流动时不减速；`runtime_error_report` 对包装异常沿原因链把 OSError/sqlite3.OperationalError 归为 io 并带 cause_type。
- `agent_py_agent/tests/test_guidance_receipt_digest_compat.py`：插话幂等回执指纹的持久化稳定性合同——固定样本的 v1/v2 指纹十六进制钉死，a646a4885 之前口径写的回执能读、同键重试回同一条、记了版本的严格按版本；对账遇到旧回执自然收敛为排队；真坏账保持 terminal_unknown 但不重写文件、计入 summary 并经 raise_if_input_reconcile_unsettled 抛给循环守卫。
- `scripts/bench/decision_quality_bench.py` 与 `scripts/bench/decision_quality/`：决策模型中文质量基准（J12）。用例经各点位真实材料构造生成请求，按可接受答案集合打分；`thresholds.json` 给全部点位定阈值，`results.json` 登记成绩。`test_decision_quality_bench.py` 强制：随包默认打开的点位必须有用例与材料摘要都和当前一致、且达标的成绩。
- `scripts/bench/`：GW-03 与慢模型 A 项的长期可复跑配对基准（`measure_scheduler_reads.py` 锁内解析成本、`bench_owner_fact_kind.py` 各路径单测、`paired_owner_fact_kind.py` 两 checkout 交替比较）。只含结构化夹具，无真实会话内容；README 说明"锁更短 ≠ 整体更快"与"跨组数字不可相除"的证据边界。
- `agent_py_agent/tests/test_scheduler_scan_costs.py`：调度账本读取成本的合同——waiting 投影缓存的锁外探针 + 锁内三重校验（命中/外部改写/同 stat 不同内容/缓存清空都不得改变调用方结果）、`runtime_snapshot` 的锁外解析与旧实现参考投影逐字一致且锁内不再做 JSON/逐 run 解析、owner 事实判定缓存的签名失效与 TTL 边界。


- `agent/conversation/history_display.py`：同一 canonical message/native envelope 的只读公开显示转换入口，
  Gateway 与本地恢复共用；不拥有模型上下文、Compact、工具执行或第二份会话账本。

```text
~/.my-agent/owners/<provider>/<kind-or-id>/
|-- SOUL.md                             # AI 人格；用户明确确认后经 PersonaRepository 更新
|-- USER.md                             # 当前用户明确表达的稳定画像与偏好
|-- AGENTS.md                           # 长期合作方式；模型经 PersonaRepository 自主维护
|-- memory-hot.md                      # 每轮必读的少量高频短规则与精确 lesson 引用
|-- memory.md                          # 短导航，不保存第二份事实或教训正文
|-- memory/
|   |-- long_term/memory.jsonl         # 当前 active 正式长期事实唯一权威
|   |-- daily/YYYY-MM-DD.jsonl         # Curator 提炼的经历摘要和 refs，不是正式事实
|   |-- candidates.jsonl               # owner 唯一候选账本与状态机
|   |-- ops.jsonl                      # 无正文正式 Memory 操作审计
|   |-- curator/state.json             # 所有触发共用的 lease、cursor 与失败状态
|   |-- curator/runs/YYYY-MM-DD.jsonl  # 后台运行审计
|   |-- lessons/*.md                   # 可复用正式教训唯一详细正文
|   `-- routing/INDEX.md               # 由 lesson metadata 确定性重建的路由索引
|-- audit/YYYY-MM-DD.jsonl              # raw turn/tool/gateway 黑盒索引；非完整对话权威
|-- tasks/<date>/<task-slug>/           # 模型自行整理的用户工作；命名仅为软提示，旧目录不搬迁
|   |-- inputs/                         # 按需保留来源材料
|   `-- output/                         # 用户成品；代码按实际项目结构组织
|-- runs/<date>/<runtime-key>/          # 宿主运行归档，不作为 cwd 或普通文件权限根
|   |-- output/                         # 宿主兼容交接区，不是新业务默认落点
|   `-- work/                           # 状态、日志、代理交接与大输出归档
|-- agents/<run_id>/                    # 子代理 refs-only projection
|-- data/artifact_backups/v1/           # 前台 shell 改动 ready 产物时保留的 owner 私有哈希恢复 blob
|-- data/skill_proposals/<id>.json      # 子代理经验 Skill 提案（开关开启后按需创建）；自学习开启时自动走确认链装到 skills/lesson-*；不用 learning_drafts 目录名
|-- data/skill_learning/                # 自动总结 Skill：requests/、registry.json、ledger.jsonl、versions/、removed/（开关开启后按需创建）
|-- skills/learned/<name>/SKILL.md      # 自动总结发布的 Skill；归属只由 data/skill_learning/registry.json 认定
|-- workspace/runtime/workspaces/<scope>/# LocalStore、gateway、conversation 等 workspace 账本
`-- global_index/                       # 可重建轻量索引

~/.my-agent/shared/                     # 管理员显式发布的公共 skills/tools/role templates；不放 owner 私有资料
```

普通运行不读写 repo 根 `data/*` 作为事实源；测试 fixture 或用户显式配置路径除外。

- `agent_py_agent/agent/tooling/mcp_transport.py`、`mcp_protocol.py`：请求和线程永远绑定原连接；临时清理与永久关闭分开，不持有插件激活权威。
- `agent_py_agent/agent/tooling/mcp_managed_process.py`：插件连接只走原托管启动，输出交给唯一文本读取端，原资源锁内复查原激活，清理保留完整 Store 回执与异常。
- `docs/design/MCP_TRANSPORT_LIFECYCLE.md`：MCP 的关闭、重连、发布顺序与未知清理边界；对应开发用例为 `agent_py_agent/tests/test_mcp_lifecycle.py`。

- `agent_py_agent/agent/agent_core/orchestration/decision_subagent.py`：创建前可选模型建议，复用原目录、准备/物化、原工具快照和幂等身份。
- `agent_py_agent/agent/agent_core/tool_request_projection.py`：真实工具轮与创建前容量共用 PromptBuilder/IR 格式，不刷新宿主、不探测或发送；完整输入缺失时明确返回 unknown。
- `agent_py_agent/agent/agent_core/subagent/model_selection.py`：子代理首业务模型请求前复核 Jev 建议、完整依赖与持久目录代次；原线程一次 CAS 采用或保留，未知不触发逐 child 用户操作。
- `agent_py_agent/agent/model_request_selection.py`、`agent_py_agent/agent/gateway_model_adoption.py`：主会话经原模型请求回调验证完整输入、工具、窗口及目录代次，只在实际发送前提交准确线程的自动选择；发送前明确拒绝才沿原模型一次。
- `agent_py_agent/agent/gateway_compact_context.py`：原 Gateway 恢复请求与 core 展示之间的应用层编排，同 turn 重载不复活已失效的展示建议。
- `agent_py_agent/agent/gateway_compact_recovery.py`：Gateway 已绑定请求溢出后，冻结原恢复准备，只替换候选历史与证据；原 CAS 后用同一请求继续生成。
- `agent_py_agent/agent/conversation/compact_projection.py`：只读来源与完整候选投影的临时值合同；获选材料只随原 CAS 成功返回，不形成第二持久状态。
- `agent_py_agent/agent/conversation/compact_calibration.py`：压缩候选与压缩前计量的唯一校准口径（纯函数）。宿主在 `PreparedCompactRecovery.select` 按同一表面指纹冻结供应商观测后传入；候选比观测小按比例折算（下限 50%），不小于观测沿预检追加口径，无观测原样返回。改公式要同步预检 `_provider_calibrated_context_tokens` 的注释与 `test_compact_calibrated_candidate_gate.py`。
- `agent_py_agent/agent/runtime_context.py`：当前 runner 的线程本地属性权威供 Tooling、Conversation 与 core 共用，作用域退出清理；不保存持久任务身份。
- `agent_py_agent/agent/settings/decision_experiment_schema.py`、`decision_experiment.py`、`agent_py_agent/agent/conversation/decision_experiment.py`、`agent_py_agent/agent/contracts/model_call_budget.py`：宿主专用有限许可（v2 信封记录用户接受的经验上界口径）、原账带标签上界预留、单次发送许可消费与保守结算。
- `agent_py_agent/agent/gateway_parts/request_experiment.py`：`/experiment` 的唯一产品授权入口；只读入口冻结的 system_task，在主轮绑定 run/attempt 后、首个模型调用前于原回合锁内写 granting→granted/rejected 回执，失败只提示用户。
- `agent_py_agent/agent/conversation/decision_send_permit.py`、`agent_py_agent/agent/backends/provider_send_gate.py`：实验发送硬门；传输层在最终字节生成后、任何 DNS/连接/遥测前调用许可，拒绝抛不被重包装的 `ProviderSendRefused`。
- `agent_py_agent/tests/test_decision_experiment_authorization.py`、`test_model_call_input_budget.py`、`test_decision_experiment_send_gate.py`、`test_decision_experiment_command.py`：许可/预算/发送门/授权入口的合同与本地 HTTP 组合回归，统计 TCP accept，不调用真实供应商。
- `agent_py_agent/agent/capability/decision_experiment_sample.py`、`agent_py_agent/agent/gateway_parts/request_experiment_records.py`：E2 对照记录（身份、配置版本、基线/候选名单、原账结算视图）只写 Gateway 请求记录的 `experiment_records`；回合正常收尾按结构化工具账补写实际用量，停止/关闭不补写，普通请求零 I/O。
- `agent_py_agent/agent/conversation/decision_experiment_evaluation.py`、`agent_py_agent/agent/gateway_parts/request_experiment_promotion.py`：F1 只读证据评估（≥3 可比较、全部 charged、召回 1.0、有节省）与 `/experiment apply` 授权内经原设置 CAS 的一次性晋升；回执在请求记录 `experiment_records.promotion`，用户后改优先。
- `agent_py_agent/agent/gateway_parts/request_experiment_notice.py`：只把新 applied 回执变成原 host_notice，点位/模式/样本/门槛取回执冻结值，稳定 notice_id 沿原请求去重；当轮 TUI 与同会话 IM final 共用原提示链，历史回执不补投。
- `agent_py_agent/tests/test_decision_experiment_records.py`、`test_decision_experiment_evaluation.py`、`test_decision_experiment_promotion.py`、`test_decision_experiment_gateway_turn.py`：E2/F1 合同、写入器、规则矩阵、证据链、CAS 晋升与真实 Gateway 回合组合回归，不调用真实供应商。
- `agent_py_agent/tests/test_user_config_owner_scope.py`：普通 user 主回合 read/patch 的可信线程及 CAS 预览、跨 owner/子代理拒绝和 main_agent 原能力回归。
- `agent_py_agent/tests/test_gateway_model_adoption.py`：Gateway 到原生成投影、最终发送和线程 CAS 的本地 HTTP 替身矩阵，不当作供应商真实验收。
- `agent_py_agent/tests/test_gateway_compact_recovery.py`：原Gateway overflow到同次恢复发送的完整材料对照；失败边界由原CAS、停止及模型生成链验证。
- `agent_py_agent/tests/test_gateway_compact_recovery_continuation.py`：恢复后的真实工具往返及无transcript来源的活动回合回退，核对新代次和准备顺序。
- `agent_py_agent/tests/test_subagent_first_request_selection.py`、`test_model_scope_dependencies.py`：首请求/后续轮真实载荷、撤销竞争、取消与作用域生命周期的定向验证。
- `agent_py_agent/tests/test_tool_request_projection.py`：验证完整 system/动态段/schema/IR 与真实 provider payload 一致，缺输入 unknown、原历史不改和纯渲染不读宿主。
- `agent_py_agent/tests/test_gateway_capability_compact.py`：验证同工作片能力展示在 Gateway、子代理 transcript Compact 及重建输入中保留，并覆盖新片清空和失效回原面。
- `agent_py_agent/tests/test_subagent_capability_compact.py`、`test_background_capability_compact.py`：实际宿主循环与 Compact 接续的展示载体回归；比较 provider 输入，并区分未评估、已评估无建议和显式空选择。
- `agent_py_agent/agent/memory_store/decision_recall.py`：原召回预算后排序及有界补充查询，消费前复核正式来源，不扩大权限或新增记忆存储。
- `agent_py_agent/agent/agent_core/tool_context/external_material_order.py`：原工具归档之后仅追加阅读提示，复用决策期限/账本并保留来源权威。
- `agent_py_agent/tests/test_decision_external_material_order.py`、`test_external_material_order_integration.py`：安全输入、失效、取消、原页面归档链和设置消费的离线证据。
- `agent_py_agent/agent/agent_core/tool_context/decision_delivery_quality.py`：新验证或成功写入让已有焦点 stale 后，可选追加复核提示；写入只选多个 stale，两种触发共用本轮逐记录一次；外发不含路径/命令/输出，不改结果、归档、验证账或收口。
- `agent_py_agent/tests/test_decision_delivery_quality.py`、`test_decision_delivery_quality_integration.py`、`test_decision_delivery_stale.py`：资格、隐私、非选择、来源复核、取消、写后触发及去重，以及原验证账到 text/native 展示与假 Jev worker 的离线证据，不代表真实模型采用。
- `agent_py_agent/agent/agent_core/tool_context/decision_action_candidate.py`：插件只读观察工具的归档带宿主铸造的观察候选时，可选请决策模型选一个下一步先核对的候选并追加软提示；新鲜度只问插件线的 `plugin_observation` 权威，不生成参数；材料里带几何派生的归一化粗位置；建议采用后交 `decision_action_execute` 决定要不要计划一次宿主自动执行，本模块不执行工具。
- `agent_py_agent/agent/agent_core/tool_context/decision_action_execute.py`：J16 第 7 节的自动执行规划与记账：能力开关 `action_candidate_auto_execute_enabled`、本机管理员主代理、所选候选恰有一个只凭候选编号的动作、同一观察没被碰过（runtime_events 的 `observation_action`）时把 `actor=decision` 的宿主 ToolCall 计划进 `params.host_actions`；执行由 `tool_loop/round_execution` 走模型调用同一条链；决策账补充行与工具账共用 `decision_ref`。
- `agent_py_agent/tests/test_decision_action_candidate.py`：资格、隐私、非选择、新鲜度与来源复核、取消、text/native 同段提示和设置入口；两项集成用例经真实写入口 `persist_tool_runtime_ledger`（无 `runtime_gate` 的归档）接插件线真实新鲜度权威。
- `agent_py_agent/tests/test_decision_action_execute.py`：片 D 的假 Jev 四档、粗位置、自动执行各生效条件、两本账 `actor`/`decision_ref`、审批/失败/中断只记不重试、不进原生 IR；归档与权威事件走真实写入口。
- `agent_py_agent/agent/capability/decision_candidates.py`、`decision_recommendation.py`：原授权能力候选与单工作片推荐消费者；一次调用、旧建议复核、按需展示，原Registry/Skill快照及搜索执行仍唯一。
- `docs/tasks/DECISION_MODEL_P2_SUBAGENT_HANDOFF.md`：子代理选择生产接线、联合验证和完整窗口待验边界。
- `docs/tasks/DECISION_MODEL_P3_RECALL_HANDOFF.md`：记忆排序、来源撤销、原本轮复用及本地验证交接。
- `docs/tasks/DECISION_MODEL_TAKEOVER_HANDOFF.md`：暂停后交由新agent的完整P1—P5目标、进度快照、最新证据、共享所有权及下一步。
- `docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md`：完整请求、创建前容量与 Compact 展示面的实际诊断和分工边界。
- `docs/tasks/DECISION_MODEL_REAL_VALIDATION.md`：隔离 Jev、官方 MiniMax 与 TUI 的真实调用、失败和验收证据。

- `agent_py_agent/agent/settings/decision_probe.py`：显式连接测试，复用唯一原生后端、调用账和独立用量结算，不改变设置。
- `agent_py_agent/cli/chat_parts/tui_decision_menu.py`：从原 `/model` 进入的共用设置表单和探测回执；当前有效值与来源由原服务给出。
- `docs/tasks/DECISION_MODEL_SETTINGS_SCOPE_HANDOFF.md`、`DECISION_MODEL_TUI_HANDOFF.md`：后台范围和真实按键设置的本地证据。

- `docs/tasks/DECISION_MODEL_SETTINGS_HANDOFF.md`：原模型操作、原生连接测试、双HTTP运输与独立用量结算的组合交接。

- `docs/tasks/DECISION_MODEL_USER_CONFIG_OPERATIONS_HANDOFF.md`：原主代理配置工具的脱敏决策目录、显式连接测试与真实工具状态。

- `agent_py_agent/agent/agent_core/tool_request_capture.py`：正常选模和压缩恢复共用真实请求捕获；未知历史不填零或空。
- `agent_py_agent/agent/agent_core/compact_request_recovery.py`：共享真实提示冻结、完整候选计量、原CAS提交及同次继续生成；宿主只提供历史投影。
- `agent_py_agent/agent/agent_core/subagent/compact_recovery.py`：child只替换自身历史和原第0注入，权限与首请求选模不变。
- `agent_py_agent/tests/test_subagent_compact_recovery.py`：两协议原生HTTP恢复载荷对照和失败无业务发送。

- `agent_py_agent/agent/agent_core/compact_active_projection.py`：按IR结构化source替换carried交接和活动摘要，冻结请求计量与发送共用；完整归档不裁剪。
- `agent_py_agent/agent/conversation/background_compact_recovery.py`：将后台已准备上下文、同scope历史或活动归档投影为公共恢复候选，不拥有调度和投递状态。

- `agent_py_agent/agent/conversation/compact_tool_summary.py`：完整渲染被选工具的原模型可见投影，供transcript和活动归档共用原分段摘要；不读外置全文，不将展示截断当作来源覆盖。

- `agent_py_agent/agent/agent_core/compact_tool_partition.py`：复用原 ToolCall 四元身份与时序配对，冻结真实 IR/归档的共同来源及保留区；不读存储、不调用模型、不新增持久状态。
- `agent_py_agent/tests/test_compact_tool_ref_archive_chain.py`：以真实归档/读取/投影链验证外置文本、同组内联结果、来源引用与恢复attempt的Compact分区；零模型/网络，摘要材料仍是原模型可见IR。
- `agent/conversation/input_media.py`：入站媒体唯一文件/ref 合同；`cli/chat_parts/tui_media.py`、`tui_media_clipboard.py` 只处理输入动作。
- `docs/design/TUI_INPUT_MEDIA.md`：新媒体能力、迁移、平台和供应商边界。
- `agent_py_agent/tests/test_input_media.py`：字节、归属、历史恢复和媒体预算合同测试。

- `agent/backends/request_content.py`：原内容完整性检查，不读文件、不估算视觉token；主子选模与Compact共用。
- `agent_py_agent/tests/test_compact_media_recovery.py`、`test_compact_transcript_media_partition.py`、`test_request_content_capacity.py`：媒体完整往返、准确覆盖和未知容量保留的隔离验收。

- `agent_py_agent/tests/test_compact_retained_history.py`：保留历史首尾、原生媒体/工具配对、三宿主候选与两协议最终HTTP载荷，以及超容量拒绝不提交。

- `agent_py_agent/agent/conversation/compact_text_source.py`：原摘要循环的临时顺序字符源，EOF/hash验证后才能返回摘要；不落盘、不写检查点。
- `agent_py_agent/tests/test_compact_text_source.py`：编码源完整性、有限窗口、取消与迭代器释放、纠正请求预算及大批消息峰值验证。

- `agent_py_agent/tests/test_compact_capacity_facts.py`：两条压缩链候选过大时的容量计量，从错误对象、failed 进度、公开白名单一直到 TUI 失败行；包括选中最小候选、无候选或已接受时不带计量。
- `agent_py_agent/tests/test_background_active_turn_carry.py`：子代理生命周期唤醒片续接原用户回合的工具事实；锁 owner 根与任务两处索引按精确请求编号流式读取、别的请求与别的 run 不进来，前台记录只进运行时状态（去重、已执行工具、工具轮数），溢出压缩携带保留，本片新增工具轮额度不变。
- `agent_py_agent/agent/agent_core/runtime/turn_trigger.py`：回合触发类型 `TurnTrigger` 的唯一定义；只按 kind 决定当前回合用“# User Task”还是“# Host Event”开头、推荐区是否用固定短名单、哪些宿主快照不进后续重放，不解析事实正文，也不授权。
- `agent_py_agent/agent/conversation/lifecycle_wake_event.py`：把子代理生命周期唤醒信封投影成 `TurnTrigger`（字段白名单、键排序、有界）；不收原始结果 JSON；原任务不在历史里（已核实的 Goal 任务来源或唤醒没有请求编号）时才附有界原任务，`origin_request_ids` 只放真实历史请求。
- `agent_py_agent/tests/test_lifecycle_wake_host_event.py`：锁唤醒事实投影、原生 IR/文本 prompt/缓存布局的宿主事件开头、固定推荐短名单、唤醒片历史不重放后台注入；并用按“最新 User Task”行事的脚本化假模型走真实后台链路，证明唤醒片不再重复派工、一律派工的变体被去重门拦下；历史读不全时，写明 replacement_for_run_ids 接替已完成子代理的派工照常放行并落账为 superseded。
- `agent_py_agent/agent/subagents/services/takeover/record.py`：接管/接替边的唯一落账入口。已结束来源（DONE/ABANDONED/CANCELLED）终态不改写，只追加 `superseded_by` 与一条 TakeoverRecord；未结束来源照旧转 TAKEN_OVER；保存后重读核对，未落盘抛 `TakeoverNotPersistedError` 且不写 TAKEOVER.md。
- `agent_py_agent/tests/test_subagent_done_supersede.py`：锁已结束子代理被接替的完整合同：状态保持并记 superseded、BLOCKED 仍 TAKEN_OVER、落账入口与回执两道落盘核对、第二次接替被预检拒绝、已关闭记录的单调追加白名单与同状态旧快照、接管 run 按 superseded_by 幂等、kernel 与 list_agents 视图的 replaced_by。
- `agent_py_agent/tests/test_background_child_control_tools.py`：锁后台子代理整合档与 goal 子代理两档在策略收紧前包含 `DIRECT_CHILD_CONTROL_TOOLS` 全部工具（原顺序不变、末尾补缺），owner/task/显式配置收紧只做减法，真实后台运行参数与注册表快照里生命周期唤醒片能用 `list_agents`。
- `agent_py_agent/tests/test_compact_capacity_host_chain.py`：走真实 `PreparedCompactRecovery` 两个入口（三宿主 transcript、联合来源、活动回合），只替身摘要与末端 HTTP；锁固定开销经宿主只计量入口只在失败时量一次、测不出缺失，保留 IR 按候选实际发送材料计，以及与候选替换规则的等价。
- `agent_py_agent/tests/test_task_run_settle_quiescent_children.py`：`settle_task_run_if_agent_tree_terminal` 的树判定回归：根终态 + BLOCKED 子 run 能关 TaskRun 并在 `task_run.closed` 留静止子 run 证据；子 run 再起 attempt 经 `task_run.reopened` 重开；attempt 在跑/仍持锁/根未终态/无 attempt 都保持开放；发现扫描能关掉存量；pending 激活的 started 事件按各自列写。
- `agent_py_agent/tests/test_owner_wake_discovery_task_run_no_link.py`：发现层 TaskRun 重放的"没有会话任务"分支（C12c）：只有规范关联目录列全、任何目录都没有 `<task_id>.json`、根执行轮已终态时才按 `no_conversation_task` 走树终态 CAS。
- `agent_py_agent/tests/test_run_closeout_settlement.py`：回合收口统一推进 agent_run/task_run（rco）。Gateway 回合走真实 `_run_with_params` 收口链路（只替换 `_run_once_with_params` 等外围副作用），四条结束路径逐条断言 run/task_run 终态与事件：正常完成→done、协议违规 blocked→failed、取消→cancelled、执行错误→failed；等用户族（needs_user_input/approval_required）保留 created 只关 attempt（runtime_db 无 waiting 状态、不新造）；可续跑族保留非终态；子代理失败族映射（BLOCKED/CHANNEL_ERROR/TIMEOUT→failed）与 BLOCKED 端到端（settle_runtime_run_for_result→run failed + agent_run.completed）。去掉统一收口或 BLOCKED 映射的变异分别被对应用例杀死。
- `agent_py_agent/tests/test_run_closeout_backfill.py`：rcb 迁移用例——三条悬挂按分族补收口（blocked→failed、user_stop→cancelled、ok→done）并核对事件/迁移记录/task_run 关闭；可续跑族、等用户族、最近活动、未结算操作、活跃执行锁、unknown attempt、无结束事实七类不动；recovered→cancelled（runtime_reason=attempt_recovered）且 recovered 有未结算操作/执行锁时不动；迁移只执行一次（第二次空操作、记录不变）；预览只读（库文件字节与 mtime 不变）且计数与应用一致（含 recovered_cancelled 细分）；分族与 runtime_mixin 对拍；owner 维护集成（首轮迁移、次轮 already_migrated）。
- `agent_py_agent/tests/test_compact_calibrated_candidate_gate.py`：候选接受门与预检同一校准口径的回归：纯校准函数与预检逐项相等、宿主边界按 fingerprint/代次冻结观测、transcript 与活动回合两条门在估算偏高 43% 时接受候选且恰好等于上限仍拒绝、进度事件的结构化触发来源，以及隔离 home 两回合假 LLM 复现（接受后下一次真实预检与接受基准一致、失败路径线程快照不被原始值误导、无观测行为不变）。
- `agent_py_agent/tests/test_compact_output_reserve.py`：真实冻结请求与本地输出预留门组合，当前要求和工具schema保留，过界零业务发送/覆盖提交，Responses普通及OAuth未知上限分开验证。
- `agent_py_agent/tests/test_session_task_real_chain.py`：会话互通的真实链路门禁，每次交付先用它把关。前台走真实 Gateway ask，后台走 Gateway 同款调度器，唤醒、认领、run_claimed、回合装配都是真实的，只替换供应商传输；覆盖派活与消息唤醒的正向对照、list_owner_sessions 列会话后按清单派活或发消息（不含别的 owner、不含正文）、取消的四个窗口（调用中打断两窗、后端不响应停止、停止旗丢失）、同一对会话连发三条（按单条消息去重）、空转回归与兜底、默认链深 4，已知未修项用 strict xfail（只接受 AssertionError，链路断掉抛 RealChainBroken）。

- `agent_py_agent/tests/test_model_selection_isolation.py`：复用原Gateway、线程CAS、模型目录与校准入口验证并发隔离和窗口变化；仅HTTP与决策回复为替身。

- `agent_py_agent/agent/conversation/compact_checkpoint_scan.py`：固定单次账本描述符及EOF，完整解析后按行地址/hash读取已提交候选，内存索引随作用域释放。
- `agent_py_agent/tests/test_compact_checkpoint_stream.py`：对照原检查点读取峰值并验证坏orphan、重复ID、Unicode/末行、晚追加、改写和异常关闭。

- `agent_py_agent/tests/test_native_history_projection_memory.py`：单次隔离复制与完整Compact投影内存对照、不同调用及匿名重复输出隔离、原生往返与孤儿修补保真。
- `agent/tooling/runtime_facts.py`：只读canonical执行事实，保留未知与独立清理确认；不复制PID、诊断正文或授予执行权。宿主停机未启动调用的关门原因（`project_host_shutdown_facts`）也只在这里清洗，档案和索引两侧共用。
- `agent/conversation/input_media.py`：入站媒体唯一文件/ref 合同；`cli/chat_parts/tui_media.py`、`tui_media_clipboard.py` 只处理输入动作。
- `docs/design/TUI_INPUT_MEDIA.md`：新媒体能力、迁移、平台和供应商边界。
- `agent_py_agent/tests/test_input_media.py`：字节、归属、历史恢复和媒体预算合同测试。

- `docs/tasks/TUI_STEER_CONTROL_HANDOFF.md`：TUI 插话与停止 T 线的持续实施记录、测试证据和集成边界。
- `docs/design/LONG_TASK_RUNTIME_REVIEW.md`：长任务 Goal、模型采用与持久调度的审查和 L 线执行依据。
- `docs/design/SCHEDULER_EXECUTION_POLICIES.md`：S1–S3 的配置、同一执行身份、交付与检查点；实现入口为 `scheduler/execution.py`、`policy.py`、`precheck.py`、`run_preparation.py`。
- `agent_py_agent/agent/agent_core/model/selection_refresh.py`、`selection_history.py`：显式选模安全点与历史兼容；只复用 canonical thread、原 Compact 和调用账。
- `agent_py_agent/agent/agent_core/tool_only_execution.py`：宿主显式只读工具的原权限、取消、归档、事件与执行收口入口，不构造模型请求。
- `docs/tasks/LONG_TASK_RUNTIME_HANDOFF.md`：L/T 固定候选的验证、安装证据和剩余边界，作为后续评审导航。
- `agent_py_agent/agent/conversation/goal_transition.py`：Goal 最近变更和进度投影，模型 complete 不等同独立验收。
- `agent_py_agent/agent/gateway_parts/execution_target.py`、`execution_control.py`：T 候选从原 claim 与回合绑定读取、固定真实控制目标；L 组合沿同一入口。

发布新增领域入口说明：`conversation/session_task_dispatch.py` 持有原任务记录、guidance、wake 和配额副作用，原模型工具只校验/授权后调用；`conversation/im_task_status.py` 读取原孩子终态集合，不让 Gateway 直接依赖子代理内部实现。两者不创建第二套存储。

- `docs/design/IM_SECURITY_AUDIT_20261013.md`：本期攻击事实、原权威修复、覆盖矩阵和未验边界。
- `docs/tasks/IM_TASK_SCHEDULER_SECURITY_HANDOFF.md`：Session 目标完整性与 actor/监控安全验收交接。
- `docs/tasks/MODEL_SHUTDOWN_PROVENANCE_HANDOFF.md`：停机晚到模型结果沿原 closure/restart_resume 处理，不绕来源门。

- `agent_py_agent/agent/auth/configuration.py`：启动前校验部署边界并构造原鉴权中间件；不读取秘密、不新建状态。

## IM 凭据与权限专项评估入口（2026-10-14）

```text
`-- docs/design/IM_SECURITY_CREDENTIAL_ASSESSMENT_20261014.md # 普通/群权限、恶意脚本和受管/明文凭据传播的证据与边界
```

该报告记录测试与未实施设计，不能替代实际沙箱/卡片/外发结果，也不是新的权限或凭据事实源。

`docs/design/FEISHU_CLI_INTEGRATION_ASSESSMENT.md`：用户调整为 CLI 优先后的可行性、身份使用方式、官方扩展接口与隔离测试记录；尚未增加产品入口。

`docs/design/IM_UNATTENDED_EXECUTION_REVIEW.md`：一个管理员和大量员工的非交互执行目标、审批卡去重/超时重询、活执行锁与会话消息安全点、管理员历史策略差异和待实施路线；不作为产品修复完成。

`docs/design/IM_CAPABILITY_DISCOVERY_AUDIT.md`：原工具/Skill发现链、真实权限状态缺失、普通worker/后台目录及开关披露差异；保留测试红和现场未知边界。

S01–S05关键职责：`scheduler/creator_access.py`只读原身份索引与官方完整当前成员，不另建权限账；所有写操作沿`control.authorize_job`，列表和计数只读当前owner。原线程累计模型指标不跨空间合并。

## 本期 Skill 维护、迁移与计数诊断入口

```text
agent_py_agent/agent/capability/
|   |   |-- skill_learning_publish.py  # S3 用户确认后的受控发布：重名/删过的名字/上限/所有权 hash/脱敏/解析/guard，回滚与删除
|   |   |-- asset_promotion.py # S1/S3/回合提炼共用registry出处、语义族、四载体选择及机器晋升公开入口
|   |   |-- skill_learning_promotion.py # 默认静态核验/真实宿主证明复用、同族候选与安全发布接线；自主关闭仍待审
|   |   |-- skill_learning_validation.py # 名称/手工目标/脱敏/方法正文/Python/工作流DAG确定性检查，不冒充业务执行
|   |   |-- skill_learning_budget.py # 共享home根的UTC日部署总模型预算，与owner预算并守
|   |   |-- skill_learning_proposals.py # 待确认S3候选，准确修订的人类确认/拒绝后才走原发布
|   |   |-- skill_learning_host_feedback.py # 原验证命令/包检查器准确采用归因、唯一技能无目标反馈、默认机器核验及原registry证明绑定
|   |   |-- skill_learning_maintenance.py # 原registry的观察、固定/停用、冷藏、可恢复归档与推荐
|   |   |-- skill_learning_protection.py # 原任务/子代理/未删除监控refs保护与完整性
|   |   |-- skill_learning_migration.py # 按原committed来源与hash纳管S1，保原路径和身份
|   |   |-- skill_feedback_tool.py # 当前任务结构化采用声明，不能冒充人类结果或自批
|   |   |-- skill_learning_versions.py # 自学单调修订与不可变正文快照固定，撤销后禁止读历史
|   |-- test_skill_learning_confirmation.py # S3仅候选、精确确认/拒绝、停用阻旧确认及IM用户入口
|   |-- test_wave7_autonomous_learning_closure.py # W2自包含F01–F17及R2六项修复回归；CAS/冻结/输出合同限W2自身入口
|   |-- test_wave7_w1_integration_contracts.py # W1三项原普通断言原样保留，正常收集、不skip/xfail，跨线集成须另验
|   |-- test_skill_learning_maintenance.py # 采用与质量分离、引用保护/归档回滚、52条真实库与缓存失效
|   |-- test_skill_learning_versions.py # 自学CAS/回滚交错、版本不可覆盖和旧快照正文回归
|   |   |-- skill_transfer.py           # 精确清单签名、来源授权与接收入口
|   |   |-- skill_transfer_manifest.py  # 当前owner快照与显式资源安全清单
|   |   |-- skill_transfer_import.py    # 原配额/registry事务下整批接收与恢复
|   |   |-- skill_transfer_memory.py    # 明确记忆候选转入目标原待审核账
|   |   |-- skill_transfer_control.py   # 原IM/TUI人类双端确认与群成员证明
agent_py_agent/tests/
|-- test_monitor_counts_degraded.py  # 账本不可读或孤儿run时计数未知，不伪装为零
|-- test_skill_transfer.py  # 双端确认、资源隔离、一次复刻与撤回竞态
`-- test_skill_learning_host_feedback.py  # 宿主准确run/hash验证归因
```

Skill维护见 `docs/tasks/SKILL_MAINTENANCE_HANDOFF.md`，显式投递见 `docs/tasks/SKILL_TRANSFER_HANDOFF.md`；监控计数故障交接见 `docs/tasks/MONITOR_COUNT_UNKNOWN_HANDOFF.md`。

`tests/test_managed_operation_recovery_fences.py`：同attempt取消/终态及跨root重放生产API回归；交接见 `docs/tasks/S07_AUTHORITY_FENCE_HANDOFF.md`。

`tests/test_skill_summarize_owner_scope.py`：普通/群当前claim只产候选与准确本人确认；`tests/test_skill_transfer_grant_race.py`：授权撤回线性化、反向并发锁序及资源父链接回归。

## 交互卡取消入口

```text
agent_py_agent/agent/
|-- user_space/interaction_actor_audit.py  # 原owner安全点击者审计与显示
|-- user_space/interaction_cancellation.py  # 模型/凭据原卡取消回执
|-- gateway_parts/model_control_dispatch.py  # 模型子卡锁外投递与父卡CAS
`-- session_lock/card_control.py  # 原密码用户行卡片nonce和取消生命周期
agent_py_agent/tests/
|-- test_card_cancellation.py  # 凭据/模型空表单、并发与输入收口
`-- test_card_cancellation_controls.py  # 审批、密码、OAuth、人设取消及迟到回执
```

交接见 `docs/tasks/CARD_CANCELLATION_HANDOFF.md`。
2026-10-15 凭据持续使用与认证拒绝通知：沿原凭据记录、原调度 run 和原 Wake/outbox 扩展，不新增授权库或通知队列。

```text
|-- agent_py_agent/agent/conversation/required_user_action.py # 原后台任务的需用户处理事实、表达与投递前资格核验
|-- agent_py_agent/tests/test_scheduler_user_action_notice.py # 静默监控认证拒绝、来源隔离、取消和原outbox重投
|-- agent_py_agent/tests/test_background_result_identity.py # 任意业务结果、宿主精确身份交接与Compact末次attempt回归
|-- agent_py_agent/tests/test_required_notice_reply_slot.py # 普通与必要通知共同canonical槽、精确工作身份及补账故障窗
|-- agent_py_agent/tests/test_background_reply_candidates.py # 来源工具回执候选筛选、无候选零读取actor及有候选原身份/来源拒绝
|-- docs/tasks/CREDENTIAL_HTTP_FAILURE_HANDOFF.md # HTTP认证失败分类与同代单次拒绝记录的交接
`-- docs/tasks/CREDENTIAL_REJECTION_NOTICE_HANDOFF.md # 原后台通知主链、测试证据与边界交接
```

关键职责：`required_user_action` 只读取原工具记录与凭据代次，持久义务仍属于原 Wake；授权继续由原 owner/actor/root/job/origin 决定。生命周期与HTTP直接回归入口见下方凭据文件条目。

本轮 CI 合同修复新增长期测试入口：

```text
agent_py_agent/tests/
|-- support/ci_authenticated_gateway.py    # 经原Gateway凭据与会话事实构造可信客户端
|-- support/ci_host_context_fixtures.py    # 冻结原run/owner/actor的测试宿主身份
|-- test_recursive_host_actor.py          # canonical子孙谱系与跨owner/外部缺身份负例
`-- test_request_owner_roundtrip.py       # 已接受owner的读写往返与拒绝式路径校验
docs/tasks/
`-- CI_BASELINE_REPAIR_20261016.md         # 上轮main CI失败分组、真实修复与验证边界
```

- `agent_core/compact_transcript_prefix.py` 只校验同次主投影与连续覆盖，不签发权限；`test_context_usage_binding.py` 与三个 `test_f7_*_contract.py` 验证采样CAS和当前缓存/队列账本。

## 六项统一验收支持（2026-10-16）

```text
agent_py_agent/tests/
|-- test_compact_independent_contracts.py # 连续前缀、媒体屏障、有界恢复与迟到上下文统计的独立回归
`-- support/
    |-- ci_cli_owner_fixtures.py # CLI旧替身使用真实临时owner路径，不补造权限
    `-- ci_release_layout.py # 源码/正式pyc布局的准确来源与子进程校验
```

上述测试支持只服务隔离验收；不是新的产品权限、记忆或运行时权威。

### 片A关键文件说明

owner_resources只计算配置路径不签权；owner_services仅持原三对象并执行原保存链，不拥有生命周期。两者不让冷入口自动可用；HTTP/seal/真正C末writer另片接合。

## 正Count角色生命周期关键文件（127候选）

`multiprocess_generation_metadata.py`沿原唯一历史路径保存纯count和显式v2关闭范围；`multiprocess_drain_runtime.py`只由原producer完整封口后一次提交。五份`test_gateway_role_lifecycle_*`是最终新包必须实际执行的运行行为回归；合同见[原角色生命周期](docs/design/GATEWAY_ROLE_LIFECYCLE_CLOSURE.md)。

## 本机插话固定对账回归（154候选）

`test_local_control_reconciliation_writer.py`保原callee结果与expected回执绑定；普通commit不能借结果内容跨过unknown，最终两OS包pyc必须实际执行。

当前W心跳双读验证入口：`agent_py_agent/tests/test_gateway_role_heartbeat_stability.py`，复用原P4/producer与受控物理端点，覆盖动态变化不永久隔离及静态/旧代来源继续拒绝；不是source-only或真实native证明。

原子snapshot读验证入口：`agent_py_agent/tests/test_nofollow_atomic_snapshot.py`，仅普通临时文件及原受控W producer，核严格有界A/B替换窗口与父链/权限/FD拒绝；须最终两OS包pyc实跑，不是source-only或写者/物理证明。

普通准入关闭运行验证入口：`agent_py_agent/tests/test_gateway_admission_closing.py`，仅原临时组件和受控线程核A1/A2共门；停止前无终态的occupied仍不封存，须最终双OS包pyc运行。

当前C元数据运行验证入口：`agent_py_agent/tests/test_gateway_c_state_publication.py`，只用原临时control/P4/完整reader及异线程屏障验证controller→lease；须最终双OS新包pyc运行，非source-only或实际native证明。

当前新增运行验证：角色源只读取规范责任，辅助JSONL和精确暂存仍核实际FD与目录身份；ready管理回执不扩普通perform权限。上列五测试入口属于最终两平台包内运行义务，不能归入源码专用或用组件结果替代实际验收。

原话备份收缩运行验证入口：`agent_py_agent/tests/test_compact_landmark_remeasure.py`，核完整投影、原容量门、单次重算与中断；修改的原landmarks节点和原mixed长路径节点同属两平台包内运行义务。

原native安全诊断回归入口 `agent_py_agent/tests/test_native_transport_diagnostic.py` 覆盖固定字段、异常隐私与原ACK/责任边界，不替代真实三代或发行门。

`test_gateway_closed_generation_progress.py`只用临时格式fixture及原心跳/归档writer验证旧闭包消费；固定expected/covered/committed不变、本代责任自然推进不授当前终态或关闭权，新增行为须最终双OS新包pyc实跑。


委派交付原来源核验统一读`conversation/im_delegation_delivery.py`；单发话及欠账恢复回归见`tests/test_im_delegation_single_speaker.py`。无新持久目录或schema。

会话审批缓存回归补充：`test_mcp_approval_sink_reuse.py`同时检查真实主claim的缺失/错配运行来源和原后台publisher、前台writer正例；只fake审批运输。

能力申请回归入口：`agent_py_agent/tests/test_capability_grant_reuse_501.py`使用真实临时canonical任务/grant，覆盖显式工作根、同授权复用和完整结构权限负例。

能力授权覆盖回归补充：同入口检查持久grant根/祖先symlink漂移，以及已声明宽祖先根包含home时每个foreign/宿主目标的独立拒绝。

同回归入口额外核普通/MCP工具canonical禁用后不能复用旧GRANTED，enabled原正例保留。

### /ask与/control控制一致性回归入口

`agent_py_agent/tests/test_gateway_ask_control_parity.py`使用原临时HTTP/G1/C、模型卡和管理领域验证两个入口共享合法分派；普通消息不改路由，Count0和来源权限不借C扩大。仅测试运输为既有fake，真实飞书及发行另验。

closed角色第三次读取验证入口：`agent_py_agent/tests/test_gateway_role_marker_snapshot.py`，复用原producer/P4与受控端点，核原stat/open窗口、同parent FD三次上限及默认CountNone/危险叶边界；须最终两OS新包pyc实跑，不代表真实关闭或608根因证明。
## 2026-10-19 委派接单表达回归

```text
|-- agent_py_agent/tests/test_im_dispatch_ack_presentation.py # 原接单通用事实经两协议serializer进入wire，保零工具及任务状态
```

关键文件说明：`test_im_dispatch_ack_presentation.py` 经原接单及自然回复入口验证表达角色、任务延续、缺接单与已有 pending 优先级；不依赖仓外QA，不替代真实模型验收。

`test_control_receipt_delivery_closure.py`贯穿原领域生产者、规范回执与通道投递；仅IO替身，不绕过身份或显式终态发送门。


### 输入期限与静默执行账关键文件

- `agent_py_agent/agent/runtime_db/input_lifecycle.py`：原运行库整行claim、效果窗口和迟到complete/release CAS；新的active-Link例外在最终SQL核整树UNKNOWN/活动。
- `agent_py_agent/agent/gateway_parts/runtime_wake_source.py`：canonical owner DB行的只读来源快照，不新建业务Agent。
- `agent_py_agent/agent/conversation/runtime_wake.py`：Count0与P5 W共用单行算法；只有当前claim可expire或发布原Wake。
- `agent_py_agent/tests/test_runtime_input_deadline_bridge.py`：对应普通runtime回归，覆盖字段绑定、重排/取消时序，非真实服务验收。


后台输入身份关键职责：`conversation/tool_approval_scope.py`只读原claim和实际执行代次；`task_run_closeout.py`仅以输入冻结attempt反查真实run，scope任务编号不改。`test_background_main_scope_attempt.py`覆盖main run与task不同及child原canonical边界，不新增执行器或持久账。


角色收口回归补充：

```text
|-- agent_py_agent/tests/test_dead_worker_role_closure.py # 原producer异常W退出、未知拒绝与真实waitid子进程
```

关键职责：`multiprocess_drain_runtime`仅在原已核W物理退出时区分非零退出与正常心跳，仍由完整P6封存；该测试不提供新执行器或自动恢复入口。

- `agent_py_agent/agent/conversation/background_shutdown.py`：只保存同Gateway原future/claim/worker引用；停止服务不取消持久业务，未退/未结交原cleanup为incomplete。
- `docs/design/GATEWAY_BACKGROUND_SLICE_SHUTDOWN.md`：后台片停机顺序、真实退出与676失败/恢复边界。


### 监护当前配置预核回归

```text
agent_py_agent/
|-- agent/gateway_parts/supervisor_generation.py  # 原配置N/home/Gateway根与S2正常来源的负向预核；不授迁移许可
`-- tests/test_supervisor_current_configuration.py  # 当前YAML漂移零spawn和支持版Count0来源分界
```


### 监护整代恢复只读接缝

```text
agent_py_agent/
|-- agent/gateway_parts/supervisor_generation.py  # 原P4健康及完整P6关闭的只读恢复门，不创建执行者或关闭账
`-- tests/test_supervisor_generation_recovery.py  # 受控OS/start边界的整代恢复正负合同，不代真实服务
```

关键文件：`supervisor.py`仍唯一负责原有界监护循环及公开启动/准确停止；`multiprocess_drain_runtime.py`只接受原精确异常退出事实，完整来源/原树关闭边界不变。


## 监护CLI配置运输回归补充（2026-10-20）

`agent_py_agent/tests/test_supervisor_cli_agent_binding.py`：覆盖status/start-all沿已解析Agent核Count0与完整Count2健康，不重复构造Agent或改状态。


## 终态身份合同测试补充（2026-10-20）

```text
agent_py_agent/tests/
`-- test_gateway_terminal_identity.py  # 原终态writer、P6与冷监护的身份运输及Count0字段回归
```

关键文件说明：`test_gateway_terminal_identity.py` 沿原context/publisher/P4/P6测试终态身份不丢失；不替代真实整代恢复验收。


## W末心跳合同测试补充（2026-10-20）

```text
agent_py_agent/tests/
`-- test_worker_terminal_heartbeat.py  # 原run_worker/实际pump关闭和native受控端点的末心跳时序回归
```

关键文件说明：`test_worker_terminal_heartbeat.py` 通过原执行循环/线程池和原关闭路径区分退出原因与关闭阶段，覆盖首次/第二次pump.close及native.close失败不发表stopped；不构造P6许可或真实服务。


## 2026-10-20 监护装配层次补充

```text
agent_py_agent/
|-- cli/supervisor.py  # 唯一实际Agent组合根，运输同配置/规范GatewayPaths
|-- agent/gateway_parts/supervisor.py  # 消费必传Agent/Paths的原监护生命周期，不反向导入CLI
`-- tests/test_supervisor_layer_binding.py  # 真实配置/Agent的单次装配及缺失/冲突拒绝运行合同
```

关键文件说明：`test_supervisor_layer_binding.py` 以隔离home和echo配置验证CLI→runtime单向装配；原进程、身份、Count0/正N及P4/P6门继续由现有模块负责。


## 2026-10-20 宿主通知阶段文件职责

- `agent_core/host_turn_scope.py`：共用原core绑定、当前prompt与准确attempt结算，四actual不伪结。
- `conversation/host_notification_scope.py`：同原claim/owner/actor的通知热范围；不是权限或第二状态账。
- `test_host_notification_scope.py`、`test_host_notification_transport.py`：双平台普通运行，覆盖词法隔离、成本、原通知、C/W运输与来源失效。


- `test_supervisor_first_start.py`：公开parser与原make_agent的fresh启动父目录责任；Popen受控，不能替代真实角色生命周期。

## 已发表目录稳定名称回归入口

`agent_py_agent/tests/test_generation_source_stable_names.py`：合成发表交错验证 generation 有限完整重读，不授关闭/来源权；默认 P6 与完整历史拒绝保持。

旧W首次当前发表验证入口：`agent_py_agent/tests/test_gateway_closed_worker_publication.py`，原关闭producer和后继P4/心跳受控交错；16普通runtime义务，不是source-only或真实682根因证明。


## 私卡503诊断回归入口

`agent_py_agent/tests/test_private_interaction_safe_diagnostic.py`仅在隔离fake中核原HTTP回执和安全日志，不代真实凭据提交成功或故障根因确认。

`agent_py_agent/tests/test_generation_source_missing_publication.py`：原归档事务受控交错验证首读前离名，保末names仍在、reader异常、链/设施/未知名、三轮预算、默认P6与旧expected拒绝。

- `agent_py_agent/agent/gateway_parts/process_completion_source.py`：MP原ProcessSession只读来源与单会话完成通知，不新进程账。
- `agent_py_agent/tests/test_multiprocess_process_completion.py`：对应双OS普通运行义务，非真实模型验收。

- `agent_py_agent/tests/test_process_completion_receipt_install.py`：MP通知原redo安装硬错与actual传播，双OS普通运行义务。

关键文件说明：`test_required_notice_reply_slot.py`是normal/required共同回复槽的真实本地管线回归入口，合成运输不代表真实飞书验收。

关键文件说明：`agent_py_agent/tests/test_background_result_identity.py`验证业务结果与宿主执行身份分离，关闭发布后冻结及未发表拒绝；合成测试不代表包验或真实服务。

关键文件说明：`agent_py_agent/tests/test_background_reply_candidates.py`验证来源工具回执先按结构化形状筛候选，无候选不读取actor或触发来源认领，有候选保持原身份/目标/attempt与canonical操作门。
