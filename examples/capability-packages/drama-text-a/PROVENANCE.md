# 来源与迁移范围

参考来源：[zenstory-ai/drama-skills](https://github.com/zenstory-ai/drama-skills)，固定 commit `0e8929881bb59248618c4f402707c64723adc017`。这是已读本地公开副本的版本，本轮未联网确认最新版。

已读相关入口：`skills/short-drama-storyboard/SKILL.md`、各发布 Skill 元数据、`skills/short-drama/scripts/project_tool.py` 的生命周期说明及生产脚本引用入口。
本包方法为有来源标记的改写，脚本和合成故事为新编写；没有复制上游运行时、安装器、生产账本或用户材料。原文/场次/镜头 JSON 是本样例格式，不是上游兼容格式。

| 上游范围 | 本包映射 | 当前覆盖与缺项 |
| --- | --- | --- |
| 原著分析与改编 | `methods/workflow.md`，当前原文字节摘要、段落依据和省略检查 | 只做短篇文本切片；长篇索引、全量章节分析未迁移 |
| 单集剧本与资产 | 场次、角色引用和创作审阅 | 无完整上游模板、人物变体和所有质量规则 |
| 分镜与冻结关键帧 | 起点/动作/终点、可见角色、场次引用和场次/镜头各自正时长 | 只保留核心方法与结构核对；节奏仍需审阅，真实参考图和供应商时长兼容未验 |
| 生产、剪辑、音乐 | 无 | 未迁移，缺生成服务与媒体执行验收 |
| Dashboard、项目接受/发布 | 无 | 不复制上游状态机；沿宿主现有任务与交付事实 |

包内新编写代码及文档按主仓库 Apache-2.0（`LICENSE`）；改写方法同时保留上游 MIT 版权许可（`licenses/drama-skills-LICENSE`）。素材只含本样例合成数据。
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
