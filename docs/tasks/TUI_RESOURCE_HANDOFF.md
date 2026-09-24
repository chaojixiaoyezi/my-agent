# TUI 资源与状态连接交接

## 基本信息

- workstream：TUI 资源、HTTP 连接寿命、空闲 owner 缓存。
- branch：`codex/tui-scalability`，基线 `c9042f5f2`，独立 worktree。
- owner：Codex TUI 资源线；已向“模块重构”和“接入决策模型”协调文件及测试环境。
- date：2026-09-23。

## 官网真模型和媒体输入补充片

资源片提交为 `a2a9e0e16`，本补充片在同分支上继续；未推送、未切换默认运行环境。
候选经官网 MiniMax-M2.7 和 MiniMax-M3 验收，旧假模型负载记录不再作为本轮真实可用依据。

- 媒体入口：`/attach`、终端文件路径粘贴/拖入、本机 Ctrl+V 截图；草稿可删除、暂存、恢复，运行中媒体排入下一轮。
- `input_media.py` 统一 owner 私有原件、内容寻址 refs、输入验证、发送投影的总字节预算。
  Gateway ask/指纹、native UserTurn 和后端适配只传同一组结构化 refs；base64 在供应商边界临时展开。
- 图片/视频/退出后续问经官网 M3 的真实原生 TUI 通过；私有只读请求观察器确认原件哈希和真实外发字节一致。
  macOS 系统截图 Ctrl+V 也经官网 M3 通过，原剪贴板已恢复；本机隔离测试进程已退出。
- M2.7 官网真实并发：1 CPU / 2 GiB，100 独立身份全部 done，50 槽峰值；204.3 秒完成。
  原生 TUI 并行问答正确，44 帧未见未同步；cgroup 峰值 1047.1 MiB，末次 Gateway/TUI RSS 313.8/62.6 MiB。
  `/status` 全部成功，但高峰 P95 3268 ms、最大 4897 ms，单核批量冷启动延迟仍须治理。
- 一万行无 checkpoint 真实续聊完成，410.81 秒、4 次供应商调用、generation=1；正确回答四加四等于八。
  千万行无 checkpoint 的 `after_compact_report` 在隔离 384 MiB 地址空间约束下立即 MemoryError，**未通过**。
  `append_once` 仍全量读历史去重；这两处不得与千万行只读浏览通过混淆。
- 组件矩阵 1008 passed、1 skipped（既有 HTTP stop 409）；真实验收与组件测试分别记录。
  详细测试口径和证据名见 [TESTS](../../TESTS.md#官网真模型与tui媒体验收)，媒体合同见
  [TUI_INPUT_MEDIA](../design/TUI_INPUT_MEDIA.md)。
- Ruff、doc sync、strict code-size、diff check、clean-package 通过；47 个候选生产文件与专用测试机 SHA256 一致。
- 用户新要求：后续长任务改用官网 M2.7 自主将 fd 从 Rust 复刻为 Python；此前合成大行数只作存储边界定位。
  本线仅提交一次真实 TUI 需求和旁观资源采样，没有代写产物；该项已自然结束，最终结果见下文。
  阶段记录约 57 分钟、168 模型轮、自然 Compact 1，552 次采样未见未同步；原始与 A 端正常退出且进程消失，
  当时只保留 B 端观察同一任务，之后继续采样到真实 TaskRun/AgentRun 结束，没有把 HTTP done 当项目完成。

补充片涉及 `tui_media.py`、`tui_media_clipboard.py`、TUI 草稿/按键/worker、
`conversation/{input_media,history_display}.py`、`backends/{tool_ir,message_adapter,anthropic,openai_chat,responses_wire}.py`、
`loop_support._native_initial_tool_ir_history`、Gateway 入站 refs/指纹与配置。
未修改 `compact.py`、`compact_request_budget.py`、Compact scope/checkpoint/CAS 或第 7 步子代理终态链。
“接入决策模型”负责后续有界来源读取与 Compact 组合；本线不以截断历史或伪造 checkpoint 绕过失败。

主线整合须同时保留媒体字节预算与另一线的工具历史引用：前者只投影 user 媒体，后者管工具结果归档。
停 Gateway 前除 HTTP 队列外还须检查 canonical running attempts/后台任务；processing=0 不等于进程空闲。

## 本线目标与实际完成

解决长对话重复净化造成的卡顿、退出后轮询不收口、半请求长占 HTTP 工位，
以及历史用户实例长期保留。历史规模不应等比例增加 TUI 常驻内容。

- 稳定内容缓存保存已净化不可变行；装饰及新内容继续过滤终端控制字符。
- 正常、EOF、键盘中断及运行异常统一设置 TUI 停止事件，退出不取消服务端持久任务。
- HTTP 固定 worker 和原上限不变，增加传输空闲与排队期限；队列只读计数使用 scandir。
- 原历史字节游标支持超大工作组硬预算续页；近期事件、身份及冻结阅读窗口有界。
- 同 owner 首次构建共享 Future；请求持有实例租用，维护不续期。
- 空闲 60 秒且无在途及持久硬事实才退池；新消息重新建立，历史/记忆/插件不删。
- 关闭策展不构造软实例，已完成策展立即释放，独立于下一轮派发时间。

## 文件归属

主要变更在 `cli/chat_parts/tui_*`、`agent/conversation/history_page.py`、
`gateway_parts/{bounded_http_server,io,request_worker,owner_retention}.py`、
`owner_scoped_pool.py`、`cli/gateway_loops.py` 与相应配置/测试/文档。
不修改 `subagents/runner_result_*`、`direct_parent_lifecycle.py` 或第 7 步任务终态合同。
入口文档头部是本线增量，主线整合时保留另一线更新，不整份覆盖。

## 验收

- 最终定向矩阵：818 passed，1 skipped（旧 HTTP stop fixture 返回 409，原测试自行 skip；
  真实 Gateway HTTP 提交与结果链已通过 100 身份矩阵）。
- Ruff、doc sync、strict code-size、diff check、clean-package 全部通过；未推送，线上 CI 未作为验收来源。
- 原生 TUI 千万条消息/千万行原文、真实键盘/鼠标、resize、退出；读写证据与 CPU/RSS/cgroup
  口径详见 [TESTS](../../TESTS.md#tui-资源与空闲用户验收)。
- 100 local owner / 50 执行槽使用真实 Gateway 与假模型，两轮有效矩阵均全部完成；
  一轮额外原生 TUI 排队收到答复，30 帧未观察到未同步。
- 官方 MiniMax-M2.7 真实 `sleep 15` 及对话恢复通过；另在 `sleep 60` 执行中退出 TUI，
  Gateway 保持 processing=1，新 TUI 恢复同一会话并取得完成回复。假模型负载不代表真实平台/重工具容量。
- 20 个变更生产文件与专用测试机 SHA256 一致；后续修改需重新核对受影响场景。

## 发布与风险边界

后续稳定缓存补充（基于 `a2a757c80`）只改 `TuiStateStore` 快照、provider 块版本键、
`TuiBlockRenderCache` 静态前缀和 `SafeFormattedLines`；已与集成 owner 确认函数归本线，未改 Gateway 或模型路径。
289 项回归、等历史官网 M2.7 原生 TUI 对照及导航通过；基线/候选 CPU 4.68%/2.21%，内存未宣称大降。
首次详细展开约 1.2 秒单列，不把后续约 30 ms 的滚动延迟套到首次展开。

fd 原任务已自然结束，约 109.5 分钟、Compact 3；原工具 24/24 自测通过，不声称全部上游行为等价。
任务期 1180 次采样未见未同步，模型产物与原失败保留；另一次同会话真实 `sleep 30`/四十二续聊通过。
本次所有已识别测试 TUI 已正常退出且 PID 消失，Gateway 全程同一个，running attempts=0；
未知 `product-r5-ts-fzf` 未动。清理后 Gateway CPU 2.43%、RSS 297.08 MiB，候选未切默认环境。
私有 `fd-port/` 的原始任务、工具输出、项目归档、等历史对照、退出与空闲样本是验收来源。

独立提交链为 `c9042f5f2 → a2a9e0e16 → 3adb61904 → 9083661dc → 1b7b93bfc`；
分别为既有基线、资源寿命、媒体输入、迟到结果和可见帧性能。主线按函数整合，保留 Compact owner 的并行修改。

独立绘制片仅改 `tui_ui_setup.py` 的帧合并为 20 Hz、`tui_threading.py` 的周期动画为 4 Hz；
原事件/模型/工具流完整保留。真实 fd 会话两个只读原生 TUI 同时对照 CPU 16.02%→10.23%，
候选翻页中位/最大 38/68 ms；实际方向键、滚轮和当前处 Ctrl+O/Ctrl+E 通过，157 项相关测试通过。
相同 Gateway 和原业务任务始终继续，不为对照重复下发开发任务。原始帧及采样在私有 `fd-perf/`。

迟到终态补充片：`gateway_client.poll_gateway_chunks` 截止时先读终态，TUI 显式回调续等并用原游标退避；
`tui_worker_paths._worker_gateway_path` 绑定页面停止事件，不重复入队。plain 有限等待不变。
109 项定向回归通过；官网 M2.7 真 TUI 0.2 秒窗口复现基线原页缺回复，候选原页接收成功；
另暂停测试客户端 9.28 秒，服务端期间完成，恢复后接收同一请求的唯一答案。未重启 Gateway 或中断 fd 任务。

候选尚未推送、未替换用户本机或既定默认 Gateway；专用测试机只有一个候选 Gateway。
该机旧根分区已满，候选和证据放在另一有空间分区，旧代码、配置和测试痕迹保留可回退。
旧 8 个已识别测试 TUI 保存证据后通过 `/exit` 退出；未知后台工具进程和其它工作不清理。

1C2G 的 50 执行槽是可配置上限；项目默认仍是 500，不能将验收 profile 等同默认配置。
CPU 满载会有冷启动及排队延迟；没有验证 100 小时、100 个真实 IM 平台账号或 50 个重工具。
单条巨大 JSON 仍需完整解析；分页限制不等于可忽略磁盘和网络故障。
本机此前 100+ 旧测试客户端没有在本线批量杀掉，应由测试拥有者先保存静态证据、辨明活动任务后退出。

## 建议下一步

主线 owner 按本独立提交整合并跑组合 focused gate，再选择无冲突的运行窗口切换默认 Gateway。
本线可以并行只读复核和专用测试机复验；其它线继续子代理/决策模型，但不要并行改本片生命周期入口。
默认机器切换时同时落地显式 50 槽低资源配置，并逐个退出已确认遗留测试 TUI，避免把旧进程噪声算成新版本表现。
