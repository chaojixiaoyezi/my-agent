# R03 云表格权限与读回交接

## 基本信息

- workstream：IM 云表格授权修复
- branch：codex/im-entry-update-assessment
- owner：原 R01/R02 子代理
- date：2026-10-14
- 同线共享 checkout；未提交、推送、部署或访问真实租户文档。

## 本线目标与实际完成

资源编辑已获授权但保护集合合法省略时，旧门误拒绝。本线保留原实时资源权限/保护检查，只在同 extFields=protectedRange 响应的完整唯一工作表/正确清单规模/正网格/无矛盾保护指标中接受官方可省略集合。裸 sheetId 重复响应、畸形数组、重复/cross-sheet 仍失败关闭，增加 protection_metadata_incomplete/invalid/target_missing 结构诊断，不要求用户重复分享来补 API 响应。

原云工具增加同 adapter/后端/资源的一次机械 GET：append 只用官方 updates.updatedRange，update 用真实 values 矩形并检查供应商给出的 range 未冲突。字面比较给 verified/mismatch，缺范围/公式/富值/表示变换/读取失败给 unverifiable，开关关闭给 disabled。write_accepted=true 与核验状态分开；核验失败不变成 not_started，不重写业务数据，不公开被比较 values。

## 改动文件

- agent/user_space/feishu_sheet_authorization.py
- agent/user_space/feishu_sheet_verification.py（新增）
- agent/tooling/feishu_cloud_tool.py（只描述、execute 写后核验接线）
- tests/test_feishu_sheet_cloud_authorization.py
- tests/test_feishu_sheet_verification.py（新增）
- docs/design/IM_SHEET_PROTECTION_METADATA.md
- 本交接

root 独占并已添加 feishu_cloud_verify_sheet_writes_enabled 的 YAML/dataclass 默认 true；请 root 完成配置解析与根文档同步。

## 官方依据

[官方 Go SDK v1.1.48 model.go](https://github.com/larksuite/oapi-sdk-go/blob/v1.1.48/service/sheets/v2/model.go#L412)：Sheet.ProtectedRange 为 omitempty 集合，AppendResult.Updates.updatedRange 与 UpdateResult.updatedRange 定义返回范围。[同版本 api.go](https://github.com/larksuite/oapi-sdk-go/blob/v1.1.48/service/sheets/v2/api.go#L454)：GET metainfo + SetExtFields，原 tenant/user 身份。版本、源文件 SHA256 与现代 GetSheet 无保护字段的边界见模块设计。只核对公开 schema，没有新增 SDK 依赖；API 文档站部分动态页无法展开，没有拿镜像作为授权依据。

## 测试命令和准确结果

解释器/Ruff：项目 .venv。

- 三文件 metadata/cloud HTTP/ToolExecutor 组合：87 passed。
- 加三个 prior-edit-authority 负例与写后核验后，四文件组合 test_feishu_sheet_verification.py、test_feishu_sheet_cloud_authorization.py、test_feishu_cloud_collaborator_authorization.py、test_feishu_cloud_tool.py：103 passed。
- 最后追加 provider 更新范围冲突五例并细化该判断：最新 test_feishu_sheet_verification.py 为 18 passed；根应统一重跑四文件组合，预期 108 项，不把预期写成已跑。
- 最终本线五个源码/测试 scoped Ruff：passed。
- 最终 git diff --check：passed。

第一轮新用例四个错误是断言误把获取 token 的 auth 路径当作权限调用，已修为过滤认证路径并通过复验。不是产品错误，不放宽断言的权限先后关系。

## 主线复核与协调

根需同步 DESIGN_LEDGER、CODEBASE_TREE、TESTS 及 covered module 四件套，并将新增文件纳入发行。新增配置由根统一解析/部署上界与默认值；本线读取 capability_config_for_agent 的真实配置，disabled 的集成用例验证没有 GET。

保留 v2 保护范围用户编辑者合同，不从现代 SDK chats 推断旧接口群编辑权；群资源编辑权不能借群成员/应用私人保护编辑权。显式准确 administrator_grant 保留已有权威。

## 剩余风险

未做真实租户/飞书收件验收；SDK 省略 schema 证据证明合法形态，不保证任意省略响应完整。严格完整性门保留。readback 只能对已确认范围、字面形态做确定比较；读失败或表示变换明确不宣称已验证。并发他人改同单元格可能导致 mismatch，宿主不擅自覆盖或重试。原提供方读取无 range 时是 unverifiable。

建议下一步：根先统一四文件组合/严格 gate 和配置门，再用授权隔离文档检查真实 empty/omitted 响应与写读一致性。其他 agent 可并行只读审查省略边界及 accepted/verified 分离，不改生产权限、不借其它 owner 或应用私人权限。
