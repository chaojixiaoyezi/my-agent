# 飞书云资源 C01–C04 修复交接

## 基本信息

- workstream：云文档真实本人/群权限、wiki 定位、管理员查询审批、接口错误分层。
- owner：latency_prompt_audit；主线程统一集成与发布。
- date：2026-10-13。
- 基线：`84a63ea87646570b83e5e808cb29b021c5301b27`。
- 当前：本地切片完成；不自行提交、推送、部署，不发送真实 IM，不读取生产对话或密钥。

## 本线目标与实际完成

普通用户和群不必逐文档申请管理员手工登记，只要飞书已经明确分享给当前本人/群，宿主可用同一应用凭据代为操作。共享应用有权不代表所有调用方有权。原准确资源/操作人工许可保持原单一存储，没有第二份授权账。

1. 当前 owner 身份复用原 `resolve_message_target`。私聊本人 `openid`、群 `openchat` 精确匹配当前 owner；本地管理员仅用唯一绑定，不回落任一管理员身份。
2. 每次实时查询官方协作者，读支持 view/edit/full_access，写需要 edit/full_access。仅非 wiki 私人 owner 可用准确资源元数据的 owner_id 证明本人所有者。未知/冲突/撤销事实拒绝。
3. wiki 在宿主内部统一 `get_node` 映射，核对原节点 container/single_page 页面许可。页面授权前不返回映射；底层私人文件权限不替代页面许可，不重复手工授权 wiki 与真实对象。
4. 表格自助写入先查工作表保护元数据及每批最多 5 项的保护详情。正规 A1 更新按闭区间行列相交；值超过声明范围也纳入核验。本人只使用准确 openId 编辑者，群不能借私人编辑者。追加目的行无法预先证明，存在保护时需本人所有保护项编辑权；群拒绝受保护追加。准确人工管理员操作许可仍是已有显式授权来源。
5. `admin_controls` 的 list/resources 只读查询不再 always 审批； mutating/dangerous 保持必须本人确认，Full Access 不免写入确认。通用 `ApprovalPolicy.always_effects` 接线由主线程实现。
6. 云错误公开 HTTP/API 数值和稳定 reason，区分 scope、认证、资源拒绝、不存在、限流、额度、故障和拒绝重定向。写入 429/5xx、陌生 API 错误和损坏响应保留 unknown。无自动重试；原 ToolExecutor 操作账不重复追加。成功输出使用 `resource_authorization`，避开原 executor 的 authorization 字段投影。

## 改动文件与主线协同

| 文件 | 职责 |
| --- | --- |
| agent_py_agent/agent/adapter/feishu_cloud.py | 标准库固定云调用，元数据只读 POST，分层错误、秘密回显清理、拒绝重定向 |
| agent_py_agent/agent/user_space/feishu_cloud_authorization.py（新增） | 当前真实主体、实时准确协作者/所有者、wiki 页面与业务对象映射 |
| agent_py_agent/agent/user_space/feishu_sheet_authorization.py（新增） | 目标工作表保护核验、A1 相交、准确编辑者、批次查询 |
| agent_py_agent/agent/tooling/feishu_cloud_tool.py | 接线、可选 resource_type=wiki、资源类型核查、保护门、许可来源结果 |
| agent_py_agent/agent/tooling/admin_controls_tool.py | 只读与写入的审批效果分离 |
| agent_py_agent/tests/test_feishu_cloud_tool.py | 原人工许可关闭自助模式回归、供应商合成服务 |
| agent_py_agent/tests/test_feishu_cloud_collaborator_authorization.py（新增） | 真实 HTTP + ToolExecutor 的用户/群/wiki/撤销/身份/错误/审批验收 |
| agent_py_agent/tests/test_feishu_sheet_cloud_authorization.py（新增） | 保护编辑者/相交/未知事实/批次及准确人工许可回归 |
| docs/design/HOST_CLOUD_RESOURCE_ACCESS.md | 当前授权来源、保护范围、配置、错误及真实验收边界 |

主线程负责 capability YAML/dataclass 的 `feishu_cloud_collaborator_authorization_enabled: true`，根功能开关仍为 `feishu_cloud_tools_enabled`。主线程也负责共享文件树、设计台账、测试入口以及通用错误 taxonomy。

新增稳定错误：`CLOUD_RESOURCE_MAPPING_INVALID`、`CLOUD_RESOURCE_PRINCIPAL_UNAVAILABLE`、`CLOUD_RESOURCE_PERMISSION_UNKNOWN`、`CLOUD_RESOURCE_TYPE_MISMATCH`、`CLOUD_RESOURCE_RANGE_NOT_AUTHORIZED`；`FEISHU_CLOUD_SCOPE_REQUIRED`、`FEISHU_CLOUD_AUTH_INVALID`、`FEISHU_CLOUD_RESOURCE_FORBIDDEN`、`FEISHU_CLOUD_RESOURCE_NOT_FOUND`、`FEISHU_CLOUD_RATE_LIMITED`、`FEISHU_CLOUD_QUOTA_EXCEEDED`、`FEISHU_CLOUD_SERVICE_UNAVAILABLE`、`FEISHU_CLOUD_REDIRECT_REFUSED`。

## 测试命令与结果

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD:$PWD/agent_py_agent" \
  python -m pytest \
  agent_py_agent/tests/test_feishu_cloud_tool.py \
  agent_py_agent/tests/test_feishu_cloud_collaborator_authorization.py \
  agent_py_agent/tests/test_feishu_sheet_cloud_authorization.py \
  agent_py_agent/tests/test_decision_audit_controls.py -q -o addopts='' --tb=short

ruff check agent_py_agent/agent/adapter/feishu_cloud.py \
  agent_py_agent/agent/tooling/feishu_cloud_tool.py \
  agent_py_agent/agent/tooling/admin_controls_tool.py \
  agent_py_agent/agent/user_space/feishu_cloud_authorization.py \
  agent_py_agent/agent/user_space/feishu_sheet_authorization.py \
  agent_py_agent/tests/test_feishu_cloud_tool.py \
  agent_py_agent/tests/test_feishu_cloud_collaborator_authorization.py \
  agent_py_agent/tests/test_feishu_sheet_cloud_authorization.py

python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json \
  --report /tmp/IM_CLOUD_CODE_SIZE_REPORT.md
git diff --check
```

最终定向验收 **87 passed / 38.43s**，包含陌生 API 与响应形状的未知写入用例；本片 Ruff 全绿，strict code-size blocked=False，diff check 无空白错误。没有用合成服务冒充真实租户验收；主线程仍须跑完整发布 gate。

## 官方接口证据

公开说明：[获取协作者](https://open.feishu.cn/document/server-docs/docs/permission/permission-member/list)、[表格元数据及保护清单](https://open.feishu.cn/document/server-docs/historic-version/docs/sheets/obtain-spreadsheet-metadata)、[保护范围详情](https://open.feishu.cn/document/server-docs/docs/sheets-v3/protect-range/retrieve-protection-scopes)、[wiki 映射](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/get_node)、[通用错误码](https://open.feishu.cn/document/server-docs/api-call-guide/generic-error-code)。

公开门户 `document_portal/v1/document/get_detail` 的完整响应与正文临时保存在 `/tmp/im_cloud_official_api_20261013/`，仅为本次复核证据，不是长期运行事实源。`members` 证明 openchat 与 wiki perm_type；`protection-details` 证明保护起止从 1 开始且两端包含、空 dimension 表示整工作表、memberType=openId、每批最多 5 个 ID；`sheet-metainfo` 证明 extFields=protectedRange。

## 剩余风险与建议下一步

先由主线程集成并跑发布 gate，再在隔离飞书测试资源做本人/群追加读回与撤销验收；可让另一 agent 并行只读复核 ACL 与 ToolExecutor 接线，不能替被测对象补结果。应用需要查看协作者的权限与云接口 scope，隐藏/组织/父目录继承未明确列出当前主体时不推断；本期没有用户 OAuth。

真实接口对零保护工作表是否始终返回显式 `protectedRange: []` 仍需验证，缺字段会明确拒绝，不假定无保护。追加的保护判断比更新保守，不能声称精确知道供应商最终追加行。保护所有者未明确列为编辑者时也不会凭应用权限代写。权限撤销按每次现查执行；已在飞书处理的调用无法倒流，也没有跨 API 事务保证消除证明与写入之间的竞态。
