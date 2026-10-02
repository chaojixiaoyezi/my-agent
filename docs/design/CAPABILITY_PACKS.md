# 能力包与内化合同

状态：2026-10-01，Mac 生产已上线 step16v／16w／16x；验收中发现的宿主缺陷 D1、D2、D3、D4、D5、O1 均已修复上线并经真实模型复核（Responses 失败事件分类、一次选择 schema 修复随 step16x 上线），前台失败后台完成已核实送达。最终固定矩阵 27 次执行完成（业务 16/27、原资源执行 12/18）；自然选包、实际方法采用、原资源执行和业务质量分开统计，稳定采用及准确交付尚未全过。G01／G02 用 gpt-6-luna 生产规模与串行长任务各跑一次，均未命中自然压缩。七组缺项：G04、G06 已闭合，G07 基本覆盖，G03、G05 部分覆盖，G01、G02 未覆盖；Linux 真机未部署（测试机下线）。当前状态以[收口审计与七组未覆盖范围](../tasks/CAPABILITY_PACK_ACCEPTANCE.md#收口审计与七组未覆盖范围2026-09-28)和唯一 TODO 为准。
执行入口：[完整 Goal 与唯一 TODO](../tasks/CAPABILITY_INTERNALIZATION_GOAL.md)。
验收入口：[来源、能力矩阵与测试计划](../tasks/CAPABILITY_PACK_ACCEPTANCE.md)。
操作入口：[来源迁移、候选构建、启停与版本回退](CAPABILITY_MIGRATION.md)。

## 解决问题

用户希望把外部 Agent 的整套方法、流程、模板和脚本沉淀成可持续完善的专业能力，
在新会话里根据任务自然找到。把全部内部方法展开成几百个全局 Skill，会导致重名、
召回噪声和难以停用；只复制上游资料，也不代表宿主已经能够完成任务。

能力包是独立的产品概念，复用现有安装、授权、执行和快照底座：

| 概念 | 职责 |
| --- | --- |
| MCP | 连接外部服务的协议 |
| 插件 | 可装卸的命令、工具、面板等外部装备 |
| 个人／公共 Skill | 可独立复用的单项方法 |
| 能力包 | 一个可独立发现、版本化和验收的专业能力集合 |

内化表示 my-agent 在自己的任务、模型、工具、子代理与权限链内使用这些方法，
不表示修改模型权重，也不能把完整任务转交原外部 Agent 后就称为内化。

## 首期边界

- 主产品是通用能力包功能；短剧等领域仅是代表样包。验证要分别记录通用机制、真实迁移/可执行链及领域输出质量，不为某个故事、文件类型或上游专项流程增加宿主分支。纯领域质量缺项保留，不要求某一领域达到100%完美才算具备通用包机制；自然使用和原资源正确执行仍须有实际证据。
- A、B、C 来源先成为独立包，各有来源、版本、许可、覆盖矩阵与验收；不自动三合一。
- 需要组合时由宿主在同一任务中选择多个包，分别保留来源和版本。
- 正式融合必须有明确输入输出、冲突处理和对照证据；首期不做自动融合或依赖求解器。
- 安装、启用、停用、更新、卸载复用原插件管理服务与原命令入口，不另建安装库。
- 不新增包专用模型循环、任务账本、审批系统或隐式后台学习任务。
- 自动总结个人 Skill 由独立开发线负责；本线不修改其学习提案或发布规则。

## 包协议

新增显式 `plugin_package.v7`，`package_kind=capability`。
首片只支持纯内容包，不为资料创建 Python 环境、伪造 MCP 入口或空进程。

```json
{
  "schema_version": "plugin_package.v7",
  "package_kind": "capability",
  "plugin_id": "story-workbench-a",
  "version": "1.0.0",
  "summary": "短剧文本与分镜方法",
  "capability": {
    "description": "从既有故事形成可审阅的短剧结构与分镜",
    "keywords": ["短剧", "分镜", "故事改编"],
    "entry_document": "CAPABILITY.md"
  },
  "files": [
    {"path": "CAPABILITY.md", "sha256": "<构建时计算的摘要>", "executable": false}
  ],
  "settings_schema": {"type": "object", "properties": {}, "additionalProperties": false}
}
```

示例摘要是占位说明，不能作为有效包直接安装。所有资源必须在 `files` 中有内容摘要，
入口必须存在；重复路径、大小写／Unicode 冲突、越界、链接、夹带资源、篡改均拒绝。
成员数量、目录、单文件、归档和展开字节都保留有界预算，不能以资料多为由取消预算。
声明、构建、安装都不运行包内代码。首期内容包不声明 executable runtime、tools、actions、
panels、skills、host_api；脚本文本是私有资源，读取不等于执行授权。

旧 v1—v6 的序列化、目录摘要和运行语义保持；已有 v3 插件导出的全局 Skill 仍走原链。
新版本不修改旧包字节。旧宿主不能读取 v7；同一 owner 必须使用支持该协议的 Gateway。

## 唯一安装事实与生命周期

安装仍使用 `PluginInstallStore` 的原表、包 blob、目录锁、版本 CAS 和操作回执。
`PluginInstallation.activation` 增加明确的内容变体，禁止以 `entry is None` 猜类型。

内容激活身份绑定原 operation、plugin ID、包摘要、安装版本、设置版本和 phase。
进程激活保持旧 schema；内容激活单独版本化，并有明确 active／revoked 迁移。

| 操作 | 合同 |
| --- | --- |
| install | 完整验证后原子保存，默认停用 |
| enable | 复验内容和配置，原锁 CAS 直接发布 active；不准备环境或 MCP |
| disable | 原表 active→revoked，关闭新发现／新读取准入，再 release；结构化说明无运行资源 |
| update | 旧包先停用并释放，再验证并提交新包，显式重新启用 |
| 回退（update） | 明确指定旧版本包走同一更新链；首期不猜“上一版本” |
| 卸载（remove） | 使用原完整记录 CAS 移除，原回执确定后才回收无引用 blob，保留任务产物 |

提交后异常不得当作未发生，UNKNOWN 不得自动重做整个操作。
停用不能让已进入模型上下文的内容消失，也不替代当前任务／工具的停止合同。
当前轮已经冻结的摘要和成员名仍可能展示；新轮发现移除包，任何新的正文读取都复核鲜活安装。
更新不是无缝热升级；旧任务不可在停用后悄悄转投同名新版。

## 发现、作用域和按需读取

`SkillsService` 仍是逐轮快照装配入口。`SkillSnapshot.packages` 独立存放包级摘要，
既有 `entries`、`enabled_entries`、`resolve` 和全局 name 去重仍只处理公开 Skill。
能力包目录不能进入 `_skill_roots` 或旧 `plugin_roots`，即使内部文件名是 `SKILL.md`。

- 包级引用为 `capability:<package_id>`；内部资源键为 `(package_id, member_path)`。
- `resolve_reference` 支持公开 Skill 与包级引用；内部资源须显式限定包。
- `resolve_in_package` 定位声明成员，`read_in_package` 默认读取入口文档。
- `skill_search` 无包范围时仅返回公开 Skill 和包级摘要；明确包范围后可列资源、按页读取。
- 全局模型索引中独立显示能力包摘要，不把内部资源计入个人／公共 Skill 数量。
- 包摘要、关键词只用于软召回；授权、身份、状态与验收不从自然语言推断。

发现卡片提供可原样传回 `skill_search` 的 `next_read`：普通 Skill 使用 `skill_id`，能力包使用 `package_id`，
包的 `stable_id` 只用于授权和持久引用，不能填成 Skill 选择器。包建议携带 `expected_package_sha256` 和
`expected_activation_id`；即使下一轮同名包已经升级或重新启用，也不能沿旧建议静默换代。
错误选择器仍返回失败；仅在当前受限快照精确命中公开包身份时返回 `selector_mismatch` 和正确 `next_read`，
由模型发起新的显式调用。错误本身不读取正文、不晋升、不 pin，不靠名称相似度补权限。

`resource_path` 仅用于显式 `action=get`。`action=search`（含省略动作的默认检索）只要显式携带该字段，
即使空串，也在读取快照前返回 `TOOL_INVALID_ARGUMENTS`；不能忽略它后返回成功、自动改成读取或查询词。
参数错误只说明正确用法，不暴露未授权包身份、成员或正文，不改变当前任务引用。

`get`的包可见性及预期代次核验后，若`package.resolve(resource_path)`未命中声明成员，
沿既有`TOOL_INVALID_ARGUMENTS`返回`CAPABILITY_RESOURCE_NOT_AVAILABLE`及同代入口`next_read`，
让原恢复合同选择`repair_tool_arguments`。入口名由包声明决定，建议省略成员路径并带准确摘要和activation；
须再次显式调用才进入原reader并在成功后pin，错误本身不读取或改引用。原get任务晋升策略仍可先发生。
未知/未授权包、受限快照里授权代次已变及声明成员真实缺字节/摘要变化保持原快照错误；取消和中断继续传播。
包已在当前快照解析、只是显式`expected_*`与当前包不符（抄错或沿用旧建议）时，同样归`TOOL_INVALID_ARGUMENTS`：
回执`error=CAPABILITY_PACKAGE_CONTINUATION_MISMATCH`，`continuation_mismatch`只列字段名，不回显当前摘要/代次，
也不给同名新一代`next_read`；模型须不带旧字段重新search，再用当前`next_read`从入口重读，旧页码作废。
宿主读取入口`read_package_page`遇到同样不符仍抛`SKILL_SNAPSHOT_STALE`。（H1，2026-10-01）
主任务旧 pin 已停用或换代时，快照只留结构化 pin 诊断；`skill_search`据此回`CAPABILITY_PACKAGE_TASK_PIN_UNAVAILABLE`
（`report_blocker`，`details.error_code`为原诊断码），提示告诉用户或开新请求，不再套用子代理口径的“由父代理重新授权”；
没有 pin 诊断的缺包仍是原快照错误。（H3，2026-10-01）
本片参考Free-Code固定`6b25ab68`的FileReadTool结构化缺文件回执、Codex固定`578c1b22`的精确资源身份读取；
只复用本仓已有参数错误与`package_read_parameters`，不移植参考项目的自动重试或模糊路径猜测。

成功的包范围 search/get 同时返回 `resource_namespace` 和限页的同代 `next_search`。
命名空间只描述清单中已经声明的包根相对成员；正文提到的业务输入、交付路径不因此变成包成员。
模型先检索声明，再使用匹配项 `next_read`，不能把虚拟成员当工作区文件或猜安装位置。
这些字段只是原授权快照的投影，不解析正文链接、不枚举全部资源、不自动读取、物化或执行。

包段同时提供统一模型使用规则：用户点名或任务匹配时先读当前包入口，再按步骤读取相关资源；
普通无匹配任务照常处理，多个适用包只选任务所需集合。规则不自动执行、不读私有正文，
不把语义匹配当权限；有包但展示选择为空时仍保留发现/使用说明，无包时原 Skill 提示保持。
派工用已有 `allowed_skills` 明确传包级稳定 ID，版本仍由宿主的父快照/原任务引用确定。

发现只读已启用安装记录及元数据，不扫描所有正文。读取从原 blob 取得受摘要保护的成员，
核对当前 owner、package SHA、activation ID，并在内容交付前复核同一代次。
路径错误、包篡改和停用／重装后的旧快照明确失败，不静默读新版或暴露相邻 owner。
这类读取失败对外仍统一返回 `SKILL_SNAPSHOT_UNAVAILABLE`，恢复建议不变；包读取的失败回执另带 `details.reason`，只用于诊断：`activation_unavailable` 表示读取开始前代次已失效（停用、换代或卸载），`activation_changed_during_read` 表示成员字节读出后复核时代次已变化（在途切代）。原因来自安装异常的结构化字段，不解析文案，也不据此放行或自动重试。
大内容使用明确页范围和 continuation，不以静默截断冒充完整读取。
资源引用排在正文前并进入原 handler envelope；正文被归档时，复用原 `live_prompt_output` 保留
完整 `source_ref` 和受原预览配置限制的 `body_preview`，`body_preview_complete` 明确当前页是否读全。
原完整工具输出继续保存，强制保留原归档锚点，不新增正文仓库或分页账。包 `continuation` 续包正文页，
`read_artifact` 的 `has_more_after`／`next_read`／`next_offset` 续当前归档窗口，不能混用两个偏移。
此投影不保留无限全文，不绕过原上下文余量、Compact、安装复核或来源权限。
包范围搜索也沿原归档保留完整页；有界 `matches_preview` 只保留放得下的完整卡片，绝不截断读取引用。
`matches_preview_complete` 说明当前页预览是否完整，与 `has_more` 表示的后续结果页分别判断。
零预览仍保留命名空间、同代搜索参数及原归档锚点；检索的 limit、offset 和 continuation 不因归档改变。
资源物化使用原 `write_file.source_ref`，与 `content`／`data_base64` 互斥，首期只允许 overwrite。
引用必须原样来自包读取结果，固定 kind、包 ID、整包摘要、activation、成员路径及成员摘要；
宿主注入的 resolver 只读取当前受限快照的原始字节，不能由模型指定 owner、回调或安装地址。
写入沿同一个 write_file 的 ToolExecutor、任务晋升、审批、路径／配额／persona 门、版本检查、原子写和产物回执。
未注入 resolver 时保持原写工具 schema，并拒绝 source_ref；读取成功不扩大目标目录权限。
脚本执行仍由原 Shell 工具完成，source_ref 物化不会执行代码，也不创建新运行时或影子任务。
模型只看到正文预览时也能使用原引用复制完整资源；复制成功须看原回执和字节摘要。
模型手抄/改弱核验脚本后得到的退出码，不能算原能力包核验通过；应修正产物或说明真实限制。

资源来源schema必须和运行时引用合同一致：包资源模块唯一声明9字段，必需字段与运行时字段集合从该声明派生；
core沿原registry装配来源resolver和schema，工具层只接收深拷贝的声明，不反向依赖能力包模块。
两者须成对提供；宿主缺配明确失败，不以无约束object代替。原ActionPolicy/ToolExecutor参数门在handler前检查
缺字段、类型和多余字段，仍由原resolver核owner、包/激活代次、路径与字节摘要；完整name一致性不放宽。
这是已有资源复制工具的声明修正，不增设自动复制、业务执行器或参数修复模型请求。

首次 `skill_search(action=get, package_id=...)` 通过原工具策略的结构化条件晋升为任务后再读取和pin，
search 不晋升。条件声明只读取明确参数，由通用工具运行缝消费，不按工具名或用户文字硬编码。
已有 Goal 或持久执行有准确任务链接但无目录时，Gateway 保留精确 conversation_task_id；
普通 RuntimeDB 临时执行编号不能冒充已经存在的会话任务。

## 一次能力选择（已实现，真实分项已验）

当前`CapabilityPresentationSelection`只选择展示名卡；`adopted`只证明展示被采用。
成功正文读取、版本pin、原资源执行和业务验收是不同事实，不能互相代替。
已有回归明确允许模型零包读取回答；继续堆软提示不能把它变成稳定的方法加载合同。

原主请求之前的一次结构化选择已接通，真实选中、空选及入口准备已有[分项证据](../tasks/CAPABILITY_PACK_ACCEPTANCE.md)；方法实际采用与交付质量仍单独判定：

- 新模式默认关闭，开关和预算归`capability_config.yaml`及对应dataclass；关闭或无授权包零额外调用。
- 准备时绑定的主模型只见有界、当前授权的名卡；沿原`generate_structured`返回候选ID集合，只有空数组为明确空选。
- 选中后由宿主按原权限、包读取和pin合同加载有界入口，作为标明来源的宿主上下文交原主请求；不伪造工具调用，不执行脚本。
- 每任务只试一次，沿原canonical任务记录而非内存计数；resume、重放、续跑不得重复发起。
- 失败只留结构化warning并继续原业务，不建立required_actions重试循环或另一份采用账。
- 选包模型异常登记为`CAPABILITY_SELECTION_MODEL_FAILED`，恢复建议为继续原业务、不可自动重放本任务已领取的选择；取消仍沿原异常传播，不归为这个可选失败。
- 结构化选包仅在主任务进行；子代理沿原grants/pins读取，并按下节准备已授权入口，继承引用不代表已实际使用方法。

选择用的assistant/schema信封不进入业务历史，避免污染DeepSeek后续思考；正文仍是不可信外部材料，不能成为权限。
准备在`build_tool_loop_prompt`之前的一个主链接缝完成，不能在选模已冻结payload后追加内容；
后续Jev若合法采用其它模型，最终业务模型可以不同，不关闭Jev或伪造显式pin来维持“同模型”口号。
沿原辅助调用账/并发准入/供应商适配器，只有一次逻辑选择；原后端内部重试、HTTP次数和用量缺报单列。
输入和入口有预算，输出仍受既有模型cap；不宣传已经有独立小输出上限或固定一次HTTP。

一次标记放原`ThreadTaskLink`的可选typed宿主字段，JSON键为`host_capability_selection.v1`；
None不序列化键，旧记录及关闭路径字节保持。仅原新建任务可初始化pending；新Goal提前创建link也须走同一资格helper。
原task transition及JSON锁内CAS执行pending→claimed→finished；I/O前先claim，结果分selected/empty/failed。
claimed绑定实际请求/run/attempt与候选指纹；旧缺键、claimed、finished、损坏标记均不得重新领取，损坏只警告不阻普通业务。
模型身份使用准备时实际后端公开生成字段的`model_binding_digest`，不把可能已被菜单改动的thread profile冒充当前后端。
摘要不含密钥，不作为模型采用权限或凭据版本；实际调用仍看原辅助模型账。finished只保存结果数量、摘要与warning，准确refs仍归原pins。
outcome=failed 且是辅助调用抛异常时，标记另带 `failure`：只含异常类型、宿主错误码、HTTP 状态和服务商错误的 code/type/param 这些短标记，不含异常正文或服务商说明；其它结果不写这个键，旧记录字节不变。它只供排查，不进模型上下文（模型只看到原 warning 码）。
选择用的 response schema 只用各家严格 JSON Schema 模式都接受的关键字（数组只写 items/enum，不写 uniqueItems、maxItems），不重复、不越界由本地严格解析保证；2026-10-01 真实 gpt-6-luna 实测带 uniqueItems/maxItems 时订阅接口以 invalid_json_schema（param text.format.schema）拒绝。
停止或换attempt后的迟到结果不得pin/注入。bind、状态更新和原pin必须保留该字段，不把整块marker放进模型可写attrs。
不新建表/文件/采用账；原pins仍是唯一版本权威。旧程序回写丢键后，再升级按缺键跳过，不补填或重复选择。
配置开启且有授权候选时可沿原晋升创建任务，这有持久写入成本，即使最后空选也不撤掉原任务。
因此纯问答也可能在原TUI任务列表中留下普通任务，由原业务收尾处理终态；它不是Goal，不创建Goal或恢复暂停的Goal。
关闭开关仍沿原工作工具晋升规则，普通纯问答不会仅为选包创建任务。新补的on/off首请求回归只验证持久化和Goal不变，不伪造终态；真实N03另验证了完成状态。
默认开启与否留待用户查看采用、额外token及首响应时延后决定；本节不表示稳定召回已通过。

实现分工：`package_selection_scope`只核元数据资格；`package_selection`只准备材料和调用原结构化后端；
`package_selection_runtime`只在真实主业务首轮、完整build/capture前协调一次；`package_selection_authority`复用原准入与执行权；
`package_selection_context`控制入口总预算，正文读取和原`skill_search get`共用`package_read`。
新开关`enable_capability_package_selection=false`；输入预算`capability_package_selection_max_input_tokens=3000`，
候选数复用`capability_candidate_limit=5`；入口总预算首次真正消费`capability_bundle_max_tokens=3000`，不把此前未消费的字段描述成已生效。
数字配额0只移除独立限制，仍预留原模型输出并受上下文窗口限制。输入不截掉用户需求，候选只整条省略；入口分页不改原资源摘要。
实际配置文件、缓存生效及新任务验证步骤见[迁移手册6.1](CAPABILITY_MIGRATION.md#61-可选的一次选包与入口准备)；安装启用包不会自动开启此开关，新开TUI也不保证重载宿主配置。
配置缓存只接受原`CapabilityConfig`实例；无效占位对象继续原文件读取路径，文件不可读仍返回既有空结果，不凭对象真值开启包准备。没有新增配置源或热加载协议。

宿主读取用原`ActionPolicy`做只读准入：原接口要求的`ToolCall`只作为栈内评估值，
不dispatch、不进入业务history或工具操作账，也不声称模型调用过工具。ask/deny只给warning，不自动代批。
共享reader仍检查当前scope、声明成员、哈希和activation；`pin_skill_reference`在原task transition/JSON锁内复核当前执行权。
激活按原读取前后核验，已读内容可留历史，撤销后的后续读取拒绝；不声称上下文提交时仍与安装表全局原子一致。
精确 completed/interrupted 工作区的接续沿原晋升产生新链接，可初始化一次选择；旧链接和暂停Goal不补写或复活。
会话链接用于选择CAS和版本pin，实际执行准入复用原`runtime_run_scope.task_id`，两者不能混用，也不为选包重绑RuntimeDB或新增attempt。
活动回合锁只包有限本地领取/读取/提交，模型I/O在锁外；原停止或失效attempt沿取消异常上抛，不当成普通选包失败。
选择assistant/schema信封不入业务历史；有界入口作为原`RuntimeFactsTurn`进入原预算、历史、Compact、自动选模捕获及实际请求。

## 主任务、子代理和长任务

显式能力包经`allowed_skills=["capability:<package_id>"]`传给孩子；批量可逐项填写`item.allowed_skills`，未单列项沿用顶层默认列表，goal或input_refs提及包不授予读取范围。缺少能力时孩子沿原`capability_request.requested_skills`申请同一stable_id。自动路由先完成原owner路径判断，遇到包命名域后整条保留OPEN/PARENT_RESOLUTION_REQUIRED；包与工具混合申请也不能部分自动结清。该记录不证明父级有权批准；原直属父级resolve继续按当前快照验证，生成完整七字段ref并写原canonical grant。未知包批准失败时保持OPEN，可由原deny关闭；不隐式转换裸包名或资源路径，不补首请求marker、不增加安装或授权账。对应组件111项通过，模型自然采用仍须原生复验。

### 子代理首请求的已授权入口准备（2026-09-27，已实现，组合及真实入口已验）

最新长任务的四个孩子已有准确包名卡、next_read和同代引用，却没有读取包正文。现行“仅主任务自动入口”合同没有违约，
但只让名卡可见不足以保证子任务方法可用。本片扩展原首请求准备，复用`enable_capability_package_selection`及原入口预算：

- 只对新创建、具有原首请求资格的子线程装配。现有`host_subagent_first_request`的创建/领取/发送状态仍是唯一资格来源，
  原宿主创建链传入`SubagentEntryInitialization`，只有无Jev advice的新资格增加包入口purpose；已有Jev标记逐字节保持。
  不能从空历史、空工具账推断首次，不给旧无标记线程补资格。
- 只读取canonical child的显式授权与精确包pins的交集；调用原policy、快照及共享reader，沿原RuntimeFacts进入实际build/capture。
- 不运行选择模型、不改包pin、不复制父历史、不自动给权限；父有包而孩子无授权、旧代停用或不完整refs均不注入正文。
- 一次领取绑定原run/attempt；取消或换attempt后迟到读取不提交。首发重试、Compact和resume沿原状态及已存历史，不重复准备。
- 关闭开关保持原路径；Jev关闭也可有包入口准备，Jev开启的建议校验/采用规则保持。正文仍为不可信资料，具体方法按需读取。
- 原marker单独证明首请求准备资格；pending advice只控制Jev自动采用。首请求前或领取时的显式选模不会消耗合法包入口资格，仍保留所选模型；关闭包开关时保持原advice CAS。

共享首请求文件已由Jev负责人明确交接，单人实现后交回；已冻结L01失败不回填。25文件570项组件测试包含实际序列化正文、
零新增模型选择、空授权/越范围/换代/取消、预算分页、重试/恢复和Jev两态；两项因果变异核对新资格有效且旧线程不回填。
独立复核发现显式选模后漏入口的组合缺口，已补红绿及claim交错；相关十文件254项、独立11项/6组探针通过。claim结构整理后的新冻结源码三片35文件977项及本地严格gate通过；随后第九候选主线组合48文件1301项通过并精确安装。
第九候选四孩子首次请求均有同代入口且零额外选择调用；私有方法get均为0，业务汇总仍失败。入口送达、方法采用和任务质量分别记账，不能把真实入口通过写成方法或长任务全面通过。第十四候选的组合安装与普通资源消费另见[验收记录](../tasks/CAPABILITY_PACK_ACCEPTANCE.md#第十四候选通用资源消费2026-09-27)。

沿用 `allowed_skills`、`skill_snapshot_refs` 及原 capability grants 作为权威授权与版本来源，
其中允许包级 canonical ref；不另建一份 allowed_packages 账。
同一 ref 行增加明确 kind、package ID、package digest 和 activation ID，完整性不能只核对正文。

`restricted` 同时裁剪公开 Skill 和能力包；父级无授权的包不能经创建／授予／调度进入孩子。
主会话、子代理、孙代理、后台续跑、Compact 和 resume 均使用原任务引用，不从摘要文字猜。
主会话首次成功读取包在原 `ThreadTaskLink.skill_snapshot_refs` 固定引用，原 task transition／JSON 锁内幂等合并；
新轮只验证已用包，不把已用列表变成 main 的全部可见能力。无持久任务的普通临时轮仅固定当轮快照。
合法 pin 所指包停用、卸载或换代时，主任务保留旧 pin，仅从当前快照排除这个失效包，
向模型显示有界的包 ID 与不可用原因；其他包和工具继续可用，不能把一次卸载变成整个会话无法回复。
同 ID 的新版本不自动替代旧引用，get／source_ref 仍不可读取它；已交付历史内容无法撤回。
损坏的任务身份、越权线程、非法引用仍硬拒绝；子代理和定时任务的显式授权／版本边界继续严格验证。
新定时执行在原`SchedulerService.claim_wake`领取成功后、首次后台快照之前，使用已领取run的准确thread/run/prompt建立原`TaskStore`链接。只有从未开始且不存在链接的新执行可创建；已有记录在原任务锁内核对，保留pins、选包marker和工作区。终态按既有映射结算，已开始却缺失链接、损坏或身份不一致均不补造、不进入模型；绑定失败登记`SCHEDULER_TASK_BINDING_INVALID`并结算原claim。无任务的普通后台轮保持taskless，旧任务续跑缺链接仍按原退休协议处理。

任务终态不等于回复已经交付：原pending上合法冻结的回复只沿既有交付层重投，不重新选包、读取方法、预扫孩子或调用模型。定时运行保留同一claim直到该交付完成或按原取消规则抑制，再以原任务状态映射结算，不能一律写done；无有效冻结的终态定时wake也交原服务结算，避免留下queued run。准确pending无法确认或任务状态未知时保留未决事实，不把读取失败当完成。准入期间原claim已被接手、结算返回None时也不得消费wake，只有原CAS实际结算成功才能返回stale。
子代理只从 canonical task 与 grants 合并范围，不能再次套旧 run attrs 初始列表丢掉后授予能力；
孙任务的 refs 只继承父级原账，忽略模型 spec 自报版本。
`selected_skill_ids`／`required_skill_ids` 可携带包级引用用于展示与上下文选择，
不能将展示投影视为权限来源。停用／换代后当前任务仍保留旧pin；使用重新启用或更新后的代次，应在明确的新任务中重新选择。恢复原会话／原Goal不会迁移其pin，已进入历史的内容也不能撤回。
包安装启用是已有显式配置边界；关闭插件／工具开关时包不可发现，也不附加模型调用。
`enable_capability_package_recommendations` 位于能力配置，默认开启：只把已启用、当前授权包的相关摘要放入当轮候选展示。
关闭后保留静态目录和显式检索；没有包时原提示保持。候选数量及展示预算沿原能力配置/元数据预算，
不预读包内正文、不自动调用、不新增模型请求或持久状态。已有展示选择仍限制候选，不能借动态推荐绕过。
普通执行和会话Compact准备都传入当前scope及已重验的selected集合；冻结后的后台投影复用该片段，不重新搜索。
组件及分项真实呈现已有验证；是否改善自然采用仍需对照证据，不能由展示可见或词面排名推断。

## 外部项目迁移与持续完善

可复用迁移流程：固定公开来源和版本→盘点功能与依赖→核对许可→拆分可移植内容→
建立包入口／资源→构建安装→按功能矩阵验收→记录差异与改进候选。

包内保留来源和许可说明、功能映射、支持条件及未迁移项。首期不把用户原任务、
个人路径、对话、密钥、媒体素材和私有运行结果带入仓库。样包只用明确可发布内容。
“100%”只可针对固定来源版本、明确功能范围和实际通过的验收矩阵陈述；
静态资料齐全、文件很多、模型口头说学会，都不构成执行能力证据。

改进候选属于包自己的版本迭代，不自动污染个人／公共 Skill 库；候选不得无记录覆盖已启用版本。
任意新增网络依赖、生成模型或外部工具均单列，不把 M3 视觉理解当成视频生成能力。

## 参考与验证边界

已经核对本项目包声明／安装／激活合同、Skill snapshot/service/search/router、
旧随包 Skill 测试，以及主子授权和 task refs 消费者。
参考本地 Codex `core-plugins/src/{manifest,store,manager}.rs` 的可缺省内容声明、
候选准备和原子发布；模块索引命中后读取相应实现，不声称完整审计全部参考项目。
其 Skill 展开方式和目录删除不能直接替代本项目私有作用域、权限和进程退出证据。

开发顺序为合同单测→fake tool→fake LLM→replay→少量真实原生 TUI 验收。
真实阶段使用独立验收 home 和一个测试 Gateway，多个 TUI 共用它；不在用户日用 owner 上装卸。
新 TUI 不等于无历史记忆：独立召回臂通过原 owner `memory-policy.v1.enabled=false` 关闭记忆，保留原历史文件，
以实际原生输入的 Related Memory 无历史记录及响应 `used_memories=0` 联合验收。记忆开启的旧结果单列，
不得把跨例召回带来的差异全归因于包修订；本约定不改变用户的日常记忆设置或能力包权限。
官方 MiniMax-M2.7 验证 provider 与实际 endpoint；视觉测试用官方 MiniMax-M3。
每个 TUI 分开记录编号、tmux 入口、包版本、任务/run、结构化事实、产物核对和未覆盖项。
测试者只给一次普通中文需求并观察；不能补文件、替对象执行或拿旁路产物冒充通过。
不真实断网，受控故障限定为本测试的模拟依赖或明确测试进程。

建议下一步：按唯一Goal先完成通用自动Compact缺陷修复与提交后资源续用验证，再整理发布范围；
只读资料/证据审阅可并行，产品实现与运行环境分别维持单负责人，已有生命周期和领域结果不重复补算。
