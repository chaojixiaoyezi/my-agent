# 独立能力包样例

这三个独立样包用于验证 v7 内容包、独立发现和包内私有资源，当前源码版本见下表。它们是有限功能切片，未完成上游全部迁移；组件结果与真实模型/TUI 结果分列在[验收矩阵](../../docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md)，不能把可构建或一次采用成功当作完整能力通过。

| 包 | 当前源码版本 | 特点 | 交付与边界 |
| --- | --- | --- | --- |
| `drama-text-a` | `0.3.0` | 原文改编及成稿人物依据回填 | 文本方案、来源/时长声明及具名覆盖警告；不判在场或媒体 |
| `drama-workflow-b` | `0.1.3` | 五类制作资料的结构和交接 | 跨表关系、分集时长与静态报告；不具备上游全部报告交互 |
| `security-evidence` | `0.1.0` | 范围明确的既有证据整理 | 证据摘要、来源去重、发现引用和待复核报告；不扫描、不验证漏洞 |

`CAPABILITY.md` 是包入口。`methods/`、`templates/`、`resources/`、`scripts/` 只属于本包；三个包都有 `methods/review.md`，内容各不相同，不能按裸文件名覆盖。
来源版本、实际迁移与缺项见各包 `PROVENANCE.md`。许可随内容打包。全部故事和证据 fixture 都为本样例新编写的合成数据，与用户任务无关。

## A 0.1.1 的历史修订边界

该版本只调整 A 的声明、入口、方法、模板和来源覆盖说明：场次与镜头都填写正数 `seconds`；`source_sha256` 明确是当前原输入文件字节的 SHA-256，通过既有获准执行工具计算，不能用文件状态版本 `file_version` 或重新序列化 JSON 的摘要代替。模板提供待填写的完整条目形状，未执行或未通过的检查不能写成通过。

该版本的 `scripts/check_delivery.py` 和两份 `resources/example-*.json` 保持原字节，校验器没有放宽；当时脚本 SHA-256 为 `7e898ebd50a7398099c7a9c96b793525d50c1d96eb6ca94318646277bcd45408`。当时没有修改宿主读写规则或 B/C 的源码。各旧包及其真实验收结果保留，新版本单独构建、安装与验收，不改写旧失败。

## A 0.1.2 / B 0.1.1 的修订边界

真实开发集暴露了两类不同缺口：原包检查器没有比对镜头合计与声明时长；部分任务没有运行原检查器，却给出了超出依据的报告。A 新增逐场镜头与场次声明、总时长与来源明确目标的核对；B 新增按分集归集镜头与明确目标的核对。非法数字、溢出、缺目标和数值差异分开报告，浮点容差仅消除计算舍入。

这些只是包内只读检查器，仍由模型通过原资源引用物化，并按原工具授权实际执行。没有新运行时门、自动脚本执行或验证状态库；未执行就列未验证，结构通过也不能证明原文语义、创作新增、道具连续性或真实媒体正确。跨包字段转换及主子阶段材料仍须显式核对，来源 ID 不能代替制作参考条目 ID。

当前为开发候选；新版本的组件、构建和真实 TUI 结果分别记账，旧候选结果不能直接继承。

## 当前来源与交接检查候选

A0.3.0 的交付/报告显式升为 `drama_text_delivery.v3` / `drama_text_check.v3`，保留逐镜依据/改编/未知，增加显式代称与可见/画外声明；只诊断成稿字面覆盖，不判原文语义、真实在场或持物因果。新脚本不自动补旧v1/v2。具体方法、预算与未检查语义见[人物依据](drama-text-a/methods/visible-characters.md)，来源改写和缺项见[来源说明](drama-text-a/PROVENANCE.md#030-修订范围)。
B0.1.3 的交接格式为 `drama_workflow_handoff.v2`，使用明确的文件编号、JSON Pointer、对象编号及阶段范围。
`--handoff` 与重复的 `--input-file FILE_ID=PATH` 显式提供验证对象；交接文件中的路径不能自行触发文件读取。
只有项目检查时，报告明确交接未检查；存在并有摘要不证明对象映射或故事连续性正确。

上述为源码开发候选，组件和真实 TUI 验收结果分别记账；已安装旧包和旧失败证据保持原版本。

## 构建

在仓库根目录运行，输出目录由操作者指定，目标文件必须不存在：

```bash
python3 scripts/build_capability_package.py --declaration examples/capability-packages/drama-text-a/declaration.json --files-root examples/capability-packages/drama-text-a --output /tmp/drama-text-a-0.3.0.zip
python3 scripts/build_capability_package.py --declaration examples/capability-packages/drama-workflow-b/declaration.json --files-root examples/capability-packages/drama-workflow-b --output /tmp/drama-workflow-b-0.1.3.zip
python3 scripts/build_capability_package.py --declaration examples/capability-packages/security-evidence/declaration.json --files-root examples/capability-packages/security-evidence --output /tmp/security-evidence.zip
```

声明只列源文件路径。builder 生成 `plugin_package.v7`、`package_kind=capability`、逐文件 SHA256 和 `executable=false`，不执行任何资源。ZIP 不进仓库。
安装、启停和读取是否可用以当前宿主实现及验收记录为准；本样例没有独立安装器、后台服务、MCP server 或全局 Skill 注册动作。

## 开发验证

```bash
python3 -m pytest agent_py_agent/tests/test_capability_package_examples.py agent_py_agent/tests/test_capability_package.py -q --tb=short
```

2026-09-26 的 A 0.1.1 修订组合为 75 项通过（样包 47、内容包协议 28），覆盖模板按实际形状填写后校验、镜头时长缺失/无效、文件版本误作摘要，以及同一 JSON 不同字节必须重算摘要。这些都是组件结果，不代表 A 0.1.1 已通过真实 TUI。

组件测试在临时目录构建并检查真实包字节，以隔离 Python 解释器运行私有标准库脚本，覆盖引用错误、内容摘要、去重与 HTML 转义。它不是产品 TUI 执行证明。
正式使用脚本必须通过宿主对已选包资源的读取、物化与原工具授权；如果这条链尚未接通，应报告待校验，不能让模型猜源仓库路径或把示例输出当作已执行结果。
首期没有新增第三方依赖；脚本只读取明确输入并写 stdout，无网络访问和文件写入。输入上限每文件 4 MiB；长任务按章节/剧集/资产拆分，包不负责宿主调度。

建议下一步：组合验证 A/B 新检查器后固定源码与 ZIP，等现有控制任务结束再由管理席原生更新两包，复验开发集；保留旧失败，采用、原样物化、实际执行和质量继续分列。源码审阅可并行，同一 owner 的更新由一人操作。
