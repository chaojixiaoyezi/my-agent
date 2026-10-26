# 四大一等公民（Workflow / Monitor / Script / Skill）跨用户依赖闭包投递、飞书联动确认与防骚扰屏蔽设计规范

状态：2026-10-25 需求与接口契约定稿（待底座实现）

---

## 1. 业务目标与核心场景

在多用户（`local/main` 管理员、本地普通用户 `local/<user_id>`、飞书用户 `providers/feishu/users/<open_id>`）共用同一底座时，用户在 Web 工作台或飞书会话中需要将自己沉淀好的 **工作流（`workflow`）、定时监控（`monitor`）、固定脚本（`script`）、技能（`skill`）** 投递给其他指定用户。

核心业务闭环要求：

1. **全依赖闭包自动連带打包（Transitive Dependency Closure）**：
   - 用户在【工作流】【定时监控】【固定脚本】【Skill】任一页面（或通过命令/工具）选中某一项资产发起投递时，系统通过 `agent_py_agent/agent/assets/topology.py::build_asset_topology(..., root_key=f"{kind}:{id}")` 自动求取该资产的**完整前向可达依赖闭包**。
   - **典型示例**：
     - 用户只选中 `1` 条工作流 `wf_order_audit` 投递给用户 `alice`，而该工作流内部步骤引用了 `3` 个 Skill（`sk_1, sk_2, sk_3`）和 `3` 个固定脚本（`sc_1, sc_2, sc_3`），或者嵌套了子工作流与关联监控；
     - 投递单会自动把这 **`1` 个工作流 + `3` 个 Skill + `3` 个固定脚本（以及任何相互引用的工作流/监控）** 一次性完整打包进同一个投递包裹（Bundle）。
   - **独立落盘与单独复用**：
     - 接收方确认接受后，包裹内的每一项资产都会分别落盘到接收方自己 `<receiver_owner_home>` 下的标准权威目录：
       - 工作流 $\rightarrow$ `<receiver_owner_home>/workflows/<workflow_id>/`
       - 固定脚本 $\rightarrow$ `<receiver_owner_home>/scripts/<script_id>/`
       - Skill $\rightarrow$ `<receiver_owner_home>/skills/<skill_name>/SKILL.md`（及附属 `references/`、`templates/`、`scripts/`）
       - 定时监控 $\rightarrow$ `<receiver_owner_home>/schedules/jobs.json`（及监控绑定元数据）
     - 落盘后，接收方不仅可以运行该工作流/监控，**还可以单独在自己的固定脚本页面、Skill 页面独立查看、编辑、试跑或在其他工作流中复用这 3 个固定脚本和 3 个 Skill**。

2. **接收方确认接手 + 默认关闭待手动开启（Safe-by-Default Activation）**：
   - 投递发出后，不会直接写进接收方的正式资产目录，而是进入接收方的 **「待确认投递收件箱（Pending Deliveries）」**。
   - 接收方确认 **「接受（Accept）」** 后，资产正式落盘到接收方空间，但**初始运行状态一律设为「待手动开启（`enabled=False` / `status="disabled"`）」**：
     - 定时监控落盘时 `enabled: false`（不会未经接收方开启就自动后台跑定时任务）；
     - 工作流/固定脚本/Skill 落盘时标记 `enabled: false`（或 `activation_state: "disabled"`）；
     - 接收方在 Web 页面上（或在飞书里向自己的 Agent 下达开启指令）**手动点击「开启 / 启用」** 后，该资产才正式进入启用态。

3. **双端联动（Web 页面 + 飞书 Agent 主动通知与确认）**：
   - **Web 页面端**：
     - 在【工作流】【固定脚本】【Skill】【定时监控】4 个页面上，每条资产提供 **「📤 投递给其他用户」** 按钮，弹窗列出可投递的目标用户并预览自动連带打包的依赖资产清单（如“1 个工作流 + 3 个 Skill + 3 个固定脚本”）。
     - 接收方页面顶部及各模块提供 **「📥 投递收件箱」**，支持查看投递详情、一键 **「✅ 接受投递」**、**「🚫 拒绝」**、以及接受后在资产列表一键 **「▶️ 手动开启 / ⏸️ 停用」**。
   - **飞书端联动**：
     - 若接收方绑定了飞书账号（或本身是 `feishu` 通道用户），投递创建后，底座通过通道投递服务（`ChannelDelivery` / 飞书私聊通知）由接收方自己的飞书 Agent 主动向接收方发送一条投递通知消息/交互卡：
       > *“📦 收到来自【张三 (alice)】的资产投递：主资产《订单风控巡检工作流》（内含 1 个工作流、3 个固定脚本、3 个 Skill）。是否确认接收？可回复或点击【接受投递】【拒绝】【接受并立即开启】【屏蔽该用户投递】。”*
     - 接收方在飞书里确认接受后，可在飞书里继续让自己的 Agent 开启并使用这些资产。

4. **防骚扰 / 防污染的「用户级投递屏蔽名单（Sender Blocklist）」**：
   - 接收方可以随时屏蔽某个发送方用户（防止某人频繁乱投递污染收件箱）。
   - **结构化屏蔽名单**：每个接收方在自己的 `<receiver_owner_home>/data/asset_deliveries/blocklist.json` 维护已屏蔽的发送方 `owner_id` 列表（含屏蔽时间、备注原因）。
   - **前置硬拦截**：当发送方 A 向接收方 B 发起投递时，底座在入箱和发送飞书通知前先检查 B 的 `blocklist.json`；若 A 在屏蔽名单中，直接返回结构化错误码 `DELIVERY_BLOCKED_BY_RECIPIENT`，不写入 B 的收件箱，也不触发任何飞书通知。
   - **名单管理与解除屏蔽**：接收方可在 Web 页面或飞书命令中查看自己的 **「🛡️ 投递屏蔽名单」**，并随时将某用户 **「解除屏蔽（Unblock）」**。

---

## 2. 权威目录与存储 Schema（单一事实源）

遵循 `AGENTS.md` “一个概念一个权威位置”铁律，不建全局集中式数据库，所有投递状态与屏蔽名单按 Owner 空间隔离落盘，使用 `nofollow_fs` 与 `locked_json_path` 保证原子性：

```text
<receiver_owner_home>/
|-- data/
|   `-- asset_deliveries/
|       |-- .lock                                   # 接收方投递收件箱与屏蔽名单并发文件锁
|       |-- blocklist.json                          # 接收方维护的「已屏蔽发送方名单」(schema: asset_delivery_blocklist.v1)
|       `-- inbox/
|           `-- <delivery_id>.json                  # 单条跨用户投递包裹与完整冻结快照 (schema: asset_delivery_envelope.v1)
|-- workflows/<workflow_id>/workflow.json           # 接收确认后落盘的工作流（初始 enabled=false）
|-- scripts/<script_id>/script.json + main.py       # 接收确认后落盘的固定脚本（初始 enabled=false）
|-- skills/<skill_name>/SKILL.md                    # 接收确认后落盘的 Skill（初始 enabled=false）
`-- schedules/jobs.json                             # 接收确认后落盘的定时监控（初始 enabled=false）

<sender_owner_home>/
`-- data/
    `-- asset_deliveries/
        `-- outbox/
            `-- <delivery_id>.json                  # 发送方只读发件回执索引（记录投递状态：pending / accepted / rejected / revoked）
```

### 2.1 投递包裹 Schema (`asset_delivery_envelope.v1`)

```json
{
  "schema_version": "asset_delivery_envelope.v1",
  "delivery_id": "dlv_20261025_8f3a91c2",
  "created_at": "2026-10-25T19:00:00Z",
  "updated_at": "2026-10-25T19:00:00Z",
  "revision": 1,
  "status": "pending",
  "sender_owner": {
    "provider": "local",
    "owner_kind": "main",
    "owner_id": "main",
    "display_name": "管理员 (me)"
  },
  "recipient_owner": {
    "provider": "local",
    "owner_kind": "user",
    "owner_id": "alice",
    "display_name": "Alice 研发工程师"
  },
  "root_asset": {
    "kind": "workflow",
    "id": "wf_order_risk_audit",
    "name": "订单风控全链路核查工作流"
  },
  "note": "这套工作流包含3个核查脚本和3个分析Skill，接收后可在固定脚本页配置你自己的数据源凭据后开启。",
  "closure_summary": {
    "workflows_count": 1,
    "monitors_count": 0,
    "scripts_count": 3,
    "skills_count": 3,
    "total_assets_count": 7,
    "total_bytes": 18420
  },
  "bundled_assets": [
    {
      "kind": "workflow",
      "id": "wf_order_risk_audit",
      "name": "订单风控全链路核查工作流",
      "summary": "串联订单SQL核查与风险研判",
      "is_root": true,
      "content_sha256": "...",
      "payload": { "...完整脱敏工作流定义..." }
    },
    {
      "kind": "script",
      "id": "order_sql_audit_script",
      "name": "订单风控 SQL 核查脚本",
      "summary": "只读核对订单主表与支付流水",
      "is_root": false,
      "content_sha256": "...",
      "payload": { "...descriptor + entrypoint + code + files（不含任何 .vault_key 密钥）..." }
    },
    {
      "kind": "skill",
      "id": "risk-triage-guide",
      "name": "risk-triage-guide",
      "summary": "风控异常研判规则",
      "is_root": false,
      "content_sha256": "...",
      "payload": { "...SKILL.md 正文及附属静态资源文件..." }
    }
  ],
  "feishu_notification": {
    "notified": true,
    "notified_at": "2026-10-25T19:00:01Z",
    "channel": "feishu",
    "message_id": "om_xxx"
  },
  "resolution": {
    "resolved_at": null,
    "resolved_by_owner": null,
    "conflict_strategy": "keep_both_or_overwrite",
    "installed_assets": []
  }
}
```

### 2.2 投递屏蔽名单 Schema (`asset_delivery_blocklist.v1`)

存于 `<receiver_owner_home>/data/asset_deliveries/blocklist.json`：

```json
{
  "schema_version": "asset_delivery_blocklist.v1",
  "owner_id": "local/alice",
  "updated_at": "2026-10-25T19:05:00Z",
  "revision": 2,
  "blocked_senders": [
    {
      "sender_owner_id": "local/bob",
      "sender_display_name": "Bob 产品设计师",
      "blocked_at": "2026-10-25T19:05:00Z",
      "reason": "屏蔽无关测试脚本投递"
    }
  ]
}
```

---

## 3. 依赖闭包打包与安全脱敏规则

### 3.1 递归求取四类资产完整闭包
当发送方选定任意起点资产 `(root_kind, root_id)`（或显式多选一组起点资产）时：
1. 调用发送方空间的描述符收集器，汇总发送方当前拥有的全部 `workflow`、`monitor`、`script`、`skill` 的 `AssetDescriptor`。
2. 调用 `topology.py::build_asset_topology(descriptors, root_key=f"{root_kind}:{root_id}")`，利用 `_reachable` 迭代 DFS 求取所有前向可达节点集合（天然防循环依赖死锁，支持 `Workflow <-> Monitor` 或嵌套子工作流相互引用）。
3. 遍历可达集合中的每一个节点：
   - **`workflow`**：导出 `WorkflowDefinition`（保留 `steps` 中的 `script_ids`、`skill_ids`、`workflow_ids`、`monitor_ids` 引用关系，剥离历史 `last_run_id` 等运行态字段）。
   - **`monitor`**：导出定时监控规则与触发配置（保留绑定的 `workflow_id` / `script_ids` / `skill_ids`，剥离发送方私有的 `target_conversation_id` 群/私聊目标，接收方开启时自动绑定接收方自己的默认会话）。
   - **`script`**：导出 `AssetDescriptor`、`entrypoint`、`interpreter`、`timeout_seconds`、源码正文及脚本目录内附属相对文件；**保留 `data_sources` 结构化规格说明（表名、SQL 模板、用途），但严格剥离发送方 `<sender_home>/data/datasources/` 下的任何独立凭据密文与 `.vault_key`**。
   - **`skill`**：导出 `SKILL.md` 及 `references/`、`templates/`、`scripts/` 下的相对路径文件。
4. **敏感信息硬门扫描**：
   - 复用 `skill_transfer_manifest.py` 的敏感信息扫描器，对打包的所有文本/代码字节扫描 API Key、私钥、Bearer Token、明文密码形态；一旦命中直接拒绝打包并返回 `ASSET_DELIVERY_SECRET_DETECTED`，绝不带病投递。

### 3.2 接收确认后的两阶段原子落盘与「默认关闭、手动开启」
当接收方调用 `accept` 时：
1. **配额与两阶段事务**：复用 `workspace_assets.py` 的两阶段事务锁（`_BATCH_FILE`）与 `OwnerQuota`，确保包裹内的 `1 个 workflow + 3 个 skill + 3 个 script` 要么全部成功落盘到接收方对应目录，要么失败时完整回滚，不留半份残缺资产。
2. **同名资产冲突策略（`conflict_policy`）**：
   - `"overwrite"`（默认可选）：覆盖接收方同名资产并递增 `version`；
   - `"skip_existing"`：若接收方已有同 ID 的公共/自有基础脚本或 Skill，则保留接收方已有版本，仅安装缺失资产；
   - `"rename_root"`：允许接收方为根资产指定新 ID/名称。
3. **强制初始未开启状态（`enabled=False`）**：
   - 所有新接收落盘的 `workflow`、`monitor`、`script`、`skill` 在其元数据中写入：
     ```json
     "enabled": false,
     "delivery_provenance": {
       "delivery_id": "dlv_20261025_8f3a91c2",
       "from_owner_id": "local/main",
       "accepted_at": "2026-10-25T19:10:00Z"
     }
     ```
   - 调度器（Scheduler）、工作流引擎（WorkflowEngine）、脚本执行器与 Skill 快照器在读取资产时遵循 `enabled` 字段：未开启（`enabled=False`）的监控不会触发定时调度，未开启的工作流/脚本/Skill 在页面上显示 **「⏸️ 已接收待开启」** 标签，直到接收方点击 **「▶️ 开启」**（调用 `assets.delivery.toggle_asset` 或在飞书中指令开启）将其置为 `enabled=True`。

---

## 4. 需要新增开放的 Plugin Host v2 接口清单（9 个动作）

在 `agent_py_agent/agent/plugin_host/action_specs_v2_ext.py` 与 `capabilities_v2_handlers.py` 中新增以下 **9 个标准 Host v2 动作**（复用现有 `assets.read` / `assets.write` 能力声明与 `effective_owner` 强身份绑定，禁止从请求 body 伪造发送方或接收方身份）：

| 序号 | Action 名称 | 读写属性 | 所需 Capability | 功能说明 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `assets.delivery.targets.list` | 读 (`read`) | `assets.read` | 列出当前可投递的目标用户列表（排除当前登录者自身；若目标用户已将当前用户加入屏蔽名单，可返回 `blocked_by_target: true` 状态提示） |
| 2 | `assets.delivery.preview_bundle` | 读 (`read`) | `assets.read` | 给定起点资产 `(kind, id)`，递归计算并返回完整依赖闭包清单预览（含連带打包的几个 workflow、几个 monitor、几个 script、几个 skill 及字节大小） |
| 3 | `assets.delivery.send` | 写 (`write`, 幂等) | `assets.write` | 向指定目标用户 `recipient_owner_id` 发起打包投递；自动检查接收方屏蔽名单，通过后写入接收方 `inbox/`、发送方 `outbox/`，并触发接收方飞书通知 |
| 4 | `assets.delivery.inbox.list` | 读 (`read`) | `assets.read` | 查询当前登录用户的投递收件箱（支持按 `status: "pending" \| "accepted" \| "rejected" \| "all"` 筛选）及发件箱回执列表 |
| 5 | `assets.delivery.accept` | 写 (`write`, 幂等) | `assets.write` | 接收方确认接受某条投递 `delivery_id`（支持 `conflict_policy` 与可选 `auto_enable: false`），将包裹内全部资产原子落盘到接收方对应目录，初始 `enabled=false` |
| 6 | `assets.delivery.reject` | 写 (`write`, 幂等) | `assets.write` | 接收方拒绝某条投递 `delivery_id`（可选 `block_sender: true` 顺带将该发送方加入屏蔽名单）；或发送方撤回尚未被接受的投递 |
| 7 | `assets.delivery.toggle_asset` | 写 (`write`, 幂等) | `assets.write` | 接收方在页面或飞书中手动开启/关闭某项资产（`kind`, `id`, `enabled: true \| false`，支持一键同时开启同批次投递的关联依赖资产 `enable_dependencies: true`） |
| 8 | `assets.delivery.blocklist.list` | 读 (`read`) | `assets.read` | 查看当前登录用户的「投递屏蔽名单 (`blocked_senders`)」 |
| 9 | `assets.delivery.blocklist.update` | 写 (`write`, 幂等) | `assets.write` | 将指定用户 `target_owner_id` 加入投递屏蔽名单（`mode: "block"`）或从屏蔽名单中解除屏蔽（`mode: "unblock"`） |

### 4.1 关键接口请求与响应契约示例

#### （1）`assets.delivery.preview_bundle`（投递前预览依赖闭包）
- **Request Payload**:
  ```json
  {
    "root_kind": "workflow",
    "root_id": "wf_order_risk_audit"
  }
  ```
- **Response Data**:
  ```json
  {
    "root": {"kind": "workflow", "id": "wf_order_risk_audit", "name": "订单风控全链路核查工作流"},
    "closure_counts": {"workflows": 1, "monitors": 0, "scripts": 3, "skills": 3, "total": 7},
    "items": [
      {"kind": "workflow", "id": "wf_order_risk_audit", "name": "订单风控全链路核查工作流", "is_root": true},
      {"kind": "script", "id": "sc_order_sql", "name": "订单主表核查脚本", "is_root": false},
      {"kind": "script", "id": "sc_ledger_sql", "name": "渠道流水比对脚本", "is_root": false},
      {"kind": "script", "id": "sc_risk_hit", "name": "风控规则命中统计脚本", "is_root": false},
      {"kind": "skill", "id": "sk_order_triage", "name": "订单异常研判指南", "is_root": false},
      {"kind": "skill", "id": "sk_sql_safety", "name": "只读SQL安全审计规范", "is_root": false},
      {"kind": "skill", "id": "sk_report_fmt", "name": "风控复盘报告模板", "is_root": false}
    ]
  }
  ```

#### （2）`assets.delivery.send`（发起跨用户打包投递）
- **Request Payload**:
  ```json
  {
    "recipient_owner_id": "local/alice",
    "root_kind": "workflow",
    "root_id": "wf_order_risk_audit",
    "note": "发你一套订单风控巡检工作流（含3个脚本和3个Skill），接收后在固定脚本里配一下你的库就能开启使用。"
  }
  ```
- **Response Data（成功 `HTTP 201`）**:
  ```json
  {
    "delivery_id": "dlv_20261025_8f3a91c2",
    "status": "pending",
    "sender_owner_id": "local/main",
    "recipient_owner_id": "local/alice",
    "bundled_counts": {"workflows": 1, "monitors": 0, "scripts": 3, "skills": 3, "total": 7},
    "feishu_notified": true
  }
  ```
- **被接收方屏蔽时的错误响应（`HTTP 403`）**:
  ```json
  {
    "code": "DELIVERY_BLOCKED_BY_RECIPIENT",
    "message": "目标用户已设置投递屏蔽，无法向其投递资产",
    "outcome": "not_started",
    "retryable": false
  }
  ```

#### （3）`assets.delivery.accept`（接收方确认接手，原子分放各目录并默认关闭）
- **Request Payload**:
  ```json
  {
    "delivery_id": "dlv_20261025_8f3a91c2",
    "expected_revision": 1,
    "conflict_policy": "overwrite",
    "auto_enable": false
  }
  ```
- **Response Data（`HTTP 200`）**:
  ```json
  {
    "delivery_id": "dlv_20261025_8f3a91c2",
    "status": "accepted",
    "installed_assets": [
      {"kind": "workflow", "id": "wf_order_risk_audit", "path_logical": "workflows/wf_order_risk_audit", "enabled": false},
      {"kind": "script", "id": "sc_order_sql", "path_logical": "scripts/sc_order_sql", "enabled": false},
      {"kind": "script", "id": "sc_ledger_sql", "path_logical": "scripts/sc_ledger_sql", "enabled": false},
      {"kind": "script", "id": "sc_risk_hit", "path_logical": "scripts/sc_risk_hit", "enabled": false},
      {"kind": "skill", "id": "sk_order_triage", "path_logical": "skills/sk_order_triage", "enabled": false},
      {"kind": "skill", "id": "sk_sql_safety", "path_logical": "skills/sk_sql_safety", "enabled": false},
      {"kind": "skill", "id": "sk_report_fmt", "path_logical": "skills/sk_report_fmt", "enabled": false}
    ],
    "activation_hint": "全部 7 项资产已分别放入您的【工作流】【固定脚本】【Skill】目录，当前默认为未开启状态，您可在页面手动开启或单独使用其中任意脚本与 Skill。"
  }
  ```

#### （4）`assets.delivery.toggle_asset`（手动开启 / 停用资产）
- **Request Payload**:
  ```json
  {
    "kind": "workflow",
    "id": "wf_order_risk_audit",
    "enabled": true,
    "enable_dependencies": true
  }
  ```

#### （5）`assets.delivery.blocklist.update`（屏蔽某用户投递 / 解除屏蔽）
- **Request Payload**:
  ```json
  {
    "target_owner_id": "local/bob",
    "mode": "block",
    "reason": "屏蔽测试投递"
  }
  ```

---

## 5. 飞书端（IM / TUI）通知、斜杠控制命令与 Agent 工具集

除了 Web 页面调用上述 Host v2 接口外，为保证用户在**不登录网页、纯用飞书**时也能完整闭环，底座同步提供：

1. **接收方飞书 Agent 主动通知**：
   - 当 `assets.delivery.send` 写入接收方 `inbox/<delivery_id>.json` 后，若接收方拥有绑定的飞书会话（`channel_bindings.json` 或 `providers/feishu/users/<open_id>`），底座向接收方的飞书私聊投递一条结构化通知卡片/消息：
     - 展示：发送人、主资产名称、連带包含的 `workflow / monitor / script / skill` 数量与清单、发送人附言；
     - 交互按钮 / 快捷命令提示：
       - `✅ 接受投递（默认不自动开启）` $\rightarrow$ `/delivery accept <delivery_id>`
       - `🚀 接受并立即开启全部关联资产` $\rightarrow$ `/delivery accept <delivery_id> --enable`
       - `🚫 拒绝本次投递` $\rightarrow$ `/delivery reject <delivery_id>`
       - `🛡️ 拒绝并屏蔽该发送人` $\rightarrow$ `/delivery block <sender_owner_id>`

2. **统一斜杠命令 `/delivery`（支持飞书私聊与本地 TUI）**：
   - `/delivery inbox`：查看待处理的投递收件箱；
   - `/delivery show <delivery_id>`：查看某条投递内包含的所有工作流、监控、固定脚本、Skill 明细；
   - `/delivery accept <delivery_id> [--enable]`：确认接手并落盘到自己的对应目录（不带 `--enable` 时默认关闭，等待手动开启）；
   - `/delivery reject <delivery_id>`：拒绝该条投递；
   - `/delivery enable <kind> <id>` / `/delivery disable <kind> <id>`：开启或停用已接收的某项工作流/监控/脚本/Skill；
   - `/delivery block <owner_id>`：屏蔽某用户的投递；
   - `/delivery unblock <owner_id>`：解除对某用户的投递屏蔽；
   - `/delivery blocklist`：查看当前已屏蔽的用户列表。

3. **Agent 自然语言工具配合（遵循结构化控制铁律）**：
   - Agent 可通过 `AssetDeliveryTool` 帮用户**查询**收件箱、**预览**投递包裹内容、或在用户明确确认后调用宿主受控入口执行开启；但**跨用户接收落盘（`accept`）与屏蔽名单变更（`block/unblock`）必须绑定当前真实认证用户会话/卡片回调**，子代理或未认证上下文不得越权替用户静默接收跨用户资产。

---

## 6. Web 页面交互设计（工作流 / 固定脚本 / Skill / 定时监控四页合一）

1. **四个资产页面统一增加「📤 投递给其他用户」按钮**：
   - 在【🔀 工作流】【📜 固定脚本】【🧩 技能 (Skill)】【📡 定时监控】页面选中任意一条资产后，点击 **「📤 投递给其他用户」**；
   - 弹出投递窗口：
     - **下拉选择目标用户**（如 `Alice 研发工程师 (local/alice)`、`Bob 产品设计师 (local/bob)`、`飞书用户`）；
     - **自动展示依赖闭包打包清单**（调用 `assets.delivery.preview_bundle`，清晰列出：*“本次投递《xxx》将自动連带打包：1 个工作流 + 3 个固定脚本 + 3 个 Skill，对方接收后将自动分别放入对应目录，且可单独使用其中的每个脚本和 Skill”*）；
     - 填写投递附言，点击 **「🚀 立即投递」**。

2. **每个资产卡片/详情增加「启用状态开关（▶️ 已开启 / ⏸️ 已接收待手动开启）」**：
   - 刚从别人那里接收过来的工作流、监控、脚本、Skill 醒目显示 **「📥 来自 xxx 投递 · ⏸️ 待手动开启」** 徽章；
   - 用户核对无误后，点击 **「▶️ 手动开启」**（可勾选“同时开启其关联的 3 个脚本和 3 个 Skill”），即可立刻投入使用。

3. **顶部导航栏常驻「📥 投递收件箱 & 🛡️ 屏蔽名单」抽屉/弹窗**：
   - 实时显示未读投递数量角标（如 `📥 收件箱 (2)`）；
   - 点开后分为三个页签：
     - **📥 待确认收件箱**：展示谁投递了什么、包含哪几项连带资产，提供 **「✅ 接受投递」「🚀 接受并开启」「🚫 拒绝」「🛡️ 屏蔽此人」** 按钮；
     - **📤 我发出的投递**：展示对方是“待确认”、“已接受”还是“已拒绝”；
     - **🛡️ 投递屏蔽名单**：列出当前已拉黑屏蔽的所有用户（含屏蔽时间与原因），每行提供 **「🔓 解除屏蔽」** 按钮，底部也支持从下拉框直接选择任意用户加入屏蔽名单。

---

## 7. 底座代码落地模块划分建议

| 新增/修改模块路径 | 职责说明 |
| :--- | :--- |
| `agent_py_agent/agent/assets/asset_delivery.py` | **新增**：跨用户依赖闭包求取（复用 `topology.py`）、敏感信息扫描、收发件箱读写、两阶段原子落盘（初始 `enabled=False`）、资产启用/停用切换、屏蔽名单 `blocklist.json` 管理 |
| `agent_py_agent/agent/assets/descriptor.py` | **扩展**：在 `AssetDescriptor` 与四类资产存储中规范化 `enabled: bool = True` 与 `delivery_provenance: dict` 元数据字段 |
| `agent_py_agent/agent/plugin_host/action_specs_v2_ext.py` | **扩展**：注册 `assets.delivery.*` 共 9 个 Host v2 动作及其闭合 Payload Schema |
| `agent_py_agent/agent/plugin_host/capabilities_v2_handlers.py` | **扩展**：实现 9 个 `assets.delivery.*` 动作的处理器，严格绑定 `authorized.effective_owner` 与幂等回执账本 |
| `agent_py_agent/agent/conversation/control_commands.py` | **扩展**：解析 `/delivery inbox\|show\|accept\|reject\|enable\|disable\|block\|unblock\|blocklist` 命令 |
| `agent_py_agent/tests/test_cross_owner_asset_delivery.py` | **新增**：覆盖闭包连带打包（1 Workflow + 3 Scripts + 3 Skills）、接收后各归各位且可单独调用、接收后默认 `enabled=False` 需手动开启、飞书通知触发、屏蔽名单拦截与解除屏蔽恢复投递、秘密扫描拦截与配额回滚的完整回归测试 |
