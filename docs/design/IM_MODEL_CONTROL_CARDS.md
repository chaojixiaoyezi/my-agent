# 飞书模型控制卡

2026-10-14 用户新反馈已加入本期第一批：群模型卡已送达，三按钮都返回“确认未完成”。源码确认 CLI 适配器配置未传 `gateway_port`，控制回调回落 8420，普通消息却使用配置端口（离线包默认 18420）；另补原 `gateway_auth_token` 传递到同一宿主凭据服务。候选已以隔离回环 HTTP→原 Gateway handler→模型控制原入口验证，保留个人首设/解锁门；HTTP 认证、路由、超时、连接、格式和宿主故障分码/阶段回显。此为源码缺口与隔离复现，未宣称现场唯一根因或真实飞书已恢复。集成状态见 [本期实施](../tasks/IM_22_ITEM_IMPLEMENTATION.md)。

状态：R06 本地实施；合成原入口回归已执行，真实飞书租户渲染/外发未验收。

`/model` 查看复用原 owner 模型列表与线程选择；有原生控制卡服务且 `im_model_cards_enabled` 开启时，飞书返回选择卡。文字 `/model <编号>` 与 `/model default <编号>` 和终端入口保留原操作语义。群选择卡投递原群，新增/编辑按钮只向原操作者个人编号发私人表单。私人回调不生成运行任务、审批等待或模型请求。选择卡与私人表单明确展示原结构化 owner 的归属：群模型供原群执行，群新会话默认是该群未来会话默认；个人只影响本人，管理员目录仍保留共享发布/全局初始模型的独立管理员入口。归属标签由 send_model_control_card 的原 OwnerIdentity 生成，不从聊天文字推断。

## 唯一事实源

- 模型与密钥仍保存于 `HomePaths.config_dir/model-profiles/<owner身份摘要>.json`，不是 owner 工作区下的可读文件；保密读取与 shell 边界由原 H3/owner 保护负责。
- 线程仍是原 owner `ConversationStore` 的 canonical thread；回调从签名 owner/thread 取得轻量 store，禁止把群配置写到接收表单的个人目录。
- 控制元数据保存于原 `PRIVATE_INTERACTIONS_OWNER_PARTS` 目录，`model-<UUID>.json` 只保存身份、原卡回执、期限、随机目录代次、选择版本和无秘密结果，不是第二份模型配置或秘密仓库。
- 选择只影响下一次模型请求，已冻结的请求继续按原模型完成；未来默认不切换已打开的会话。

## 控制合同

签名类型严格为 `model_configuration`，冻结原 actor、owner、channel、thread、随机 control UUID、目录/共享目录/管理员来源随机代次、线程选择版本及 30 分钟有效期。回调先验证宿主 HTTP 凭据、SDK actor、MAC、期限、个人密码设置/解锁和管理员绑定，再复核真实原卡 message/chat。其它人、转发卡、错误聊天、改变卡片引用、旧目录或线程版本均拒绝。已完成控制只对完全相同逻辑载荷（decision 与完整 fields，忽略 JSON 键顺序）返回原无秘密回执。不同 profile_id、保存字段、密钥或附加字段均拒绝，不能返回未应用新值的成功。使用原受保护 owner 交互 key、独立用途和 control UUID 生成 HMAC 摘要；控制记录不保存明文字段或未加密的秘密摘要，旧 completed 行没有该摘要时拒绝重放并要求重新开卡。

选择使用稳定 profile ID，`expected_revision` 在原线程原子更新内部核对。选择期间持原 owner→管理员来源→共享目录锁，防止目录撤销与旧卡提交竞争。未来默认保存与表单写入在原目录写前检查再次复核卡版本。目录写入继续复用 `execute_model_profile_operation` 和 `model_profile_write_check`，保护管理员向量端点；没有新配置账本。

表单仅收后端、基础地址、模型名、上下文 tokens 和密码类型 API Key。未输入密钥的编辑仅在同基础地址保留原密钥；换地址必须重新输入。原模型/服务商 ID 保持；共用连接和 OAuth 档案请使用原终端管理，避免一张表单修改其它模型认证。普通用户可以配置自身 owner 的目录、选择管理员明确开放的共享模型；共享模型仅可选择，卡片不公开共享 API 地址或凭据，也不提供共享发布、全局初始模型或向量修改按钮。

## 飞书原生组件依据

沿现有密码卡/凭据卡使用 `input`、`input_type=password`、`form` 和 `action_type=form_submit`；选择使用原生 button 稳定值，不假定飞书缺少下拉组件。原生输入与表单官方入口分别为[输入框](https://open.feishu.cn/document/feishu-cards/card-components/interactive-components/input)、[表单容器](https://open.feishu.cn/document/feishu-cards/card-components/containers/form-container)。官方[表单示例](https://open.feishu.cn/community/articles/7271149634339487746)包含 `form_submit` 和 `form_value`。浏览器文本抓取对组件页正文不可读，真实客户端的密码遮罩/渲染仍需租户验收，不能把文档入口当真实外发成功。

成熟参考只读核对 OpenClaw Feishu 的 `card-ux-shared.ts` 与 `card-action-state.ts`：actor/chat/expiry 的结构化交互上下文、固定按钮 envelope 与已处理回调去重。此仓库使用已有签名/受保护记录/Delivery，未复制其进程内 Map 为另一权限事实源。

## 验证与剩余边界

定向测试从原 `/model` 和 `private-interaction` 入口覆盖个人/群/管理员、错人/转发/错卡/错群、密码首设/锁定、过期、管理员解绑、目录与线程版本、锁内并发写、稳定编辑/空 key/换地址、共享凭据不可见不可编辑、回调幂等、原 adapter form 转交和群发送编号。没有真实飞书外发或真实模型请求。

打开、保存卡片不探测网络，也不声称连接成功。卡片没有“测试连接”按钮：原终端 probe 的网络边界独立维护，不能在 2 秒 SDK 回调里发送 60 秒模型探测或新增第二模型传输链。
