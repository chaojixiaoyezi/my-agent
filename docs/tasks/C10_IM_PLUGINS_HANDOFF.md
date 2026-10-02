# C10：IM /plugins 交接（2026-10-01，sol）

## 基本信息

- workstream：三线收尾 C10
- branch：`worker/sol-im-plugins`，基于 main `34e4d874e`
- worktree：`worker-sol-im-plugins`（仅此工作树）
- owner：sol；集成者：3a
- date：2026-10-01
- 状态：源码实现、定向与变异验证完成；待集成与真实 IM 验收。本地提交号由最终交接回复给出。

## 本线目标与实际完成

让飞书等 IM 用户也能发 `/plugins [动作]`、`/plugins@<插件ID> [动作]`，不再被会话层当作未知命令。
没有复制插件解析或业务逻辑：IM 与 TUI 使用同一 `plugin_command_service`、`PluginManagement`、
插件目录、安装工具与 HostCommand。IM 只投影原服务的纯文本、请求编号和错误码。

- 会话只识别公共命名空间、保留原文，参数、引号与插件 ID 大小写仍由公共解析器处理。
- `/ask`、`/control` 不再提前绕过会话控制；原持久回执按 IM 消息 ID 去重，完成消息重送不重复副作用。
- IM 管理员照 `/settings` 的完整可信 owner 判断；已绑定管理员的私聊解析到本机管理员。
  查看和业务调用沿原插件规则，安装/配置/启停/更新/卸载仅管理员，正文角色自述无效。
- 回执中的错误码同时保留在结构字段和纯文本，不从文案推断授权或执行状态。
- 非 Python 包的启用沿已有程序信息预览与 `--confirm <确认码>`；真实预览和错误码均未启动程序。
  没有自创确认通道。IM 无 Tab 握手，宿主冻结本次目录；TUI 的目录版本校验不变。
- 补本地控制安全门：插件命令误入通用 TUI 控制时保留原文或明确拒绝，不能掉入默认停止。

## 改动文件

功能代码：
- `agent_py_agent/agent/command_catalog.py`：IM 帮助与原确认动作。
- `agent_py_agent/agent/conversation/control_commands.py`：插件专用控制类型，原文交给公共服务。
- `agent_py_agent/agent/gateway_parts/control_service.py`：插件控制分派。
- `agent_py_agent/agent/gateway_parts/http_handlers.py`：两处入口共用持久控制，不再插件提前短路。
- `agent_py_agent/agent/gateway_parts/plugin_command_service.py`：TUI/IM 共用作用域管理服务与回执投影。
- `agent_py_agent/cli/chat_parts/control_runtime.py`：本地拒绝和原文还原，防止落到 stop。

测试：新增 `test_plugins_chat_control.py`；更新 `test_conversation_control_commands.py`、
`test_gateway_plugin_commands.py` 的旧入口合同（都在 `agent_py_agent/tests/`）。

文档：`DESIGN_LEDGER.md`、`TESTS.md`、`CODEBASE_TREE.md`、`README.md`、`LLM_GUIDE.md`、`STATUS.md`、
`docs/ROADMAP.md`、`docs/COMPLETED.md`、`docs/design/PLUGIN_PACKAGES.md`、
`docs/guides/CAPABILITY_PACK_GUIDE.md`、`docs/modules/gateway/02-progress.md`、
`docs/modules/gateway/04-structure.md` 及本交接。已检索，旧 IM 不支持的说法没有剩余命中。

## 测试命令和结果

精确可复现命令在 [TESTS 的 C10 节](../../TESTS.md#c10im-共用-plugins-服务2026-10-01sol)。
均在工作树根，指定 CI Python，`PYTHONPATH=$PWD`、`PYTHONDONTWRITEBYTECODE=1`，
pytest 带 `-q --tb=short -p no:cacheprovider --basetemp=/private/tmp/claude-501/m-sol`。

| 检查 | 实际结果 |
| --- | --- |
| 定向测试 | 155 passed；变异恢复后再次 155 passed，不相加 |
| guards9 全部十份文件，含 packaging | 166 passed |
| check_import_boundaries | findings=0 |
| Ruff 全范围 | All checks passed；中途三处测试导入格式已修复 |
| check_doc_sync | DOC_SYNC_PASS |
| strict code-size / 原 baseline | hard=0、blocked=False；基线未改，CODE_SIZE_REPORT 已还原 |
| git diff --check | 通过 |
| clean-package | 未发现发布阻塞项；首次因新测试未 git add 失败，正常暂存后同一入口通过 |

变异逐个施加、逐个恢复，恢复前后两份产品文件的 SHA256 一致（完整摘要见 TESTS）：

| 变异 | 抓到它的用例（test_plugins_chat_control.py） | 结果 |
| --- | --- | --- |
| M1：IM 一律当管理员 | test_non_admin_mutations_are_rejected_with_visible_and_structured_code | 6 failed，被抓到 |
| M2：插件分派绕过服务，直接回成功 | test_http_control_receipt_replays_plugin_text_without_resubmitting | 2 failed，被抓到：调用次数为 0 |
| M3：投影吞掉结构化错误码 | test_non_admin_mutations_are_rejected_with_visible_and_structured_code | 6 failed，被抓到；文本带码不能替代结构字段 |

## 影响范围与需重点复查

- TUI `/client/plugins` 仍校验客户端目录版本、沿原 HTTP 认证角色；会话 `/ask`、`/control` 改为持久控制，
  要有原 `metadata.message_id` 和会话身份。真实适配器已有这些字段，不为缺字段另造重试账或执行旁路。
- 确认是原 `--confirm`，不等于普通危险工具的审批运输。本轮未给 IM 插件命令新增 TUI 的独立交互审批消费者；
  需要额外工具审批的动作仍由原策略裁决，不承诺所有危险业务在纯文本入口都已能确认/执行。
  若集成验收发现还需交互审批，请按原 IM 管理命令合同另行设计，不临时自创确认通道。
- 插件 `display` 是原 TUI 本地面板，IM 只收到原服务“不在此入口执行”的文本，不移植 UI。
- 用户文档已写路径是宿主本地路径，不自动下载 IM 附件，也不扩大路径权限。

## 没做、未验证和剩余风险

没有启停 Gateway、结束现有进程、运行 my-agent 命令、联网装依赖、读取真实会话或记忆正文，
没有改其它工作树或用户配置，没有 push、stash 或切换分支。

**未验证**：真实飞书/QQ 平台收发、真实管理员绑定与群聊拒绝、正确确认码后的完整 IM 插件启停/释放、
危险业务审批与真实 TUI UI。当前证据是隔离临时 owner、假 handler/渠道、公共服务及真实持久回执函数，
不是部署或平台端到端通过。未运行会启动 GatewayHTTPServer 的旧用例；线上 CI 未作为验收来源。

## 建议下一步

3a 先独立审核这份窄改动并与另外两线组合，再由获授权的集成/验收方部署同版，
在普通用户、已绑定管理员的 IM 私聊和群聊各核对查看、带码拒绝、启用预览、显式确认与重复消息。
只读代码复核可并行；部署、真实凭据和用户身份验证不要由本线代理越权代做。
