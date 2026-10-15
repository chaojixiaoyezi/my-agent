# S01–S05 当前空间监控与成员清理交接

## 基本信息

- workstream：S01–S05，后续监控展示序号3–7。
- worktree：`schedule-s01-s05-20261014/my-agent-dsh`，独立detached基线`56617f7a8`。
- 日期：2026-10-14。
- 状态：本地实施与隔离验证，未提交、推送、部署或访问真实飞书。增量不含第一批凭据/群卡/模型回调修改。

## 实际完成

1. slash和工具列表/详情/历史/status只读当前owner。私人不跨扫群；群展示该群所有创建者；跨owner准确编号不可操作；不做all聚合。
2. 列表/详情创建者姓名读原host identity_store，稳定provider/actor始终保留；缺名字不猜。姓名与备注不成为授权依据，旧不明creator可见但不可认领。
3. 原`authorize_job`成为所有现存监控写操作共同门：原creator仍有原控制权；同群其它人只在当次完整官方群成员快照证明操作者仍在、creator已不在时可pause/cancel/delete。其它修改/恢复/试跑/通知接管均不因此开放。实时查询无离开授权缓存；creator重新加入即时拒绝代删。
4. 复用原Delivery注册表的Feishu token和官方members接口，最多十页、成员HTTP总预算两秒；token认证沿原有限超时。缺权限/网络/坏页/不完整/循环/超预算皆未知，固定安全码拒绝，不当成离职。没有公开的可靠组织状态时不另猜离职，完整群快照中的真实缺席已满足本群清理需要。
5. 保留原creator与取消树/CAS；代清理意图沿原AuditLogger存实际actor、creator、job/version/request。pending是授权意图，不是执行成功；审计失败沿既有fail-soft规则，只报安全类型。没有第二任务/权限/成员账。
6. 管理员绑定IM保留对明确local creator、原local/main TUI监控的控制；不混其它群。slash缺入口线程时只按认证scope和已解析owner写原ThreadStore绑定。
7. status累计输入/输出/缓存直接取原线程public_model_metrics，与TUI原口径一致；估算标约、未知不补零。Gateway、本地status及HTTP恢复共同携带；上下文占用仍独立。

## 修改范围

生产：`scheduler/creator_access.py`（新增）、`scheduler/control.py`、`scheduler/tool.py`；`gateway_parts/schedule_control.py`、`schedule_diagnostics.py`、`status_projection.py`、`control_service.py`；`conversation/host_actor.py`（新安全码）、`control_commands.py`（用量字段/渲染）、`contracts/error_taxonomy.py`（新固定码合同）；`cli/chat_parts/control_runtime.py`（原字段传输/本地投影）。

测试：新增`test_schedule_owner_scope.py`；同步旧`test_schedule_control.py`、`test_im_scheduler_security_matrix.py`、`test_scheduler_host_actor.py`的群可见范围预期，保留身份冲突、错owner和写拒绝断言。`test_im_control_repair.py`仅同步旧mock的include_deleted关键字接口。

文档：SCHEDULER_EXECUTION_POLICIES追加当前合同、Gateway模块进度/结构、CODEBASE_TREE新增入口。本交接是开发记录，根统一更新TESTS/总台账/用户展示清单与发布状态。

## 验证与保留失败

11文件focused共403项通过：`test_schedule_owner_scope.py`、`test_schedule_control.py`、`test_im_scheduler_security_matrix.py`、`test_scheduler_host_actor.py`、`test_scheduler_control_lifecycle.py`、`test_scheduler_tool.py`、`test_conversation_control_commands.py`、`test_chat_control_runtime.py`、`test_tui_model_metrics.py`、`test_im_control_repair.py`、`test_im_self_discovery.py`。

矩阵覆盖：私有10/A群10/B群5，同群所有创建者/同名不同ID，准确跨owner ID；tool前台/后台×8动作×当前/离群/未知/操作者离群；slash同权限；重新入群/跨群/缺creator；完整/坏响应/权限拒绝/循环/十页上限/超时分页；原creator不改与实际操作者审计；管理员私聊/TUI既有控制；原token/cache/估算/未知展示。

扩大回归初跑发现7项`test_im_control_repair`旧mock不接`include_deleted`；在第一批开发树（该调度生产文件未改）复跑同样7红，证明是基线已有测试合同漂移。本线只修mock签名，恢复其原异常/脱敏断言，没有改产品来掩盖失败。另一次测试与尺寸报告并行使CODE_SIZE_REPORT被测试守卫捕获；报告已恢复，后续检查使用独立--report路径，避免并行改跟踪文件。

全`ruff check agent_py_agent scripts`、doc-sync、diff和strict-code-size均通过（hard=0、blocked=False，report写临时路径）；移除退休的跨群冷查询helper后，监控两文件87项再通过。没有运行全仓pytest（生产/测试增量远低于10000行）、真实模型、真实飞书或部署。本地测试不证明租户members权限已授予。

## 主线重点复查

- 仅应用独立增量patch，不能全文件覆盖第一批已集成的control_service/control_commands/error_taxonomy/CLI控制文件。新接口不依赖第一批凭据文件。
- 新成员码已经登记ErrorContract；未来不要从错误文案或姓名改写授权。
- 本线只证明官方当前完整群列表中的离群；不把通讯录读失败或姓名未知当离职。其它provider保持不开放代清理。
- 管理员local旧记录无creator且没有可信来源迁移时仍不冒认；展示“创建者身份待核验”，需要管理员依原迁移程序核验，不可一刀切把空creator改local。
- 成员索引不完整（大于1000个用户且十页未覆盖）明确拒绝代清理，可由原creator走原管理通路；不会为大群扩大查询预算或静默把分页缺席当离开。

建议下一步：根应用本线增量后做非作者授权审查与组合严格gate；其它agent可并行审查成员分页/未知负例和status同源性，不同时改共同授权门。随后用专用群/账号验证实际成员权限、离群/重新加入与原取消收口，禁止操作生产监控或把合成通过称真实租户已验。
