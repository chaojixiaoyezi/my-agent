# IM 无人值守审批线交接

## 基本信息

- 日期：2026-10-14。
- 分支：`codex/im-entry-update-assessment`；接续 `b3e5aae` 的同线未提交实现，未覆盖其它线文件。
- 范围：P01/P02/P03/P04/P06；根统一集成、推送、部署和打包，本线未进行这些操作。

## 实际完成

- owner 缺 `permission_mode` 时继承共享宿主 capability 默认，显式 ask/auto/full 保留，坏 JSON、空策略、非法模式关闭式拒绝。`read_permission_state(home)` 公开模式、来源和管理员事实，保留发现线依赖 API；默认不修改 access/path policy，也不降级旧本机 full 配置；子代理仍受原路径上界约束。
- `restart_gateway`、`admin_controls` 免逐次可选确认，handler 与 availability 重核可信 local/main 和 runner/registry 主代理身份；普通用户、群和子代理不能借用。重启排空、冷却、循环保护与原资源/权限策略继续有效。
- `cancel_subagents` 免确认，沿准确直接父子授权和原取消原语；`process_session.stop` 原 mutating 与 `terminal_session.close` 原 sandbox-contained 语义已免可选确认，未新增旁路。新增 ask/auto 停止策略矩阵，原真实进程与跨 owner/session 负例保留。
- 群/后台默认在 pending、私人卡或等待前拒绝剩余人工确认。交互前台保留精确确认，原请求/记录绝对截止不能被 consumer、卡片或新发布延长；超时、无人接收、运输失败分别返回明确原因，普通 unavailable 不再退回 `APPROVAL_REQUIRED`。
- 新 call/attempt 的同工具同参数摘要匹配原拒绝事实；拒绝列表包含 unavailable 与 `reason_code`，凭据线沿同 task 持久保存。批准消费之后再次核截止和取消，迟到 approved 不启动 handler。
- `host_command_approval` 同样将缺 consumer、非法/错配答复和 unavailable 结算成未启动拒绝，原 HostCommand 重送只读终态、不重复问或执行。生产插件管理/业务命令由 `PluginManagementContext` 从明确宿主源冻结确认期限，再显式注入原执行器；下层不从 CWD/owner 局部目录猜配置。
- 参数中心的部署字段与执行同读根提供的 `deployment_capability_config` 和唯一 `DEPLOYMENT_CAPABILITY_KEYS`；`running_value`、读取事实和 `/settings` 工具视图标部署只读来源，未知配置显示 unknown。模型与 `/settings` 写入仍拒绝，额外目标门防止误写 owner 局部覆盖。
- typed WS 卡按 kind/签名 reference/决定/真实 actor/message/chat 独立去重，只有成功回执才提交 seen；未解锁、异常或无效点击释放运输 claim，解锁后原卡可重试；重复成功返回原公开回执。表单秘密不进入缓存键或诊断。
- 原轮执行追加通用 `handler_details.input_dependency.status=waiting_input` 屏障，后续未执行工具沿原 `RUNTIME_TRANSITION_DEFERRED` 完整配对。凭据线独占归档、持久等待和释放槽。
- 扩展 I4 测试发现并修复：宿主 `ModelCallAdmissionClosedError` 继续透传原安全重启恢复；`gateway_recovery` 不再因展示渠道不支持 transcript 被猜作 IM，只有准确 feishu 渠道走私人卡。TUI 恢复保留原 client capability 与新精确确认。

## 改动文件

- `user_space/approval_mode.py`；`contracts/tool_approval.py`；`agent_core/orchestration/tools/cancel.py`；`agent_core/tool_loop/round_execution.py`。
- `conversation/agent_tool_approval.py`、`im_tool_approval.py`；`gateway_parts/permission_bridge.py`、`stream_approval.py`、`stream_writer.py`。
- `tooling/admin_controls_tool.py`、`gateway_restart_tool.py`；`runtime_db/host_command_approval.py`、`host_command_execution.py`；`plugin_management.py`、`plugin_invocation.py`；`adapter/feishu_ws.py`。
- `settings/parameter_registry.py`、`parameter_changes.py`、`user_config_capability.py` 的部署投影/目标门；`tooling/user_config_tool.py` 的读取事实与只读来源。未改根声明的新配置/六字段常量。
- 新测试 `tests/test_unattended_tool_approval.py`；原 owner/Gateway mode、IM/background、restart/admin、host command、WS、plugin consumer 与 control service 测试同步显式交互夹具。产品后台默认仍不可交互。

## 验证结果

- 最终审批主链 11 文件自然退出：212 项通过，0 失败/错误/跳过；XML `/tmp/im-approval-focused-final.xml`。
- 插件/精确重批/恢复/原控制/统一执行器 6 文件自然退出：149 项通过，0 失败/错误/跳过；XML `/tmp/im-approval-plugin-focused.xml`。含 I4 真正原 Gateway 请求恢复、假供应商和真实原审批。
- 最后批准消费截止增量：新无人审批与 host command 两文件 43 项通过，XML `/tmp/im-approval-late-consumer.xml`。
- 部署来源与插件命令接线 8 文件最终 244 项通过，0 失败/错误/跳过；XML `/tmp/im-approval-deployment-source.xml`。含共享与 owner 局部两份冲突配置、执行与投影一致、未知来源、模型/用户控制写门、真实管理命令精确 37 秒截止和非法期限零 consumer/handler。
- 插件业务 invocation、Gateway 管理命令与运行参数 3 文件追加 55 项通过，XML `/tmp/im-approval-host-invocation.xml`；包括原连接清理、取消、重复及普通身份拒绝。
- 冻结前来源修正：参数运行值和事实来源统一通过 `configured_home_root` 解析；默认空值或历史 `~/.my-agent` 被 `MY_AGENT_HOME` 覆盖时，展示与宿主实际加载一致。3 文件 155 项自然通过，XML `/tmp/im-approval-env-home.xml`，覆盖两个 CLI/OAuth 开关、共享文件缺失后的随包来源、显式根优先级及相对环境根拒绝；不改写配置。
- IM/background/process/PTY/普通与管理员主子孙权限矩阵曾自然通过 158 项；工具与网络仅用受控临时数据/localhost，不作为真实租户验收。
- 定向 Ruff 与 `git diff --check` 通过；strict code-size 使用原 baseline 已达到 hard=0，最终检查用 `/tmp/im-approval-code-size.md` 避免并行 pytest 追踪文件守卫冲突。
- 初始旧默认/非交互夹具失败、taxonomy 未接导致 UNKNOWN_ERROR、I4 两个恢复失败均已保留并修复后复跑。一次默认尺寸报告与 pytest 并行造成 CODE_SIZE_REPORT tracked-file guard teardown ERROR，改用临时报告并重跑后的上述 XML 为验收来源。

## 需要主线重点复查

- 四个原因码已由根注册；根最终运行完整本地严格 gate，不能把本线 focused 结果当发行验收。
- 六个部署字段的执行与投影已绑定共享 `<my_agent_home>/config/capability_config.yaml`（缺文件用随包），不读取 owner workspace 局部值。实际插件命令由共享配置冻结并显式注入期限；只供低层测试/旧直接调用的未注入请求仍有合同默认 1800 秒，不猜 home。
- 根目录采用与 core 相同的 `configured_home_root`，遵守明确非默认配置优先、默认目录允许环境覆盖；参数层要求展开后为绝对路径，来源未知不读取当前工作目录，也不允许部署字段经模型或 `/settings` 写入。
- 参数相关最后 strict hard=0，报告 `/tmp/im-approval-deployment-size.md`；定向 Ruff 通过。root Gateway/subagent 模块文档及新路由/状态同文件注释的 doc_sync 仍由根收口。已按根授权恢复验证造成的 tracked `CODE_SIZE_REPORT.md`，后续报告只写 `/tmp`。
- 同 task 拒绝持久记录不被新 call、attempt、Compact、输入 ready 清空。若允许在同 task 接受用户后续精确重新授权，应新增原控制面的结构化事件，不能解析自然语言清账。
- 真实飞书卡解锁/回调、TUI 自动停止、管理员重启、Linux 发行和正式模型尚由根最终验收；本线无真实外发、生产 owner/config 修改或真实 Gateway 重启。

## 建议下一步

根组合凭据等待/拒绝持久化跑严格 gate，并以本人独有资源验证 OAuth 预查询与业务请求都使用同源本人身份；审批与配置源码已可冻结供其它 agent 只读复核。发行/部署由根串行，普通群、跨 owner、活执行锁与未知副作用边界不得放宽。

## CLI/OAuth 非作者只读审查（根仍在修改时点）

- CLI 入口固定随包程序、私有 stdin 身份、独立临时目录、单次准确官方请求、拒绝跳转/重试与原错误分类路径已核。未替根执行真实外发或改 root 实现。
- 已向根报告 OAuth ready 保存后、输入 resolve 前退出的重放恢复缺口；配置来源和每次 token 使用重核部署开关需根修，并用故障注入验证。
- 已核 FeishuCloud 外围保留 owner/父级网络硬门；本人 UAT 当前仅注入最终云请求，资源定位/授权/表格保护仍用原 adapter，应以仅本人可见资料验证或修复预查询身份。
- 这些是审查发现与验收要求，不宣称 root 尚在修改的 OAuth 已通过完整真实租户验收。
