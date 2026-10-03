# 能力包 v2：宿主核验交付物、保护输入原件、要求交付（第 10 条选 A + 第 11 条）

- **状态：已确认设计、实施中**（2026-10-03）。块 1–5、7 已上线（main）；块 6a（`/stop` 打断宿主核验）和 P1–P3（写后检查反馈修正）已并入 step17i；块 6b（写后检查复用工作区扫描）在做，6c（文档与尾巴）由 `worker/pack-6c-wrapup` 收口；块 8 全量重跑进行中。块 2 内把检查程序的 `baseline` 推广成 `inputs`（见第 2 节），由 ae 定、待 3a 审。进度见第 7 节。
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
    - 9b 复核加固：片段前只要不是词字符、点或连字符就算分隔（`file:///Users/…`、`a;/Users/…`、`at@/Users/…`、`a&/Users/…`、`#/Users/…` 都算；词字符按 Unicode，`交付/tmp/x.json` 这类中文目录名不误伤），另认 `..`（`../../../../Users/…`），判断前先做一次 URL 解码（`%2FUsers%2F…` 也算）。`..` 通过零宽定宽后查作为边界，不消费前段路径，也不依赖 `/..` 在根目录下存在。`out/tmp/x.json`、`docs/Users/x.md`、`./out/d.json`、`out/d.json:12:3`、`https://example.com/a/b` 这类写法原样保留。
    - 防护范围：防的是 `sys.argv`、`__file__`、异常信息这类无意带出的宿主路径。故意编码（两次 %-编码如 `%252FUsers`、插 `%00`、base64、`~user/`、反斜杠写法、`vscode://file/Users` 这类）不在范围内：检查程序存心外传，换什么编码都挡不住，真正的信任边界是“能力包由管理员审过才装”。
    - 代价：JSON Pointer 的首段恰好是本机根目录名（`/home`、`/Users`、`/tmp`）时，整条置成 `<redacted>`，只丢定位、不泄露。
    - 已知代价（9b 复核，6c 核实维持）：中文直接贴着宿主路径、中间没有分隔符时不置空，例如“找不到/Users/me/x.json”。这和“`交付/tmp/x.json` 不被误伤”是同一件事的两面，正则区分不了；中文冒号、空格、括号隔开的照样置空，例如“文件不存在：/Users/me/x”。6c 实测补充：等价的 lookbehind 改写（零宽反向断言）不改变行为；任何能修好这个漏检的边界放宽（把中文也算分隔）都会把“中文目录名相对路径”（`交付/tmp/x.json`、`交付/Users/x.json`）误伤成 `<redacted>`——属于取舍变更而不是安全修复，维持现状；现状由 `test_redact_location_word_glued_paths_stay_a_known_cost` 锁定。
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
  - 原因码：`verifier_timeout`、`verifier_cancelled`、`verifier_output_invalid`、`sandbox_unavailable`、`verifier_member_mismatch`、`verifier_consent_missing`、`unsupported_runtime`、`verifier_input_unresolved`。
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
  - `task_input`：从任务的原件清单里找（块 4）。原件还是原样就交工作区里的文件；被改过但有可用副本就交副本；都不行就算找不到。
  - `turn_output`：本回合新建或内容变了的文件；
  - 两种都排除 target 本身，再按路径模式和字段匹配过滤，恰好一个才交给检查程序；
  - 每个参数名的匹配个数入账。
  - 原件清单一个任务只记一次，后一回合写出的文件不会被当成“开始时已有”。
- **写完就查**：
  - `write_file`、`edit_file`、`apply_patch` 成功后，从回执的宿主字段取写出的绝对路径（`artifact_refs` 里标 deleted 的旧地址不算），一次最多 4 个文件；
  - 对符合钉住包交付物声明的文件，跑 `applies_to` 指向它的检查程序；
  - 回执 `handler_details.pack_verification` 附有界摘要：状态、错误数和警告数、前 5 个错误码和警告码、检查结果里保存的全部错误样例（最多 10 条），错误比样例多时 `error_samples_truncated=true`（块 8 试点后改，原来只带前 3 条）；
  - 模型在 `[pack-verification]` 段看到这份摘要，归档白名单也收这个键，续跑重渲染时还在。这段文字软提示“把这次列出的错误在下一次写入里一起改完”，宿主不据此做任何判断。
  - 写后反馈上限（块 8 试点后加）：同一个检查对象（包、检查程序、目标）写后检查连续失败 6 次（`MAX_POST_WRITE_CONSECUTIVE_FAILURES_COUNT`）后，后面的写后检查不再跑，只记一条 `not_run`、原因码 `post_write_feedback_limit`（复用键留空，收尾不会复用它），交给收尾检查那一次返工。中间通过一次就重新数；收尾检查的失败、别的目标的失败都不算。
    本 run 已取消时取消优先：不记暂停行，记 `cancelled/verifier_cancelled`（块 6a 合同）。
  - 起因：块 8 试点 A05 同一文件写后失败 20 次才通过——回执只带前 3 条样例、合同不带数字、模型每次只改一处，每次都多一轮完整上下文（`decision-evidence/capability-packs-v2-b8/pilot-ede890374/A05-post-write-analysis.md`）。
- **收尾再查**：
  - 挂在 `_no_tool_calls_decision`，子代理交付闸之后，复用同一个返工通道；
  - 目标 = 和基线比，本回合新建或改过的匹配交付物，shell 写的也算，最多 8 个，超出记 `truncated`；
  - 包、检查程序、目标和各输入的内容都没变时，复用写后结果，不重跑；
  - 有 `failed` 时给一次返工提示，列出目标、包、检查程序、错误数和“错误码 @ 位置”，最多 5 个目标、每个 10 条，错误更多时写明另有几条没列出；
  - 返工先记账再发，记不进账就不返工，避免无界循环。
- **唯一账本**：`<规范任务根>/data/pack_verification/<run>.jsonl`，文件权限 600，跟着任务归档的保留期走。
  - 规范任务根是 `conversation/workspace_paths.canonical_task_root` 认的 `runs/<日期>/<键>`、`tasks/<日期>/<名>`、`audits/<编号>`。子代理 run 的工作目录在 `<任务根>/work/agents/<run>` 下，往上找到规范任务根再落盘，不会落进模型可写的 `work/`；不在规范任务根下就不核验。
  - `data/pack_verification/` 整个目录由宿主托管，经 be 的 H3（`HOST_STATE_TASK_PARTS`）对模型只读，读照常；块 4 的原件清单和副本也在这里。
  - 记录分八种：baseline、written、result、closeout、rework，以及块 4 的 input_check、input_rework（另有任务级的 originals.json）。
  - 返工次数和结果复用都只读这本账，Compact 续跑、进程重启后不会重置。
  - `runtime_events` 记 `pack_verification_completed`，只是可观测投影，不做决定。
  - 开关关着时，最终结果阶段直接返回、不去读账本，每轮收尾零 I/O。
- **对外**：
  - `AgentRunResult.pack_verifications`（`pack_verifications.v1`）→ `channel_delivery.pack_verifications`，不进公开投影白名单；
  - 回合正常返回后，Gateway 用这些事实写一条 `HostNotice`（`source=pack_verification`，`code=summary`），排入原提示队列，和当轮其它提示一起发布。检查结果、被就地改的输入原件、缺的必需交付物三类事实有任一项就发（块 4/5 起，只有后两类、没有检查结果的回合也发）。
  - 收尾跑过时，只报最后一次收尾检查覆盖的结果，也就是交付时的内容状态。
  - 没跑过收尾（工具轮次上限、失败、中断）时，报每个目标最后一次写入时的真实检查结果（写后反馈暂停行不算），并注明“没有正常收尾”。
### 3.2 块 6a：取消宿主核验（2026-10-03，sol1 实现，ae 整合到块 4/5 之上，9b 复核通过，并入 step17i）

- **来源**：3a 根据 ae 的同步核验调研派工；本片不改变包声明、同意、沙箱权限和默认关闭开关。
- **启动前**：写后/收尾钩子在 executor 令牌范围之外，显式绑定本 run 的 `params.cancellation_token`；每次检查前读 `cancellation_requested()`。已取消不进运行器、不启动新程序，剩余适用目标逐项写 `status=cancelled`、`reason_code=verifier_cancelled`。
- **运行中**：`AttemptExecutionSandbox.run` 复用新 `attempt/process_run.py` 的通用回收，给本次独立会话登记临时取消回调，按固定组号 TERM→宽限→KILL。
  - 宽限期内每 0.05 秒探测一次组是否已全部退出（9b 复核要求），收到 TERM 就退出的命令不会让 `/stop` 等满宽限期；组里还有忽略 TERM 的后代时才等满宽限再 KILL，不是只杀组长。
  - 无回调的 external_check 按 0.1 秒观察片检查；退出撤销回调、reap 组长并释放捕获输出管道。不查询其它任务或宿主进程表。
  - 组信号被拒（PermissionError）和回收后管道迟迟不 EOF 沿用改造前的合同：不抛给调用方，补杀组长、有界 reap，超时仍返回 143。
- **事实/返工**：运行器把共用 `ToolCancelled` 映射成 cancelled/verifier_cancelled；取消优先于超时、缓存质量结果和返工，取消结果不能当有效检查复用。
  - 收尾三段（输入原件、交付物、检查结果）被取消时照样记事实，但哪一段都不记返工、不出返工提示；`closeout` 记 `cancelled`。
  - `_no_tool_calls_decision` 复用既有 conversation_control 取消终态，不翻转已成功写工具的成败。
  - 最终事实在“检查结果、被改的输入原件、缺的交付物、被取消”四样都没有时才不出现；宿主提示同样四样任一为真就发，开头写明“被取消”。
- **证据**：命令和结果见 TESTS.md“能力包 v2 块 6a”。macOS 真实 Seatbelt 和 Linux 真实 bwrap（Docker 车道）的进程组取消用例都通过；bwrap 下 TERM 打掉外层后内核回收整个 PID 命名空间，不需要再发 KILL。
- **已知边界**：目标扫描和检查上限不变（收尾最多 8 个，超出仍标 truncated）；回合未到收尾入口（例如工具轮次上限）仍不做收尾检查。实际 TUI/IM `/stop`、生产部署及 v2 冻结业务重跑未验证；块 6 的其它工作不因本片而完成。

## 4. 输入保护（块 4，已实现）

- **范围**：只对本任务钉住、声明了 `input_policy=preserve_originals` 的包生效。用户要求“改这个文件”的普通任务不受影响。
- **原件清单**（3a 同意的偏离：原设计是“第一次读取时记”，它抓不到没读就改的文件，也分不清文件是不是本任务前一回合写的）：
  - 任务第一次改工作区前（复用块 3 的基线时机和同一次扫描），把已启用、声明了核验的包的声明（交付物和检查程序输入）匹配到的文件记成原件；
  - 每个原件记 `{path, sha256, size, copied, packages}`，其中 packages 是匹配到的包 ID；
  - 一个任务只记一次，清单是 `<规范任务根>/data/pack_verification/originals.json`；
  - 最多 64 个文件；不超过 1 MB 的文件另存一份副本到同目录的 `originals/<sha256>`，权限 600，每个任务合计不超过 16 MB；超出的只记摘要。
- **副本放哪**（3a 同意的偏离）：放在宿主托管目录 `data/pack_verification/` 下，对模型只读、读照常（H3）。
  - 副本每次使用前按原件摘要核对，被改过就当作没有。
  - 判定“有没有就地改”比的是清单里的摘要，不是副本，所以篡改副本也不能让检查通过。
- **比对**：收尾时，原件在本回合被改或被删就记 `INPUT_MODIFIED_IN_PLACE`，带路径、原件摘要、现在的摘要和副本位置，不管是哪个工具改的。
  - “本回合”的判断：本回合开始时的摘要和现在不同，并且现在也不是原样。
  - 本回合开始时就不在的原件不判，免得把旧回合的事算到本回合头上。
- **返工**：提示 1 次。列出原件路径和副本位置，建议用 `cp` 把副本覆盖回原路径（字节要完全一致，手抄做不到），改动另存新文件。
  - 和交付物返工各自计数；同时出现时合成一条提示，原件在前。
  - 之后照常结束，最终事实带 `inputs_modified`、`input_rework_count`，宿主提示写明“任务开始时的输入 X 被就地改了”。
  - 返工后模型已按原件恢复时（最后一次收尾查到原样），最终的 `inputs_modified` 为空，宿主提示不再提“被改”：提示只说交付时还要用户处理什么，过程记在 `input_rework_count` 里（3a 2026-10-03 定，刻意如此；17h 冒烟第 4 项实测：模型照提示 cp 还原，sha256 与开始时逐字节一致）。
- **只读保护依赖 be 的 H3**（块 4、块 5 已变基到 `claude/3a-step17h` `7e421024f`，含 be 的 H3 全部提交；回归用例 `test_pack_verification_protection.py`）：
  - 文件工具和写边界按路径判，所有任务都拒写；
  - Shell 只保护本次命令的工作目录和写根所在任务的 `data/pack_verification/`。Full Access 下别的任务的目录挡不住，这是 H3 写明的已知边界。
  - 9b 的开关前提 (b) 已有用例：主代理和子代理 × 文件工具和真实 Shell、Full Access 和隔离两种模式，追加、覆盖、新建、改写、删除、整个目录改名都写不进，读照常。
  - 账本目录在第一个 Shell 命令之前就已建好（run_command 的运行策略声明会改工作区，执行前先记基线），Linux bwrap 只能只读挂载已存在的路径。
  - 原先的结构缺口（ae 报，3a 定为必须修，be 已修）：Full Access 下命令的工作目录和写根都在任务树外时，旧版 H3 找不到当前任务根。现在本任务的核验记录只从写边界里宿主写入的结构化 `task_root` 推出，和工作目录、写根无关；macOS 另用一条正则盖住所有任务的核验目录。用例去掉了 strict xfail，Full Access 的 Shell 用例按生产路径经 registry 投影组参数。

## 5. 交付物存在（块 5，已实现：`capability/pack_verification_deliverables.py`）

- **触发条件**（3a 定）：只在“本回合改过工作区”时才查，也就是本回合有块 3 的基线（至少执行过一次写工具，或者运行策略声明了 `mutates_workspace` 的工具，比如 shell）。
  - 纯问答回合、只读审稿回合都不触发。块 7 复审时担心的“只审不改的任务会多出两次返工”，因此基本消除。
  - **已知限制**：模型一个工具都不调、直接在答复里贴内容的回合查不到；审阅时一眼能看出来，冻结用例里也没出现过。
- **判定**：只看本回合确定新建或改过的文件。基线截断时状态“不确定”的老文件不算交付证据。
  - 声明了 `required` 的交付物，路径加字段都匹配上的文件一个都没有：如果有路径匹配、但按声明格式打不开的（json 解析不了），记 `DELIVERABLE_UNREADABLE` 并列出这些文件；否则记 `DELIVERABLE_MISSING`。
  - 路径匹配、能打开、但字段不匹配的文件，不算这个交付物。
  - 都是客观事实，事实里带包 ID、交付物编号、路径模式和字段要求。
- **返工**：沿用子代理交付闸的上限，最多 2 次，先记账再发，记账失败就不返工；之后照常结束，宿主提示写明。
  - 提示末尾写“如果用户这次只要审阅、不要交付物，在答复里说明即可”。
  - 收尾三段的顺序：输入原件 → 交付物存在 → 交付物检查，同时出现就合成一条提示，三段各自计返工次数。
- **最终事实**：`deliverables_missing`（最后一次收尾的检查结果）、`deliverable_rework_count`。
- **只审不交付的任务**（ae 定，块 7 复审时）：两个包的交付物保持 `required`。B05-t5 那次没交付就是靠这条兜住的；按“任务意图”区分要读懂用户的话，铁律不允许。有了上面的触发条件，只读审稿回合本来就不会触发。

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
| 1 | `verification` 声明与安装校验；启用前执行确认；同意摘要写入内容激活 | 已上线（main；`test_capability_verification_declaration.py`） |
| 2 | 检查程序运行器（复用 `AttemptExecutionSandbox`，补通用断网选项与断网就绪探测）；`baseline` 推广成 `inputs` | 已上线（main；`capability/pack_verifier_runner.py`，`test_pack_verifier_runner.py`，macOS 与 Linux 车道实测） |
| 3 | 写完就查、收尾检查、返工、`HostNotice` 和 `channel_delivery` 事实、开关 | 已上线（main；`capability/pack_verification_*.py`，`test_pack_verification_service.py`、`test_pack_verification_matching.py`） |
| 4 | 输入原件清单、原件副本、收尾比对、返工；宿主托管文件统一落到规范任务根（只读保护依赖 be 的 H3） | 已上线（main；`capability/pack_verification_originals.py`、`pack_verification_inputs.py`，`test_pack_verification_inputs.py`） |
| 5 | 交付存在和返工（只在本回合改过工作区时查） | 已上线（main；`capability/pack_verification_deliverables.py`，`test_pack_verification_deliverables.py`） |
| 6 | 变异、门禁、文档收口 | 6a 已并入 step17i；6b 在做；6c 由 `worker/pack-6c-wrapup` 收口 |
| 6a | `/stop` 打断宿主核验：取消后不再起新检查、正在跑的整组回收、剩余目标逐项记 cancelled、被取消的回合不返工 | 已并入 step17i（sol1 实现，ae 整合到块 4/5 之上并补三处取消事实、宽限期轮询：`attempt/process_run.py`，`test_pack_verification_cancellation.py`），9b 复核通过 |
| 6b | 写后检查复用工作区扫描（基线 + written 记录 + 候选文件现状，不再整盘重扫） | 在做 |
| 6c | 收口：用户说明与设计稿更新、脱敏边界核实、块 4 遗留 xfail 核查 | 本分支 `worker/pack-6c-wrapup`（待复审） |
| 7 | A 0.5.0、B 0.3.0（be） | 已上线（main） |
| 8 | 重跑和独立审阅 | 全量重跑进行中 |
| P1–P3 | 写后检查反馈修正（回执列全错误、软提示一次改完、连续失败 6 次暂停写后检查） | 已并入 step17i |
