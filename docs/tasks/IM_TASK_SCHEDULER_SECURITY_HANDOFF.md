# IM 任务、监控与会话目标安全复测交接

## 基本信息

- workstream：IM 普通用户、群与任务权限攻击复测。
- 基线：`aa150ba0b`；与本期宿主凭据、附件安全切片共享工作树，由主线统一提交。
- owner：`scheduler_recovery_audit`。
- date：2026-10-13。
- 本线不访问真实飞书、不读取生产正文、不改生产监控、不部署或推送。

## 实际完成

1. 同群 A/B、跨 owner 和 `full_access` 两种权限配置，验证模型 `schedule` 的
   create/list/get/history/update/pause/resume/run/delete 以及控制 `/schedule` 的准确 job_id 访问。
   已知别人的任务编号也不能管理；raw owner/admin/actor 参数不覆盖宿主身份，失败后两侧账本字节不变。
2. 已有 frozen root、child、后台主体和租约恢复用例复跑。死亡执行在业务已开始后记录 unknown，
   不自动重放副作用；活跃租约仍保留。控制入口沿原 owner/creator 权威，未新增授权账。
3. 用真实 `UnlockService` 和 `SessionLockStore` 验证首次密码未设置、已锁定的私聊与群。
   合法本人告警仍沿原 Registry/Executor 到假的飞书 HTTP；目标只为本人或本群，不回落管理员。
   外发不刷新用户锁；跨 provider 或目标参数注入为零 HTTP。
4. 确认并修复错落会话目标的完整性缺口：原 send_session_message/create_session_task
   只因目标在当前目录就复用 sender 身份。现在两者共用 session_target_owner_identity 核对
   canonical 目标声明的 owner_id/home，冲突在 guidance、SessionTask 与 wake 入队前拒绝。
5. 精确 owner 已知的旧缺 home 记录继续可用；无 owner 的旧本地 local/main 目标只认原
   runtime_path_resolution 的准确 ConversationStore。未知外部 owner 不从目录继承身份。
6. 迁移两份退休 Gateway 本机信任开关的测试，共 53 项，数量不减少。
   无凭据受管请求在业务前统一 403/LOCAL_CREDENTIAL_REQUIRED，坏归档、别人的记录和缺记录
   都不泄露目标信息；有效 G1/配置 token、插件独立 token、真实 TUI 启动与原业务诊断保持可用。
7. 迁移原本机凭据/有界观察及父子工作目录旧 fixture 三文件。坏 G1、缺数据根和 Agent 合同
   在 listener 绑定前拒绝；无凭据请求仍记模板/count/last_at，不到业务入口；本机父请求登记原
   canonical thread/TaskLink，actor 沿原 local resolver 得到 None，不伪造 IM actor 或改变工作目录断言。
8. 非作者复核附件冻结和云资源授权：安全 FD 读出的原 hash/size 字节一次冻结，实际飞书发送
   只消费 typed bytes。权限自助匹配当前本人/当前群，应用权限与其他用户、群的权限不能代替。

## 改动文件

- agent/conversation/session_messaging.py：原共享目标归属核验，无新增权限或存储。
- agent/agent_core/orchestration/tools/send_session_message.py、create_session_task.py：原目标加载接线。
- tests/test_im_scheduler_security_matrix.py、test_im_session_target_security.py：隔离真实入口攻击矩阵。
- tests/test_session_task_e2e_flow.py：补 fake thread 原声明 owner；原派活/取消断言不改。
- tests/test_session_task_body_replay.py：补普通本地首片唤醒的原准确 SessionTask 账，原认领断言不改。
- tests/test_gateway_local_trust_enforcement.py、test_gateway_local_credential_required_code.py：
  去退休参数，保留正常与拒绝的两侧验证；raw false 不授予回环身份。
- tests/test_gateway_local_client_token.py、test_gateway_local_trust_observation.py、
  test_gateway_chat_conversation_context.py：迁移原准确宿主事实与凭据启动硬门的旧测试语义。
- docs/design/SESSION_MESSAGING.md、docs/modules/subagent/02-progress.md、04-structure.md：同步合同与验证边界。

## 测试命令与证据

所有命令使用项目 Python，PYTHONDONTWRITEBYTECODE=1，PYTHONPATH 为仓库与 agent_py_agent；
报告只写 /tmp。证据不包含生产数据或真实秘密。

- 原监控、控制、恢复与后台投递六文件：135/135，`/tmp/IM_TASKS_SECURITY_EXISTING_MATRIX.xml`。
- 新攻击矩阵：47/47，`/tmp/IM_SCHEDULER_SECURITY_ATTACK_MATRIX.xml`。
- 目标错落真实 Registry/Executor 反例：11 项中 8 RED，三个正常正例原本即通过，
  `/tmp/IM_SESSION_TARGET_SECURITY_TDD_RED.xml`；窄修后 11/11 GREEN。
- 会话目标、原会话/派活九文件与 Gateway 两退休夹具：183/183，
  `/tmp/IM_SESSION_CREDENTIAL_SECURITY_FOCUSED.xml`。
- 首轮 Gateway 旧接口失败证据：53 项中 34 failures/12 errors，
  `/tmp/IM_CREDENTIAL_RETIRED_FIXTURES_RED.xml`，仅用于记录退休 fixture 不兼容。
- 原完整 `test_session_task_real_chain.py` 保留 fake HTTP 原模型请求与 Registry/Executor 主链，不做用户任务旁路。
- 最后新安全矩阵、目标及 Gateway 两文件：111/111，无 skip，`/tmp/IM_TASK_SECURITY_FINAL.xml`。
- 原凭据/观察/父子 context 三文件：186 项首轮 14 RED，窄改 fixture 后 186/186 GREEN，
  `/tmp/IM_GATEWAY_FOLLOWUP_FIXTURES_RED.xml`、`/tmp/IM_GATEWAY_FOLLOWUP_FIXTURES_GREEN.xml`。
- 非作者附件、运输、后台投递、FD 与云十文件：199/199，无 skip，
  `/tmp/IM_ATTACHMENT_CLOUD_INDEPENDENT_FINAL.xml`。测试集合有重叠，不能把以上数字相加当唯一总数。
- 合成实际 Feishu typedbytes 探针：宿主合法 alias 在准备后改到 foreign 根，仍只上传原授权
  字节；冷重试原件变 foreign 则拒绝且零副作用。原缺 O_NOFOLLOW/O_DIRECTORY 探针两项现均拒，
  `/tmp/IM_ATTACHMENT_INDEPENDENT_PROBE.json`。原 helper 透传遗漏已由附件作者修复，非本线改源码。
- 原 Registry/Executor 与合成官方 HTTP 云八探针：应用、其他私人、其他群、另一群人工许可
  均不能代当前主体授权；wiki 不借底层本人所有权；本群真实分享追加读回一致，模型自报
  principal 字段零 HTTP，`/tmp/IM_CLOUD_OWNER_INDEPENDENT_PROBE.json`。
- 本线 Ruff、doc_sync、diff 检查通过；strict code-size hard=0，报告仅在
  `/tmp/IM_TASK_SESSION_SECURITY_CODE_SIZE.md`。最终整包严格验收仍由主线完成。

## 需要主线复查及边界

- 本次会话目标 P2 为合成损坏/错落宿主记录的加固；没有证据说明普通 IM 消息能制造该记录。
  没有把它升级成生产跨 owner 执行已经发生的结论。
- 错 provider 的 send_message 无 HTTP，但原 resolver 的 CHANNEL_TARGET_OUT_OF_SCOPE 在更早
  幂等业务键阶段会被 TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING 遮住。这是诊断可用性缺口，
  已交主线协调，不放宽幂等或目标门。
- 附件 FD/hash/bytes 和冷重试安全由另一切片负责；本线只读复查，不能用这份矩阵代替附件验收。
- 独立 Auth=None 单机 listener 和公共 /status、插件独立令牌保留原合同，不能误称任何 listener 都已具 owner 隔离。
- 本线没有新增 LLM 调用，没有真实网络云写入，没有修改既有任务的 origin 或投递对象。
- 云资源工具不是入站消息引用授予器：不要求资源曾出现在 incoming ref。模型猜 token 仍需
  当前 owner 的准确人工许可或供应商实时本人/群 ACL。云 principal 来自宿主 owner；群权限
  按当前群核验，不按某个群成员的私人 actor 权限借用。wiki 定位数据在原页面未获许可时不外露。
- 未验真实飞书租户 ACL、父目录/组织隐式分享、供应商撤销与已发出请求的竞争；继承权限未在
  官方列表明确返回当前主体时仍失败关闭。没有用户 OAuth，也不声称实现任意资源范围授权。

## 建议下一步

主线先联合本机凭据与附件切片做严格 gate，再提交、打包；可以并行让非作者复跑目标错落负例与正常派活真链。
不要为通过旧 fixture 放松宿主身份、target、幂等或 owner 硬门；线上 CI 未作为本线验收来源。

## 联合 gate 的本地派活旧夹具迁移

首轮 348 文件联合验收的 `test_session_task_turn_binding.py` 三项失败，红证据保存在主线
`/tmp/my-agent-im-repair-20261013.9/final-source-focused.xml`。旧 `_agent` 只写一条目标 Thread，
没有原 SessionTask/TaskLink 或准确 home；真实 `_run_params` 新身份门合法拒绝未知
`stask-wake-1`，不是生产本地派活回归。

只迁移此测试 helper：真实 Echo SimpleAgent 提供 canonical local/main home 与原 ConversationStore，
发送方和目标都是准确本地 CLI 线程，原 `tasks.bind` 登记同一后台根，host actor 保持 None。
没有手填 RunParams 回合号、没有伪造 IM 身份，没有跳过 `background_host_actor` 或改生产源。
原正文、target turn、guidance receipt/保留状态与最终 summary 断言不改；额外确认本地主体无 IM actor。

该文件与 body replay/e2e、sender 权限、宿主 actor、目标 owner 安全邻域六文件 **106/106** 通过，
0 failures/errors/skips，`/tmp/IM_SESSION_TURN_BINDING_FIXTURE_FINAL.xml`；Ruff 通过。
此前公共能力包追加四文件也 **106/106**，`/tmp/IM_PUBLIC_CAPABILITY_SECURITY_ADDITIONAL.xml`，
覆盖只共享 public 内容、安装管理员门、撤销/版本固定和资源读取/原生复制权限。两份集合及主线联合
集合有重叠，只单列证据，不相加。没有新增真实模型、飞书操作、生产数据读写或包部署。

建议下一步：主线复验当前准确候选的联合 gate 后提交/打包；可并行只读复核恢复与真实原工具链，
继续保留缺身份拒绝，不用测试目标或自然语言补造授权。
