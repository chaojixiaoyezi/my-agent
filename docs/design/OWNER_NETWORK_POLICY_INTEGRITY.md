# Owner 网络策略完整性

状态：安全缺陷修复候选，等待主线组合验收和发行；本线不提交、打包或部署。

## 已确认问题

原 `permissions.json` 读取失败返回空 payload，网络字段又缺省为开启。普通 owner 的合法禁网记录被破坏后，重建 Agent 会变成有网；正式 Registry/Executor 的合成云文档读取由零请求变为三次 HTTP。

另一个独立入口使用 Agent 创建时的 `owner_policy` 缓存。真实 macOS Seatbelt、原 Registry/Executor 与原 child 执行上下文证明：同一个活实例的有效网络策略从 true 改为坏 JSON 或合法 false 后，主代理与孩子仍各能向随机本地测试端口发一次请求。不能把 child 投影过时当作仅展示问题。

## 唯一事实源和修复

`user_space/owner_policy.py` 沿原权限文件与 `OwnerPolicyBundleReport` 处理：

- 真正不存在保持原首次初始化/旧 owner bootstrap 行为，不由本 helper 写入新授权。
- 已存在的权限必须是普通文件、`permissions.v1`、network 对象且 `enabled` 为严格 bool；坏 JSON、非法根/schema、整数或字符串开关、读取失败、目录和符号链接均关闭网络。
- 原读取原因字段保留，并附 `OWNER_PERMISSIONS_INVALID` 或 `OWNER_PERMISSIONS_UNAVAILABLE`；不可自动重试扩大权限。宿主应修复原文件，不覆盖损坏原件。
- 原父级上界继续取交集，未改变文件、审批、工具、配额、记忆或公共包授权语义。云文档和 watch 网络门继续读取同一 owner policy。

`agent_core/tool_runtime_ledger.py::_attach_owner_network_scope` 在每次原工具入口读取当前 owner policy，并和创建期 owner 权限、冻结 child 权限以及原调用边界取交集。原创建快照仍是权限上界，不冒充实时展示；旧实例原为 false 时，不因文件改为 true 自动扩大本实例或孩子权限。需要宿主正常重建新的有效实例，旧孩子的 false 上界仍保留。

真实 owner 缺少 home 或读取异常关闭网络；原独立无 owner 的库上下文只保留其既有明确上界。模型工具参数、临时审批、角色正文或 full-access 不能替代权限文件。

这是安全 bug fix，不新增配置开关、授权账、持久缓存或模型请求。

## 验收与证据

- 原主链 TDD：`/tmp/owner-network-integrity-TDD-red.xml`，七项先红；既存坏文件被保留。
- 原活实例 OS/Executor TDD：`/tmp/owner-network-live-executor-TDD-red.xml`，四项先红、七项通过；main/child/孙/background 调用在坏记录后真实请求到达。
- 最终同源 11 文件组合：`/tmp/owner-network-integrity-final.xml`，199 passed；包含完整权限配置、云 HTTP、子代理家目录、运行账、scope 与自动委派旧行为。不累加上一轮同为 199 的结果。
- Linux 使用独立源码候选与原 `.8` bundle 的 Python，实际 bwrap/Registry/Executor。最终 18 行矩阵：四类调用的合法 true 和修好后的原 true 上界每次恰好一请求；坏 JSON 与合法 false 每次零请求。额外旧 false 实例和恢复后的 false child 均保持零请求。仅合成随机本地端口，无模型、IM、真实凭据或生产状态。
- 候选源码归档摘要 `88e16690b76df83acc2330e73fd5a920b6e689ce96697187b19126e306a22829`；独立测试根的 `owner-network-integrity-final/report.json` 保存实际 18 行及两处生产文件摘要。不是旧 `.8` 包已修复或最终发行验证。
- 全源 Ruff、doc sync、diff check 通过；strict code-size hard=0，`/tmp/owner-network-integrity-code-size.md`。主线组合 gate 仍须覆盖其它并行修复。

## 明确边界

原 bootstrap 的真实缺失缺省为网络开启。本次没有证明删掉既存文件安全：普通模型不能写删受管策略；宿主自行删除策略仍可进入原初始化规则。不要把删除文件当作修复禁网的方法。

本修复收口下一次工具调用及新进程的权限编译，不声称即时撤除已经获准运行的长进程或既有 PTY 的 OS 网络 namespace。既有进程生命周期和停止仍沿原合同，未擅自终止监控。

Shell 的禁网由原 OS sandbox 执行；尝试联网可能返回 `COMMAND_FAILED`，判定依据是实际禁网边界与零请求，不根据命令输出文案猜授权。云工具沿原 `NETWORK_DISABLED` 门在 HTTP 前拒绝。

已确认 permissions 的网络 fail-open；没有把仅静态怀疑的 disabled-tool 损坏提升当作已复现漏洞。`ingestion/source_http` 的宿主 SecretRef 外泄属于另一修复线，不能用此网络策略修复或 ENV 擦洗宣称它已解决。

建议下一步：主线组合验收后做最终无源码包验证；独立 agent 可并行只读复核权限文件完整性和旧快照上界。保持原 bootstrap、子代理权限封顶和运行中进程边界，不用测试替身绕过真实执行门。
