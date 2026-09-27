# 固定上游能力清单与短剧样包覆盖

本文补充能力包迁移与验收的来源分母：先列固定版本实际公开了哪些入口、资料和执行支撑，再说明样包取了哪一片。**入口已收录、方法已改写、脚本已实现、模型实际采用、任务质量通过是不同事实。** 本文只做源码与文档盘点，不新增真实验收结论，不给出“内化百分比”。

## 1. 固定版本与盘点口径

| 对象 | 固定版本 | 本次核对范围 |
| --- | --- | --- |
| 上游 A：`zenstory-ai/drama-skills` | [`0e8929881bb59248618c4f402707c64723adc017`][a-tree] | Git 跟踪树、公开 Skill 元数据、入口工作流、关键工具接口，以及第 5 节指定的规则正文 |
| 上游 B：`eternityspring/shuohao-skills` | [`7ebef4f2f53159ee1eaaec2793271a114a8be8cc`][b-tree] | Git 跟踪树、全部 Skill 元数据、入口工作流、脚本 CLI，以及 `novel-art` 指定的 schema、方法和校验实现 |
| 本仓库样包映射基线 | `e72a1d337a32913a5d1ce129f0e963b37b41aaaa` | `drama-text-a` **0.1.2**、`drama-workflow-b` **0.1.1** 的声明、入口、方法、模板、来源说明和检查器 |

上游副本的 HEAD 与上述 SHA 一致，工作树无改动；未更新到最新版本。本仓库映射通过固定 Git 对象读取，**不包含盘点时其他开发者正在修改的样包**。以后提升包版本，应重新记录对应行的变化，不能把新版本能力回填成旧版本已通过。

“全部入口”的可复核口径是固定树中全部 `SKILL.md`，并另列 Skill 外的共享工具；不是根据 README 五阶段示意图估计功能数。

| 树索引事实 | 上游 A | 上游 B |
| --- | --- | --- |
| Git 跟踪文件总数 | 514 | 84 |
| `skills/` 下文件数 | 230 | 65 |
| 创作 Skill 入口 | 11 个 | 5 个 |
| 其他 Skill 入口 | `maintainers/skills/short-drama-knowhow/SKILL.md`，仅维护者主动使用 | 未发现其他 `SKILL.md` |
| 另列的共享能力 | 项目/Dashboard、引用迁移、评测证据检查 | 报告组合、安装/卸载软链 |

树索引覆盖了上述固定树的全部路径；阅读深度另记如下，不能用文件数冒充逐行审阅：

- **入口阅读**：读取全部 Skill 的元数据与工作流要点、所链接的资料和产出说明；A 同时读取 11 份 `agents/openai.yaml`。该 YAML 是展示和默认提示词元数据，不是独立执行器。
- **工具接口阅读**：静态查看脚本入口、参数、子命令、导入及相关注释；未逐行审计所有脚本实现。
- **重点深读**：A 的 storyboard `stage-contract.md` 全文及 checker 的边界、时长、覆盖相关实现；B 的 novel-art `schema.md`、`scene-pass.md`、`prop-pass.md`、`gateReport` 与 `validate`；本仓库两份固定版本检查器全文。
- **仅索引或未展开**：其余长篇参考、全部上游测试用例正文、历史评测产物、媒体文件和完整供应商适配器实现。本次没有读取验收保留集来调整方法，没有运行上游脚本、安装器、检查器、模型或 TUI。

## 2. 上游 A：全部入口及样包映射

A 的当前创作主线以五份可读 Markdown 为创作事实：`剧本.md`、`视觉设定.md`、`分镜.md`、`图片提示词.md`、`视频提示词.md`；剪辑另有 `剪辑单.md`。树内仍有结构化样例和校验器，不能据此声称创作主线要求同时维护另一套 JSON 真源。本仓库样包 A 的聚合 JSON 是自己的小切片格式，**不是上游文件格式兼容层**。参见固定版 [README][a-readme] 和 [总入口][a-core]。

下表“样包 A”均指上述 0.1.2 基线。“方法切片”不等于该入口已完整迁移。

| 公开入口 | 上游实际能力与关键资料 | 样包 A 已有部分 | 未迁移或未证明部分 |
| --- | --- | --- | --- |
| [`short-drama`][a-core] | 初始化、查看/继续项目、按阶段路由；创作者权威与制作形态；可选 Look Development；项目发布/接受、文本交付包、当前快照导出、Dashboard。资料含 `creator-workflow`、`contract-and-ownership`、`production-form-profiles`、`reference-roles`、`audience-reveal`、`pickup-and-alternate` | `CAPABILITY.md` 给短篇创作路径和包内导航；任务身份与恢复沿宿主 | 没有迁移项目状态机、Dashboard、创作者接受记录、交付包工具、造型试验及补拍/备选流程；包安装启用不是创作者批准作品 |
| [`short-drama-novel-analyze`][a-analyze] | S0 章节索引；S1 全书抽样快评；S2 逐章功能提取；S3 剧情单元/节奏聚合；S4 人物别名与设定归并；S5 改编价值、分集候选、对照修订初次快评；有索引、覆盖与断点资料 | 带编号的短篇段落、当前原文字节摘要、采用/省略检查 | 没有长篇章节索引、抽样覆盖、逐章提取与合并管线；段落编号齐全不证明读完或理解长篇；分析候选也不能直接冒充资产身份 |
| [`short-drama-develop`][a-develop] | 原创/改编契约、机制不同的方向、故事引擎、分集地图、导演阐述候选；多集整稿精确索引/切片/合并/续跑；题材卡、前提装置、人物前史与信息权限、机制循环、揭示/反转/兑现、参考作品吸收 | `premise`、改编说明、场次设计和未决项；方法要求保留原文事实与新增内容的区别 | 没有完整开发模板、系列弧线、题材卡库、导演阐述及整稿接入工具；没有迁移上述全部创作方法 |
| [`short-drama-write`][a-write] | 单集剧本创作/修订；既有稿规范化且保留作者语义；场景/动作/对白、画外音、内心声、音效与画面文字；可选对白预算、剧本机械索引、录音表；分场交接与声音戏剧方法 | 场次摘要、角色引用、原文依据、镜头动作及人工内容复核 | 不是完整剧本写作套件；未迁移规范剧本模板、逐字台词/录音表、对白语速估时、声音方向、剧本块索引及其全部规则 |
| [`short-drama-assets`][a-assets] | 区分角色身份/造型、地点/视角、道具/状态；出现证据；跨场连续性变化与锁；声音方向。资料有 `identity-vs-variant`、`occurrence-extraction`、`continuity-delta`、`continuity-lock` | 角色 ID、场次人物、镜头可见人物；方法提醒持物与状态连续性 | 没有完整角色造型、地点视角、道具状态、出现记录、变更影响范围或连续性锁格式；可见角色外键正确不能证明造型/持物连续 |
| [`short-drama-image-prompts`][a-image] | 人物/场景/道具板、造型状态变体、生产设定页、局部编辑、Look Development 提示词；明确参考图可以和不可以控制什么；只写提示词，不生成图片 | 无专用图片提示词能力；只保留“不把文字当真实参考图”的边界 | 全部提示词模板、编辑保留契约、参考语义、造型试验与生产设定页未迁移；没有图片生成与质量证据 |
| [`short-drama-storyboard`][a-storyboard] | 镜头职责、来源覆盖、调度/信息揭示、镜头起止、冻结关键帧、视觉依据、参考选择；可选 coverage audition 与场次视觉计划；镜头拆并后的身份谱系；SHT/CON 分级规则 | `scene_id`、正时长、起点/动作/终点、可见人物；逐场镜头合计、显式目标差值；人工检查来源与动作 | 没有完整关键帧/视觉依据/真实参考图合同、覆盖试镜、镜头版本谱系或供应商适配；第 5 节的连续性语义不能由现有脚本保证 |
| [`short-drama-video-prompts`][a-video] | 从已接受镜头边界写运动、表演、运镜、声音与时间线音乐提示词；区分真实 `REF`、外部挂图 `PLAN`、待补与明确文生选择；目标档案、H3/Seedance 方言、动作及对白窗口、实际视频/尾帧续接 | 无专用视频/音乐提示词能力 | 未迁移各模型方言、容器/动作/音乐规格、逐字声音边界、真实媒体续接；本包的秒数不是供应商支持证明，也不是实测片长 |
| [`short-drama-produce`][a-produce] | 当前 job 预览、明确确认、执行、状态、收集、审计；图片/视频/TTS/音乐按目标适配；记录供应商任务以处理中断，不把未取回结果当应重投 | 无 | 全部外部媒体执行、确认消费、供应商句柄、原子收集与失败恢复未迁移；不能把读取该方法或脚本存在算作实际生产能力 |
| [`short-drama-edit`][a-edit] | 已有素材逐段可用带、声音区间、入出点/镜序、画面校正、逐字字幕、剪辑单、渲染和测量；缺素材明确区分片段与成片 | 无 | 未迁移剪辑、字幕、编码、响度/片长测量；工具不自动完成任意混音、特效或素材规格转换，上游自身也保留外部工具边界 |
| [`short-drama-review`][a-review] | 原著分析、故事剧本、资产/连续性、图片提示词、关键帧/运动、生产成果、隐私及项目校准；独立证据审查、反模板修订、按 owner 分派；不代改来源 | 私有 `methods/review.md`；结构、来源、创作与媒体结论分开 | 没有全部领域 rubric、媒体观看/听审、项目校准与独立终审流程；模型自审或结构脚本 exit 0 均不等于完整审查通过 |
| [`short-drama-knowhow`][a-knowhow]，维护者专用 | 授权只读学习 → 私有观察/决策 → 覆盖缺口与反例 → 去标识/去复刻 → 公共 reference/rubric/合成样例候选 → 新代理盲测 → 独立审阅 → 提升/缩窄/退役 | 仅借鉴“候选先于发布”的迁移原则；未作为运行能力安装 | 没有迁移其学习流程、卡片、覆盖矩阵和发布治理；不自动吸收私人材料，不把个人 Skill 总结等同领域包版本迭代；该入口不能算第 12 个普通创作 Skill |

### A 的执行支撑、模板与验证工具

以下路径相对上游 A 固定树。命令列来自源码参数声明或入口说明，只说明接口存在；本轮均未执行。

| 路径 | 作用或公开接口 | 当前样包对应 |
| --- | --- | --- |
| `skills/short-drama/scripts/project_tool.py` | `init/status/publish/accept/review/set-authority/package/verify/export`；项目来源、接受与交付生命周期 | 不迁移；避免与宿主任务/产物事实另立账 |
| `skills/short-drama/scripts/dashboard_server.py`；`assets/dashboard/` | 项目文件与运行摘要的浏览/编辑服务；有启动状态、退出和重启入口，静态页面/JS/CSS；不是媒体生产授权器 | 无 UI 或服务复制 |
| `skills/short-drama/scripts/creator_markdown_check.py` | 创作 Markdown 的标题、引用、时长、视觉/参考及声音相关确定性核对 | 未迁移；样包 JSON 检查不是该工具替代品 |
| `skills/short-drama-novel-analyze/scripts/novel_index.py` | `index/verify/sample/coverage`；章节边界、行号、样本与提取覆盖。入口明确其 verify 不检测同行数的原地内容改写 | 无；样包原文 SHA 核对是自己的字节合同，不声称沿用上游索引机制 |
| `skills/short-drama-develop/scripts/episode_intake.py` | `index/manual-index/verify/slice/progress/merge`；多集整稿接入与续跑 | 无 |
| `skills/short-drama-write/scripts/screenplay_index.py` | 剧本块索引及来源定位，供后续覆盖检查 | 无 |
| `skills/short-drama-write/scripts/duration_estimate.py` | 按有依据的对白/动作估时参数报告；不能把估计当实测 | 无；样包只核显式数值 |
| `skills/short-drama-write/scripts/voice_sheet_check.py` | 录音表引用、台词等核对 | 无 |
| `skills/short-drama-assets/scripts/asset_check.py` | 结构化资产与来源引用检查 | 无；只重新实现极少量角色/场次外键 |
| `skills/short-drama-image-prompts/scripts/image_prompt_check.py` | 图片提示词资料的引用与结构检查 | 无 |
| `skills/short-drama-storyboard/scripts/storyboard_check.py` | 结构化分镜的时长算术、关键帧 boundary 绑定、起止条目和来源覆盖；不是全部 SHT/CON 语义规则的实现 | 借鉴结构计量与内容审查分离；未复制脚本 |
| `skills/short-drama-video-prompts/scripts/container_check.py` | 视频交付容器结构检查 | 无 |
| `skills/short-drama-video-prompts/scripts/motion_timing_check.py` | 运动/表演时序资料核对 | 无 |
| `skills/short-drama-video-prompts/scripts/music_spec_check.py` | 音乐规格与时间线资料核对 | 无 |
| `skills/short-drama-produce/scripts/production_tool.py` | `prepare/confirm/run/collect/status/audit`；有外部调用、文件落盘与收费副作用边界 | 无 |
| `skills/short-drama-produce/scripts/provider_adapters.py` | 固定源码带 Seedance、GPT Image 2、MiniMax Music/语音/H3 的适配说明与实现入口；账号配置及真实可用性另验 | 无；未核全部适配器实现，未复验当前外部 API |
| `skills/short-drama-produce/scripts/fixture_adapter.py` | 离线生产夹具，不代表真实生成质量 | 无 |
| `skills/short-drama-edit/scripts/edit_tool.py` | `check/render/verify`；对已有媒体进行剪辑、字幕及测量 | 无 |
| `skills/short-drama-review/scripts/review_check.py` | 审查资料的结构与引用检查 | 无；样包 review 是缩小后的方法 |
| `tools/compact_refs.py` | 项目引用的 expanded/compact `sources` 表示转换；默认原地写，`--check` 只核对；与会话 Compact 无关 | 无，不复制另一套引用迁移入口 |
| `evaluations/content_quality_gate.py` | 核对封存、带来源的跨题材 A/B 评测证据及配置；不是故事质量自动判真器 | 无；仅阅读头部、参数及元数据，未执行或读取保留集正文 |
| 各创作 Skill 的 `scripts/selftest.py`；`tests/` | 单技能离线自检，另有结构、来源、独立安装、项目生命周期、Dashboard/浏览器、适配、声音/剪辑等测试 | 本轮只索引，不把上游测试数量或 CI 声明计为本仓库已通过 |

模板与方法的主要族已按入口索引：开发有 brief/engine/episode-map/adaptation-map；写作有 screenplay/episode-card/beats/voice-record-sheet；资产有 character-look/location-view/prop-state/occurrences/continuity；图片有 prompt-spec/Look Development；分镜有 shot/keyframe/coverage、视觉计划与修订谱系；视频有 motion/music/container/performance/coverage-scope；审查有 finding/verdict/supersession。**这些文件多数未逐份深读或迁移**，列出名称是待阅读清单，不表示样包已经包含它们。

## 3. 上游 B：全部入口及样包映射

B 的五阶段分别形成 `outline.json`、`cast.json`、`art.json`、`script.json`、`storyboard.json`，另可渲染 Markdown/HTML 与组合报告。样包 B 的单份 `drama_workflow_project.v1` 是重新设计的聚合资料，不是这五份上游 JSON 的等价替代。参见 [固定 README][b-readme]。

| 公开入口 | 上游实际能力与关键资料 | 样包 B 已有部分 | 未迁移或未证明部分 |
| --- | --- | --- | --- |
| [`novel-outline`][b-outline] | 长篇分块/卷级摘要；先骨架再分集节拍；改编阐述、人物定位、高光地图、分集梗概、计算得到的资产；可分阶段验证和体检；资料含 `volume-pass/outline-pass/episode-pass/schema` | `episodes.id/hook/ending/target_seconds` 及交接指导 | 没有完整改编五件套、分块/摘要、分阶段 schema、资产推导及全部质量规则；入口所称 14 项质量门未完整迁移或逐项实测 |
| [`novel-characters`][b-characters] | 分块人物扫描、名字/别名精确合并、语义候选复核、选角、并发人物画像、断点续做、assemble；原文逐字引文；性格/外观/声音、风格与多语言报告；可选设定图 | 人物 ID/姓名/`visual_anchor`、场次人物及对白说话人关系 | 没有 roster/profile 管线、引文逐字校验、人物卡完整 schema、音色提示词、多语言 UI、角色设定图；同名或包含关系不自动代表同人 |
| [`novel-art`][b-art] | 从大纲 seed 地点/道具与使用关系；空间锚点、光态、母场景/变体、道具尺寸/状态/关联；图片提示词、风格一致、可选设定图；11 项 gate 和结构校验 | 地点/道具 ID、姓名、`continuity_note`、场次关系、参考计划 | 没有原 anchors/lighting/variantOf/changes/states/usage 契约及 11 项 gate；没有真实图片或跨镜头持物时序证明，详见第 6 节 |
| [`novel-script`][b-script] | 从大纲 seed 集目标和节拍；场次动作/对白、在场或画外说话人、开场钩子与结尾；对白/动作估时；大纲/美术跨表检查，人物台词及声音面板 | 场次、动作/对白节拍、在场说话人、剧集/地点/人物/道具外键 | 没有完整剧本 schema、大纲兑现、画外音表达、语速/动作估时及全部内容门；入口所称 10 项 gate 不等于样包已有 10 项对应能力 |
| [`novel-storyboard`][b-storyboard] | 段→切→帧；来源节拍连续有序覆盖、单段时长与 H3 提示词对齐；按场景/光态组批、台词对齐；导出 H3 文本/图片顺序清单；可选参考帧；外部 shot-recipes 辅助 | 本场节拍到镜头的引用和覆盖、正时长、参考条目及逐集目标对账 | 没有完整 segment/cut/frame、逐字提示词、精确有序一次覆盖、H3 export、实际参考帧、视频与合成；入口所称 17 项 gate 含可选外部卡库项，未读取卡库时须说明跳过 |

### B 的全部主要 CLI 与共享支撑

下表静态核过各脚本的命令分派。通用 `--help` 不重复列；不把方法里的模型创作步骤当成脚本自行完成的功能。

| 路径 | 实际公开子命令或选项 | 验证与边界 |
| --- | --- | --- |
| `skills/novel-outline/scripts/novel-outline.mjs` | `chunk/validate/checkup/render/assets/slug`；validate 可指定 `skeleton/beats/full` | 分块/计算/结构与质量门、MD/HTML；创作判断仍需模型/人 |
| `skills/novel-characters/scripts/novel-characters.mjs` | `seed/chunk/merge/assemble/validate/render/ui-template/styles/slug` | 可显式 `merge --apply`；候选语义归并要复核；validate 可读取原文逐字检查引用；不自行完成画像创作和图片生成 |
| `skills/novel-art/scripts/novel-art.mjs` | `seed/validate/checkup/render/styles/slug` | seed 读取 outline；schema + 11 项美术 gate；可选 cast 决定人名检查范围，详细边界见第 6 节 |
| `skills/novel-script/scripts/novel-script.mjs` | `seed/validate/checkup/render/slug` | 可传 outline/art 进行跨资料检查，缺失时不能宣称完成该部分检查 |
| `skills/novel-storyboard/scripts/novel-storyboard.mjs` | `seed/validate/checkup/render/export/stats/slug` | validate/render 需要 script；导出生产提示词与顺序清单，不调用真实视频服务 |
| `scripts/report.mjs` | `--from` 或显式 `--outline/--cast/--art/--script/--storyboard`，以及 `--out/--lang/--title` | 通过现有 render 子命令组装多面板；处理 CSS/脚本作用域和相对图片路径。某面板渲染失败会跳过，不能把总 HTML 存在当五段全部成功 |
| `scripts/install.sh` | 全部或指定 Skill，`--claude/--codex/--uninstall` | 写用户 Skill 目录软链，真实目录不覆盖；可替换软链。**本能力包不使用该安装路径**，不写个人/公共 Skill 库 |
| 五份 `scripts/selftest.mjs`；`scripts/report-selftest.mjs` | 本地合成自检与报告组合检查 | 只确认这些文件存在及其入口；未运行，未把 README 的通过数当本次证据 |

B 的关键方法/模板支撑另含：characters 的 `roster-pass/profile-pass/style-presets/sheet`，art 的 `scene-pass/prop-pass/sheet`，script 的 `script-pass/schema`，storyboard 的 `storyboard-pass/frame/h3-prompt/schema`，各模块的 `report-style`。仓库还带公开示例 JSON、预览图片和少量 shot-recipes 测试夹具；示例与测试夹具不是额外生产 Skill，也不等于已安装完整外部卡库。本样包没有复制上游报告样式、图片、示例小说或 `.mjs` 实现。

## 4. 当前两个样包的可核查首期清单

本节只描述固定基线已存在的代码和方法，不把“方法要求去做”写成“每次都实际做到了”。各包声明 10 个随包资源；源码目录各有 11 个跟踪文件，其中 `declaration.json` 是构建输入，不作为这 10 个资源之一。

| 层次 | A 0.1.2：`drama-text-a` | B 0.1.1：`drama-workflow-b` |
| --- | --- | --- |
| 入口与私有资料 | `CAPABILITY.md`、`methods/workflow.md`、`methods/review.md` | 同名文件独立属于 B；不覆盖 A 的方法 |
| 合成输入/空模板 | `resources/example-source.json`、`resources/example-delivery.json`、`templates/delivery.json` | `resources/example-project.json`、`templates/project.json` |
| 资料合同 | `drama_text_source.v1` → `drama_text_delivery.v1`；原文段落、人物、场次、镜头、省略和未决项 | `drama_workflow_project.v1`；剧集、人物、地点、道具、场次/节拍、镜头与参考条目 |
| 确定性工具 | `scripts/check_delivery.py --source <输入> --delivery <交付>` | `scripts/check_continuity.py --project <资料> --format json\|html` |
| 实际能核的结构 | 严格 JSON、输入上限、原文字节 SHA、唯一 ID、来源覆盖/明确省略、场次/可见人物引用、镜头起止/动作非空、场次至少被拍摄 | 严格 JSON、输入上限、唯一 ID、场次与集/地点/人物/道具关系、对白说话人在场、本场节拍引用和覆盖、参考条目到对象的关系及状态 |
| 实际能核的时长 | 镜头/场次有限正数；逐场镜头合计等于场次声明；总镜头合计与输入显式目标比较；没声明目标和非法目标分开 | 有限正数镜头，按 `shots.scene_id → scenes.episode_id` 逐集求和；显式目标比较；缺目标警告，非法目标/溢出/差值报错 |
| 数值边界 | `math.fsum`；相对容差 `1e-12`、绝对容差 0；未验证实际对白/动作可完成或真实片长 | 同左；全片总和相等不能替代逐集一致 |
| 报告 | stdout JSON；显式保留创作与媒体未检查警告 | stdout JSON，或转义全部文本的简易静态 HTML；不等于上游互动五面板报告 |
| 只能指导、不能自动证明 | 依据是否支持故事、改编新增是否明确、动作与持物是否连贯、因果/节奏/审阅质量 | 主子或跨包转换是否忠实、人物/道具位置和光态、叙事节奏、真实参考媒体 |
| 执行与权限 | 指导按同代资源引用物化原脚本，沿宿主原工具执行；脚本只读显式资料、向 stdout 报告，不改宿主任务状态 | 同左；B checker 不自动读取原文、其他阶段文件或子任务产物，不验证转换清单语义 |

查看固定内容可使用 `git show e72a1d337:<仓库相对路径>`。当前文件入口为 [A 目录](../../examples/capability-packages/drama-text-a/) 与 [B 目录](../../examples/capability-packages/drama-workflow-b/)，点击后看到的工作树可能已高于本节基线。

模型只读入口、只写报告、手写另一个检查器、没有原脚本执行回执，均不能算“原包检查已运行”。脚本结构通过不能推出全部来源语义或上游能力通过；本表也不替代 [真实验收记录](../tasks/CAPABILITY_PACK_ACCEPTANCE.md)。

### 4.1 本轮候选差异：A0.2.0 / B0.1.2（本地组件通过，真实待验）

上表仍保持固定旧映射。本轮候选仅增加下列明确内容，不将其他“未迁移”行改为已完成：

- A0.2.0 将交付/报告升级为 `drama_text_delivery.v2` / `drama_text_check.v2`，镜头显式保存 `source_ids`、`adaptations`、`unresolved`。来源限于当前场次声明段落，新增/未知为可见警告；合法编号仍不证明原文支持具体动作，未实现完整 CON/SHT 连续性检查。
- B0.1.2 保持原 `drama_workflow_project.v1` 及 checker，补齐项目占位模板；新增私有 `templates/handoff.json`，表达文件摘要、阶段输入输出、对象映射、省略/新增/未知。交接清单是业务资料，原 checker 不验证跨文件转换，不成为新的运行状态账。
- 两包补原子任务包级授权用法；不扩大缺省权限，不更改原快照/pin，不加入新执行器。B 原 checker 和公开示例字节保持，A 原合成来源字节保持。

新组合69个测试文件为1661 passed、9 skipped。其公开合成资料和假工具检查不能替代新包原生TUI、原脚本执行及内容质量证据；本轮尚未安装新版。原失败/未触发和候选保留集各0/3保持。

### 4.2 A0.2.1：把审阅清单迁为实际工作单元（本地候选）

本次仍固定 A 上游 `0e8929881bb59248618c4f402707c64723adc017`。定向核对 `short-drama-review` 的入口、review-method、stage-contract、四份审阅表及留存模板/检查脚本；具体读取范围和源文件 SHA 见 [A 来源说明](../../examples/capability-packages/drama-text-a/PROVENANCE.md#021-修订范围)。没有运行上游程序，也没有将索引覆盖改写成全仓精读。

当前入口和阶段合同以 Markdown 成对证据、修订结果、保持项及作者/审者分工为主；目录留存 JSON 模板与 `review_check.py` 和入口描述不一致，不推断它们已正式废弃，也不将其迁成另一套裁决账。

| 工作单元 | 新的包内资源 | 实际迁移与限制 |
| --- | --- | --- |
| 原文与下游成对证据 | `methods/review-source.md` | 对照来源、改编新增、未知、人物关系及场次实际变化；不是原文语义自动裁判 |
| 同镜、相邻镜与跨场接续 | `methods/review-continuity.md` | 读取上一终点/过渡/下一起点及相关状态，保留有依据的叙事省略；未实现完整 CON 结构合同或媒体检查 |
| 并行合并与最终陈述核对 | `methods/review-delivery.md` | 读取实际子产物、转换两端和原检查回执，新增拟陈述与真实事实对照；不新增宿主完成门 |
| 可修订反馈 | `templates/review.md` | 新编 Markdown 模板，保留问题、依据、应恢复结果、保持项及复读范围；不是机器验收状态 |

入口和 review/workflow 已接入按需读取，声明资源由 10 增为 14；入口字符数由 2915 减至 2546。原检查器、v2 JSON 模板、两份合成资料与许可证字节不变，B/C 未改。候选时65项A定向组件与两次同字节构建通过；后续938+A0.2.1原生复验已完整读取三个分表，原checker实际通过，来源/接续语义仍失败。第4节历史基线及前轮失败保持，不把方法迁移或读取当成完整来源覆盖或内容质量通过。

### 4.3 A0.2.2：普通创作的作者修订（候选）

本次固定来源不变，补读总入口、creator-workflow、write及storyboard owner 的交稿前步骤，四份来源文件与固定Git对象同字节，摘要见[本包0.2.2来源说明](../../examples/capability-packages/drama-text-a/PROVENANCE.md#022-修订范围)。该范围显示：普通创作以当前成稿反查并直接修订原文档；正式审稿是用户点名的工作，独立审者不默认代改作者文件。未发现普通创作必需但漏迁的自动审阅执行器或语义脚本。

反向证据是A0.2.1已要求作者修订、逐镜和相邻状态检查；本次原生轨迹没有发现问题后拒绝修改的行为，最终反而声称无漏项。因此只能提出责任/次序澄清的改进假设，不能确定其为本例唯一原因或已排除模型判断限制。

A0.2.2只调整已有入口/workflow/review的接缝，普通作者回读候选、用原三分表逐项反查、将明确问题修入同一作品，再查最终字节并准确汇报；用户只要审稿时仍交还问题。现有14资源不扩张，三分表、原checker、模板和许可保持；不增加必交报告、固定写次数、独立代理或宿主完成门。流程明确与实际充分执行分别验，构建通过不能代替新的原生内容证据。

### 4.4 成稿可见人物依据的下一迁移切片（待设计实施）

固定上游仍为`0e8929881bb59248618c4f402707c64723adc017`。本次补读27条SHT、7条CON、storyboard/资产连续性方法及当前`creator_markdown_check.py`的实体解析、具名覆盖与真实调用点；未全读所有脚本和测试，未执行上游。当前Markdown主线只修订原作者文档，树内留存的JSON模板/checker不代表普通任务必须另建索引、QA或接受记录。

已明确的可迁移工作单元：storyboard `SKILL.md:60–71`先完成镜头正文，再反向提取实际可见实体，明确画外项；core `creator-documents.md:79–100`定义清单/画外/匹配退出，`creator_markdown_check.py:544–822,1267–1316`实际解析声明并检查具名正文覆盖。这对应SHT-22/26的结构辅助，当前A只有人物外键、没有该核对。

下一片先限定A已有角色范围，在同份交付内复用cast及visible_character_ids，设计明确画外、代称与字面诊断退出的版本合同。辅助只报告名称出现位置与声明不对应，正文含义保持warning/作者判断；名称被提及不等于人物必须在场，不自动补人、改剧情或翻转宿主任务状态。标准库即可实现，但不能只抄`name in text`就称完整移植；需核字界、长名优先、歧义退出、错误定位及有界输入，保留MIT来源。

非覆盖范围：代词/未点名主体、实际画面真假、来源取舍、持物因果、地点/道具/造型、关键帧/参考图仍未迁完。上游CON-01/02及SHT-14也依赖语义判断；CON-07锁的是跨镜不变外观，不能拿来锁定会变化的持有人。A21已完整取得方法仍漏掉口袋→手，A22完整workflow及实际回读仍漏掉人物清单矛盾，反证继续叠加同义提醒不足以关闭问题。这一子集尚未实现，不承诺能使两例整体通过，也不增加宿主模型循环或质量硬门。

## 5. A 的 CON / SHT-14：可迁移的连续性证据

固定版 [storyboard 阶段合同][a-stage] 的 `SHT` 共 27 条、`CON` 共 7 条。规则分为可机械核对的结构、需要证据判断的语义、可覆盖的创作默认和创作者选择；不能把整个表统称为确定性检查器已实现。

| 规则 | 上游真实表达与等级 | 可迁移方法与当前缺口 |
| --- | --- | --- |
| SHT-14 | `reviewed_invariant`：争夺中的移动物件跨切保持持有关系、轨迹、方向、时间/轮次状态和终点；允许有授权的省略 | 可指导逐镜引用已有来源，复核交接前后状态。A 只有自由文本起点/动作/终点及人工核对，脚本不理解持有人和轨迹；字段非空不能证明成立 |
| CON-01 | `structural_invariant`：相连终点与下一起点相符，或有明确 owner 修订 | 当前没有完整链接状态对账；不能用两段都写了“结束/开始”代替相符证明 |
| CON-02 | `reviewed_invariant`：知识、伤势、持有、天气、光照、物理状态不能无故事原因瞬移或倒退 | 可迁移证据式审阅；不是关键词命中、姓名对齐或自动剧情判定 |
| CON-03 | `craft_default`：只跟踪下游相关变化，不逐镜重抄整个设定集 | 可直接用于简洁方法；不强制每镜列固定数量字段 |
| CON-04 | `structural_invariant`：变化说明 before/after、原因/来源场景、生效范围、受影响可见 ID | A 基线没有这套完整表达与核对；如引入，需先明确资料合同及兼容边界，不能为一个失败故事凭空补事件 |
| CON-05 | `taste_option`：创作者声明的蒙太奇、省略、梦境、主观画面可以有意打破通常连续性 | 保留例外依据；未知时记待确认，不能擅自把断裂解释成授权省略 |
| CON-06 | `structural_invariant`：变化列明受影响的现存下游文档；未来工作只描述 | 当前没有跨五文档影响检查；不要为了补齐表格提前造不存在的文件或任务 |
| CON-07 | `structural_invariant`：视觉设定中的连续性锁规定原样表述与镜头/图片作用域，范围内关键帧、运动和图片提示词保持该表述 | 当前没有完整锁面和范围传播；不能仅凭角色 ID 未变宣称服饰/物件或真实画面不变 |

同一合同的 SHT-16 只要求时长算术与目标差值诚实报告，SHT-22/23 又区分视觉依据、真实图片与外部挂图计划。由此适合迁移的是“来源→职责→状态→下游影响”的可读方法和已经能客观核对的字段关系；不是增加通用宿主的“剧情合理才允许交付”硬门。

本轮对 `storyboard_check.py` 的定向阅读可确认时长、边界绑定和覆盖等机械检查入口，**没有据此证明其自动判断 SHT-14 或全部 CON**。规则文本、checker 实现和实际媒体表现应分别取证。

## 6. B 的 novel-art：资产连续性实际表达

已深读固定版 [schema][b-art-schema]、[场景方法][b-art-scene]、[道具方法][b-art-prop] 和 [脚本 gate/validate][b-art-code]。上游没有只靠一句“保持一致”代表资产设计；它把多种可见事实拆开表达：

| 对象 | 固定源码中的表达 | 对连续性的帮助与边界 | B 样包 0.1.1 |
| --- | --- | --- | --- |
| 场景身份与外观 | `id/name/primary/summary`，`anchors[{name,desc}]`，`lighting[{state,prompt}]` | 以稳定空间锚点和原剧情出现的光态区分可复用底景，不随意生成整套昼夜状态 | 仅 `locations.id/name/continuity_note`；无对应完整契约 |
| 母场景与变体 | `variantOf`、`changes` | 指向实际母场景并说明变化；方法要求继承锚点，变体出图可参考母图 | 无母子场景/变更 schema 或原 gate |
| 道具身份与可见状态 | `id/name/scale/summary`、锚点、`states[{state,prompt}]`、`image` | 尺度与状态服务叙事道具，状态由剧情提取；布景细节与需要独立设计的道具区分 | 仅 `props.id/name/continuity_note`，不能冒充状态设计和图像验证 |
| 出现与关联 | `usage.episodes/beats`，道具 `relatedScenes`；可选 `carriedBy` | 使用关系来自上游；`relatedScenes` 可查已有场景。`carriedBy` 是名字文本，不是完整逐镜交接/持有时间线 | 场次的 `prop_ids` 等只证明引用存在；无完整 usage 或持有变化核对 |
| 图片与语言 | `image.prompt/negativePrompt/sheet/tags`、风格；英文图片提示词与报告语言分离 | 可选逐资产出图及报告渲染；提示词存在不证明生成或图像相同 | 仅参考条目的 planned/provided 声明，媒体不自动验证 |

`gateReport` 实际有 11 个 ID：`anchors`、`lighting`、`no-people`、`english`、`no-names`、`variants`、`style-match`、`prop-states`、`prop-scale`、`prop-hands`、`prop-white`。它们检查数量、非空、已有引用、支持值及部分提示词字符串条件；`validate` 另核必要字段、唯一 ID、`relatedScenes` 和 usage 数组等。

这 11 项不能被扩大解释：未传人物资料时相关人名检查可能没有输入；没有道具时相应道具 gate 不会证明实际做过道具设计；字符串检查通过不等于图中无人、手没有出现、背景真正纯白或光态一致。脚本没有因此证明每一镜的物件持有和空间轨迹。上游方法也需要创作与图像审阅。

后续可先迁移“锚点/光态/变体/状态/来源用途分开”的方法与示例，再决定哪些已有资料字段有必要扩展。任何结构扩展须说明权威输入、可核事实与未知项，不能把故事中未发生的交接写成新字段来让测试变绿。A 的 SHT-14 与 B 的资产设计相互补充，**不是同一能力的重复实现**。

## 7. 依赖、许可与验证边界

| 范围 | 固定上游声明/源码事实 | 迁移时要另行满足的条件 |
| --- | --- | --- |
| A 文本与本地脚本 | README 声明 Python 3.9+；所盘点核心 Python 工具使用标准库；Skill 本身依赖可读写文件的 Agent | 不把“stdlib”理解为不需要模型、权限、足够上下文或输入；本轮未运行其代码 |
| A 媒体生产 | 外部 adapter 配置、实际账号及授权；固定树提供多供应商参考 | 按具体服务的接口、费用、凭据隔离和素材授权另验；本文不确认当前线上 API 或账号可用性 |
| A 剪辑 | `ffmpeg`、`ffprobe`；默认字幕需要相应能力。可选 Remotion 模板固定 `@remotion/cli`/`remotion` 4.0.418、React/React DOM 19.0.0 | 编码器、字体/字幕、Node/npm/浏览器与素材分别确认；不因文本方法迁移就安装或运行这些依赖 |
| A 验证环境 | CI 声明 Python 3.9/3.10/3.14、Node 20、unittest；另有 Python 3.12 + Playwright 1.58.0/Chromium 的 Dashboard 检查及 Windows 回归 | 这是固定 CI 配置，不是本次运行结果；媒体、浏览器、账号和平台仍需独立实测 |
| B 脚本 | Node 18+，核心 `.mjs` 仅标准库；report 通过 `child_process` 调原 render；安装器需要 Bash/软链 | 不需要 npm 库不等于没有运行环境或模型成本；README 只声明作者在 macOS + Node 24 验过，不能自动扩为全平台验证 |
| B 可选图片 | 入口给 Codex/imagegen 的宿主使用路径；没有时交付提示词并说明未出图 | my-agent 接线、实际视觉模型、工具授权和图片输入输出需单独设计/验收；不复制外部 Agent 的调用当已接通 |
| B 可选 shot-recipes | storyboard 可消费独立卡库；本树有少量测试夹具 | 缺卡库时明确相关项未检查；单独核完整卡库来源、版本与许可，不以测试夹具冒充安装完成 |
| A 许可 | [MIT LICENSE][a-license]，版权声明为 drama-skills contributors；样包 A 保留对应许可文本 | 方法改写的来源与许可持续保留；输入小说、图片、音频及私有项目使用权不由代码许可自动授予 |
| B 许可 | [Apache-2.0 LICENSE][b-license] 与 [NOTICE][b-notice]；样包 B 保留两者 | NOTICE 的原样例声明保留不代表样包复制了该故事；不复制商标、私人材料或未经授权媒体；外部工具/素材另核许可 |
| 当前样包 | 新写代码/文档按主仓库 Apache-2.0，各保留上游许可；Python 标准库小脚本，无媒体 adapter | 不沿上游安装器写全局 Skill；不复制 Dashboard、项目状态机或第二执行器。构建、组件验证、真实采用和质量分别报告 |

许可盘点仅说明固定文本及当前保留位置，不扩大第三方材料授权范围。本次没有执行任何上游自检、真实媒体生产或样包 checker，因此不存在可由本文新增的测试通过次数。

## 8. 后续迁移和验收如何使用这份清单

1. 从第 2、3 节选择明确范围，先读该模块剩余合同、模板与实现；给每项记录“方法改写 / 确定性工具 / 外部依赖 / 暂不迁移 / 待核实”，不要只写“已学习某仓库”。
2. 保持 A/B 独立包及各自私有命名空间。跨包协作使用显式来源与字段/ID 转换说明；个人 Skill 自动总结、公开 Skill 安装和领域包迭代继续分开。
3. 优先补可独立交付的文本方法和客观核对；长篇索引、五阶段完整资料、可见资产设计、媒体生产和剪辑应分别立范围，不能一次读取入口就宣称全部拥有。
4. 新增字段先确定来源事实和消费者，再写模板、检查器和合成反例；来源无法支持的内容保留未知。语义质量给证据与修订意见，不变成宿主专项权限/交付硬门。
5. 包版本候选完成后沿现有构建、显式安装启用与回退流程验收。依次区分：包被选择/固定、入口加载、私有方法读取、原资源复制、原脚本真实执行、交付内容质量；缺一项就记录该项未覆盖。
6. 真实验收仍使用独立原生 TUI、核对实际官方模型来源、冻结输入与版本并只发一次普通业务需求；开发者不补产物、不代跑 checker。媒体能力另需实际媒体证据，纯文本检查不能代替。

本次文档只新增来源清单；包修订及其验收由相应负责人单独记录。建议下一步先按第 5、6 节完成当前包方法/模板的范围评审，再决定下一独立能力切片；只读来源审阅可并行，包实现和发布各保持单一负责人。

[a-tree]: https://github.com/zenstory-ai/drama-skills/tree/0e8929881bb59248618c4f402707c64723adc017
[a-readme]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/README.md
[a-core]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/skills/short-drama/SKILL.md
[a-analyze]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/skills/short-drama-novel-analyze/SKILL.md
[a-develop]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/skills/short-drama-develop/SKILL.md
[a-write]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/skills/short-drama-write/SKILL.md
[a-assets]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/skills/short-drama-assets/SKILL.md
[a-image]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/skills/short-drama-image-prompts/SKILL.md
[a-storyboard]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/skills/short-drama-storyboard/SKILL.md
[a-video]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/skills/short-drama-video-prompts/SKILL.md
[a-produce]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/skills/short-drama-produce/SKILL.md
[a-edit]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/skills/short-drama-edit/SKILL.md
[a-review]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/skills/short-drama-review/SKILL.md
[a-knowhow]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/maintainers/skills/short-drama-knowhow/SKILL.md
[a-stage]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/skills/short-drama-storyboard/references/stage-contract.md
[a-license]: https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/LICENSE
[b-tree]: https://github.com/eternityspring/shuohao-skills/tree/7ebef4f2f53159ee1eaaec2793271a114a8be8cc
[b-readme]: https://github.com/eternityspring/shuohao-skills/blob/7ebef4f2f53159ee1eaaec2793271a114a8be8cc/README.md
[b-outline]: https://github.com/eternityspring/shuohao-skills/blob/7ebef4f2f53159ee1eaaec2793271a114a8be8cc/skills/novel-outline/SKILL.md
[b-characters]: https://github.com/eternityspring/shuohao-skills/blob/7ebef4f2f53159ee1eaaec2793271a114a8be8cc/skills/novel-characters/SKILL.md
[b-art]: https://github.com/eternityspring/shuohao-skills/blob/7ebef4f2f53159ee1eaaec2793271a114a8be8cc/skills/novel-art/SKILL.md
[b-script]: https://github.com/eternityspring/shuohao-skills/blob/7ebef4f2f53159ee1eaaec2793271a114a8be8cc/skills/novel-script/SKILL.md
[b-storyboard]: https://github.com/eternityspring/shuohao-skills/blob/7ebef4f2f53159ee1eaaec2793271a114a8be8cc/skills/novel-storyboard/SKILL.md
[b-art-schema]: https://github.com/eternityspring/shuohao-skills/blob/7ebef4f2f53159ee1eaaec2793271a114a8be8cc/skills/novel-art/references/schema.md
[b-art-scene]: https://github.com/eternityspring/shuohao-skills/blob/7ebef4f2f53159ee1eaaec2793271a114a8be8cc/skills/novel-art/references/scene-pass.md
[b-art-prop]: https://github.com/eternityspring/shuohao-skills/blob/7ebef4f2f53159ee1eaaec2793271a114a8be8cc/skills/novel-art/references/prop-pass.md
[b-art-code]: https://github.com/eternityspring/shuohao-skills/blob/7ebef4f2f53159ee1eaaec2793271a114a8be8cc/skills/novel-art/scripts/novel-art.mjs
[b-license]: https://github.com/eternityspring/shuohao-skills/blob/7ebef4f2f53159ee1eaaec2793271a114a8be8cc/LICENSE
[b-notice]: https://github.com/eternityspring/shuohao-skills/blob/7ebef4f2f53159ee1eaaec2793271a114a8be8cc/NOTICE
