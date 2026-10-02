# 来源与迁移范围

参考来源：[zenstory-ai/drama-skills](https://github.com/zenstory-ai/drama-skills)，固定 commit `0e8929881bb59248618c4f402707c64723adc017`。这是已读本地公开副本的版本，本轮未联网确认最新版。

已读相关入口：`skills/short-drama-storyboard/SKILL.md`、各发布 Skill 元数据、`skills/short-drama/scripts/project_tool.py` 的生命周期说明及生产脚本引用入口。
本包方法为有来源标记的改写，原检查脚本和合成故事为新编写；0.3.0 的名字覆盖算法另按上游改写，差异见对应修订节。没有复制上游运行时、安装器、生产账本或用户材料。原文/场次/镜头 JSON 是本样例格式，不是上游兼容格式。

| 上游范围 | 本包映射 | 当前覆盖与缺项 |
| --- | --- | --- |
| 原著分析与改编 | `methods/workflow.md`，当前原文字节摘要、段落依据和省略检查 | 只做短篇文本切片；长篇索引、全量章节分析未迁移 |
| 单集剧本与资产 | 场次、角色引用和创作审阅 | 无完整上游模板、人物变体和所有质量规则 |
| 分镜与冻结关键帧 | 起点/动作/终点、可见角色、场次引用和场次/镜头各自正时长 | 只保留核心方法与结构核对；节奏仍需审阅，真实参考图和供应商时长兼容未验 |
| 生产、剪辑、音乐 | 无 | 未迁移，缺生成服务与媒体执行验收 |
| Dashboard、项目接受/发布 | 无 | 不复制上游状态机；沿宿主现有任务与交付事实 |

包内新编写代码及文档按主仓库 Apache-2.0（`LICENSE`）；改写方法及名字算法同时保留上游 MIT 版权许可（`licenses/drama-skills-LICENSE`）。素材只含本样例合成数据。
构建/脚本组件验证不代表自然召回或真实 TUI 通过；完整来源覆盖尚未完成，不能宣称 100% 内化。

## 0.1.1 修订范围

修正本包自己的指导和模板：镜头字段补齐 `seconds`，说明 `source_sha256` 必须由真实输入文件字节计算，区分宿主文件版本与内容摘要，并要求如实记录已执行、待执行和未通过的结构检查。空模板改为待填写的完整条目形状，占位值不表示有效交付。

原 `scripts/check_delivery.py`、`resources/example-source.json` 和 `resources/example-delivery.json` 字节保持不变；不放宽来源、时长或引用检查，不增加宿主专项规则。仍使用相同 `drama_text_delivery.v1` 合同，0.1.0 的包和验收结果不被本次修订改写；新版本需独立构建与验收。

## 0.1.2 修订范围

本次重新核对固定来源中的 `skills/short-drama-storyboard/references/stage-contract.md`、`references/review-and-fixtures.md` 与 `scripts/storyboard_check.py` 的时长求和和目标差值合同，以及 `skills/short-drama-novel-analyze/references/chapter-extraction.md` 的事实追溯边界。参考其“结构计量与语义审阅分开”的做法，扩展本包自己的标准库脚本，没有复制上游状态机或执行链。

脚本按唯一镜头 ID 求和，核对每场镜头秒数与场次声明，再核对总镜头秒数与当前输入显式 `target_seconds`。数值比较只接受浮点舍入误差；不存在目标字段时报告未声明，非法目标和求和溢出返回结构化错误。旧引用、来源摘要和正时长检查保留，输出仍为 `drama_text_check.v1`，增加镜头合计、逐场合计及目标差值指标；输入/交付 schema 不变。显式目标的一致性是本包已有制作资料的客观约束，不复制上游固定镜长、语速或创作质量阈值。

入口和方法同时明确：只有真实运行的结果才能称为已验证；来源事实、改编新增、持物连续性和创作质量仍需独立阅读，不能从结构通过推断。并行阶段合并沿现有来源/场次/角色引用记录完整转换关系，不新增宿主状态或权限。

0.1.2 修改校验脚本；上节“字节保持不变”只描述 0.1.1 当时的修订。公开合成原文、示例交付和模板本次均保持不变。旧版本包及真实验收结果不回写，新版组件验证不代表新版已通过真实 TUI。

## 0.2.0 修订范围

再次按上述固定 commit 读取[事实提取](https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/skills/short-drama-novel-analyze/references/chapter-extraction.md)及[分镜合同](https://github.com/zenstory-ai/drama-skills/blob/0e8929881bb59248618c4f402707c64723adc017/skills/short-drama-storyboard/references/stage-contract.md)，参考原文追溯、未决事实与结构/语义审阅分离的原则，为本包新编写镜头依据校验，没有复制原执行器或所有质量规则。

资料显式升级 `drama_text_delivery.v2`、报告升级 `drama_text_check.v2`；每镜增加 `source_ids`、`adaptations`、`unresolved`，原输入仍为 v1。脚本核对本场来源引用及三个列表，提示新增/未知需要审阅，不解析正文判断真实支持关系。`covered_passages` 的场次覆盖含义保持；顶层与镜头未决事项不混为一份。

本次模板与公开合成交付同步升级，示例把原文未明说的拾书/指认动作标为改编；原始合成 source 字节不改。没有自动转换或兼容旧 v1 交付的旁路，旧包 ZIP 和既有真实结果保留。字段检查不能保证模型不漏标，也不提供道具状态推理、真实视觉生成或全部上游能力。组件与新版本真实 TUI 结果分别记录，尚未真实验收。

## 0.2.1 修订范围

固定来源仍为 `0e8929881bb59248618c4f402707c64723adc017`。本轮核对 `skills/short-drama-review/` 的当前入口、阶段合同与审阅方法，并定向读取原著分析、故事、视觉运动及资产审阅表；本地来源文件与固定 Git 对象一致，未联网更新。

当前 `SKILL.md` 和 `references/stage-contract.md` 明确以 Markdown 保留问题、证据、影响、修订结果和作者职责，不建立 JSON/JSONL 裁决或另一份创作真源。同目录仍留存 `assets/finding-template.jsonl`、`assets/verdict-template.json` 和 `scripts/review_check.py`，它们核对声明形状而不打开证据正文；这些资源与当前入口描述不一致，不能据文件存在将它们当作当前主线，也不能推断其已正式废弃。本次不迁入这些留存模板/脚本，也不复制上游项目状态机。

| 固定来源（均相对 `skills/short-drama-review/`） | 源文件 SHA-256 | 本包改写落点 |
| --- | --- | --- |
| `SKILL.md` | `251364f35886455bded0310ac980927a93dd7563dcad53bd5271f83ea211a7c9` | `methods/review.md` 的范围、作者/审者分工、定向阅读及 Markdown 反馈 |
| `references/review-method.md` | `832cc8ac66920d485d3dc6495de363dba72e5c58f7d03b3caffab143bc08915c` | 成对证据、应恢复结果、保持项与受影响位置复核 |
| `references/stage-contract.md` | `c08599a33866a131529c83e7ea0b1ca1e2d0f621fceeddfc7c54cf07feb66029` | 不建第二裁决状态，规则等级与当前范围分离 |
| `references/rubric-source-analysis.md` | `dcb62babcd674acf8129839b693ae5e3f176b4b9ece8adcc2a812aaa15fc5809` | `methods/review-source.md` 的事实回查、指代与未知；上游章节工具未迁移 |
| `references/rubric-story-script.md` | `4394c080da47ae6bbb45e0a8ada29ea71dc41721bab333befd3a2db227177338` | 场次承载与实际变化；不把创作默认变成通用配额 |
| `references/rubric-visual-motion.md` | `3f594c34c003f7d81aee6cf80adedc6c04c7605703cbf156224b2d4e4cc38e37` | `methods/review-continuity.md` 的同镜/相邻镜/跨场接续；不声称已观察媒体 |
| `references/rubric-assets-prompts.md` | `b313caf053d50a84abc099fae8589841739f9a2a315926ccfcc4c79747fb0695` | 物件身份、持有/位置/状态的阅读方向；媒体生成方法未迁移 |

`methods/review-delivery.md` 将上游跨文档综合和修订后复读映射到本包已有字段，并新增“拟对外陈述与实际文件/计量对照”的明确操作。`templates/review.md` 是本包新编的 Markdown 反馈形状。以上均为保留 MIT 声明的中文改写，不是上游格式兼容实现；不将某个故事、目标秒数或固定镜头配额写进方法。

入口和原工作流接入按需读取；14 个资源仍只在包内声明，不注册为全局 Skill。`drama_text_delivery.v2`、`drama_text_check.v2`、原检查器、JSON 模板及两份合成数据的字节保持。无新增依赖、设置、宿主规则或执行器；旧包与旧失败记录保留。

这是方法迁移候选，不是模型已采用或语义质量已改善的证明。私有方法未读、独立审阅未发生、原脚本未执行及最终报告失真须继续分别记录；新版构建/组件验证和原生 TUI 结果单列。

## 0.2.2 修订范围

固定来源仍为 `0e8929881bb59248618c4f402707c64723adc017`。本轮补读普通创作 owner 的收尾，核对本地文件与固定 Git 对象逐字节相同；没有更新上游或运行其程序。

| 固定来源 | 源文件 SHA-256 | 本包改写落点 |
| --- | --- | --- |
| `skills/short-drama/SKILL.md` | `a8efa0cc1f73a4cbc55ee1f1aa05491b2f43fba89a43fb87a21aef5f2b612784` | 普通创作完成点名范围，正式审稿仅在用户点名时进入 |
| `skills/short-drama/references/creator-workflow.md` | `5796f68fe3242da4010036ce3c7ff1f22edf548acb80caff9b988b69921d28cd` | 同请求内完成已授权修订，最终回报作品与真实未决项 |
| `skills/short-drama-write/SKILL.md` | `f0a74465dbdd12c06ee7a37b2eda84423d1c8d03d364f0ef42c1e04b586f81cc` | 交稿前从成稿反查承诺/因果，明确问题直接修，保持无关有效内容 |
| `skills/short-drama-storyboard/SKILL.md` | `f893fb81cb45f02affd985fcb225cfa7a261f3020c33c4de4ce48c5669299944` | 按实际镜头的状态链和相邻接续反查，遗漏直接修入作品 |

0.2.1 的来源、接续和交付分表已实际完整到达一次真实任务，原检查器也已运行，但该任务仍漏检语义问题；不能再将原因描述为“方法没有给到”。上游普通创作将作者反查和直接修订放在交稿前，正式审稿则交还证据问题；本次把这两种工作在入口和 workflow 中分清，作者沿原任务回读当前候选、逐项检查、修订同一作品，再运行最终检查并如实交付。没有发现或迁入一个隐藏的自动执行器。

三份领域分表、原checker、JSON模板、两份合成数据及许可字节保持；仍为14个包内资源和原v2资料合同。无新增资源、依赖、设置、宿主工具、完成门或第二模型循环，也不为某个故事添加判断分支。正式审稿仍可采用原反馈模板，普通创作不因该模板缺失判失败。

0.2.1 已有逐镜检查和作者负责修订的要求；公开轨迹显示未识别出问题，没有显示模型已发现问题却因审者职责拒绝修改。因此本次是责任与次序的澄清假设，不是已证实根因修复。新版方法采用、实际修稿、脚本执行、来源/接续质量和最终汇报仍须用新原生任务分项验证，旧版本失败保持。

## 0.3.0 修订范围

固定来源仍为上述 commit。本次读取当前 Markdown 主线的成稿回填、人物可见/画外及名字覆盖函数，不把留存 JSON 工具当作当前五文档工作流。新增 `methods/visible-characters.md`，单文件原检查器中改写名字覆盖算法；资源增至15项，资料/报告显式升为 v3，原 source v1 字节及原摘要、来源、时长含义保持。模板和公开示例同步增加代称、退出理由和画外列表，不回填旧作品或旧验收。

| 固定来源 | 源文件 SHA-256 | 本片实际改写 |
| --- | --- | --- |
| `skills/short-drama-storyboard/SKILL.md` | `f893fb81cb45f02affd985fcb225cfa7a261f3020c33c4de4ce48c5669299944` | 先完成镜头，再从实际成稿回填可见依据 |
| `skills/short-drama/references/creator-documents.md` | `3176642b8395c0967c2ec829c56db00fc3393ee20afc16fddda691431c1b8293` | 明确可见/画外、字面代称与匹配退出 |
| `skills/short-drama/scripts/creator_markdown_check.py` | `21f0af79598d6161cec8c7bf720fe9acbe4d4de4319c9eb1499c8e5a77277e97` | `_visual_entries`、`_named_entries`、`_check_named_coverage` 的人物声明与具名覆盖子集 |
| `skills/short-drama-storyboard/references/stage-contract.md` | `afc5fc4dc60c49e37cb71955f345a2049720f5f94ecf58eae1cc9cd92eaea745` | SHT-22 中人物字面覆盖；不是整张视觉资产清单 |

差异是明确的：新格式要求显式 `text_names`，不默认追加展示名；同词多人只报歧义。仅扫描镜头三个原字段，精确大小写/空白/Unicode，不复制上游空白折叠、casefold差异检查或否定排除；ASCII邻接显式加入下划线。只有通过字界的长名才覆盖短名，按长度分组和区间前缀最大值替换逐区间比较，不复制上游无效长词也占位的行为。保留原Unicode字符位置、限制警告预览和输出数量，扫描预算不足给 `not_checked` 和null计数。名字问题为warning，不变成宿主完成硬门。

上游 SHT-26 检查本镜短引文，而本包 `source_ids` 仍只引用整段，**本片未迁 SHT-26**；不能据原文整段名字推断本镜人物。代词、实际在场、虚假退出/画外、来源语义、持物因果、其它视觉实体和媒体仍需独立阅读或后续迁移。新组件通过也不证明 A0.2.1/0.2.2 的语义失败已修复；新原生验证单列。

保留 `licenses/drama-skills-LICENSE` 的 MIT 版权声明。没有复制上游运行时、五文档状态机、模型循环、安装器或生产依赖，也没有改变宿主配置/工具链。

## 0.4.0 修订范围（C13 第二部分）

固定来源不变。这次根据固定最终矩阵里失败样本的独立审计，补了几项"只要作者写成结构化字段，就能确定性核对"的检查。所有新字段都是可选的：资料仍是 `drama_text_delivery.v3`，报告仍是 `drama_text_check.v3`，只增加键。

| 固定来源 | 源文件 SHA-256 | 本片实际改写 |
| --- | --- | --- |
| `skills/short-drama-storyboard/references/stage-contract.md` | `afc5fc4dc60c49e37cb71955f345a2049720f5f94ecf58eae1cc9cd92eaea745` | SHT-26 的子集：只核对作者显式标成原文逐字的台词（`verbatim_source_id`）和原文短句（`source_quotes`），并且必须是所引段落的连续子串。CON-01 的结构子集：相邻镜头 `prop_states` 的持有人和标签是否接续 |
| `skills/short-drama-write/SKILL.md` | `f0a74465dbdd12c06ee7a37b2eda84423d1c8d03d364f0ef42c1e04b586f81cc` | 对白要写成可念的台词，而不是转述。改写为可选 `lines[]`，原检查器只数条数、核对说话人在本镜声明内 |

**和上游的差异**：
- **只认显式标记**：逐字比较只看作者显式标出的字段，不解析散文里的引号，因为散文里的引号常常是作者自己写的话。
- **不归一化**：不做空白、标点或全半角归一化。
- **只比标签**：道具接续只比作者自定的标签，不读起点、动作、终点的正文；标签对得上不等于正文写对了。
- **跳接**：有意跳接用 `continuity_break` 声明，检查器只看写没写，不解析内容。
- **级别**：
  - 0 条台词、持有人在画外、相邻状态对不上，都只是 warning。
  - 说话人或持有人不在本镜声明里、标了逐字却不是子串，是 error。
- **检查器身份**：报告新增 `checker`（包 ID、版本、按本文件实际字节算出的 sha256），帮助区分原包程序与自写脚本。它不是安全控制，也不能替代宿主的执行记录。

**不覆盖的范围**：漏标的创作新增、把原文事实说成改编、意译后的冲突、同一镜正文自相矛盾、人物离场是否交代、实际在不在画面、台词够不够排演——这些仍要靠方法和独立阅读。根据这些失败改动之后，原冻结用例再跑一次只能算"已见回归"，不是新的保留集。
