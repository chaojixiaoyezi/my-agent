# 按用户与会话独立选择模型

## 解决问题

旧实现把 owner 文件的 `selected` 当成每个执行工作片的当前模型。同一用户开两个 TUI，
第二个窗口选择 B，会使第一个窗口下一工作片也读取 B；后台恢复、手动压缩和状态展示同样存在串线风险。

本切片将模型目录、用户默认、会话选择分开，不新增模型执行链，也不复制凭据。

## 权威与行为

- owner 私有 provider/model 文件仍是模型定义、连接和密钥的唯一源。
- 文件 `selected` 只表示新会话默认模型。内部修改操作明确命名 `set_default`。
- `ConversationThread.model_profile_id` 是当前会话选择，schema 为 `conversation_thread.v9`。
- 新建普通 thread 时，由已认证宿主提供默认引用并直接持久化。重新绑定窗口、恢复旧会话不重新取默认。
- v8 等旧 thread 的空引用在首次使用时原子冻结当时的 owner 默认；这属于显式字段迁移，不根据历史正文猜模型。
- 两个 TUI 对应不同 thread 可以 A/B；同一个 thread 的两个窗口共享同一选择。不同 owner 的目录与选择继续隔离。
- 同一个通道绑定首次并发打开时，查找与创建共享按身份的短事务锁；不会产生两个 thread，也不锁住模型执行。
- `default` 是“部署默认配置”的固定引用，不代表每次跟随 owner 的 `selected`。
- 禁用、删除或不可解析的配置报错，不静默回退到 owner 默认或另一模型。

## 管理协议

Gateway `/client/models` 沿用认证 owner/channel/conversation/user 解析，不接受客户端指定 owner 或 thread 路径。

| operation | 参数 | 效果 |
|---|---|---|
| `list` | 原会话身份 | 返回模型目录、当前会话选择、用户默认 |
| `select` | `profile_id` | 仅修改当前 thread |
| `set_default` | `profile_id` | 仅修改未来新会话默认 |
| 原管理操作 | 原参数 | 管理 owner 模型定义；不自动更换会话选择 |

响应保留 `selected` 供菜单识别当前选项，增加 `default_selected`、`thread_id`、`selection_scope`。
已有 `profiles/providers` 为脱敏投影。保存、打开和切换不调用模型，`probe/discover` 保留显式网络语义。

本地非 Gateway TUI 使用 `execute_local_model_operation(agent, session_id, operation, payload)`，
沿用正式 `chat/local-agent` 绑定，再调用同一个底层操作。缺少真实 conversation store 时明确报错，
不能退回全局选择。冷 Gateway 菜单只创建必要会话文件，不启动 owner Agent、MCP、模型请求或后台任务。

## 执行边界

前台先解析真实 thread，再进入 `selected_model_scope(thread_id=...)`。
后台工作片、恢复继续、手动 Compact、`/context` 使用同一引用解析入口。
正在执行的请求冻结完整模型配置（接口、密钥、容量、采样等），菜单选择只在下一个工作片生效。
即使选择部署默认，也要建立作用域绑定，防止嵌套 `.run()` 重新读取刚变化的 owner 默认。

`/status` 运行中显示该工作片实际模型名，空闲显示 thread 当前选择；进程内名称表只是显示投影，
不存秘密，不做模型路由，退出工作片即清除。

换模型清除旧 `provider_context_observation/model_context_usage` 数值投影，保留 transcript、
摘要、压缩 generation/checkpoint 与精确游标。小窗口能否完成分段压缩仍由原 Compact 流程负责；
本切片不改摘要请求、摘要重试、预算或提交逻辑。

新子代理创建时记录父级冻结配置 ID，含明确 `default`；已有显式 `model` 参数仍可选择本人其他配置。
父会话后续切换不改变 child/grandchild 已保存引用。子代理 thread 物化后是选择权威；
task 创建引用只用于初始化尚未物化或旧记录的空字段，恢复不能拿它覆盖 thread 的新选择。

## 对照实现

- 会话运行时 `会话运行时-rs/core/src/session/config_lock.rs`：从 session configuration 生成线程配置快照；
  模型与 provider 配置按真实来源绑定，不按共享实例的临时字段推断会话。
- 会话运行时 `会话运行时-rs/core/src/session/mod.rs`：turn context 作为每轮运行的稳定配置表面。
- 通道运行时 `src/sessions/model-overrides.ts`：会话级模型覆盖，切换时清理失效运行模型与窗口数值。

本项目不照搬新的配置锁文件；直接复用现有 canonical thread、owner 私有配置及 ContextVar 作用域。

## 验证与集成边界

定向测试覆盖同 owner 多会话并发、跨 owner 不可引用、同会话双窗口、默认仅影响新会话、
重开、旧 schema 迁移、模型切换清投影不删历史、工作片冻结、默认嵌套作用域及子代理继承。
前后台和会话控制已有定向测试作为调用链回归。测试不发送真实模型请求。

真实单 Gateway TUI 已验证同 owner A/B、不同 owner、修改默认不改变已有会话，详见
验收报告。重开、切小窗口与后续任务按报告分别标明，不能统称全部通过。
管理员显式共享目录已集成到同一个 `selected_model_config` 与 child 显式解析入口，
不将共享密钥复制进用户会话或 TUI 回执。

建议下一步：继续验证会话恢复和不同通道接入；独立 Compact 修复仅在入口配置一致性上联合验证，
不要把单元测试通过当作跨模型长上下文验收完成。
