# TUI / IM 临时更新合同（2026-10-12）

2026-10-12 发布收口：TuiApplication 不再包装父类可变参数构造，首次单调帧基线由标量默认值提供，渲染后只写当前实例；Application 原参数和生命周期不变。飞书群直接 mention 开关同时由原 YAML/registry 生成前端目录，配置权威仍在后端。

状态：独立临时分支已实现；交付版本 0.3.0-tui-preview.20261012.5。未合并、未替换生产安装。

## 本机管理员发送到飞书

复用 `send_message`，新增可选 `notify_channel`，例如 `feishu`。不新增品牌专属工具，不把 send_session_message 变成 IM 唤醒工具。
目标来自管理员身份绑定文件；只有完整 local/main/main owner、开启 IM 管理员绑定、渠道在 `capability_config.yaml` 的 `admin_message_channels` 中且身份唯一才允许。
普通 IM 用户仍只能发给自己的渠道；不接受任意用户、群 ID 或 URL 参数。绑定损坏、缺失、多条歧义都返回结构化错误，撤销绑定后下次调用即不可发送。
默认允许已绑定的飞书身份，配置 `admin_message_channels: []` 关闭；多渠道可以显式列入，不硬编码产品渠道作为权限来源。
真实投递、附件范围、发送去重和未知副作用恢复沿原 DeliveryService 与 Tool Gateway 操作账本。附件仍必须为 owner 内已登记产物。
目标解析不会唤醒飞书会话或调用模型改写内容；私聊闲置锁属于入站会话保护，不作为管理员外发授权时钟。

## 单条流式进度

`/verbose stream` 为新增明确档位；`off/on/full` 不改变。设置只属于当前 canonical thread，后续回合读取该值。
过程窗口固定为文本消息，不随内容 Markdown 形态改变类型；最终答复沿原渲染规则。
首条进度创建后保存 provider message_id，后续编辑同一条，最终答复单独使用原终态投递键发送。
快照来自模型流和已有结构化工具事件，不由运行时编造普通过程文案。模型候选最多一秒一次公开快照，渠道进度更新也限频一秒。
内容限 8000 字符；候选快照替换上一候选，工具步骤追加到有界草稿。超长内容保留最新窗口，最终答复沿原完整分片规则。
PendingGatewayReply schema 8 在原路径保存 progress_draft 的 mode/content/message_id/disabled/error_code/sent_at。旧 schema 7 缺草稿时继续原逐条逻辑。
外发意图仍先耐久保存，ID 保存和游标推进使用原 owner/epoch 与完整快照 CAS。发送后本地写盘失败仅重试本地提交，不重复外发。
进程死亡后的首发重放仍核验原 provider 幂等窗口；草稿内容取已冻结快照，不能让新批次改变旧外发正文或稳定键。
无法验证或编辑失败时停用进度并保留错误码，不新发替代进度、不吞最终答复。终态未知仍沿原终态合同，不伪装发送成功。

## 状态与群聊

`/status` 展示上下文当前数字/容量/压缩触发线、压缩次数、子代理运行计数、真实 owner 角色和文件/Shell 权限。
上下文数字复用 TUI 的本代 model_context_usage；估算明确标“约”，缺失或旧代快照为未知，不读正文重算、不开模型请求。
群聊触发门位于解锁与 Gateway 派发前；私聊不变，群聊默认只有结构化 mention 精确命中机器人 open_id 才派发。
@全员的广播键永不作为直接 mention，即便事件异常地同时携带 bot ID；@其他人和只写机器人昵称都不触发。
WS 归一保留 mentions，与 webhook 共用判据。post 的结构化 at 节点可作为缺失 mentions 时的来源。
bot open_id 从本应用的 `/bot/v3/info` 取得，按 app_id 缓存，失败节流 30 秒，未知身份不派发群聊。
配置 `feishu_group_require_bot_mention` 默认 true，YAML、AgentConfig 与适配器启动同时传递，不用正文判断授权。

## 重绘和退出

prompt_toolkit 3.0 用墙钟计算 redraw_interval 与 postpone；时钟倒退可能令 `_invalidated=True` 持续几十秒。
TuiApplication 保留原输入、renderer、终端生命周期，只以单调时钟合并重绘并在 UI loop 用 call_later 调度；无全局 monkeypatch、无额外依赖。
受控测试对比原 Application 与 TuiApplication：两者都会收到输入，只有旧实现因墙钟倒退延迟可见重绘。
这确认可修缺陷条件，不证明现场已经发生校时。用户现场几十秒冻结尚未取得 Python 栈，慢输出导致的同步 flush 阻塞也与该波形不同。
退出 finally 先关闭 1000/1002/1003/1005/1006/1015 鼠标模式并 flush；POSIX TTY 在 raw 上下文有界清掉在途输入，再恢复原 termios。
清理最多 250ms，只丢弃退出交接窗口的输入，不消费管道，退出后新的 shell 输入正常；不保存输入内容。

## 参考和验收

参考 OpenClaw 对应合同索引与 draft-stream-loop、bot-identity-cache、bot-content；统一发送复用本仓库 CHANNEL_DELIVERY_DESIGN。
合同单测先覆盖绑定撤销/歧义/普通用户拒绝、@all/直接 @/WS、进度创建/编辑/重启/失败、墙钟倒退与真实 PTY 清理。
最终验收使用用户授权的 Ubuntu 24.04 测试机真实 TUI、官方 MiniMax-M2.7 与 SSH；包复验 Ubuntu 20/22/24 离线安装。
不读真实任务正文、不改生产模型配置、不推送或合并。测试替身不算真实飞书送达；实际 bot 权限与客户端展示另列验收边界。

## 验收结算

- 已确认源码缺陷条件：旧包真实 TUI 模拟墙钟倒退八秒，输入 Buffer 最慢 2.8ms 更新，但重绘延迟 7,975ms；同条件修复版重绘最慢 75.8ms。测试未修改系统时钟。
- 正常真实模型四回合（含长回复）240/240 字符均匹配可见重绘，最慢 77.8ms；OpenSSH 恢复会话两回合 120/120，时钟校准区间估算上界约 92ms。未见运行期 reader 脱离或 loop 异常。
- 真实安装目录对象确认管理员角色；真实 MiniMax 普通需求自行选择发送工具，通道替身接收一次且原句一致。撤销绑定后的原生工具不可发送。这里没有真实 Feishu 收件人或客户端验收。
- 本地定向回归、Ruff、doc sync、strict code-size、diff 与 clean-package 为验收来源；不推远端，未用线上 CI。最终发行的三版 Ubuntu 断网安装及数据保留另见发行验收文件。
- 现场那次几十秒冻结仍未取得 Python 栈，无法证明现场实际校时；已修复可复现条件，等待用户在原环境验证。慢输出的同步 flush 条件不计为本轮已解决。


## 个人私聊密码首设门追加

个人锁启用时，未设密码、闲置锁定或状态不可用均不得分发普通输入；原队列保存消息身份，首设/解锁成功才 FIFO 续送，错误密码或异 actor 不继续。设置事实由原密码服务结构化 ok 返回，不读取卡片颜色授权。告警主动出站不检查个人锁、不更新活跃时间；群聊与显式关闭不受个人锁影响。原“首次不阻塞”描述为历史行为，本追加为当前合同。
