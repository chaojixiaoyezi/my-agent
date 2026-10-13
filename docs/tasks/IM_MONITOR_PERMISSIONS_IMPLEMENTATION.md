# IM、监控与后台权限实施记录

2026-10-13 用户追加 R14/R15：监控回执可静默、通知与执行分离；长工具的同条短窗口持续状态。本期现为 15 项，全部完成和严格验收后再合 main 推送，原 13 项编号不改。

状态：实施中。用户于 2026-10-12 明确授权全部 R01～R13 开发，全部验收后合入 main 并推送。2026-10-13 继续实施；首批验证不表示全部完成。

基线：远端 main `08fd572f6`；需求文档检查点 `32c97abd7`；独立分支 `codex/im-entry-update-assessment`。原工作区其它线的未提交内容保持独立。主需求唯一入口：[13 项总表](../design/TUI_IM_PREVIEW.md#requirements-overview-20261012)。

## 逐项进度

| 范围 | 当前进度 | 剩余验收/接线 |
| --- | --- | --- |
| R01 | 前置可信 bot 提及规范化，text/post 走原敏感命令与控制解析；其它提及参数保留 | 真 IM 入站、首次密码和群聊组合 |
| R02 | 原目录增加角色/入口声明；IM/TUI 共用帮助与单项用法，锁定只给基础帮助 | 原控制、补全和各动作的联合权限回归；真 IM |
| R03 | 默认 100；原草稿、消息 ID、节流/dispatch 保留；有效配置通过 Gateway 事件传到投递端 | 完整配置/HTTP链、后台进度回归及真 IM |
| R04 | 尚未实现通用任务凭据收集 | 复用私聊卡片、锁、加密存储与任务唤醒；身份/用途约束；实际卡片验收 |
| R05/R08～R12 | 尚未实现复合监控及用户级查询/控制 | 扩展原 job/rule/run、NO-LLM 路径、依赖条件、恢复与跨入口 |
| R06 | 文件门与进程沙箱共用最具体路径顺序；绝对锁/安装/宿主保护分开；保留正式业务写根；完整 HomePaths 回归 | Linux 同文件/PTY/后台对照；三类身份自动委派、孙代理和汇总链 |
| R07 | 缓存隔离 claim/attempt；原 pending 添加私人卡运输回执，主/子/孙无 TUI 可决定；首设/锁定、防转发/错人/旧执行与真实 HTTP 接线已回归 | 真后台工具继续执行、父级能力裁决组合、前台普通用户/群审批与真 IM；不能把首批卡片测试当全部闭环 |
| R13 | 尚未接离线实例发现及原 update | 原实例定位、排空/停止/交接/服务恢复、数据保留和误装防护 |
| R14 | create/update 通知策略、run 冻结策略与内部提交已接；新 IM 默认 local、旧记录保持 origin；后台补 schedule 候选 | 原地恢复/规则通知组合及真实 IM；空供应商响应仍失败，合法内部总结不外发、不靠空回复躲通知 |
| R15 | 唯一执行器实际 handler 边界接入单调时间心跳，经原 chunk/草稿编辑同条短窗口；用户追加默认 120 秒，off 也有极简状态/正文更新时间，0 关闭 | 原 Registry 到 HTTP/投递端联合回归、取消/异常和真实 Linux 长命令验收；当前只报告等待工具结果，不推断业务进展 |

## 首批事实与边界

- 参考：既有评估已查 Codex permissions 索引和源码；本轮重读 `codex-main/codex-rs/protocol/src/permissions.rs::resolved_entry_precedence` / `resolve_access_with_cwd_uses_most_specific_entry`。仅采用通用路径优先级，不移植任务规则。
- Seatbelt 原来的宽祖先 deny 最后覆盖窄工作根，与文件门不同。现按同一普通路径排序编译；绝对保护仍最后覆盖，owner 隔离保留。
- 主代理业务 tasks 不再被整个任务树误挡；孩子保持原默认保护，正式窄业务授权与宿主绑定的自身 run work 被保留。精确宿主状态沿原 PathAccessPolicy。
- `locked_files` 进入原进程绝对保护；PTY 会话权限键包含普通禁止范围，后台命令使用同一沙箱策略。
- R07 从原 pending 发私人卡，签名只冻结 canonical compare 身份、发起 actor 和 expiry，不把显示名当授权键；真实 message/chat ID 参与回调核对。收到卡的等待能力只覆盖这条请求，不给全 root 续租。私人决定入口强制实际宿主凭据，即便旧 G2a 回环兼容开着也不准模型复制引用自行批准；scope 有效还需 claim/attempt 未变。主线程的 UI 选定工作目录不是执行权，已移除此项错误前置条件。
- R03 内部候选仍使用原 8000 字符事件预算，只在投递前应用短窗口；最终结果不裁剪。上限通过 typed event 传递，配置不能只在适配器一端生效。
- Linux 实测发现写根简化会抹掉父根下的窄授权；有普通禁止根时必须保留父/子写根。Full Access 两平台也沿同一最具体规则，绝对保护最后覆盖。PTY 空绝对保护统一为同一个权限键，避免 None/空 tuple 造成自身会话被误报不存在。
- R15 不从输出猜健康状态。真实 handler 运行才启动观察，monotonic 计时，取消停止、finally 收回线程，终态前无迟到心跳；审批、重放、前置拒绝不假报执行中。`im_stream_heartbeat_seconds` 按用户最新决定默认 120、0 关闭；IM off 只给极简状态/更新时间，TUI 不创建观察线程。更新时间来自执行侧事件，复用原 `timezone`；投递端轮询不能制造“仍活着”的新时间。短窗口去掉进度路径说明，最终结果的路径净化/说明保留。
- 群来源转私聊的控制引用共用 `user_space/interaction_refs.py`：HMAC 签名绑定 owner/actor/channel/expiry，密钥放原 H2 受保护空间；引用不是权限决定。接收方还须核对执行代次、请求与实际卡片身份，主子审批账仍是原文件，尚未接入 IM 卡片。

## 验证记录

- 权限首批四文件 110 项通过：child own workspace、owner home、second layer、write boundary。真实本机 Seatbelt 能写窄根、拒绝兄弟根；不是 Linux 证明。
- 原 IM/进度/后台审批/控制/媒体五文件 78 项通过，与其它组合有重复，不累计。
- 新命令、短窗口、后台审批与 PTY 四文件 70 项通过；99/100/101 字符、多次编辑、自定义上限、完整最终回复及换 claim/attempt 已覆盖。事件配置接线变更后须复跑。
- 沙箱组合中一个 PTY 测试 helper 未接受新增参数，已修正；9 个仅 Linux 条件测试在本机跳过，最终 Linux 补验待做。
- 192.168.1.19 已认证连通，Linux 6.8，2 CPU、约 4GB 内存；暂只读取机器状态。登录凭据不写入仓库，实际运行拓扑须再核对。
- 尚无真实模型、真实飞书、最终本地严格 gate、合并或远端推送证据。
- Linux 首次因 Ubuntu 24.04 AppArmor 缺 bwrap 专用配置，真实沙箱不可用；只加载 Ubuntu 发行包的 `bwrap-userns-restrict` 后自检正常。原系统限制值仍为 1，不全局关闭。随后真正暴露并修复父子挂载简化问题，Linux 核心权限/PTY/进度/通知组合已通过，实际 120 秒心跳长等待正在验收。来源见 [Ubuntu 官方说明](https://discourse.ubuntu.com/t/understanding-apparmor-user-namespace-restriction/58007)。
- Linux 实际 245 秒无输出 Shell 长等待已通过，120/240 秒两次进度，原消息只创建一次，后续同 ID 编辑、正文时间和独立 final，final 后无刷新；运输是替身，不能算真 IM/TUI/LLM。更广 24 文件首轮 683 passed/2 failed/15 skipped 后修复前台错误展开 schedule，相关 259 项复验通过；数字有重叠、不相加。
- 私人卡主/子/孙、首设/闲置锁、他人/转发/旧 claim、群来源冻结发起人首批通过；实际原 HTTP 路由 + 适配器回调到原等待记录也通过。只替换 provider 运输，没有 TUI consumer，尚未真飞书发送。

## 后续必须完成

1. 完成 R06/R07 的后台主子权限和 IM 审批接线，真实 ToolExecutor/生产派发验证，包含原父级能力裁决。
2. 完成 R04/R05/R08～R13，原目录、身份、任务、调度和消息账各保持一个事实源。
3. 联合 focused tests、fake tool/LLM/replay 后做隔离 Linux 实际 TUI/IM 验收；只给正常需求并观察原账，不代被测 Agent 写产物。
4. 对最终提交执行 pytest focused、Ruff、doc sync、strict code-size、diff check、clean-package；累计生产/测试变更达到约 10000 行时再追加全仓 pytest。
5. Gate 全过后重新 fetch main，整合其它已授权主线变化，再合并 main 和推送、核对远端。线上 CI 若暂停，报告它未作为验收来源。

建议下一步：继续完成全部范围，优先后台权限与审批链。独立展示/安装可由其它 agent 准备，但当前没有启用并行委派；每个核心状态模块保持单一修改负责人，避免串 owner、重复执行和覆盖他人工作。
