# 群事实查询安全诊断交接

## 基本信息

- workstream: 飞书群事实查询只读诊断及固定错误投影
- branch: `codex/feishu-native-images`
- owner: `live_route_attribution`
- date: 2026-10-15
- source baseline: `2812638850ff02c091c5bc980019f5c9474a9b30`

## 本线目标

定位重启后 `/group` 返回 `GROUP_AUTHORITY_UNAVAILABLE` 的可能阶段，使原调用方能安全报告具体固定原因，而不是把注册、认证、传输、群字段或分页失败都说成权限不足。

## 实际完成

- 保留原Gateway懒加载REST factory和owner克隆，不引入WS、配置重读、备用token或另一权限来源。
- `FeishuProviderFactError.safe_diagnostics()` 是唯一公开诊断投影，只返回固定白名单 `reason_code`、准确整数或空值 `provider_code`、固定阶段 `stage`；不投影异常正文、响应正文、URL、token、成员或群值。
- 固定阶段为 `auth/registry/chat_info/bot_membership/members/message/response_validation`；未知值关闭式净化，布尔值不算整数码。
- 原REST读取明确区分HTTP、传输、解码、非零平台码及坏响应。保留原无重定向、原超时和失败关闭，未增加重试。
- 群主open_id或tenant字段不可信时提前给准确阶段错误；没有将缺字段推断为默认身份，没有忽略未知成员类型或不完整分页。
- 当前官方群授权语义与诊断在控制入口的投影由主线及群授权作者接线，本线不修改其文件。

## 改动文件

- `agent_py_agent/agent/adapter/feishu_provider_facts.py`
- `agent_py_agent/tests/test_feishu_bot_observations.py`
- 本交接文档

## 测试命令和结果

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m pytest \
  agent_py_agent/tests/test_feishu_bot_observations.py -q --tb=short
```

结果：51 passed，0 failed/error/skip。原XML为本轮工件 `evidence/group-provider-diagnostics-focused.xml`；局部Ruff及本线diff检查通过。新增测试覆盖每个群查询阶段的供应商错误、HTTP/传输/解码失败的无值诊断、未知诊断净化、坏owner/tenant仍拒绝，以及真实默认REST factory和owner克隆冷查询不启动WS。

## 已证事实与未决根因

- 原Gateway `core.py` 会创建 `build_default_channel_registry`；原群控制冷host继承基础 `delivery_service`，不依赖入站WS热身或已加载群owner。
- 隔离真实factory及 `_config_with_owner` 均完成chat info、bot membership、members三个查询并成功；这反证“重启一定丢REST注册”这一泛化说法，不能排除真实进程的配置或认证故障。
- 本地官方Lark SDK的 `GetChatResponseBody` 明确声明 `owner_id_type/owner_id/tenant_key`，`ListMember` 声明 `member_id_type/member_id/name/tenant_key`，`IsInChatChatMembersResponseBody` 声明 `is_in_chat`。原合成夹具形状与这些字段一致。
- 缺少owner_id_type的合成响应可复现同一旧公开错误，但没有真实响应事实证明当前现网就是这一原因，故没有放宽解析。
- 本线未读真实配置、密钥或现网群数据，未外发、启动WS或操作服务。

## 需要主线重点复查

- 群控制仅消费typed错误的 `safe_diagnostics()`，不要复制白名单或输出异常链。
- 群授权新口径及可信mention接线需沿原认证metadata和同群当前成员门；不能从正文姓名或显示名推身份。
- 主线取得真实官方查询的固定阶段和无值形状后，再决定是否存在需修复的字段解析或部署问题。

## 剩余风险

本切片改善可定位性，尚未证明用户现网故障的唯一根因；消息主动发送/云文档成功也不能证明群成员查询拥有相同权限或成功响应。

## 后续建议

建议下一步：主线先核真实查询的阶段/固定码与字段存在性，再做最小准确修复和统一gate。群授权作者可并行接诊断和已授权新语义，不能用诊断切片放宽群主、机器人或完整成员事实门。
