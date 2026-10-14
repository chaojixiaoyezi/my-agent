当前实施导航：本期已接原权限自查、目录交集与准确availability，见 [实施合同](IM_UNATTENDED_IMPLEMENTATION.md) 和 [实施交接](../tasks/IM_SELF_DISCOVERY_HANDOFF.md)。下面保留原只读审计事实及当时测试红，不代表当前代码状态。

# IM 能力发现与自身状态查询审计

状态：2026-10-14，首轮只读审计完成；未修改生产源码、仓库测试、配置或安装包。基线 `b3e5aae686c067ee19c03672ab717f7526175acb`；当前 docs-only 工作树不改变 `.20261014.2`。承接[无人值守执行评估](IM_UNATTENDED_EXECUTION_REVIEW.md)，用户追加要求：助手应能自己查询底座功能与当前开放能力，不依赖用户知道 auto 等术语。

## 已有能力需要保留

原 ToolRuntimeSnapshot 同时决定工具目录、Schema、检索和执行；list_tools 与 tool_search 已可用，搜索只在原授权与实际 availability 交集内，不增加权限。原生模型直接获得可见工具 Schema；折叠工具有名字及一句用途索引，Skill/公共内容包有有界摘要，并可按需检索正文。不能将此次结论说成“完全没有能力发现”。

子代理 prompt 已包含创建期 allowed_tools/skills、grant 数量、读写根和 effective_permissions；Shell 错误有沙箱边界事实，controlled_exec 的 apply=false 可做不执行的预览。工具错误也已有 error_code、failure_stage、handler_executed、retryable、recommended_action 和 recovery_hint。缺口是事实不完整、投影丢失、不同上下文接线不同。

## 已确认的发现缺口

| 编号 | 事实 | 原实现与证据 |
| --- | --- | --- |
| D01 | 正规自查缺真实 ask/auto/full-access；工具清单 permission_mode 只是作用域标签 | contracts/tool_manifest_contract.py:200 硬映射 same_as_root_agent/owner_scoped；实际权威为 user_space/approval_mode.py |
| D02 | /status 不能区分 ask 和 auto；/permissions 只在 TUI/F4/认证接口 | status_projection.py:24 只给文件/Shell边界；command_catalog.py:393/431 从 IM 排除 permissions；两模式状态文本相同已合成复核 |
| D03 | 管理员参数查询不含 owner 审批模式，普通与群缺安全的通用自身状态入口 | admin user_config 查参数中心，无 permission_mode；普通 user 只有 decision 设置；group 不注册 user_config/gateway_status/audit_records，见 core.py:1247 |
| D04 | list_tools 短投影丢用途、完整参数、不可用原因与上下文标识，策略也漏字段 | registry.py:372；manifest approval_policy 只投 mode、不含 always_effects/owner_grant_parameters，实际 AdminControlsTool 合成投影已复核 |
| D05 | 私人凭据开关关闭，目录与 Schema 仍声称 ready | task_credential_tool.py 无 availability 覆写；配置 false 时 availability/snapshot/model_visible/list_tools 均 true，execute 首分支才返回 TOOL_UNAVAILABLE；已复现 |
| D06 | 普通执行子代理默认缺自身发现及新增通道工具，角色表不能证明真实创建链有它们 | coding 默认缺 list_tools/tool_search/request_credentials/feishu_cloud/send_message/schedule/user_config；tool_grants→create_policy→role_contracts 的非 None 名单不并角色默认表；纯合同探针已确认 |
| D07 | 内部后台续跑与正式定时计划的工具面不同 | 生命周期/进度/Goal 默认子集缺发现/HTTP/凭据/云工具；scheduled_job_due 显式 allowed_tools=None，保留完整 owner 目录，不能统称后台缺工具 |
| D08 | 描述与测试清单落后于实际发现行为 | tool_search 仍描述下一轮临时加载，实际按线程累计并跨 Compact 恢复；新 feishu_cloud 已默认折叠，冻结测试集合未同步，两个测试真实失败 |

list_capabilities 当前只见残留注释，没有注册工具。飞书 CLI 也未有专用模型工具或身份桥；通用 Shell 和已隔离验证的 CLI 二进制，不代表普通/群 OAuth 及产品接线完成。不能让模型把设计文档或名字出现当成当前开放能力。

## 上下文与权限区分

- 管理员有参数中心/网关工具；普通私聊只有自身决策设置；群没有宿主诊断工具。应补本人的窄只读投影，不把管理员配置、日志或秘密开放给群。
- 普通 coding worker 的实际默认工具面与 coordinator/主代理显式 allowed_tools 不同；能力上抛可以申请原父级允许的内容，不能靠角色文字猜拥有工具。
- 内部后台续跑有默认子集；正式 schedule 到期获取完整 owner 目录。公共内容包不等于私有包/插件执行权限；普通 MCP 是否可见仍由配置、连接、禁用表和发布事实决定。

## 复用方向（尚未实施）

扩展原 list_tools 的当前上下文只读事实；继续用 tool_search 读取实际可用工具详情，人工命令目录与 /status 从同一权威投影获得状态。不要再造平行能力平台、权限账，或让模型扫源码/读取宿主配置猜功能。

需要区分产品支持、当前已配置、当前角色开放、本轮已就绪、Schema已加载，以及具体资源是否已授权。返回真实审批选择/执行模式、文件与网络边界、父级收窄、来源、不可用理由和正规下一步；查询只认宿主冻结的 owner/actor/run，不接受模型指定别人的身份。

短模型视图保留用途、必要可用性原因与真实有效状态；完整参数按需读取，避免全功能说明每轮灌入上下文。提示只加简短导航：用户问能力/权限时先查真实状态，再解释；自然语言引导不替代执行器授权。

查看能力与修改权限分开。模型可以解释 auto 和正规控制入口，不能自行改权限、打开插件或解锁凭据。普通/群的宿主默认自动执行仍需按 U01～U08 验证硬边界、停止免审与等待释放资源，不因增加查询入口就宣称自动化已修。

## 本轮验证与局限

- 根发现/配置/IM命令五文件：53项，53通过、0失败、0跳过，自然exit0。
- 自查/manifest/代理树/状态八文件：61项，61通过、0失败、0跳过，自然exit0；与根53项不重叠。
- 发现/折叠/公共包等七文件：86项，84通过、2失败、0跳过；两次保留相同失败，均涉及 test_tool_default_deferral.py 的 DECLARED 集合遗漏 feishu_cloud。未改断言或产品，不称全绿，不将交叉批次相加成唯一总数。
- 合成实证：不同私人审批卡/解锁重试被吞；关闭凭据仍声明 ready；真实策略字段在 manifest 丢失；普通 worker 默认工具名单和两模式相同状态投影。没有真实飞书/模型/业务请求。
- XML、原红log、合成JSON和来源元数据已保存独立产物 my-agent-im-unattended-audit-20261014，hash与分批结果见 audit-verification.json。初次探针环境 import 失败另保留，不算产品故障或通过案例。

没有真实管理员失败回调、员工账号业务结果或大规模负载验收；不能断言现场点击失败仅由去重造成，也不能声称当前已支持几十万同时执行。模型是否主动正确采用查询，需要后续真实模型和租户验收。

## 建议下一步

先冻结本审计与无人值守评估；修复时优先停止免审、卡片去重、错误终态与角色同源自查，再解决默认自动执行、持久输入释放资源和子代理真实工具继承。只读投影、创建/后台工具合同与 CLI 后端可分线并行；原 owner/actor/资源硬门及唯一状态来源必须保留。本轮不发布或代员工审批。
