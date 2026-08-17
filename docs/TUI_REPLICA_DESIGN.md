# TUI 复刻设计方案 v2（群复核 2525/2528 采纳版）

> 状态：v2 待群复核（2026-08-17）
> v1 → v2 变更：按知衡 2525 + 维护记录 2528 复核意见重写桥接/分期/边界——
> 接受架构方向，但「后端零改动/全量流式/历史恢复」从已成立 acceptance 改为
> **需先冻结 P0 HTTP client contract** 后再逐期验证。

## 1. 目标与边界

**目标**：把 CLI 交互升级为 长期助手 ui-tui 形态——TypeScript + Ink（React 终端
渲染）前端，版式/交互复刻 长期助手/终端交互，**命令体系与内部机制全部用我们
自己的**。

**边界（群复核修正）**：
- 定位：**仅回环本机（localhost）薄客户端**（P0 起点）；带认证远程客户端
  后续单独评估，不混入 P0
- 前端只做**渲染与输入适配**；权限/审批/澄清/memory/goal/handoff/skills/
  定时任务/工具执行全部由后端既有机制负责
- `tool_progress` 是**观察事件**，不等于工具成功；工具状态一律以结构化
  operation/ledger/result 为准
- 现有 Python plain chat 保留为唯一回退（无 Node 时），**禁止双 renderer
  同时接管 stdin/终端**

## 2. 技术选型与架构（v2：HTTP progress polling 薄客户端）

```
frontend/tui（TS + Ink，独立进程）          gateway（现有，后端按需小改）
-----------------------------               ------------------------------
src/entry.tsx                                POST /ask        → 202 + request_id
  -> TuiHttpClient                          GET  /progress/<id>?since=N（cursor，≤200 行/页）
       ├─ ask 提交（幂等键）                GET  /result/<id>（终态权威）
       ├─ progress 轮询（cursor+去重）      POST /control（stop/btw/goal，带 conversation scope）
       └─ result 收口状态机                 GET  /history/<owner>（P1 新增只读 owner-scoped）
```

- **前端**：新建**隔离 `frontend/tui` 包**（Ink 不入现有浏览器管理后台
  frontend/）；Node 版本/lockfile/离线安装/dist 交付/入口选择写清
- **桥**：HTTP progress polling 薄客户端状态机——**cursor + 去重 + 轮询重试 +
  result 收口**；**禁止前端直接读服务端 chunks.jsonl**（仅同机内部实现，
  非多用户/远程合同）
- **协议事实**（知衡实核）：
  - `/progress` 只投影 `assistant_commentary` + verbose on/full 的
    `tool_progress`；**不返回 assistant_final** → 最终答案以 `/result` 为权威
  - `/progress` 无稳定 event_id/终态/失败标记 → 文本流语义**明确降级**
    （P1 只承诺 progress 观察流 + result 收口；token 级完整流需新增
    text-delta 合同，另议）
  - **`POST /stop` 是停止 gateway 守护进程的管理命令**——TUI 的
    stop/interrupt/steer **一律走 `/control` + typed command +
    conversation scope**，禁止绑 /stop

## 3. 前端组件清单（复刻核心，不变）

消息列表（虚拟滚动+markdown）/ 工具调用观察流（着色+流式，上限+脱敏）/
多行输入+斜杠补全（**来自后端权威 command catalog，不复制命令表**）/
主题系统 / 会话控制（/control）/ 配置只读显示 / 帮助面板（复用 CHAT_HELP_TEXT）

## 4. 桥协议（v2 修正）

| 能力 | 接口 | 状态 |
|---|---|---|
| 提交请求 | `POST /ask`（202 + request_id） | 已有 |
| 进度轮询 | `GET /progress/<id>?since=N`（cursor/去重/200 行上限/owner 鉴权） | 已有，P0 冻结 |
| 最终结果 | `GET /result/<id>`（终态权威） | 已有 |
| 会话控制 | `POST /control`（stop/btw/goal + conversation scope + typed status/error_code） | 已有，P0 冻结 |
| 历史恢复 | `GET /history/<owner>`（只读 owner-scoped snapshot） | **P1 新增**；P0 明确降级（不承诺历史恢复） |
| resume | `_build_ask_request` 需**贯穿 resume_context 到 HTTP payload** 并证明 | **P1 验证**；未证明前不承诺 |

**客户端状态机要求**：同会话第二请求定义排队/steer/拒绝；gateway 重启后
queued/processing/in-flight 恢复语义；**会话键由 TUI 窗口稳定生成并持久保存
（禁多窗口共享 default）**；ask/progress/result/control 全程携带同一
authenticated user + channel + conversation_id。

## 5. 不复制清单（不变，补充两条硬约束）

同 v1（billing/多代理 UI/学习 UI/技能面板/审批弹窗/移交 UI/cron 面板/宠物
→ 用我们机制）。
补充：①slash 补全来自后端权威 command catalog；②tool_progress 仅观察事件。

## 6. 部署改造

| 项 | 改动 |
|---|---|
| 代码结构 | 新建 `frontend/tui` 隔离包（独立 package.json/lockfile/dist） |
| testbox | Node（或 bun）+ 前端产物；`my-agent` 检测 tty+node → 拉起 TUI；否则唯一回退 plain |
| 信号语义 | SIGINT 区分：退出界面 / 停止当前 turn（/control stop）/ 停止 daemon（/stop 仅管理） |

## 7. 分期计划（v2：P0 起）

| 期 | 内容 | 验收 |
|---|---|---|
| **P0** | 冻结 HTTP client contract fixtures：ask 202 / progress cursor 分页 / result 终态 / 错误中断 / owner+conversation scope / 薄 TS client adapter | contract fixtures 全绿 |
| **P1** | 文本 ask→progress→result、断线重连、去重、无重复提交、Python fallback；历史 API（或明确降级） | 真实对话闭环 |
| **P2** | tool progress 观察流：稳定 event schema（event_id/round/tool/status/phase）、上限/脱敏/虚拟列表、乱序重复缺口处理 | 复刻任务过程全可见 |
| **P3** | slash 补全（权威目录）/主题/帮助；/control 的 stop/btw/goal + busy-turn/CAS/过期 scope 测试 | 交互闭环 |
| **P4** | Node 打包、clean testbox 安装、终端兼容（尺寸/Unicode）、发布 | 全量验收 |

## 8. 风险与权衡（不变）

双栈维护（长期助手 先例）/ 工程量 ~1.2-1.8 万行 TS / Ink 技能门槛 /
testbox 加 Node（fallback 兜底）

## 9. 验收矩阵（v2 新增，11 项，进 P1 前补齐）

1. 普通文本 ask→progress→result
2. 工具多轮（tool_progress 观察流 + result 收口）
3. 202→progress→result 全链路 + cursor 分页
4. 半截/失败 stream（无 assistant_final → 以 result 收口）
5. 断线重连 + 去重（不重复 POST /ask）
6. stop/btw（/control，busy-turn/CAS/过期 scope）
7. 并发窗口 + 跨用户隔离（owner/conversation 鉴权）
8. 认证失败路径
9. 终端尺寸/Unicode/输出上限（截断/脱敏）
10. clean install（testbox 无 Node 环境唯一回退 plain）
11. 长任务（复刻级）全程稳定 + 重启接续

## 10. 群复核记录（2026-08-17）

- 知衡 2525：方向可行；P0 client contract 先行；/progress 不承诺 token 级流；
  /stop 命名陷阱；历史恢复缺 HTTP 端点；多用户鉴权；终端注入过滤
- 维护记录 2528：HTTP 薄客户端状态机；/control 结构化控制；多用户隔离；
  resume 未贯穿 HTTP；frontend/tui 隔离包；slash 权威目录；验收矩阵 11 项
- 本方案 v2 已按上述全部修正；待群复核通过后按 P0 实施
