# 独立能力包样例

这三个独立样包用于验证 v7 内容包、独立发现和包内私有资源，当前源码版本见下表。它们是有限功能切片，未完成上游全部迁移；组件结果与真实模型/TUI 结果分列在[验收矩阵](../../docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md)，不能把可构建或一次采用成功当作完整能力通过。

| 包 | 当前源码版本 | 特点 | 交付与边界 |
| --- | --- | --- | --- |
| `drama-text-a` | `0.1.1` | 先核对原文依据，再改编场次和镜头 | 文本方案、来源覆盖与分镜状态；不生成媒体 |
| `drama-workflow-b` | `0.1.0` | 五类制作资料的结构和交接 | 人物、美术、剧集、场次、镜头跨表关系及静态报告；不具备上游全部报告交互 |
| `security-evidence` | `0.1.0` | 范围明确的既有证据整理 | 证据摘要、来源去重、发现引用和待复核报告；不扫描、不验证漏洞 |

`CAPABILITY.md` 是包入口。`methods/`、`templates/`、`resources/`、`scripts/` 只属于本包；三个包都有 `methods/review.md`，内容各不相同，不能按裸文件名覆盖。
来源版本、实际迁移与缺项见各包 `PROVENANCE.md`。许可随内容打包。全部故事和证据 fixture 都为本样例新编写的合成数据，与用户任务无关。

## A 0.1.1 的修订边界

本次只调整 A 的声明、入口、方法、模板和来源覆盖说明：场次与镜头都填写正数 `seconds`；`source_sha256` 明确是当前原输入文件字节的 SHA-256，通过既有获准执行工具计算，不能用文件状态版本 `file_version` 或重新序列化 JSON 的摘要代替。模板提供待填写的完整条目形状，未执行或未通过的检查不能写成通过。

`scripts/check_delivery.py` 和两份 `resources/example-*.json` 保持原字节，校验器没有放宽；脚本 SHA-256 为 `7e898ebd50a7398099c7a9c96b793525d50c1d96eb6ca94318646277bcd45408`。本次没有修改宿主读写规则或 B/C 的源码。首批 `0.1.0` 包及其真实验收结果保留，新版本单独构建、安装与验收，不改写旧失败。

## 构建

在仓库根目录运行，输出目录由操作者指定，目标文件必须不存在：

```bash
python3 scripts/build_capability_package.py --declaration examples/capability-packages/drama-text-a/declaration.json --files-root examples/capability-packages/drama-text-a --output /tmp/drama-text-a-0.1.1.zip
python3 scripts/build_capability_package.py --declaration examples/capability-packages/drama-workflow-b/declaration.json --files-root examples/capability-packages/drama-workflow-b --output /tmp/drama-workflow-b.zip
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

建议下一步：按验收矩阵在独立测试 owner 的原生 TUI 验证 A 新版本，保留旧版本失败及 B/C 各自结果；自然采用、资源原样物化、脚本实际执行和业务质量分开报告。同包安装更新由一人负责，其它包的只读审阅可并行。
