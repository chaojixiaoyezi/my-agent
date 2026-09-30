# my-agent 收尾交接（2026-09-29 起草，2026-09-30 定稿）

> dsh-9a 09-29 起草，my-agent-dsh-3a 09-30 定稿。事实以定稿时（09-30 早上 PDT）为准：起草时在途的 step16l、step16m 都已上线，会话互通禁令已解除。
> 文中 `~/.my-agent/...` 是生产 home 下的证据目录，不进仓库。

## 基本信息

- workstream：my-agent 收尾。用户 09-29 19:15 决定停用 my-agent，之后由 Claude 会话收尾。09-30 用户又定：剩下的工作主要由 3a 自己做，其他会话要用户允许才参与。
- branch：main 是 `ac82541e5`，即 step16m，已上线。起草时在途的 step16l、step16m 都已并入 main，没有在途批次。
- worktree：
  - 集成工作区 `~/my-agent-worktrees/claude-capability-merge2`；
  - Linux 通道 `~/my-agent-worktrees/linux-test-main`，detached；
  - 出站协议合同的工作区 `~/my-agent-worktrees/claude-3a-wire-contract`，分支已合入。
- owner：my-agent-dsh-3a 负责集成、部署和定稿。参与会话有 dsh-75、dsh-38、dsh-be、dsh-9a、dsh-9b、dsh-ae，09-30 起都已停手待命。
- date：2026-09-29 起草，2026-09-30 定稿（PDT）。

## 本线目标

09-29 当天发生了两件事：
- 04:35 服务商套餐的每周额度用完，4 个 my-agent 的后台线程全被挡住；
- 19:15 用户决定不再续费。

从这以后，my-agent 不恢复。已经开工的活由 Claude 会话做完、合入并部署；新想法只记进台账。本文件让用户和后来的人看清几件事：生产现在是什么版本，这两天上线了什么，哪些要用户拍板，最后怎么清理。

## 实际完成

### 生产现状：step16m

- **版本**：
  - main 等于 step16m，即 `ac82541e5`；
  - 部署为 `runtime-step16m-bd81569c`（wheel sha256 `bd81569c…`），09-30 05:23:03 PDT 切换；
  - Gateway pid 62817，上一版是 87774。只切了 Mac。这一版飞书适配器的代码有变，适配器随之重启（pid 79040 → 63488）；
  - 定稿时 `/status`：调度器和适配器都在线，派发 0 错误。
- **09-29 晚到 09-30 的上线链**：

  | 版本 | main | runtime | 切换（PDT） | 内容 |
  | --- | --- | --- | --- | --- |
  | step16k | `4c0edd913` | `runtime-step16k-437eec03` | 09-29 20:22 | 额度用完的判定与结算、压缩触发绝对上限、车道环境暂停（毒丸 C5）等 |
  | step16k1 | `622292662` | `runtime-step16k1-8bcba4d7` | 09-30 01:25 | 热修：Chat Completions 不再发出既没正文也没工具调用的 assistant，DeepSeek 对这种消息回 400 |
  | step16kb | `b58ad91e1` | `runtime-step16kb-b60405e1` | 09-30 03:01 | 出站协议合同：三个后端出口修整坏历史并按协议校验 |
  | step16l | `20d642aa8` | `runtime-step16l-89cce858` | 09-30 04:20 | 会话互通收口、毒丸第 3 步 C1、维护状态说清执行与否、出站合同复审跟进 |
  | step16m | `ac82541e5` | `runtime-step16m-bd81569c` | 09-30 05:23 | 毒丸第 3 步 C2–C4 与 C6、第 4 步 `/wakes`、派活取消判定的窄窗口 |

- **回滚边界**：每个版本的发布目录里都有 `rollback-boundary.txt`。step16m 的回滚是恢复 `default-entry.before`，再用 `~/.local/share/my-agent/runtime-step16l-89cce858` 重启单个 Gateway。切换后写入的数据全部保留，不覆盖历史。详见 `~/.my-agent/releases/step16m-bd81569c/rollback-boundary.txt`。
- **验证**：
  - 每一版都跑了远端提交前的严格 gate：ruff、doc sync、code-size strict、diff check、clean package 全部通过。
  - Mac 12 分片全量：step16l 25164 过，step16m 25241 过，都是 0 失败。step16m 第一轮有 1 个 MCP 注册表重连用例失败，单独重跑 3/3 通过，按负载偶发处理；第二轮 0 失败。
  - Linux 容器通道：step16kb 24995 过，step16l 25119 过，step16m 25196 过，都是 0 失败。step16k1 是热修，没有单独跑，它的提交包含在 step16kb 那次里。证据在各发布目录的 `linux-lane/`。
  - 真实模型验收见下文「这两天上线的内容」和「会话互通禁令」。
- **生产配置**：压缩触发绝对上限 `memory_compact_auto_trigger_max_tokens` 现在不在 `~/.my-agent/config/desktop.yaml` 里，压缩回到按窗口 90% 触发。
  - 09-30 00:49，模型通过自助配置入口把这个键 reset 掉了。`~/.my-agent/config/settings-changes.jsonl` 里记录的 actor 是 model，原因写的是用户要求恢复为窗口的 90%。
  - 用户 09-30 决定保持去掉，不再设回。
- **用户能感到的变化**：
  - 撞到每周额度时，前台立刻提示额度用完，不再挂住约 20 分钟（step16k）；
  - 密钥、额度这类环境问题会让车道暂停，按探测放行，进度策略不会因此被停用（step16k）；
  - 坏历史不会再让请求永远 400：发出前先修整，修不好就在本地报错，请求不发出（step16k1、step16kb）；
  - 会话互通第一期可以用了：派活、发消息、取消（step16l，禁令已解除）；
  - 同一条唤醒同因连续失败 5 次就结案，不再每 30 秒重领；管理员可以用 `/wakes` 查看和人工重放（step16m）。
- **更早的版本**：
  - step16k（`4c0edd913`，含 step16j）09-29 20:22 上线：全量 24987 过，Linux 通道 24943 过，都是 0 失败。C5 的 MiniMax-M2.7 真实验收证据在 `~/.my-agent/releases/step16k-437eec03/c5-real-acceptance/`。
  - step16i（`e850ceb04`，含 step16h）09-29 03:44 上线。首次维护的证据在 `~/.my-agent/releases/step16i-df0114f2/first-maintenance/`：
    - README 第一节：03:57 global_index 首次压缩，三份索引从 921 MB 压到 153 MB；
    - README 第二节：21:39 R4 积压执行对账，local/main 11742 个动作，0 失败。

### 这两天上线的内容（step16k 之后）

- **出站协议合同**（step16kb，设计见 [PROVIDER_WIRE_CONTRACT.md](../design/PROVIDER_WIRE_CONTRACT.md)）：
  - Chat、Anthropic、Responses 三个组包出口，先在副本上修整历史：删空块，工具结果紧跟调用，孤儿结果删掉，缺的结果补"结果未知"回执，调用和结果按出现次数配对。
  - 修整后再按协议校验。违规就在本地报 `PROVIDER_REQUEST_SHAPE_INVALID`，请求不发出。
  - 真实模型对照，证据在 `~/.my-agent/decision-evidence/wire-contract-2026-09-30/`：
    - MiniMax-M2.7 和 M3：修整前 24 个请求有 7 个 400（M2.7 2 个，M3 5 个），修整后 24 个全部 200；
    - DeepSeek 官方 deepseek-v4-flash：修整前 Anthropic 协议 6 段里 5 段 400，Chat 协议 6 段里 4 段 400，修整后 12 个全部 200；
    - opencode Go 通道：修整前后都是 200，它不挑这些形状。
  - 9a 事后复审提了 1 条必须改：空 id 和重复 id 的配对口径。已在 step16l 修复（`20d642aa8`）。
- **会话互通**（step16l）：3a 做完最终验收，禁令已解除，详见下文。
- **唤醒毒丸**（设计见 [WAKE_POISON_PILL.md](../design/WAKE_POISON_PILL.md)）：
  - step16l 带来第 3 步 C1（纯函数与存储层），step16m 带来接线 C2–C4、C6 和第 4 步运维面；
  - 同一条唤醒同因连续失败 5 次就结案；瞬时故障、环境类故障和优雅停机都不计数；
  - 进程中途死掉时，那一片认领的会话消息会退回；
  - 管理员用 `/wakes` 查看、人工重放，TUI 和飞书都有；结案记录 14 天后归档；
  - step16m 上线后，3a 在隔离 home 用 M2.7 重跑了 `/wakes` 真实验收：TUI 10 步、假飞书 6 步，全部符合设计。证据在 `~/.my-agent/decision-evidence/wake-ops-m27-ac82541e5/`；
  - 第 5 步「真实链路门」的两个注入还没做，见下文台账待定项。
- **维护状态**（step16l）：`data/maintenance.json` 只加键，旧的 `status` 口径不变。
  - 新键：`apply_outcome`（applied / refused / legal_hold）、`isolated_error_count`、`last_applied_at`；
  - Gateway 维护摘要里的 failed 只算整次被拒和执行期失败；
  - 首晚核对见「剩余风险」。

### my-agent 暂停

- **额度**：09-29 04:35，local/main 主模型所用套餐的每周额度用完。服务商 429 错误体里有 `metadata.limitName="weekly"`。
- **止血**：04:22，4 个看板监控 job 被暂停。
  - 走的是产品 `SchedulerRepository.pause_job`，脚本是 `ops_pause_jobs_429.py`，恢复用同一脚本的 `--resume`；
  - 用户原有的两个一次性 job 本来就是暂停或已完成状态，没有动。
- **用户决定**（09-29 19:15）：my-agent 的模型不再续费，4 个 my-agent 不恢复，剩余工作全部由 Claude 会话收尾。
  - my-agent 手上的活都已合入或被接手：取消线、去重改造由 dsh-75 重做；root 下的测试修复交给 dsh-38。
  - 4 个看板都贴了最终说明；09-30 又贴了会话互通禁令解除的通知。
- **恢复预案**：见 `~/.my-agent/decision-evidence/compact-429-20260929/resume-plan/README.md`，只预演过，没执行。步骤是：
  1. 确认生产在线、压缩上限是 300000。定稿时这一步的前提变了：生产已是 step16m，上限已按用户决定去掉。恢复前要先请用户决定要不要重新设上限；
  2. 探测额度；
  3. 逐个手动压缩 4 个线程；
  4. 把周期改成 20 分钟，4 个 job 各错开 5 分钟；
  5. 一次只恢复一个 job，观察一个周期、没有 429 再恢复下一个；任何一个出现 429 就立即暂停。
  - 用量估算见同目录的 `usage-estimate.md`，首次压缩的代价见 `~/.my-agent/decision-evidence/compact-trigger-cap/first-compaction-cost.md`。
  - 排队输入回执的处理，以预案 README 里的更正为准。

## 改动文件

- 本提交只改文档：
  - 新增 `docs/tasks/MY_AGENT_WRAPUP_HANDOFF.md`（本文件）；
  - `CODEBASE_TREE.md` 加一行登记；
  - `DESIGN_LEDGER.md` 和 `docs/design/WAKE_POISON_PILL.md`：更正毒丸第 3、4 步的状态（已随 step16m 上线），标明第 5 步待做。
- 各批次的代码改动见下文「已上线批次」和提交记录。

## 测试命令和结果

```bash
python3 scripts/check_doc_sync.py --base origin/main
git diff --check
```

结果：

- 本提交：doc sync 通过，diff --check 通过。
- 各版本的全量流水线、Linux 通道和真实验收结果见上文。

## 影响范围

- 本提交只做记录，不改任何运行行为。
- 生产影响见上文「生产现状」。会话互通禁令已在 09-30 解除。

## 需要主线重点复查

### 已上线批次（起草时的「在途批次」）

- **step16l**（`20d642aa8`，30 个提交）：在 step16kb（`b58ad91e1`）上重建。起草时列的 20 个提交，rebase 后的 SHA 对应如下：
  - 会话互通：
    - 去重键按单条消息区分：`b19ad05af`；
    - 方案 A，目标回合没消费就释放给下一回合，最多 5 次：`120a67857`；
    - 后台片没有正常结束时收尾：`5af028152`；
    - 还原 `123f6f3b4` 里说错的那处：`45fa159eb`；
    - 派活正文在非取消的失败后退回给同一任务号的重跑：`cb25e6924`；
    - M1，取消时撤回已退回的正文：`1d75de50d`；
    - M2，已取消任务的派活唤醒在开跑前结案：`27cd1e472`；
    - M2 的前提说明：`0016a2774`。
  - 毒丸第 3 步 C1：`fddbe4317`、`aefb53fae`。
  - 子代理压缩时撞额度，失败类型记成额度用完：`74e5dbd4e`。
  - 压缩上限跟进：`de29c2ff9`。
  - 测试卫生：`9158e24c2`、`d934562ee`、`e32f69ca6`、`1a8b4de2b`。
  - 台账和文档：`b8dfd6cdc`、`f6030b3ea`、`85a72289f`、`010bd2aec`。
  - 起草时还没进来、这次一起进的 10 个：
    - 派活线 R-a `e95968a7c`、R-b `26b1a189d`（ae 实现），75 复审后的跟进 `849690a37`；
    - 释放计次裁定 (a) `db97e0369`，ae 复审补的用例 `00f792805`；
    - 9b 的维护状态 4 个提交：`10a68c2d6`、`3eb44ef83`、`a5383fd7d`（38 复审必修）、`310d0661f`（说法更正）；
    - 3a 按 9a 复审出站合同写的跟进 `20d642aa8`。
- **step16m**（`ac82541e5`，10 个提交）：
  - 75 的第 3 步 C2 `1fc9612d9`、C3 `aaf90187b`、C4 `891fd5fd0`。起草时 ae 复审 C3 提的 F1、F2 已在 C3 里改完；
  - ae 的窄窗口 `b25c9467c`：派活回合的取消判定统一按任务号读；
  - be 的第 4 步 `/wakes` `62224c9f6`，注释更正 `fbe987428`（9a 必须改 1）；
  - 3a 的第 4 步复审跟进 `44b85c417`：必须改 2 改为导入 C4 的 `wake_domain_status`，重放来源的坏记录结构化拒绝，归档清理措辞按实测更正；
  - 第 3 步 C6 `23ee21a46`、`ac82541e5`：原由 38 负责，09-30 改由 3a 完成；
  - 文档 `02cb06257`。
- ae 的窄窗口 WIP 备份分支 `claude/ae-task-wake-fixes-wip-9b86`（`9b86b9439`）已被完成版 `b25c9467c` 取代，不要再拣。

### 3a 做过的可撤回判断

- 测试 venv：起草时写的「ci-venv 的 editable finder 改指 merge2」已经不适用。
  - scratchpad 里的旧 ci-venv 09-30 约 00:05 被 macOS 的 /tmp 清理弄坏；
  - 持久替代是 `~/.my-agent/releases/claude-tools/ci-venv-312`，editable 现在指向 `claude-3a-wire-contract`；
  - 集成专用的是 `ci-venv-312-int`，editable 指向 `claude-capability-merge2`，集成脚本都用它。
- 09-27 的旧插话回执挪到了 `~/.my-agent/releases/claude-tools/stale-steer-20260929/`，可以挪回。
- 集成脚本的 doc sync 门改成 `--base origin/main`。脚本默认只对比 HEAD，对已提交的分支查不出东西。

## 需要其他线协调

### 会话互通禁令（已解除）

- 现状：**已解除**（09-30），4 个看板都贴了解除通知。
- 起草时列的解禁前提都已满足：M1、M2、后台回合失败时释放预留、R-a、R-b、释放计次裁定，都在 step16l。
- 最终验收原定由 ae 做，09-30 起改由 3a 在 step16l（`20d642aa8`）上执行：
  - 第一层：替身模型真实链路门禁连跑 20 次，每次 30 个用例，20/20 通过；
  - 第二层：MiniMax-M2.7 真实模型一轮，隔离 home、三个真实 TUI。R1–R6 全部通过：派活、给空闲目标发消息、取消、忙碌时收消息、被 /stop 后仍送达、忙碌目标收派活。14 个前台请求 0 失败，9 条回执全部 consumed，唤醒没有残留。
- 证据：`~/.my-agent/decision-evidence/session-task-chain-e2e/final-step16l-20d642aa8/`。

### 清理计划

- 盘点结果在 `~/.my-agent/decision-evidence/worktree-inventory-20260929/inventory.md`：一共 150 个已注册工作树，每个都有合入判定和建议。
- 清理前按当时的 main，用同目录的脚本重跑一次盘点。step16l、step16m 已经合入，判定会变。
- 3a 的裁定：现在什么都不删，最后统一清理时再执行。
  - 已合入的 127 个，加上 9a 核过的 2 个：各会话清自己的。9a 的是 17 个 `claude-9a-*`。09-30 起要别的会话动手，先征得用户同意。
  - `my-agent-self-3`：工作树删掉，分支引用 `my-agent/self-dev-3-cancel-final` 保留。这条线已由 75 重做，并以 -x 拣进上游。
  - `claude-38-review-index`：游离 HEAD `d386ab282` 不在任何分支上，是被 `ddfab5392` 压平取代的旧版本，由 38 决定。要保留，就先打一个 `archive/` 分支引用，再删工作树。
  - 「同名但补丁不等价」的 3 个，最后清理时由各自的会话确认差异：38 的 `claude-38-delete-dead-config`、`claude-38-scenario-gateway-stop`，be 的 `claude-be-effort-probe`。
  - 有 1 个已跟踪文件没提交的 2 个（`claude-decision-point-fields`、`claude-integrate-self-dev`）：由 3a 看过改动内容，再决定丢弃还是保留。
  - 保留两个：`claude-capability-merge2` 和 `linux-test-main`。`ci-venv-312-int` 的 editable 指向 merge2，`ci-venv-312` 指向 `claude-3a-wire-contract`，删这两个工作树之前要先改 finder。
  - 范围外的临时工作树由属主自己删：`/private/tmp/base-check-*` 和 `/private/tmp/cs-before`，HEAD 在 `claude/38-*` 分支上；还有 75 的 scratchpad 里的 `wt-st`。
  - Codex 的地方一律不动，写进待用户决定的事项，见下文。

### 真实测试规矩（09-30 更新）

- 真实模型：
  - MiniMax 官网（`api.minimaxi.com`）的 MiniMax-M2.7 和 MiniMax-M3；备用是 `api.minimax.cn` 的 M2.7；
  - 09-30 用户放开 DeepSeek 官方（`api.deepseek.com`，模型目录里的两个档案）；
  - 09-30 用户放开 opencode Go 通道，用用户给的 key。key 存在 `~/.my-agent/releases/claude-tools/secrets/opencode.key`，权限 600，不打印，不复制进仓库和证据，收尾测试结束后删除。请求要带 `User-Agent` 头（不带会被 Cloudflare 以 1010 拦下）和 `x-opencode-session` 头（不带回 400）；
  - 高频测试用 deepseek-v4-flash。
- 模型目录副本：
  - 只放进隔离的测试 home；
  - 只保留选中的服务商和它的模型档案，其他服务商和密钥全部删掉，并关闭决策模型；
  - 权限 600，不打印，用完就删。
- 只用私有端口，不碰生产的 8420。
- Jev 复测只读已有账本，不发新的 Jev 请求；确实需要真实 Jev 调用，先问用户。
- 测试 helper 只在 pytest 下跑，因为 conftest 会隔离 `MY_AGENT_HOME`。

## 剩余风险

- **维护状态首晚核对**：起草时的风险「只有隔离错误的运行被记成 failed」已在 step16l 修复，见上文。要在 09-30 21:39 那一拍核对一次，原定 9b，09-30 起由 3a 做。9b 给的预期：
  - ran=10、refused=1（`ebd40e6fc3ec`）、isolated=3（local/main 约 355，`86462b8c9517` 4，`93b8c3ffffb8` 4）；
  - failed=1，就是那次整次被拒，不是回归。
- **出站合同的已知边界**，详见设计文档「不做与已知边界」：
  - 不删无签名的思考块，所以跨服务商切到官方 Anthropic 时仍可能被拒；
  - 调用和结果之间夹了 assistant 时，真实结果会被当成孤儿删掉，换成"结果未知"回执；
  - 主链路上旧的孤儿清扫仍会丢掉空 id 的结果，和出站合同的口径不一致，已记台账待定。
- 定时任务撞额度时车道不暂停：额度一直不恢复，就每个到期点发一条通知。这条已记台账待定；生产目前没有启用中的定时任务，影响为零。
- 飞书用户 owner `ebd40e6fc3ec` 的 v1 保留策略每天整次被拒，一条数据都没动，等用户决定。测试 owner `93b8c3ffffb8` 已用产品迁移器迁到 v2，只读预演 0 个动作。
- 355 棵旧版任务目录（17.6 MB）保留原样，受保护、不会再增长。见 [STORAGE_RETENTION.md 第 9 节](../design/STORAGE_RETENTION.md)。
- 知会，不需要决定：
  - litellm 在监听 `0.0.0.0:4000`；
  - Chrome 的 APFS 克隆占的空间，重启 Chrome 可以清掉。

## 后续建议

### 待用户决定的事项

1. **local/main 自己的主模型。**
   - 3a 09-29 按结构化字段核实：local/main 下有两层模型选择，都走服务商 opencode-go（opencode.ai），也就是这次用完每周额度的 Go 套餐。
     - 默认选择是档案 `6bf0febf…`（deepseek-v4-flash），新对话和用户自己的会话都用它。
     - 4 个 my-agent 线程各自选的是档案 `536c11f9…`（deepseek-v4.1-flash）。台账和 09-29 04:07 汇报里说的 v4.1-flash，指的就是这几个线程。
   - 所以用户自己在飞书、TUI 上的对话，同样被每周额度挡住。
   - 09-30 的新情况：用户给了一把 opencode key，3a 在 Go 通道实测可用（deepseek-v4-flash，12 个请求都是 200）。只比对了哈希：它和 local/main 模型目录里的那把不是同一把；另外 4 个 owner 的模型目录从 09-09 起用的就是这把。
   - 选项一：等每周额度重置。
   - 选项二：把 local/main 模型目录里 opencode-go 服务商的 key 换成这把，两层模型选择都不变。
   - 选项三：把 local/main 的默认选择切到 MiniMax 官网服务商 `d9607663…`（`api.minimaxi.com`）的 M2.7。09-29 9b、be 和 09-30 3a 的真实验收都用它调用成功。
   - 选项四：切到目录里 DeepSeek 官网的档案，服务商 `api.deepseek.com`，模型 deepseek-v4-flash；`ff961d14…` 走 openai 协议，`79960dbe…` 走 anthropic 协议。09-30 两个协议都真实调用成功，出站合同也覆盖了它更严的消息规则。
   - 切换会改变用户自己助手的模型和花费，由用户定。
2. **飞书用户 owner `ebd40e6fc3ec` 的 v1 保留策略要不要迁移。** 这个 owner 07-14 以后不活跃。迁移后第二天的维护会按默认规则清理它的旧数据，其中一部分不可恢复。
3. **355 棵旧版任务状态要不要做一次性显式迁移。** 做法是管理员命令逐条写迁移记录，比如标成已放弃，再按正常保留窗口清理。见 [STORAGE_RETENTION.md 第 9 节](../design/STORAGE_RETENTION.md)。
4. **Homebrew python3.14 的 editable 安装要不要卸载。** 它的 `my_agent` 指向 6 月的旧目录 `~/my_agent/my-agent`，在工作树外起的子进程测试会导入这份旧代码。
5. **capability 配置读不了时，开关是否一律关闭（fail closed）。**
6. **要不要让模型看到具体原因码。** 现在模型只看到通用错误码和合同提示，具体原因码只给宿主看；要开放需要评估一个开关。
7. **飞书用户 `ou_16d7…`。** 它的 owner 没有配置模型：09-25 的 6 条消息全部 `ModelNotConfiguredError`，Curator 每小时告警一次。选择是给它配一个模型（共享或单独），还是继续不服务。测试 owner `tui-matrix/p1-r141-local-compact` 也是同样情况，可以一起决定是否归档。
8. **`.env` 墙策略。** 见台账「【待用户决策】owner 墙模式下凭据文件（`.env` 等）不被拦」。
9. **会话互通第二期**：IM 目标、普通用户开关、跨 owner 授权。见 [SESSION_MESSAGING.md](../design/SESSION_MESSAGING.md) 和台账「会话间消息与派活」。
10. **Codex 的地方，我们没动：**
    - `~/my-agent-worktrees/r230-*` 共 6 个，是 09-11 仓库重建前的旧历史，和 main 没有共同祖先；
    - 失联的 `~/my-agent-worktrees/r231-incomplete-protocol/`（36 MB），它的 git 管理目录已经不在；
    - 建在 `~/my-agent-dsh/.claude/worktrees/` 里的一个 agent 工作树（已合入）；
    - `~/.codex/worktrees/` 下的 6 个。

### 台账里的待定项（只列标题，内容见 [DESIGN_LEDGER.md](../../DESIGN_LEDGER.md)）

- 出站协议合同：坏历史不再让请求永远 400（其中「主链路旧孤儿清扫丢空 id 的结果」待定）
- 旧版任务状态文件：355 棵 `TASK_STATE_INVALID` 保留原样（待定、未落地）
- 插件子进程测试会往源码树写 __pycache__（待定）
- 模型每周额度用完：判定、用量与后台轮询成本（第 4、5 项未落地）
- 定时任务「输入没变就跳过模型调用」：现有机制调查（待定、未落地）
- 普通消息迟到送达：信封带提交时间与提升年龄门（待定，只记方案）
- 定时任务撞模型额度用完：按失败结算，不再每 30 秒重跑（其中「额度通知按线程限频，或连续撞额度时自动暂停 job」待定）
- 压缩调用撞模型额度用完：后台按额度用完处理（其中一个既有问题待定）
- 唤醒认领的毒丸处理（第 1–4 步已上线；第 5 步「真实链路门」的两个注入待做；`archive/ledger/`、`archive/unreadable/` 没有保留上限，由运维清理）
- 只读动作的权威读失败以后可换一个可重试码（待定）
- Gateway 派发线程存活成为结构化事实（外部判活的长期方向未落地）
- 墙钟超时后旧请求仍在途，重试可能向供应商重复发送（暂不排期）
- 【待用户决策】owner 墙模式下凭据文件（`.env` 等）不被拦
- 产品持久数据保留策略盘点（部分落地，见 [STORAGE_RETENTION.md](../design/STORAGE_RETENTION.md)）
- Jev 决策调用的链路分段计时（B 第 0 步）（B 第 1 步连接复用暂不排期）
- 会话间消息与派活（第二期未开始；「交付前的取消判定每片全量扫会话任务」已随 step16m 的窄窗口提交解决，控制面 `_bound_background_turn_is_running` 的全量读取照旧）

### 建议下一步

1. **先请用户定 local/main 的主模型**（上面第 1 项）。
   - 为什么先做：用户自己的助手现在还被每周额度挡住，这是唯一直接影响日常使用的事。
   - 定了之后由 3a 按用户选的方案改，改完做一次真实调用核对。这一步只动一份配置，不适合并行。
2. **09-30 21:39 之后做维护状态首晚核对**：只读，按上面的预期值比对，由 3a 做。
3. **最后统一清理**：重跑盘点，3a 清自己的工作树；别的会话的工作树要用户允许才动；Codex 的地方不动。收尾测试结束后删掉 opencode key 文件。
4. **是否恢复 my-agent**：由用户决定。恢复就按预案走，注意预案第 1 步的上限前提已经变了。
- 风险边界：
  - 部署只切 Mac，只在空闲窗口切；
  - 真实测试只用上面列出的来源；
  - 不读配置正文，不打印密钥；
  - 不碰 Codex 的检出；
  - 找别的会话帮忙之前先问用户。
