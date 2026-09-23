# P5-D 主会话自动选模隔离真实验收

状态：2026-09-23 三条普通中文新会话样本已完成；安全回退真实通过，跨模型自动采用尚未出现，P5-D 不关闭。本轮只新增验收文档，未改生产源码、未提交/推送/部署。

## 范围与来源

使用 `codex/decision-model-integration` 独立 worktree、私有测试 home 和唯一 8431 Gateway。TUI owner 为隔离普通 user，初始聊天模型是**官方 MiniMax-M2.7**，其真实请求到 `https://api.minimaxi.com/anthropic/v1/messages`；候选目录同时含官方 MiniMax-M3 和 OpenCode DeepSeek-V4-Flash。本轮测试者只给每个新会话一次普通中文架构分析任务，没有在 prompt 中点名模型、代选候选、修改测试对象的线程状态或代完成任务。

通过原 decision settings 的 owner CAS 临时把 `points.model_selection.mode` 从继承 `off` 改为 `apply`，revision 11→12。首轮保留默认 2 秒单次/4 秒阶段预算；第二轮起另用原 CAS 临时改为 8 秒单次/10 秒阶段预算，revision 12→13。没有改日常 Gateway 或模型配置。原 HTTP 旁观只记录 endpoint、model、载荷字节数与哈希、消息/工具数量、HTTP 状态和耗时，不记录认证头或消息正文。原 Gateway 请求记录、canonical thread 与原模型用量账仍为选择和执行事实源；脱敏结构与冻结源码 SHA 位于仓库外 `p5d-main/run.Tmd5Zx/rounds-summary.json`。

## 三轮结果

| 轮次 | Jev 选择事实 | 真实执行与边界 |
| --- | --- | --- |
| 默认短期限 | 官方 Jev 一次真实 HTTP 约 2.01 秒被期限取消，原 marker 为 `deadline/provider_failed` | 自动保留官方 M2.7，主请求完成；超时输入用量未知，不能记零 |
| 可调长期限，相同任务 | marker 为 `observed/need_data`，没有合法候选采用 | 自动保留官方 M2.7，主请求完成；这是缺资料分支，不是异模失败证明 |
| 可调长期限，另一普通技术任务 | marker 合法选择当前官方 M2.7 profile | 选择与冻结模型相同，保持原 profile/revision，主请求完成；没有候选探针或异模业务请求 |

三轮原 Gateway `request status=done`、`turn_end_reason=completed`；canonical thread 的 profile 都是官方 M2.7、`source=default/revision=1`，没有伪称 `automatic`。旁观共捕获 **5 次 Jev HTTP 尝试**（4 次 HTTP 200、1 次期限取消）及 **4 次官方 M2.7 HTTP 200**；未捕获 M3/DeepSeek 请求。后两轮原会话决策输入分别为 18,487 和 18,503，首轮含缺报，不能合计为完整输入量。两条长期限请求另有 `skill_tool` 决策，不能把 5 次 Jev 全算成模型选择。

本轮真实证明 `apply` 下短期限/`need_data`/建议当前模型时主代理可继续且不重发异模。它**没有**证明 Stage C 的真实跨 provider 自动采用、完整历史/工具轮保真或接近 250K/1M 的容量表现；这些仍由原 fake HTTP 合同与下一次合适真实样本分别验。`need_data` 是用户要求保留的合法选项，不用提示词强迫它选模型。语义选择是否过于保守需另做自然任务对照，不能以三次样本统计质量。

## 清理与验证

测试者退出三个 TUI 后，通过原设置 `restore` 一次 CAS 撤销三个临时字段，owner revision 13→14；非秘密 owner overrides 的规范哈希回到测试前 `93d529b3b6a9036532f27bea82d1894a4a1fd8e1f54c0d91fa15a56c8caca268`，有效模型选择模式回到 `off`。原 CLI 停止隔离 Gateway PID 55925，8431 无监听；日常 8420 保持 PID 2543。私有源 SHA、请求回执与出站旁观均留在上述私有证据目录。

本片没有因真实回退改生产代码，因此没有把既有 fake 合同测试重复算作真实采用通过。主线已跑 doc sync、Ruff、E1 语法及 diff；汇总严格 gate 须在 P4-B 修复稳定后再跑。

建议下一步：先修普通 owner 的可信会话来源与设置回执截断问题，再用同一 8431 串行复测；P5-D 可在不人为指定候选的任务中继续观察异模建议，并只在原完整请求、目录、线程 CAS 和实际发送事实都成立时计为自动采用。其它 agent 可并行只读核对其容量/模态边界，不得同时改 Gateway 原发送接缝。
