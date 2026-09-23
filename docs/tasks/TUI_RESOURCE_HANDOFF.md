# TUI 资源与状态连接交接

## 基本信息

- workstream：TUI 资源、HTTP 连接寿命、空闲 owner 缓存。
- branch：`codex/tui-scalability`，基线 `c9042f5f2`，独立 worktree。
- owner：Codex TUI 资源线；已向“模块重构”和“接入决策模型”协调文件及测试环境。
- date：2026-09-23。

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
