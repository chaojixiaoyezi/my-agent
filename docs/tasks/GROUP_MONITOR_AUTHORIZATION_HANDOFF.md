# Workstream Handoff：群监控简明授权

## 基本信息

- workstream：群监控授权范围与安全诊断
- branch：`codex/feishu-native-images`
- worktree：本任务已附唯一共享源码树
- owner：群授权开发线
- date：2026-10-15
- baseline：`2812638850ff02c091c5bc980019f5c9474a9b30`

## 本线目标

群主或已核验邀请人用 `/group 监控 @某人`、`/group 监控 全群` 明确开放监控。全群包括当前和后续实际成员；普通聊天和其它已有功能不要求这层附加群授权。

## 实际完成

- 唯一权限状态仍在原 `tool_policy.json/group_authorization`，使用原角色、当前成员、应用/租户、群代次、策略锁和CAS。
- 整群grant新增明确 `scope: group` 且 `subjects: {}`；只允许 `monitors.create`。每次副作用仍核当前成员，新成员/重入成员可用，离群者不可用。
- 已发布八字段精确成员和 `all-current` grant继续按原成员epoch核验，缺scope绝不推断为整群；原旧配置grant可读/撤销，不再作为配置附加门。
- 新中文入口只接受 `全群` 或宿主归一后的精确 `member:<open_id>`；显示名、原始手打@文本不做ID猜测。
- 主展示为简明中文命令。English `allow monitors.create` 缺省明确整群；已发布 `member:`、`all-current`、revoke仍保留原精确语义。
- `assistant.configure` 不再查询provider或监控grant；其原同步身份租约仍校验HostActor/thread/owner与最终写入目标，防跨owner借宿主。该租约无snapshot，不能借给监控，不能作为监控权限。
- 配置与审批写入继续沿原锁；原管理员、路径、签名、目录版本及CAS约束不变。
- provider错误只消费其唯一 `safe_diagnostics()`，控制结果给固定reason_code、准确整数provider_code和固定stage，不复制异常正文、响应、URL、身份值或凭据。
- 没有改监控创建者/删除规则、停止取消、HostMessageContext/TaskLink或真实生产账。

## 改动文件

- `agent_py_agent/agent/user_space/group_authorization.py`
- `agent_py_agent/agent/user_space/group_authorization_context.py`
- `agent_py_agent/agent/gateway_parts/group_authorization_control.py`
- `agent_py_agent/tests/test_group_monitor_authorization.py`（新增）
- `agent_py_agent/tests/test_group_authorization.py`
- `agent_py_agent/tests/test_group_authorization_settings.py`
- 本交接文档。

## 测试命令和结果

```bash
python -B -m pytest agent_py_agent/tests/test_group_monitor_authorization.py agent_py_agent/tests/test_group_authorization.py agent_py_agent/tests/test_group_authorization_settings.py agent_py_agent/tests/test_group_feature_integration.py agent_py_agent/tests/test_im_model_cards.py agent_py_agent/tests/test_feishu_vision_model_cards.py agent_py_agent/tests/test_scheduler_host_actor.py agent_py_agent/tests/test_host_message_context.py -q --tb=short
ruff check agent_py_agent/agent/user_space/group_authorization.py agent_py_agent/agent/user_space/group_authorization_context.py agent_py_agent/agent/gateway_parts/group_authorization_control.py agent_py_agent/tests/test_group_authorization.py agent_py_agent/tests/test_group_authorization_settings.py agent_py_agent/tests/test_group_monitor_authorization.py
git diff --check
```

结果：274 passed，0 failed/error/skip；新文件23项。首批20项有16红，修复后增加真实ScheduleTool创建、配置身份租约不可借监控、provider安全诊断三项，核心四文件139通过；原红与最终JUnit仓外保留。

覆盖整群当前/未来/重入、旧快照不扩权、当前成员、跨owner/app/tenant、bot代次、撤销、创建者身份、topic/子孙、原配置/审批/persona目标、CAS和签名回调。已有监控停止/创建者删除规则通过原集成与HostActor回归，不做新权限放宽。

## 需要主线重点复查

- 旧grant只读保持原范围；新scope不能从旧all-current或缺字段推导。
- 配置身份租约不是配置能力授权，既不查provider也不带监控snapshot；仍不能借host写其它owner/root。
- provider完整成员失败仍failclosed，公开安全诊断用于定位，不能靠未知成员类型或缓存名单强行授权。

## 需要其他线协调

根负责 `protocol` 原可信mentions到精确command.value的规范化；本线不读取新增metadata权威，不按显示名授权。provider API/schema诊断由另一开发线独占。全局/模块文档及树由根统一同步；本线未修改已冻结的图片模型卡源文件。

## 剩余风险

未操作真实配置、群账、服务或平台；`GROUP_AUTHORITY_UNAVAILABLE` 的真实根因须依原实例新typed诊断核验。本线模拟通过不替代真实群主/邀请人授权。未提交、推送或发行。

## 建议下一步

根先审新grant范围与mentions接线并统一strict gate，再在唯一测试群用简明命令授权和新成员单prompt创建监控；原provider失败证据保留。非作者只读审可并行，真实平台和部署仍根独占。

## 显式IM回执补充（最终冻结）

安全诊断同时进入 `message`，只显示中文失败阶段、原固定reason和存在时的整数provider代码；不要求客户端读取details。仅明确scope_required建议检查应用群读取权限；传输或完整分页失败使用中性排查说明，不误要求重新授予群监控权限。最后出口再次经provider原白名单净化，不复制异常正文/响应/身份值。成功回执不变。列表只展示监控授权，旧assistant配置记录在原策略保留可读、可精确撤销，不显示为当前权限。

补充先红10项后绿；3文件108通过，最后仅增加旧配置列表隐藏测试后新直接文件33通过（0失败/错误/跳过）。原274集中结果与此补充分别保留，未将两次数量相加为唯一测试数。Ruff/diff通过。除回执显示和新测试之外，其余冻结实现未变。
