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

## 第四个样本：明显超出当前窗口的长资料任务（2026-09-24，main `3d2424786`）

**环境**：
- 授权测试机上的新隔离目录，由 main `3d2424786` 构建 wheel（SHA256 `425bc2385005c655cb9d184df0c72be7d58a20a2d8c9c86750c65fbeaf7db1ea`，5,589,594 字节；git archive SHA256 `b0727492519a3278544c1d40e9c68f0d4fffcd5f99edec178369765d5d055401`）。
- 唯一 Gateway 监听 `127.0.0.1:8431`，原生 TUI。
- 模型目录原样复制，不改任何字段：默认官方 MiniMax-M2.7（声明窗口 200,000）；候选另有官方 MiniMax-M3 与 OpenCode deepseek-v4-flash（都声明 1,000,000）。
- 决策设置经原 CAS 只开 `points.model_selection.mode=apply`，关掉 skill_tool 与 subagent_model，减少干扰；期限 8 秒单次、10 秒阶段。压缩点 50%。
- 材料：8 份中文盘点资料，合计 325,451 字。

**需求**（新会话，只给一次普通中文 prompt，不点名模型）：完整阅读 8 份资料（合计约 32 万字），逐份核对每个仓位的差异件数与原因，最后给出交叉对比结论。

**结果**：
- Jev 真实调用 1 次（HTTP 200，1.04 秒，决策输入 1,120 tokens）。结构化观察 `model_selection_observation`：`status=observed`、`choice` 等于当前 M2.7 的 profile、`adopted=false`、`adoption_eligibility=not_evaluated`（选的就是当前模型，不需要采用）。
- 请求 `gwreq-1790261260-7e8ced84d9894cadaf77826e6b897f5d` 为 done/completed，8 个工具轮，202 秒；全程在 M2.7 上执行，线程压缩到第 2 代，模型来源仍是 `default`。
- 用量：主模型 M2.7 9 次、输入 293,291；摘要 2 次、输入 179,680；决策 1 次、输入 1,120。出站 16 次（Jev 1 次、MiniMax 15 次）全部 HTTP 200。
- 回答质量不因 done 自动通过：最终回答把"每份约 670 条记录"说成了"每份差异总件数都是 670"，与材料不符。这是模型的计算/归纳错误，不加针对样例的分支。

**结论**：
- 即使任务明显超出 M2.7 的声明窗口，Jev 仍建议保留当前模型；容量由宿主靠压缩解决，符合"容量由代码判断、Jev 只比较语义"的设计。
- 至此主会话共 4 个真实样本：超时、need_data、两次选当前模型。真实跨模型自动采用仍未出现，P5-D 不关闭。
- 候选材料只有模型名、后端和声明窗口，指令又要求"候选声明不是实际能力证明"，Jev 缺少语义依据。要让自然的跨模型采用有机会出现，需要给候选补上由用户授权的结构化用途说明。这是设计方向，未实施，已记入 [DESIGN_LEDGER](../../DESIGN_LEDGER.md)。

**清理**：TUI `/exit`，Gateway 经原 CLI 停止，PID 不存活，8431 无监听，pending/processing 为 0；原 CAS 恢复隔离 owner 设置（owner revision 25，enabled=false）。清理记录 `artifacts/cleanup-current.json`（SHA256 `57c36820d4424616d6146abfdcf55d90ef7b96568b32410513e43223f6704a40`），本机副本在 `~/.my-agent/decision-evidence/p5d-3d2424786-20260924/`（仓库外）。本轮 Jev 1 次，累计 66 次。

## 第五、六个样本：候选带用途标签（2026-09-24，main `4ec0e11f3` 与分支 `claude/decision-selection-question` `10050d927`）

**共同环境**：
- 测试机新隔离目录，唯一 Gateway 8431，原生 TUI，决策只开 `points.model_selection.mode=apply`（期限 8 秒单次、10 秒阶段），压缩点 50%。
- 需求、8 份材料（合计 325,451 字）与第四个样本逐字相同。
- 唯一不同：经原 `save_model` 操作（与 `/model` 表单同一路径）给三个对话模型写了用途标签：M2.7 为 `general`，M3 与 DeepSeek（都声明 1M 窗口）为 `long_document, large_context`。

**第五个样本（main `4ec0e11f3`，wheel SHA256 `9c878e90aa781ee60b6d6a1074e41add7a32f0f9fde475175e9dabac53e42ae4`）**：
- Jev 1 次（HTTP 200，0.73 秒，1 题），仍选当前 M2.7：`choice` 等于冻结的 M2.7，`adopted=false`，`not_evaluated`。
- 主代理这次把 8 份资料分给 8 个子代理（子代理选模关闭，都用继承的 M2.7），请求 51 秒 done，后台接续给出交叉结论。
- 结论与事实不符：各份差异件数合计多数算错，"差异件数最多"与"最多的原因"都答错（事实：每份 670 段、各有 61 个零件仓位，合计 3,345–3,354 件，ledger-08 最多；四类原因各 1,340 次，完全打平）。
- 原因：采用模式会整段替换问题说明，替换后的说明丢了用途标签的解释，只强调"候选声明不是能力证明"，也没说宿主会核对容量，Jev 没理由离开当前模型。

**修复**（分支 `claude/decision-selection-question`，`10050d927`）：采用模式的问题说明改为照实描述宿主行为——完整首请求准备后独立核对容量、工具与版本，通过才采用、未通过就保持原模型——所以请按任务的语义需要挑最合适的候选；两种模式共用同一句用途标签说明；当前模型同样合适时选 retain_original。宿主的采用门不变。

**第六个样本（`10050d927`，wheel SHA256 `998188b7d465e5cfb97204b801de16a152a6a4bc5ca5366cbdb7daaa88168d43`）**：
- Jev 1 次（HTTP 200，0.8 秒，1 题）选 MiniMax-M3。宿主按完整请求估算核对（`validated_estimate`，保守输入上界 127,428，M3 声明窗口 1,000,000）并探测 M3 工具支持（HTTP 200）后，经线程 CAS 自动采用：观察记录 `adopted=true`，状态为设计上的 `send_intent_uncertain`（只记录发送意图，不冒充 HTTP 已接收）；线程模型改为 M3，来源 `automatic`，选择版本 1→2。
- 之后 8 次业务请求全部发往 M3（29 个工具，HTTP 200），请求 done，没有压缩（Context 约 32k/1.0m）。
- 回答与事实完全一致：8 份合计逐份正确，ledger-08 最多（3,354），合计 26,797；四类原因各 1,340 次、并列，模型也如实说明"无单一最高项"。
- 出站 12 次全部 HTTP 200：Jev 1、M3 9（探针 1、业务 8）、M2.7 2（探针与一次后台请求）。
- 清理：TUI `/exit`，Gateway 经原 CLI 停止，8431 无监听，pending/processing 为 0；原 CAS 恢复隔离 owner 设置（enabled=false）。证据：测试机隔离目录 `artifacts/cleanup-current.json`（第五样本 SHA256 `3a8683d9d67b4de1834f178ba4c47ea5188dcdfa89fd118d3801b992dbafe146`，第六样本 `fb7906a1d43a3d331e0e5fb6afd08cda3650fe6612fdfa6b52cfc02885f20088`）；本机副本在 `~/.my-agent/decision-evidence/tags-4ec0e11f3-20260924/` 与 `tags2-10050d927-20260924/`（仓库外）。

**结论**：
- 主会话第一次真实跨模型自动采用成功：建议来自 Jev，容量与工具由宿主客观核对后才采用，采用后的模型完成任务且答案正确；同一任务在 M2.7 上两次都答错。
- 问题说明是决定性因素：同样的候选和标签，采用模式的说明照实描述宿主核对流程后，Jev 才会建议更合适的候选。
- 本两轮 Jev 共 2 次，累计 77 次。
