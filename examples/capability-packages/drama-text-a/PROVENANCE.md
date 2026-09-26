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
