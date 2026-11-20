# 显式宿主命令的执行身份

状态：第 4 步开发中。本地源码已接原运行登记、执行收口、管理授权、HTTP/direct 提交和原请求查询；尚未发布部署。
安装、配置、启停释放、卸载与明确业务调用均已本地接通；TUI/Gateway 的命令交互审批运输已有本地实现，实际验收未完成。

## 解决问题

跨 UI、Gateway 和工具执行的进程内取消令牌统一归 `agent/common/cancellation.py`。
迁移保留原 ContextVar、异常类型、回调与绑定语义，全部消费者直接引用同一实现；旧 tooling 入口删除，不留转发。
它只表示本次调用取消，不拥有 Goal 状态、持久任务权限或 OS 资源清理；这些仍由原控制合同裁决。

插件管理不需要模型决定是否调用工具，但有副作用的操作仍须进入原执行器和操作账本。
重复请求、回包丢失和宿主线程退出都不能导致重复安装，也不能借正在运行的业务任务身份执行管理动作。

## 请求与运行

原请求记录 v2 的 `context_digest` 只保存宿主所选目录版本、命令输入、工作目录和权限的规范摘要；
工具参数摘要仍单独对应实际 ToolCall，不把控制字段送入插件。原 v1 按严格字段显式读取为空上下文摘要，
请求索引的身份算法保持 v1，旧请求不会因协议升级换 operation。上下文已变化的同编号提交拒绝，查询仍只依赖可信身份。
调用方必须在准备固定 handler 时复核原选择；目录版本已经绑定完整安装、激活及 schema，不能在等待后换成新选择。

需要审批的宿主命令在原 attempt_executor 区间内通过原 ToolApprovalRequest/Decision 等待，只允许本次批准。
批准后将精确 binding 交回同一 ToolExecutorRequest，再检查权限、取消及固定插件激活；无消费者、拒绝或取消明确未启动。
它不使用模型轮或第二套操作账，也不继承缺少 activation 作用域的会话批准缓存。
本片接线约定：每次 TUI Enter 固定命令编号、独立取消令牌及审批消费者，沿原 FIFO 排队，不创建聊天回合。
同机 Gateway TUI 在原 HTTP 请求线程执行，JSONL 只运输心跳、原审批请求及原结果；连接关闭仅取消本请求。
审批复用原 StreamApproval 与文件桥，缓存为空；路径由原 GatewayPaths、可信 owner 和命令编号派生，HTTP 不接收路径。
连接首帧给出服务端认证与 owner 映射后的规范身份；客户端只能在自己原服务根内派生哈希桶，不按本地 owner 配置猜地址。
全部帧核原命令编号；无连接首帧、重复连接声明、坏消息及过大消息都关闭运输，原结果仍可按编号查询。
客户端持续读流，独立等待 UI 决定；退出关闭本命令的面板和等待，不触碰其它命令或主子任务。
plain 无交互消费者时仍明确返回需要审批；本片不提供跨机器共享文件审批，也不把断连或清理失败当成未执行。
原始命令文本属于请求输入摘要的一部分，同编号改动命令即拒绝；业务作用域变化后仍可按可信身份只读查询原结果。
旧 v1 管理请求显式补零上下文，只读时不重写旧记录；新增上下文不能附到旧请求上以改变执行选择。

- 入口先验证管理授权，再由宿主绑定 owner、操作者、通道、线程；网络可信来源不等于操作授权。
- 请求包含稳定 request_id、命令名和规范输入摘要。摘要须覆盖参数、目录版本、来源及授权相关选择；不存密钥或原始请求正文。
- 唯一范围是 owner、操作者、通道、线程、request_id；同范围改变命令或输入摘要拒绝，不建立第二棵运行树。
- 在原 RuntimeDB 的 `BEGIN IMMEDIATE` 内查找或登记一个独立 Task、TaskRun、角色为 `host_command` 的 AgentRun 和 pending attempt。实体 ID 仍由原生成器产生。
- 冻结请求只存于 `task_runs.metadata_json.host_command`。原 `runtime_events` 的 `host_command.registered` 事件以请求范围摘要作为唯一 event_id，索引原运行与首次 attempt；不增加操作历史表或结果副本。
- 读取须联查原 Task、TaskRun、AgentRun、首次 attempt，验证全部冻结身份。现存索引的断链或身份冲突不得当作新请求。事件索引不授予工具执行权。
- 去重依赖原事件表 append-only 保留；不清理请求索引，不承诺检测手工删除整条索引或任意篡改数据库。
- 实际启动使用原 `create_attempt` 的精确 pending 激活，随后 `attempt_executor` 登记真实线程区间。重送不能换代，也不启动模型、会话 wake 或子代理投影。
- `host_command` 角色名本身不是调度屏障；管理运行不能创建普通 ConversationTaskLink、`work/state.json` 或 wake，否则可能被普通未完成任务扫描拾起。

## 操作与结果

本地收尾修复（待发布验收）：执行者在原 `attempt_executor` 内注册本次资源的释放回调，
prepare、审批、执行任一出口先完成释放调用，再登记 executor 退出和运行终态；未启动拒绝也在释放后结算。
重复请求没有领取成功，不注册或调用释放回调；清理期间查询可读已经确定的业务结果，但 `finalization_pending` 仍为真。
插件清理未确认保留独立回执及原资源门，不能据业务成功假定资源退出；不新增持久状态或自动重试。
实际目录分页暴露的窗口是持久任务先结束、连接后释放，UI/HTTP 最终回包本来就在释放之后，不能混淆。

- 工具参数、权限、资源范围及执行继续走原 ToolExecutor / ManagedOperationStore。管理角色不是管理员权限，包文件读取也要单独验证。
- 终态请求从原工具操作行只读重放；核对 owner、task、run、attempt、工具名和参数摘要。不能给终态 attempt 重开执行权来获取旧结果。
- 公开查询的工具名来自原请求，正文来自原持久结果；UNKNOWN 不投影未经确认的正文，不另存一份业务结果。
- 原 Repository 取消尚未开始的 `CLAIMED` 占位时，保留输入指纹、幂等身份、持有者、代次及资源声明，只更新规范取消结果和时间。不得因取消覆盖原领取事实，导致精确回读失效。
- 取消执行权与结果可读性分别裁决：原关系型字段证明从未启动时仍可取消；损坏 JSON、非对象或未知结果版本保持原字节，查询明确拒绝，不伪造已修复回执。空对象占位沿原协议可取消，但不补造输入指纹。
- 原操作已持久成功或失败后，才能按原 API 收口 AgentRun 与 TaskRun。任务身份本身不携带本次执行状态。
- 未决副作用保持 UNKNOWN 和原锁；Gateway 活着不代表该命令执行线程仍活着。不按超时、心跳年龄或客户端断连猜测未执行。
- UNKNOWN 后的对账只能领取原操作的核对权，以精确提交回执证明结果，不重新调用 handler；恢复仍遵守原显式授权合同。
- HTTP/direct 的管理授权、来源读取及结果查询已在本地接线；自动对账尚未开放，未知操作不能因重送而再次执行。

## 管理执行接线（本地源码）

显式提交使用稳定 request_id；查询按同一可信 owner/操作者/通道/会话和 request_id 查原绑定，不要求重新读取来源文件。
同请求的命令和输入摘要仍须完全一致；只读查询不激活 pending、不换代、不对账或重跑。网络异常返回结果未知及原查询标识，不宣称未执行。
只有精确原 pending 的 CAS 激活成功者进入原 attempt_executor 和 ToolExecutor。原操作确定终态后才关闭原 AgentRun/TaskRun。
执行前被原工具策略拒绝且无 operation 时，原 agent_run.completed 事件保存唯一未启动回执；需要审批与拒绝分别记录。
存在 operation 时只读该行，不在事件复制结果。执行器退出但原副作用未决时保留 UNKNOWN 与原锁，自动对账仍未开放。

`host_command_execution.py` 强制使用原 ManagedOperationStore 及登记时的 task/run/attempt，不信任适配器另传的操作 Store。
显式宿主执行仅免除模型可见性要求；模型调用仍默认检查可见性，参数、快照、路径、禁用、取消和持久门顺序不变。
规范参数补全后再次核对原摘要，失配在领取操作前拒绝，不能以原输入登记另一个实际动作。
确定工具结果与运行收尾分开：后者失败返回 `finalization_pending`，显式原请求重送只补原运行收口，不重进 handler。
查询使用只读数据库连接，不初始化或迁移 schema，不创建线程、运行、锁或重试记录。

Gateway 复用原 auth 管理员裁决；没有中间件不能推断管理员。只有配置明确关闭认证且监听和对端均为本机，才沿原本机身份授权。
正文不提供可信操作者或 owner；direct 入口沿原本机 owner 权限。模型配置与插件入口共用 `owner_conversation_store.py`，旧私有组装函数移除。
`enable_plugins` 默认 true，允许显式安装/配置/停用管理，不自动加载插件；false 拒绝新管理执行，但原管理员仍可通过 `/plugins status <请求编号>` 只读查询。
开关经原配置布尔归一化，带引号的 false 也必须关闭；`enable_tools` 和 owner 禁用工具策略继续约束安装。
每次明确提交生成请求编号，传输超时或身份变化只返回未知及原查询编号，不自动重送，不回退本地执行。
目录刷新失败不抹掉已读取的原结果；查询不要求旧来源文件、可用目录或当前 revision。

管理授权与读取包文件的授权分开；读取固定路径的单次字节快照，后续不重新打开来源。严格入口要求逐段 dirfd/no-follow；
不具备该能力的平台明确拒绝安装，普通核心功能继续使用既有文件原语。非阻塞打开避免校验后被换成 FIFO 而卡住宿主。

`plugin_configure` 与安装分别服从原 owner 工具禁用策略，共用宿主请求、执行器及操作账。
`plugin_disable` 同样是模型不可见的原管理工具；读取完整旧目录与安装版本，旧请求重送仅查原账。
内部资源控制可按可信 owner 与持久 operation 引用反查原首次管理绑定，严格验证冻结请求、链和原资源集合。
该反查不接受模型 owner，不创建运行、不跟随 current attempt；源列表顺序和重复项不改变既有资源集合身份。
完整私有配置只从授权文件读取，不放进 ToolCall 或结果；原请求输入绑定文件引用、目标插件、目录版本和权限摘要。
handler 构造使用同一次安装快照的目录及目标，写入时原安装表再做 revision CAS；重复请求只读原操作结果。

## 参考与验证

定向阅读本地 Codex `578c1b2` 的 `app-server/src/request_processors/plugins.rs`：安装是宿主结构化请求，经配置与权限检查后调用插件管理器，不需要制造模型轮。
本仓库进一步沿现有持久 run/attempt、操作账本和提交回执接入，不照搬自动启用或 OAuth 行为。
本片不修改 auth 登录，也不增加新的执行器、调度队列或常驻线程。

开发验证使用临时 RuntimeDB，覆盖并发重复请求、参数冲突、跨身份、事务回滚、损坏原链、终态只读结果、精确激活和 UNKNOWN 不重跑。
请求登记片 18 个相关文件 429 项通过，包含原主/子创建链及旧工具幂等路径；重放的执行标记、核心字段、规范 effect 和嵌套结果独立复制已做检查。
开发测试不计作真实验收；完整插件装卸仍须在多个实际 TUI 与核心任务并行验证。

取消记录修复是 Repository 合法持久状态的开发验证；当前 ManagedStore 的领取直接进入 `EXECUTING`，并非此次复现了新的 TUI 资源漏停。
该片 12 个相关文件 360 项通过，涵盖三个收口入口、重复取消、精确作用域、损坏结果和新旧查询；尚未发布部署。

## 环境准备的精确资源检查（本地开发）

插件环境计划由宿主 prepare 冻结到原 policy 的单个 logical scope，原执行器完成 claim 后才允许创建候选。
准备器冻结一次原 claim，ManagedOperationStore.require_authority 的显式 claim/scopes 分支核对当前操作、代数、epoch 和锁；
默认分支继续支持 handler 前尚未 claim 的检查。PID 与退出回执沿原 ProcessSessionStore，不写进 operation holder 或新建进程表。
同步等待继续检查原操作，交接后的寿命通过原 host 的明确参数控制。原请求 UNKNOWN 或终态不重新准备；此接线不代表完整插件启用已开放。

## 2026-11-19 M04：命令流终帧单写者、嵌套脚本内因与启动身份、索引截短再核对（Conveyor）

本节记录 Conveyor M04（Astra-R3 L21、L22、L24、L26；Verdict 3B.1 D08.7、D10.19；CI 行 5、17、23、27、28）建立的不变量与验收证据。基线 `d9caa95af`，父提交 `7e94753e8`（M03）。只加附加字段和两条错误分类登记，外码与存储格式不变，`~/.my-agent` 无需迁移，不加配置键。

**不变量**

- 命令流单写者（L24）：`gateway_parts/command_stream.py` 的 `_CommandStream.publish` 是本连接唯一写入口。它在本连接锁内先裁决已断连、已提交终帧，再编码写出；`final=True` 的终帧与 `finished` 在同一临界区提交。心跳走同一入口，终帧取得写权后才到期的心跳只会被拒绝，终帧之后不再产出任何帧。这把锁只属于本连接，不是全局锁，写入受原 socket 超时约束；终帧写失败只标记断连，不补发第二个终帧。
- 嵌套脚本内因（L22）：bash/sh 固化脚本缺系统沙箱时，`workflows/script_runner.py` 仍抛外码 `SANDBOX_UNAVAILABLE`，并在异常上附 `inner_code=NESTED_SHELL_SANDBOX_UNAVAILABLE`、`stage=sandbox_prepare`、`effect_outcome=not_started` 与 `next_action`。`classify_credential_exception` 只读这些结构化属性，不解析消息；产出方未挂内因时记 `unknown`。不自动放宽执行环境。
- 启动身份（D08.7）：`run_script` 每次调用在第一道启动前闸门之前取一个 `launch_id`（`uuid4().hex`），可重现的 `run_id` 不作替代。启动前拒绝和其它离开 `run_script` 的异常、结果上的效果回执（`effect_outcome`）、凭据回收回执（`credential_execution` 的进程树回收、临时目录清理、FD 关闭）带同一个值。`tooling/scripts_tool.py` 在 run 的每个分支原样复制；只有 runner 从未进入（如凭据授权在进入前被拒）时为 `""`。同一逻辑任务重试得到新值。
- 索引完成证明绑定文件身份（L26、D10.19）：`reconcile_home_index_page` 在推进游标或阶段前，用已持有的 FD 再 `fstat`，比较 `(st_dev, st_ino)` 与本页冻结身份、实际长度与冻结 `end`。截短或身份不符走原 `INDEX_SOURCE_CHANGED`：回偏移 0，重新打开文件幂等重读，不读旧缓冲。Python 3.14 的 `io.DEFAULT_BUFFER_SIZE` 为 128 KiB（3.12 为 8 KiB），会把整页预取进缓冲，此前同 inode 截短碰不到真实 EOF。
- 分类登记（L21；C23）：`contracts/error_taxonomy.py` 新增两条。`GOAL_CONTINUATION_WITHHELD` 为 state、不可重试、manual_review，继续条件是 `/goal resume` 或同会话直接回复，也可 `/goal pause`，不自动重放或改写目标。`NESTED_SHELL_SANDBOX_UNAVAILABLE` 为 tool、不可重试、change_strategy，只作内因。`goal_progress_fuse.py` 发出的细节已带目标、修订、唤醒信号与结构化结束原因，未改。

**验收证据**

- 红到绿：CI 行 17（码未登记）和行 27、28（缺 `inner_code`）在父提交于 3.12 与 3.14t 失败；行 23 在父提交于 3.14t 失败（3.12 通过）。行 5 在父提交加慢终帧探针（终帧编码后延迟 0.25 s）后于两个版本都失败。本模块后全部通过，行 5 在 3.14t `-X gil=0` 连续 200 次通过。
- 新增 `tests/test_conveyor_m04_base_cures.py` 六项，在父提交全部失败：
  - Event 定序的“终帧取得写权后心跳才到期”，送达与写超时两种，断言只产出一个终帧且其后无心跳；
  - 整页预取后同 inode 截短，断言出现 `INDEX_SOURCE_CHANGED`，续跑后收敛到 `done`；
  - 经 `ScriptsTool` 的 D08.7 三种情形：启动前拒绝、非零退出并回收进程树、重试换新 id；
  - 目标续跑扣下码的分类与继续条件。
- 目标、CI 节点与影响测试在 Python 3.12 与 3.14t（`-X gil=0`）通过；静态门（ruff、导入边界、`git diff --check`、clean package）通过。

**2026-11-20 M04 复核修正（Astra M04 审计三项）**

- 已发布身份（D10.19）：持有的 FD 在快照发布器原子替换已发布索引后仍指向旧 inode。推进游标或阶段前除 FD 的 `fstat` 外，再 `stat` 已发布路径，`(st_dev, st_ino)` 须等于本页冻结身份；被替换或删除同样走 `INDEX_SOURCE_CHANGED`，回偏移 0 重读新文件。rename 原子，单次 `stat` 即线性化点，不拿发布锁；之后的替换由下一页开头的身份核对接住。追加增长与页预算不变。
- 取消回执（D08.7）：`ToolCancelled` 继承 `RuntimeError`，此前被 `ScriptsTool` 的通用分支吞成 `TOOL_EXECUTION_FAILED/failed`。`run_script` 现在先按同一份回执装配记下阶段、退出码、进程组清理与凭据回收（同一 `launch_id`），把结果挂在原异常上继续上抛，不计入资产摘要；`ScriptsTool` 对 run 返回 `CANCELLED`，子进程创建前为 `not_started`/`runtime_gate`，创建后为 `unknown`/`execution`，并要求先核实副作用再重跑。
- 结果字段（D08.7）：`ScriptExecutionResult` 声明 `launch_id`（kw-only，默认 `""`，空值不进旧 JSON），经 `to_dict`/`from_dict` 往返保留，旧记录读成 `""`；run 的预派发校验拒绝回执带显式 `launch_id: ""`。工作流步骤持久回执 `escalation_result.script_execution` 尚不带 `launch_id`，属 `workflows/engine.py`（M42/M57/M59/M67）与步骤效果状态（M58）范围，留作 M58 跟进。
- 证据：`tests/test_conveyor_m04_base_cures.py` 新增七例（索引原子替换 ×2、取消前/后 ×2、codec 与经序列化工具往返、预派发拒绝 ×2），在 `6e7ee6d32` 全部失败，修正后在 3.12 与 3.14t（`-X gil=0`）通过。
