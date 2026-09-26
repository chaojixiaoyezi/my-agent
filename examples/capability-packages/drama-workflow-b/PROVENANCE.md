# 来源与迁移范围

参考来源：[eternityspring/shuohao-skills](https://github.com/eternityspring/shuohao-skills)，固定 commit `7ebef4f2f53159ee1eaaec2793271a114a8be8cc`。本轮依据已下载公开副本，不声称是远端最新版。

已读：五个 `skills/novel-*/SKILL.md` 的元数据、`skills/novel-script/references/schema.md`、分镜脚本的接口和 README 的报告组装说明。
本包按这些方法重新编写少量资料指导与 Python 标准库检查器，使用新的聚合 JSON 格式和新编故事，没有复制上游 .mjs 脚本、安装器、报告样式、图片或示例小说。

| 上游范围 | 本包映射 | 当前覆盖与缺项 |
| --- | --- | --- |
| novel-outline | 分集目标与 hook/ending 提示 | 方法简化；原有完整改编五件套和全部质量规则未迁移 |
| novel-characters / novel-art | 角色、地点、道具与参考计划 | ID 对账；真实设定图、造型变体与全部美术规则未迁移 |
| novel-script | 场次和动作/对白节拍 | 说话人、场次关系；原语速估时及全部内容质量门未迁移 |
| novel-storyboard | 节拍到镜头、参考图计划 | 引用与时长声明检查；H3 导出和真实图片一致性未验 |
| report.mjs | 安全转义的静态 HTML 核对页 | 无原多面板交互、图像布局、导出按钮或原报告样式 |
| 安装与生成能力 | 无 | 不软链到全局 Skill；不调用原 Codex imagegen，不安装 Node 依赖 |

本包新编内容按主仓库 Apache-2.0（`LICENSE`）；上游相应 `LICENSE` 与 `NOTICE` 分别保留于 `licenses/shuohao-skills-LICENSE`、`licenses/shuohao-skills-NOTICE`。NOTICE 中的原样例声明被保留不代表本包复制了该样例。
当前为可构建/组件验证的文本切片，不是原项目完整迁移；真实 TUI、自然召回、媒体和视觉验收均另列。
