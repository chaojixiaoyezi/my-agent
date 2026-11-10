# 同线程旧产物的原登记来源

当前补修区分业务文件路径与登记根。`write_file` 普通、覆盖、追加以及输出外置的正式 Executor 回执均含非空 path；归档仍只在原 run workspace 的唯一 registry 登记。文件可以合法位于另一棵 durable task 树，不能因此在目标路径祖先再建一份登记，也不能把当前文件重新 hash 当旧证明。

历史恢复仅从宿主同线程消息读取结构化 assistant `gateway_request_id`，最多选择常量两级热尾的最多 80 个物理行中的 8 个不同旧请求。无 delivery_artifacts 或旧引用只有 ID/name 时，仍可检查这几份确切已完成请求，而不是扫 owner/runs 或 tasks。规范 terminal 请求沿既有 include_sealed 读口读取，必须核原 schema/指纹、frozen owner 与当前 owner 目录、request/thread/runtime task 绑定。task_path 只能成为 owner 内逐级 no-follow 核验的原 registry 候选；位置不授权发送，消费端仍检查原登记的 path/id、ready、hash/size 及投递时安全 FD。

`artifacts.registry_provenance.verified_recent_registry_roots(agent, trusted_scope)` 返回 `(roots, errors)`。无 session_id 的 standalone 为不适用空结果。会话来源缺失、损坏、跨 owner/thread/request 或根越界返回无 roots 的结构化错误。可核候选返回软 `history_window_partial` / `partial=true`，因为近期窗口不能证明全历史不存在：真实精确路径命中可继续核原登记，负命中保未知；裸名称或 ID 不能凭近期唯一冒充全史唯一，需当前任务唯一或原同线程显式引用绑定。

投影读取保留原消息上的 gateway_request_id，未修改旧消息或旧登记。重启从同一消息与原 terminal/sealed 回执重读，不增加权威账本、注册表或持久缓存。原近期消息读口会扩大物理窗口，本读口改复用 no-follow 固定尾窗，常规读256KiB/80物理行，仅没有完整助手时再读至多1MiB，最多两次（总1.25MiB），不继续扩倍或全扫；committed manifest最多64KiB，复用原rotation_id/热区前缀证明，只有热文件与前缀均在1MiB内才解析该同FD热区，不打开任何冷索引/冷段。prepared、旧无portable proof、超限热区仍明确cold_transcript partial。每份原 terminal 使用既有封存1MiB上限读取热源，冷源沿原有界精确封存段读口，校验复用原终态 schema/指纹纯函数。普通上一轮热尾可恢复，冷源、超大行或展示行挤出窗口时保明确未知。

验证只在隔离目录：真实 WriteFileTool→ToolExecutor→归档→原 registry 的12种组合；无正文路径旧来源、owner/thread/request/路径/损坏拒绝、原 sealed 发布后冷 store 读取、最多8份规范回执。消费端真实发送往返由对应通道测试使用隔离 adapter 验证，不发送真实飞书消息。


共享 Gateway 根只能取本次宿主 `params.recovery_content_paths` 的准确 processing 请求与同根 response 路径，并核原请求、线程和 frozen owner；`gateway_root_from_host_params` 不创建文件，核不实返回 None。消费端经宿主边界传 `__artifact_gateway_root`，不得接受模型同名字段或根据 Feishu owner 的执行 cwd / 相对 gateway_workspace 猜队列位置；缺显式根只可复用绝对规范配置根。

每一份旧 terminal 是独立正来源。坏/缺/跨 owner/thread 的候选永远不加入 roots，但其错误保留在软 partial 的 `source_errors`，不抹掉另一份已经完整核实的原根。只有独立已核根中的精确路径命中能继续发送；负命中仍是未知，不能把腐坏当未登记，也不能按近期唯一放行裸ID/名称。


候选目录在支持 dir_fd 时沿原目录 FD 核验；Windows 等平台复用原 portable 逐级 no-follow 目录检查并拒绝 reparse/junction。它只是位置候选，不替代后续原 registry 与附件的安全文件描述符验证。强制 portable 回归不等于 Windows 原生验收。
