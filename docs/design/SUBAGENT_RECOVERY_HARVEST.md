# 子代理恢复预算与采集窗口合同

状态：2026-10-29 源码候选，尚未部署。对应 e905 sol-6 Round 1 的 F1–F16 与 Astra-2 F5。验证入口见仓库根 `TESTS.md`，本合同不代表真实 Gateway 重启或外部来源端到端验收。

## 一、恢复状态与预算

`runner_attempts` 继续是审计/CAS 总执行次数，不是失败重试额度。普通失败与供应类失败各自记账；正常 yield、等待下级和 Compact 不消费失败额度。只有从失败状态开始新的准确 attempt 才消费重试次数，同一收口快照重复保存不能重复计数。

派发、恢复和计数写门共用 `SUBAGENT_RETRYABLE_FAILURE_STATUSES`（BLOCKED、FAILED、TIMEOUT）。状态集合不单独授予重试权：仍须同时通过失败类型、显式停止、能力阻塞、原预算与准确 attempt 等既有门。普通额度和供应类上限不能因 BLOCKED 绕过。

无进展含义是“自上次宿主证实产物字节推进以来的无进展失败累计”，不是连续执行次数。正常切片不清零；真实同集合内容推进才重置。未知观察既不冒充进展，也不增加已证实无进展次数。

### canonical 扩展字段

以下字段存于原 `canonical_state.json` 顶层，未知字段通过 `_extra_fields` 保真；不新增第二份事实文件，不借 DTO 默认零值洗掉旧数据。

| 字段 | 合同 |
|---|---|
| `failure_retry_count` | 普通失败后已启动的重试累计 |
| `provider_retry_count` | 供应类失败后已启动的重试累计，与普通预算分离 |
| `no_progress_count` | 宿主已证实无进展的失败累计 |
| `no_progress_unknown_count` | 因观察集合、预算或读取故障无法判定进展的累计；正式策略回执及恢复编排快照/原账本保留 |
| `recovery_history_unknown_count` | 恢复预算历史损坏/未知的正计数；候选门 fail-closed |
| `recovery_safe_resume` | 同一准确 attempt 有宿主关闭证据、无未知效果时的安全接续事实；不覆盖显式停止等门 |
| `recovery_progress_baseline` | 启动时的有界宿主字节指纹/引用集合；不以模型声明或首次看到旧文件当本轮进展 |
| `recovery_progress_baseline_attempt_id` | 基线所属的准确执行轮，防止跨轮比较 |

产物观察最多 16 件、单件 256 KiB、合计 1 MiB；越界、无法 no-follow 读取、引用集合变化等场景按未知处理。此熔断不是任意工具效果、所有 Goal 或纯知识工作的通用进度证明。未知观察单列可见，普通失败额度仍独立生效。

### 旧数据迁移与坏账

`recovery_counters` 是 load 与只读准入共用的纯校验口。仅当所有新计数字段均缺失、状态/失败类型/非负整数 `runner_attempts` 有效时，认定干净 legacy：按 `max(0, runner_attempts - 1)` 保守初始化当前类别预算，不标记永久未知。不推断此前正常切片的数量。

load 只在返回对象中派生字段，不改原文件。下一次正常 canonical 保存才持久化 `recovery_counter_migration` 的结构化来源及审计次数。部分字段缺失、布尔、负数、非整数、重试计数和超过总 attempts，或已有新计数却缺失原始 attempts，必须首次只读 load 即设置正 unknown；不能等 save 后才安全，不能把 dataclass 默认零当原始 JSON 证据。

坏恢复历史同时阻断策略和正式恢复编排：即使有有效 refs，也返回 `manual_review_unknown_recovery_history`、`blocked_by: [recovery_history_unknown]`，不建议自动续派或接管；`no_progress_fuse` 为 false，不把未知冒充已证无进展。这个坏账门与仅描述产物观察不完整的 `no_progress_unknown_count` 不同，后者本身不制造坏账。已有合法 99 次审计历史仍按原显式停止协议恢复；一次保存跨多个未见 attempt 则如实标未知，不能为兼容测试放开。

### 阈值唯一来源

生产 `RunnerCandidatePolicy` 调用方传入原能力配置的 `subagent_no_progress_attempt_limit`；0 明确关闭无进展熔断，不关闭独立失败重试额度。

真实启动 preflight、恢复编排与树报告也必须使用同一有效配置。组合根向原 manager 绑定 `capability_config_for_paths` 的只读依赖，保留原构造 root 与准确覆盖路径；每次恢复读取，不从任务目录或 owner 名猜另一份配置。坏配置不静默用默认 4 自动启动。无组合根依赖的独立旧调用仅使用 `CapabilityConfig` 默认值。

## 二、缓存和根任务列表

缓存仍以原 locator/canonical 文件为权威，锁内只取/发布/淘汰；stat、解析、predicate、deepcopy 在锁外。两份文件都须达到 `CACHE_TRUST_AGE_SECONDS` 才可命中/发布，发布前重新核对两份指纹。粗时间戳新 canonical 不能借老 locator 提前取得信任。根列表回退复用规范 strip 匹配，不另外维护根任务状态。

## 三、采集窗口边界

控制信息仍在原 WatchState checkpoint 的 `_host_harvest` 中，通过原 WAL/事务提交；不建立另一份进度账。来源提供的 checkpoint 只能更新来源部分，不能覆盖宿主边界、退避和 due 信息，也不能用伪造的 `_host_harvest` 注入宿主状态。

### 有界来源

文件冻结 dev/inode 与字节上界，同 deadline 复用相同边界。首次冻结前发现物理代次变化，按原轮转语义重置游标、登记 gap，再从新文件开头冻结；已有边界后换源不能让失败拍回退已提交的游标或身份。读取不得越过原边界，尾部不完整或尚未追平保持 partial。

游标有显式数值上界时，接受 `record_ordinal` 以及项目规范 `cursor_position_semantics: contiguous_record_ordinal`；不能从 opaque token 猜数值终点。

### 没有可证明上界的 cursor/adapter

到期仍实际执行一次受 drain budget 约束的最终补拉。成功或到 EOF/空页之后设置窗口终结事实，并明确记 `boundary_mode: deadline_catchup_unbounded`、partial/unbounded 元数据；“窗口结束”不等于“证明全部 pre-deadline 数据完整”。错误时不 finalize，沿正退避重试，不无限停在未发请求的 SOURCE_ENVELOPE_INVALID。

截止判断必须与本次事务是否冻结边界这一事实绑定：入口检查与 WAL/fsync 之间跨过 deadline，也不能用旧 finalize 标记绕过已有边界，或在没有边界的事务上误报 complete。完成通知仅跟随已提交的窗口终结标记。

## 四、节拍与取样

截止前的 backoff 等待/工作发现须被 deadline 钳制，及时唤醒冻结；截止后重试仍保持正等待，不能被 deadline 压成零。非截止背压在领 lease / 写 WAL 之前返回 busy；仅到期冻结需要元数据事务，避免磁盘水位背压每拍反而 fsync。

有样本但后页失败，保留可用样本以及 coverage/error/partial；完全失败且 0 条样本时，仅更新失败覆盖信息，保留上次成功样本的 fields 与时间，不破坏高频判据证据。无生产调用的 `sample_file_lines` 包装删除，测试使用真实 drain 原语。

## 未验证边界

受控临时文件、RuntimeDB、真实本地生命周期/请求入口与模拟来源回调可验证合同，但不证明真实 Gateway 重启后的自动调度、用户外部 cursor/adapter 网络、跨机器可达、Windows 或生产压力行为。上线与这些观察点复核仍归集成车道。
