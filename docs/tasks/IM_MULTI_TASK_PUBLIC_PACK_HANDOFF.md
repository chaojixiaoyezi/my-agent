# IM 多任务与公共能力包实施及验收报告

状态：2026-10-12，三项功能已在临时工作树实现并完成下列本地、Linux 验证；尚未打包、提交或推送远端、合并 main、切换线上安装。飞书出站使用模拟适配器，不能称为真实飞书实发验收。

工作分支：`codex/tui-steer-control-20261009`，沿既有 TUI/IM 临时修复分支开发；保留此前全部 staged/unstaged 修改，未重置主仓库。此前交付的离线包没有变化。没有开发子代理并行修改生产代码。

## 1. 最终用户行为

### 定时任务

- 从 IM 创建的新计划各自拥有独立后台执行会话，支持真实 LLM、工具、Goal、子代理和原有续跑链。来源聊天不必等待到期工作。
- 管理员从 TUI 创建计划继续使用原会话。判断来源读取可信通道绑定，不根据正文自称管理员/IM 判断。
- 原有计划保持原线程；没有追改正在执行的计划或迁移历史 job。
- 定时策略和当天委派开关正交。不开 `/delegate` 也能创建独立 IM 后台计划。
- 并发复用原全局/每 owner 后台限额，超额沿原持久唤醒队列等待，不另外创建 Gateway 或绕过额度。

### 当天自动委派

命令：`/delegate enable`、`/delegate disable`、`/delegate status`，仅供 IM 的当前可信发送者使用。

- 默认关闭；开启到部署 timezone 的下一零点。离线跨日再启动也关闭，夏令时不硬算 86400 秒。
- 读取时按到期时间失效，不在零点扫描全体用户、唤醒模型或取消旧任务。
- 接收消息时在原入站回执固定方式和偏好版本。重送、排队、执行、汇总时不会因 disable/零点重新选方式。
- 开启后辅助模型仅返回结构化 chat/task 和所选授权能力引用。普通聊天、任务查询/取消、计划管理留在聊天；独立工作项进入同 owner 的后台执行线程。
- 主聊天生成接单回复并释放；后台主代理经真实 ToolExecutor/G1 创建真实子代理，子代理执行，后台主代理按真实结果汇总。
- 根主代理不重复做孩子的工作。孩子缺能力时只保留原直属父级 `resolve_capability_requests` 裁决工具，授权仍守原上界。
- 原根任务子代理限额与 owner 累计限额共同生效。满额不转成前台执行，沿原非失败退避等待。
- 指定任务停止沿原根任务、精确 Gateway 执行目标和子代理资源停止，不停来源聊天或其它任务。
- 系统未启用子代理或 owner 禁用创建工具时拒绝 enable；后续新受理保持原模式。旧快照不绕过后续安全权限撤销。
- `/status` 展示当日状态、有效期/时区、自身后台排队和执行计数、正在跑的子代理及原额度。

### 公共能力包

管理员使用：`/plugins install <包路径> --scope public`，再使用原 `/plugins enable <包ID>`。默认仍为 private。

- public 仅允许内容能力包；插件和私有能力包继续限管理员使用，普通用户不能安装、更新、停用或运行私有插件。
- 方法、模板、脚本资源共享；输入、记忆、凭据、工作目录、产物仍保存于各自 owner 下。
- 普通用户在 `enable_plugins=false` 时也能读取公共内容，不因此取得插件连接或管理员的工具、文件、凭据权限。
- 主代理和子代理均经原 snapshot/pins/grants 使用；后台计划和委派任务沿相同授权边界。
- 目录只暴露元数据，需要时再读取入口和具体资源，不将全体包正文塞入每次模型请求。
- 管理员唯一安装表升级 v5，旧 v1-v4 显式迁为 private。没有每用户复制包或第二套公共安装表。
- 公共命名域为 `public/<包ID>`，完整引用固定内容摘要和激活身份。主子同代使用。
- 更新保留原安装范围；想改范围须卸载后显式重装。更新回放同时校验 scope、摘要、原 revision 和输入摘要。
- 停用/更新后后续发现变化；旧 reader 复核失败，不静默换成新版本。已跑任务的安全授权仍可撤销。

## 2. 代码入口与唯一权威记录

| 入口 | 职责 |
| --- | --- |
| `conversation/execution_threads.py` | 原 ThreadStore 创建独立线程，保存准确源绑定、原 SessionTask/root/child refs |
| `scheduler/execution_policy.py` | 新计划按可信 IM/TUI 来源选择后台或原线程 |
| `scheduler/tool.py`、`repository.py`、`schedule.py` | job/run 来源字段、模型工具语义、部署时区 |
| `settings/im_task_preferences.py` | owner 配置目录下唯一 `im_preferences.json`，逐发送者到期偏好 |
| `gateway_parts/delegation_control.py`、`command_catalog.py`、`control_commands.py` | `/delegate` 解析、可信控制及资格检查 |
| `gateway_parts/im_dispatch_policy.py`、`http_handlers.py` | 原 input receipt 固定当天模式，正文不能伪造宿主字段 |
| `gateway_parts/im_task_routing.py` | 原辅助模型的结构化语义选择，原请求持久路由，不按词表硬判 |
| `agent_core/orchestration/tools/create_session_task.py` | 原 SessionTask/guidance/Wake 派工，普通用户仅能走自身宿主创建目标 |
| `conversation/im_delegation.py`、`agent_core/_tool_loop_service.py` | 真实子代理创建、恢复、等待、能力裁决与主模型汇总 |
| `conversation/background_claim.py`、`background_execution.py`、`runtime.py` | 生命周期续片保持原任务身份、准入退避和取消；不复制任务状态 |
| `gateway_parts/session_task_stop.py`、`conversation/session_task_report.py` | 精确停止和去重，避免完成后再唤醒聊天转述 |
| `plugin_installation*.py`、`plugin_install_store.py`、`plugin_update*.py` | private/public 安装事实、显式迁移、CAS、升级与回放 |
| `capability/package_provider.py`、`package_snapshot.py`、`package_selection_scope.py`、`subagent_package_entries.py` | 公共目录投影、鲜活读取、主子授权与同代引用 |
| `core.py`、`plugin_management.py`、`plugin_activation_ref.py`、`tooling/plugin_registration.py` | 私有包/插件管理员边界，不为普通用户开插件总闸 |

新配置均在 `config/capability_config.yaml` 与 `agent/capability/config.py`：

- `im_scheduled_tasks_background: true`
- `im_task_delegation_enabled: true`
- `public_capability_packages_enabled: true`

模式、线程、任务、孩子、停止和版本判定均来自原结构化记录；提示词只帮助模型理解，不能授予机器权限。
后台回送固定受理时的原布置者，保留现有私聊通知方式；群里后来的发送者不会将结果改投给自己。

## 3. 本地验证

28 个相关文件的联合回归：**739 passed**，覆盖入站/恢复、实际创建服务/G1、取消、容量让出、SessionTask/turn-end、调度、安装升级/迁移、公共权限、主子版本、结构化后端、shell 未启动与 UNKNOWN 收口。

补充 owner 禁用子代理创建与无配置时区展示的用例后，偏好/入站两个文件 **8 passed**；两个新用例不在先前 739 项中。父级能力裁决组合文件 **10 passed**，验证限于裁决工具时仍能沿原权限授予直属孩子调度能力。

初次联合运行与尺寸报告写入并行，测试 teardown 守卫检测到报告变化；单例复跑通过，随后串行完整联合回归 739 项无错误。没有用 Git 恢复或删掉此前报告修改来绕过守卫。

最终静态 gate：Ruff、doc-sync、strict code-size（hard=0、blocked=False）、diff-check、clean-package 均通过。测试只有原 ctypes `_NetPortAttr` layout deprecation 警告，未修改无关模块。没有将线上 CI 作为验收来源；未提交远端。

## 4. Linux 真实验收

在用户指定 `192.168.1.19`，Ubuntu 24.04.4、x86_64、2 CPU/约 4 GB，隔离 home 中使用单 Gateway、官方 MiniMax-M2.7 与真实子进程。未改变原 preview 安装、日常模型或生产任务。

- 真实 HTTP 普通 IM 身份开启 `/delegate`，连续提交两个独立文件任务和短聊天。
- 两个原 SessionTask 均 `done`，两个实际 child 均 `DONE`；后台主代理各自产生终态汇总。
- 短聊天的原请求先于两个任务终态结束；聊天不等待子代理或后台汇总。
- 两孩子与各自根策略均固定同一个 `public/shared-guide` activation/content 摘要，实际产出来自被测子代理，测试者没有补文件。
- IM 每日晚间计划的执行 thread 与来源不同；真实 SSH PTY TUI 中只给一个普通中文需求，上午计划原生 `schedule create` 成功，执行 thread 与来源相同。每日计划只验持久登记及归属，不把模型对“明天”的日期解释算作日期语义验收。
- 新的独立一次性 IM 计划到期后，原 scheduler 唤醒实际后台模型；history 记录 `done`、空错误码、投递 `sent`，job 为 `one_shot_finished`，来源/执行 thread 不同。
- 出站由模拟飞书适配器记录收件地址、时间及长度；没有给真实用户发测试消息。

本轮保留各次真实失败记录，不用最终成功覆盖之前失败。观察器最初误查 per-job 目录及待执行 runs，已改用 canonical `scheduler/store.json` 和完成后的 `history.jsonl`；该修正没有改变被测任务文件。

## 5. 真实失败推动的通用修正

1. 结构化路由调用传入 request-local options，但 Messages/Chat 结构化生成签名不接收。统一接收并传递 system、首事件/总期限、输出封顶；Messages 强制 JSON 工具信封继续按原约束关闭思考。新增 DTO 回归及真实路由复测。
2. 初版后台根又做孩子工作，且生命周期续片丢了原输入身份。现由原 canonical SessionTask 引用提供稳定回合、原正文确认和停止目标；模型收口保留真实证据，宿主创建不伪造模型工具往返。
3. 容量不足原会落成业务失败。现在 typed 让出沿原耐久退避，根保持未完成，不消耗业务失败重试。
4. Ubuntu 24.04 测试机阻止临时 bwrap 创建 namespace；shell 原 process 事实已有 `not_started`，却误判 UNKNOWN。修复只读结构化启动事实，不解析错误文本。测试环境只给专用 bwrap 临时窄 AppArmor profile，未关闭系统限制或应用沙箱；结束后已卸载并删除，系统 userns 限制仍为 1。
5. 真 UNKNOWN 被当成普通 interrupted 子代理重新排队，随后撞到原执行仍忙。现在 UNKNOWN 收口为 blocked，不自动重复未知副作用；已知可续失败保留原未完成路径。
6. 计划管理与“调用外部工具即独立工作”的路由说明冲突。已明确计划创建/查询/修改/取消/立即触发属于管理；后台真正执行仍沿独立线程。
7. 纯汇总根不能处理真实孩子追加权限申请。现在 canonical OPEN 申请只开启原直属父级裁决工具，根不代执行业务；授权仍由原 snapshot/owner/path/tool 边界判断。

一次“立即执行刚建计划”的真实请求未在 210 秒观察窗内产生 scheduler run，被模型路由成新工作项；该次不算通过，清理时沿原精确停止控制取消其资源。最终代码已另做真实复测：该管理请求被准确路由为 chat，实际调用 schedule，原工具返回 `APPROVAL_REQUIRED`，未创建额外孩子。run_now 在原工具政策中为 dangerous，测试没有伪造批准；此复测证明路由修正生效，不算立即触发后的执行成功。一次性到期计划已真实通过。

## 6. 边界与后续

- 真实飞书网络、机器人权限和客户端展示没有在本轮实发验证；主链 HTTP/owner/任务/模型/子进程/调度是实际实现，只有出站 transport 是 fake。测试进程/孩子已结束，8420 端口已释放，私有测试记录保留供核查。
- 模型语义选择不保证每种措辞都正确；结构错误有两次有界重试，不能靠正文关键词硬路由兜底。
- 旧计划未迁移，公共包旧 schema 只显式迁 private。没有重新发行旧离线包。
- 普通运行回复由原模型生成；只有 `/delegate`、`/status` 等显式控制用确定文本。
- 本地代码和测试完成不等于已安装到用户内网服务器；线上仍是原版本。

建议下一步：保留此临时分支等待用户的打包/合并指令。若安排后续验收，优先用独立测试机器人核对真实飞书投递与 `/status` 展示，再验证原立即触发确认交互及更多自然语言样本；其他 agent 可并行只读审阅本报告和差异，不在同一工作树改同一入口或重启生产 Gateway。
