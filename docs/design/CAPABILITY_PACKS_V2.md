# 能力包 v2：宿主核验交付物、保护输入原件、要求交付（第 10 条选 A + 第 11 条）

- **状态：已确认设计、实施中**（2026-10-02，3a 审定）。块 1 已实现，见第 7 节进度表。
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
                 "applies_to": "delivery", "args": ["{target}", "--host-json"], "timeout_seconds": 20,
                 "baseline": {"source": "input_same_deliverable", "flag": "--baseline-project"}}],
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
- **基线**：来源名 `input_same_deliverable` 指“本任务开始前就存在、且符合同一交付物声明的输入文件”，宿主找到唯一一个时才用声明的参数名把它交给检查程序。
- **输入策略**：开放字符串，宿主只执行 `preserve_originals`。
- **启用前确认**（`agent/capability_verifier_consent.py`）：
  - 声明了检查程序的包，`/plugins enable` 先返回确认回执（`kind=capability_verifiers`），列出每个检查程序的成员、sha256、运行方式、参数和超时；凭确认码才启用，和 v6 程序确认是同一条回执路径。
  - 确认内容的完整摘要写进 `PluginContentActivation.verifier_consent_sha256`；为空时不输出，旧记录和旧代次不变。
  - 宿主运行前用 `verifier_consent_matches` 重算比对：包、成员摘要、参数、超时任何一项变了，旧同意就失效。
  - 没声明检查程序的包，启用流程不变。

## 3. 宿主跑钉住的原版检查程序（块 2、3）

- **执行边界**（3a 审定）：
  - 只跑声明过、启用时确认过、按 sha 钉住的原件；
  - 断网；目标只读；只写临时目录；
  - 没有沙箱就不跑，记 `sandbox_unavailable`。
- **沙箱只有一个入口**：复用 `AttemptExecutionSandbox`，不新写 sandbox-exec 配置。它还不支持断网的话，就在同一个 `AttemptSandboxSpec` 上加通用的断网选项，Linux 和 macOS 都要有测试（Linux 在车道跑，macOS 写单测）。
- **成员原件**：从已安装的包 blob 按 sha256 读出成员字节（`read_capability_member` 一类入口，核对激活代次），放进宿主临时目录，用宿主自己的 Python（`-I -S`）运行。工作区里的副本一律不用。
- **输出合同**：stdout 是一个 JSON 对象，`schema=pack_verifier_result.v1`，内容 `{valid, errors[{code, location}], warnings[{code, location}], metrics}`。宿主只读 `valid`、code 和计数，不解释 message；格式不对记 `verifier_output_invalid`。
- **什么时候跑**（不新增工具）：
  - **写完就查**：钉住包的任务里，写工具写出声明的交付物后马上检查。写工具回执只附**有上限的摘要**：通过或失败、错误码计数、前几条错误码；全文进账本，不把工具结果撑大。
  - **收尾再查**：回合结束前，对本回合写过、改过的全部匹配交付物各跑一次，shell 写出来的也算。
- **入账**：
  - 检查程序身份：包 ID、版本、整包 sha、成员路径和 sha、runtime；
  - 目标和基线：相对路径和 sha256；
  - 运行结果：rc、`valid`、各 code 计数、耗时；
  - 原因码：`verifier_timeout`、`verifier_output_invalid`、`sandbox_unavailable`、`verifier_member_mismatch`、`verifier_consent_missing`、`unsupported_runtime`。
  - 写进运行事件和 `channel_delivery.pack_verifications`。
- **质量走返工，不前置硬拦**：收尾检查有错误时，返工提示 1 次；返工后仍有错误，照常结束并带收尾说明；只有警告不返工。
- **结论来源**：最终的检查结论由宿主用 `HostNotice`（`source=pack_verification`）给出，模型自述不算事实。

## 4. 输入保护（块 4）

- **范围**：只对 `input_policy=preserve_originals` 的包、在钉住它的任务里生效。用户要求“改这个文件”的普通任务不受影响。
- **记基线**：第一次成功读取一个任务开始前就存在的工作区文件时，记 `{path, sha256, size}`。不超过 1 MB 的文件同时存一份原件副本：放在本 owner 的任务归档里，权限 600，每个任务合计不超过 16 MB，跟着现有归档的保留期走。每个任务最多 64 个基线文件。
- **比对**：收尾时重新算哈希，变了就记 `INPUT_MODIFIED_IN_PLACE`（路径和前后哈希），不管是哪个工具改的。
- **返工**：提示 1 次（恢复原件，另存新文件，告诉模型原件副本在哪），之后照常结束并带收尾说明。

## 5. 交付物存在（块 5）

- **判定**：声明了 `required` 的交付物，收尾时本回合没写出任何匹配文件，记 `DELIVERABLE_MISSING`；有匹配文件但打不开或解析不了，记 `DELIVERABLE_UNREADABLE`。都是客观事实。
- **返工**：沿用子代理交付闸的做法，最多返工 2 次，然后照常结束并带收尾说明，不改结束原因。

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
| 2 | 检查程序运行器（复用 `AttemptExecutionSandbox`，补通用断网选项） | 待做 |
| 3 | 写完就查、收尾检查、返工、`HostNotice` 和 `channel_delivery` 事实、开关 | 待做 |
| 4 | 输入基线、原件副本、收尾比对、返工 | 待做 |
| 5 | 交付存在和返工 | 待做 |
| 6 | 变异、门禁、文档收口 | 待做 |
| 7 | A 0.5.0、B 0.3.0（be） | 待做 |
| 8 | 重跑和独立审阅 | 待做 |
