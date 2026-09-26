# 能力包与内化合同

状态：2026-09-25 已与协作方确认边界，进入首片实现；尚未通过真实 TUI 验收或发布。
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
| rollback | 明确指定旧版本包走同一更新链；首期不猜“上一版本” |
| uninstall | 使用原完整记录 CAS 移除，原回执确定后才回收无引用 blob，保留任务产物 |

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

首次 `skill_search(action=get, package_id=...)` 通过原工具策略的结构化条件晋升为任务后再读取和pin，
search 不晋升。条件声明只读取明确参数，由通用工具运行缝消费，不按工具名或用户文字硬编码。
已有 Goal 或持久执行有准确任务链接但无目录时，Gateway 保留精确 conversation_task_id；
普通 RuntimeDB 临时执行编号不能冒充已经存在的会话任务。

## 主任务、子代理和长任务

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
子代理只从 canonical task 与 grants 合并范围，不能再次套旧 run attrs 初始列表丢掉后授予能力；
孙任务的 refs 只继承父级原账，忽略模型 spec 自报版本。
`selected_skill_ids`／`required_skill_ids` 可携带包级引用用于展示与上下文选择，
不能将展示投影视为权限来源。停用／换代后需要明确重新选择，不自动替换运行中的版本。
包安装启用是已有显式配置边界；关闭插件／工具开关时包不可发现，也不附加模型调用。
`enable_capability_package_recommendations` 位于能力配置，默认开启：只把已启用、当前授权包的相关摘要放入当轮候选展示。
关闭后保留静态目录和显式检索；没有包时原提示保持。候选数量及展示预算沿原能力配置/元数据预算，
不预读包内正文、不自动调用、不新增模型请求或持久状态。已有展示选择仍限制候选，不能借动态推荐绕过。
普通执行和会话Compact准备都传入当前scope及已重验的selected集合；冻结后的后台投影复用该片段，不重新搜索。
该展示改动仍在组件验收；改善自然采用的效果须重新通过真实 TUI 验证，不能由词面排名推断。

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
官方 MiniMax-M2.7 验证 provider 与实际 endpoint；视觉测试用官方 MiniMax-M3。
每个 TUI 分开记录编号、tmux 入口、包版本、任务/run、结构化事实、产物核对和未覆盖项。
测试者只给一次普通中文需求并观察；不能补文件、替对象执行或拿旁路产物冒充通过。
不真实断网，受控故障限定为本测试的模拟依赖或明确测试进程。

建议下一步：组合回归和候选打包后，先运行官方模型原生 TUI 冒烟，再并行验收三包自然使用；
生命周期交错由唯一测试负责人执行，旧任务换代不能影响无关任务或使权限复活。
