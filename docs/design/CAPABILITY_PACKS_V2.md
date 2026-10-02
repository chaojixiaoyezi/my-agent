# 能力包 v2：宿主核验交付物、保护输入原件、要求交付（第 10 条选 A + 第 11 条）

- **状态：已确认设计、实施中**（2026-10-02，3a 审定）。块 1、2、3 已实现，块 7 已复审通过，见第 7 节进度表。块 2 内把检查程序的 `baseline` 推广成 `inputs`（见第 2 节），由 ae 定、待 3a 审。
- **依据**：用户第 10 条（“包要好好做，我很看重能力包”）和第 11 条（宿主证实包检查真跑过）。
- **底子**：`c13-frozen-reruns-28435`，业务审阅 10/27（A 包 1/9、B 包 1/9、不该用包的 8/9）。17 次失败的逐条归因在证据目录 `capability-packs-v2-design/attribution.md`：
  - 宿主机制 5 次；
  - 检查器盲区 7 次；
  - 包内容 2 次；
  - 模型能力 3 次。
- **验收标准**（C16，不变）：某个包的冻结业务重跑 9/9 全部通过，才装进生产、打开一次选择；过不了就不装，并如实给逐条归因。
- **分工**：宿主线（块 1–6）ae；包内容（块 7）be，ae 审；重跑和审阅（块 8）ae。每完成一块交 3a 审。

## 1. 现状（只读核对，5a56714dc）

- **没有检查程序声明**：v7 内容包没有结构化的检查程序声明，脚本只是私有资源，靠模型用 `write_file.source_ref` 复制后自己用 `run_command` 跑。冻结重跑里，模型两次复制失败后手写或改写检查器，再谎报“已验证、警告 0”（A05-t5、A10-t5）。
- **主代理收尾不查交付物**：主代理收尾时不检查交付物在不在（`_finalization_service.py`），只有子代理有交付闸（`deliverable_closeout.py`，最多返工 2 次）。`delivery_contract.v1` 只能由 CLI 传入，而且只转成提示文字。
- **没有输入基线**：宿主不记录任务开始时已有的输入文件，产物登记只有写后哈希。
- **能复用的现成机制**：
  - `AttemptExecutionSandbox`（`agent/attempt/sandbox.py`，Linux bwrap 整根只读、macOS Seatbelt，插件进程沙箱也复用它）；
  - `HostNotice`（宿主撰写、模型看不到、回合结束时发出）；
  - `_no_tool_calls_decision` 返工通道（各类返工有上限）。

## 2. 包声明：`capability.verification`（块 1，已实现）

v7 能力包可选块，由 `agent/capability_verification_manifest.py` 校验。没声明时，序列化结果与旧包逐字节一致。

```json
"verification": {
  "deliverables": [{"id": "delivery", "path_patterns": ["**/*.json"], "required": true,
                    "field_match": {"format": "json", "field": "schema", "equals": ["drama_text_delivery.v3"]}}],
  "verifiers": [{"id": "check-delivery", "member": "scripts/check_delivery.py", "runtime": "python",
                 "applies_to": "delivery", "args": ["--delivery", "{target}", "--host-json"], "timeout_seconds": 20,
                 "inputs": [{"flag": "--source", "source": "task_input", "path_patterns": ["**/*.json"], "required": true,
                             "field_match": {"format": "json", "field": "schema", "equals": ["drama_text_source.v1"]}}]}],
  "input_policy": "preserve_originals"
}
```

- **交付物识别是开放的**：
  - 先按工作区相对的路径模式匹配（glob，支持 `*`、`?`、`**`）；
  - 可选再用结构化字段匹配，`format` 是开放字符串；
  - 宿主认识的格式（首期 `json`）按字段比对；不认识的格式，以及没声明字段匹配的交付物，只核对“存在、能打开”；
  - 不靠文件名语义或答复文字认交付物。
- **检查程序**：
  - `member` 必须是本包 `files` 里带 sha256 的成员；
  - `args` 里恰好一个 `{target}`，其余只能是不含花括号的字面量；
  - 超时 1–120 秒；
  - `runtime` 是开放字符串，宿主不支持的只记 `unsupported_runtime`，不跑，也不拒绝安装。
- **关联输入**（`inputs`，每个检查程序最多 4 条）：检查程序除了 target 还要看的文件，比如另一种 schema 的原文、本回合写出的交接文件、改动前的原件。
  - 每条 `{flag, source, path_patterns, field_match?, required}`，匹配写法和交付物一样；参数名互不相同，也不能和 `args` 里的字面量重名。
  - `source` 是开放字符串，宿主首期认两种：`task_input` 指任务开始时已有的文件，被就地改过时交宿主存的原件副本，没有副本就算找不到；`turn_output` 指本回合写出或改过的文件，不含 target 本身。不认识的来源按找不到处理，不拒绝安装。
  - 恰好匹配一个才算找到，0 个或多个都算找不到。`required` 的找不到时不运行，记 `verifier_input_unresolved`，不返工，进事实和收尾通知；非必需的找不到时照跑，只是不带这个参数。
  - 宿主只按声明顺序传声明过的参数名，调用方多给的一律不传。检查器需要的其它参数（例如要读懂交接内容才能拼出来的文件清单）宿主不拼，否则就成了专项合同。
  - 改动缘由：be 对齐包内容时发现，A 的检查器要 `drama_text_source.v1` 原文，B 要本回合的 `handoff.v2`，原来只有一个“同交付物基线”不够用。
- **输入策略**：开放字符串，宿主只执行 `preserve_originals`。
- **启用前确认**（`agent/capability_verifier_consent.py`）：
  - 声明了检查程序的包，`/plugins enable` 先返回确认回执（`kind=capability_verifiers`），列出每个检查程序的成员、sha256、运行方式、参数、超时和关联输入；凭确认码才启用，和 v6 程序确认是同一条回执路径。
  - 确认内容的完整摘要写进 `PluginContentActivation.verifier_consent_sha256`；为空时不输出，旧记录和旧代次不变。
  - 宿主运行前用 `verifier_consent_matches` 重算比对：包、成员摘要、参数、超时、关联输入任何一项变了，旧同意就失效。
  - 没声明检查程序的包，启用流程不变。

## 3. 宿主跑钉住的原版检查程序（块 2、3）

- **执行边界**（3a 审定）：
  - 只跑声明过、启用时确认过、按 sha 钉住的原件；
  - 断网；目标只读；只写临时目录；
  - 没有沙箱就不跑，记 `sandbox_unavailable`。
- **沙箱只有一个入口**：复用 `AttemptExecutionSandbox`，不新写 sandbox-exec 配置。断网用同一个 `AttemptSandboxSpec.network_access`：
  - Linux 原本就有 `--unshare-net`；macOS 这次补上，配置最后加 `(deny network*)`，默认 True 时现有配置逐字节不变；
  - `network_access=False` 时，就绪检查会实际试一次断网，失败记 `SANDBOX_NETWORK_ISOLATION_UNAVAILABLE`，按沙箱不可用不跑；
  - 实测：Docker 容器里以 root 跑 bwrap、没有 `NET_ADMIN` 时，回环配置失败（`loopback: Failed RTM_NEWADDR`），这时宿主拒绝运行；车道加上 `--cap-add NET_ADMIN` 后能真实断网；
  - 宿主触发检查程序前先显式调用 `require_ready()`，不就绪时一个进程都不起。
  - 检查程序只由宿主触发，模型只看到结论摘要。插件管理工具对模型不可见，模型拿不到确认码。
- **成员原件**：从已安装的包 blob 按 sha256 读出成员字节（`read_capability_member` 一类入口，核对激活代次），放进宿主临时目录，用宿主自己的 Python（`-I -S`）运行。工作区里的副本一律不用。
- **输出合同**：stdout 是一个 JSON 对象（不超过 1 MB，日志写 stderr），`schema=pack_verifier_result.v1`，内容 `{valid, errors[{code, location}], warnings[{code, location}], metrics}`。
  - 宿主只读 `valid`、code 和计数，不解释 message，也不读 `metrics`。code 是非空字符串，不超过 128 字符；不同 code 最多 64 个，超出的计入 `_other`。
  - 为了让返工提示能定位，宿主保留前 10 条错误的 code 和 `location`（去掉控制字符、截到 128 字符），转给模型，自己不解释。
  - `location` 是检查程序写的任意文字，转给模型前宿主先脱敏宿主路径（9b 复审 F4，`capability/pack_verifier_redaction.py`）：
    - 目标和输入换成工作区相对路径，工作区根前缀去掉；
    - 本次临时目录换成 `<verifier>`，宿主解释器换成 `<python>`；
    - 换完仍含宿主路径的整条置成 `<redacted>`。是否“仍含宿主路径”只看结构化事实：片段以 `~/`、盘符开头，或者是 `/<段>` 且这一段在本机根目录下真实存在。JSON Pointer（如 `/shots/0`）的首段在本机不存在，不会被误伤。
  - `valid` 必须等于“errors 为空”。自相矛盾、格式不对都记 `verifier_output_invalid`。
  - 退出码不参与判定，只用来识别超时。检查器写出 v1 就退 0；目标坏了给 `valid=false` 加错误码，崩溃没写出 v1 时宿主记 `verifier_output_invalid`。
  - 运行形态：宿主只把这一个成员拷进临时目录、改名后用 `python -I -S` 跑。检查器必须是单文件、只用标准库，不 import 包里其它文件，也不读包里的模板或资源。
- **什么时候跑**（不新增工具）：
  - **写完就查**：钉住包的任务里，写工具写出声明的交付物后马上检查。写工具回执只附**有上限的摘要**：通过或失败、错误码计数、前几条错误码；全文进账本，不把工具结果撑大。
  - **收尾再查**：回合结束前，对本回合写过、改过的全部匹配交付物各跑一次，shell 写出来的也算。
- **入账**：
  - 检查程序身份：包 ID、版本、整包 sha、成员路径和 sha、runtime；
  - 目标和基线：相对路径和 sha256；
  - 运行结果：rc、`valid`、各 code 计数、耗时；
  - 关联输入：每条的参数名、来源、是否必需、相对路径和 sha256（没找到时为空）；
  - 原因码：`verifier_timeout`、`verifier_output_invalid`、`sandbox_unavailable`、`verifier_member_mismatch`、`verifier_consent_missing`、`unsupported_runtime`、`verifier_input_unresolved`。
  - 写进运行事件和 `channel_delivery.pack_verifications`。
- **质量走返工，不前置硬拦**：收尾检查有错误时，返工提示 1 次；返工后仍有错误，照常结束并带收尾说明；只有警告不返工。
- **结论来源**：最终的检查结论由宿主用 `HostNotice`（`source=pack_verification`）给出，模型自述不算事实。

### 3.1 块 3 的实现（`capability/pack_verification_*.py`）

- **生效条件**（都是结构化事实）：
  - 能力配置开关 `capability_pack_host_verification_enabled` 打开（默认 false，管理员边界项，模型不能改）；
  - 插件总闸打开，有可信 owner；
  - 本任务钉住了这个包（主会话读任务链接的 pins，子代理读本 run 的 task 引用）；
  - pin 的激活代次还有效，整包摘要和安装项一致；
  - 包里声明了检查程序。
- **基线**：本 run 第一次改工作区的工具执行前（写工具，或运行策略声明了 `mutates_workspace`，比如 shell），按全部已启用、声明了核验的包的路径模式扫一次工作区。
  - 用全部已启用的包而不只是已钉住的包，是因为包可能在回合中途才被钉住。
  - 扫描不跟随符号链接，最多走访 2 万个条目、记 512 个文件，单文件 16 MB 以内才算摘要；超限记 `truncated`。
  - 基线截断时（9b 复审应修 2），不在基线里的文件可能是漏扫的老文件：
    - 只有写工具回执证明本回合写过它（账本里的 `written` 记录）才算本回合改的；
    - 其余算“不确定”，收尾照样检查、入账（`uncertain_targets`），但它的失败不触发返工。
  - 收尾时的当前快照截断也记进 closeout 记录和最终事实（`current_truncated`）。
- **关联输入怎么找**：
  - `task_input`：基线里有、现在内容没变的文件；
  - `turn_output`：本回合新建或内容变了的文件；
  - 两种都排除 target 本身，再按路径模式和字段匹配过滤，恰好一个才交给检查程序；
  - 每个参数名的匹配个数入账。
  - 被就地改过的输入在块 4 之前算找不到，块 4 会改成交原件副本。
  - 多回合任务里，上一回合写出的文件在本回合算“开始时已有”。如果因此匹配到多个，就按找不到处理，不会误交。
- **写完就查**：
  - `write_file`、`edit_file`、`apply_patch` 成功后，从回执的宿主字段取写出的绝对路径（`artifact_refs` 里标 deleted 的旧地址不算），一次最多 4 个文件；
  - 对符合钉住包交付物声明的文件，跑 `applies_to` 指向它的检查程序；
  - 回执 `handler_details.pack_verification` 附有界摘要：状态、错误数和警告数、前 5 个错误码和警告码、前 3 条错误样例；
  - 模型在 `[pack-verification]` 段看到这份摘要，归档白名单也收这个键，续跑重渲染时还在。
- **收尾再查**：
  - 挂在 `_no_tool_calls_decision`，子代理交付闸之后，复用同一个返工通道；
  - 目标 = 和基线比，本回合新建或改过的匹配交付物，shell 写的也算，最多 8 个，超出记 `truncated`；
  - 包、检查程序、目标和各输入的内容都没变时，复用写后结果，不重跑；
  - 有 `failed` 时给一次返工提示，列出目标、包、检查程序、错误数和“错误码 @ 位置”，最多 5 个目标、每个 5 条；
  - 返工先记账再发，记不进账就不返工，避免无界循环。
- **唯一账本**：`<任务存储根>/data/pack_verification/<run>.jsonl`，文件权限 600，跟着任务归档的保留期走。
  - 记录分四种：baseline、result、closeout、rework。
  - 返工次数和结果复用都只读这本账，Compact 续跑、进程重启后不会重置。
  - `runtime_events` 记 `pack_verification_completed`，只是可观测投影，不做决定。
  - 开关关着时，最终结果阶段直接返回、不去读账本，每轮收尾零 I/O。
- **对外**：
  - `AgentRunResult.pack_verifications`（`pack_verifications.v1`）→ `channel_delivery.pack_verifications`，不进公开投影白名单；
  - 回合正常返回后，Gateway 用这些事实写一条 `HostNotice`（`source=pack_verification`，`code=summary`），排入原提示队列，和当轮其它提示一起发布。
  - 收尾跑过时，只报最后一次收尾检查覆盖的结果，也就是交付时的内容状态。
  - 没跑过收尾（工具轮次上限、失败、中断）时，报每个目标最后一次写入时的结果，并注明“没有正常收尾”。
- **已知限制**：
  - 检查程序运行期间不响应 `/stop`（运行器拿不到取消信号），只受声明的超时约束，最长 120 秒；块 6 评估是否接入取消；
  - 回合没正常收尾时不做收尾检查。

## 4. 输入保护（块 4）

- **范围**：只对 `input_policy=preserve_originals` 的包、在钉住它的任务里生效。用户要求“改这个文件”的普通任务不受影响。
- **记基线**：第一次成功读取一个任务开始前就存在的工作区文件时，记 `{path, sha256, size}`。不超过 1 MB 的文件同时存一份原件副本：放在本 owner 的任务归档里，权限 600，每个任务合计不超过 16 MB，跟着现有归档的保留期走。每个任务最多 64 个基线文件。
- **比对**：收尾时重新算哈希，变了就记 `INPUT_MODIFIED_IN_PLACE`（路径和前后哈希），不管是哪个工具改的。
- **返工**：提示 1 次（恢复原件，另存新文件，告诉模型原件副本在哪），之后照常结束并带收尾说明。

## 5. 交付物存在（块 5）

- **判定**：声明了 `required` 的交付物，收尾时本回合没写出任何匹配文件，记 `DELIVERABLE_MISSING`；有匹配文件但打不开或解析不了，记 `DELIVERABLE_UNREADABLE`。都是客观事实。
- **返工**：沿用子代理交付闸的做法，最多返工 2 次，然后照常结束并带收尾说明，不改结束原因。
- **只审不交付的任务**（ae 定，块 7 复审时）：两个包的交付物保持 `required`。B05-t5 那次没交付就是靠这条兜住的；按“任务意图”区分要读懂用户的话，铁律不允许。代价是只审不改的任务最多多两次软返工提示。返工提示里要写明“如果用户这次只要审阅、不要交付物，在答复里说明即可”。

## 6. 开关、测试、重跑

- **开关**：`capability_config.yaml` 新增 `capability_pack_host_verification_enabled`，**仓库默认 false**。宿主执行包里的代码属于安全边界，按惯例新行为默认关。隔离重跑时打开；生产在装包时由 3a 一起打开。关掉时块 3–5 都不生效（启用确认照常要求）。
- **测试要点**：
  - 声明校验；
  - 确认和同意摘要；
  - runner 只读 blob 原件，篡改的工作区副本不影响结果；
  - 断网和只读（两个平台）；
  - 输出格式不对、超时；
  - 基线和原件副本上限；就地修改的三种来源（`edit_file`、`apply_patch`、shell）；
  - 交付缺失和解析失败；
  - 返工上限；
  - `HostNotice` 内容只来自事实；
  - 开关关闭时行为不变。
- **变异**：至少 12 个。
- **重跑**：同一套 harness、同一批冻结用例、MiniMax-M2.7，每例 3 次；先跑 3 个试次核对用量和宿主事实，再跑全部。
  - 审阅说明只改检查程序版本号，判据和计分逐字节不变，说明文件的 diff 存进证据。
  - harness 先补取证缺口：预检模型身份和全新会话事实、测试方新旧两版检查、宿主事实进审阅包、删根前归档工具索引。
  - 用量估约 1,600 万–2,000 万 token，单次长任务停线不变。

## 7. 进度

| 块 | 内容 | 状态 |
| --- | --- | --- |
| 1 | `verification` 声明与安装校验；启用前执行确认；同意摘要写入内容激活 | 已实现（`test_capability_verification_declaration.py`） |
| 2 | 检查程序运行器（复用 `AttemptExecutionSandbox`，补通用断网选项与断网就绪探测）；`baseline` 推广成 `inputs` | 已实现（`capability/pack_verifier_runner.py`，`test_pack_verifier_runner.py`，macOS 与 Linux 车道实测） |
| 3 | 写完就查、收尾检查、返工、`HostNotice` 和 `channel_delivery` 事实、开关 | 已实现（`capability/pack_verification_*.py`，`test_pack_verification_service.py`、`test_pack_verification_matching.py`） |
| 4 | 输入基线、原件副本、收尾比对、返工 | 待做 |
| 5 | 交付存在和返工 | 待做 |
| 6 | 变异、门禁、文档收口 | 待做 |
| 7 | A 0.5.0、B 0.3.0（be） | 复审通过（`claude/be-capability-packs-content-b2` 5bf9bb61d） |
| 8 | 重跑和独立审阅 | 待做 |
