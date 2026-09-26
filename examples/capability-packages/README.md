# 独立能力包样例

这三个 `0.1.0` 样例用于验证 v7 内容包、独立发现和包内私有资源。它们是有限功能切片，未完成上游全部迁移，也未通过真实模型或 TUI 验收。

| 包 | 特点 | 交付与边界 |
| --- | --- | --- |
| `drama-text-a` | 先核对原文依据，再改编场次和镜头 | 文本方案、来源覆盖与分镜状态；不生成媒体 |
| `drama-workflow-b` | 五类制作资料的结构和交接 | 人物、美术、剧集、场次、镜头跨表关系及静态报告；不具备上游全部报告交互 |
| `security-evidence` | 范围明确的既有证据整理 | 证据摘要、来源去重、发现引用和待复核报告；不扫描、不验证漏洞 |

`CAPABILITY.md` 是包入口。`methods/`、`templates/`、`resources/`、`scripts/` 只属于本包；三个包都有 `methods/review.md`，内容各不相同，不能按裸文件名覆盖。
来源版本、实际迁移与缺项见各包 `PROVENANCE.md`。许可随内容打包。全部故事和证据 fixture 都为本样例新编写的合成数据，与用户任务无关。

## 构建

在仓库根目录运行，输出目录由操作者指定，目标文件必须不存在：

```bash
python3 scripts/build_capability_package.py --declaration examples/capability-packages/drama-text-a/declaration.json --files-root examples/capability-packages/drama-text-a --output /tmp/drama-text-a.zip
python3 scripts/build_capability_package.py --declaration examples/capability-packages/drama-workflow-b/declaration.json --files-root examples/capability-packages/drama-workflow-b --output /tmp/drama-workflow-b.zip
python3 scripts/build_capability_package.py --declaration examples/capability-packages/security-evidence/declaration.json --files-root examples/capability-packages/security-evidence --output /tmp/security-evidence.zip
```

声明只列源文件路径。builder 生成 `plugin_package.v7`、`package_kind=capability`、逐文件 SHA256 和 `executable=false`，不执行任何资源。ZIP 不进仓库。
安装、启停和读取是否可用以当前宿主实现及验收记录为准；本样例没有独立安装器、后台服务、MCP server 或全局 Skill 注册动作。

## 开发验证

```bash
python3 -m pytest agent_py_agent/tests/test_capability_package_examples.py -q --tb=short
```

组件测试在临时目录构建并检查真实包字节，以隔离 Python 解释器运行私有标准库脚本，覆盖引用错误、内容摘要、去重与 HTML 转义。它不是产品 TUI 执行证明。
正式使用脚本必须通过宿主对已选包资源的读取、物化与原工具授权；如果这条链尚未接通，应报告待校验，不能让模型猜源仓库路径或把示例输出当作已执行结果。
首期没有新增第三方依赖；脚本只读取明确输入并写 stdout，无网络访问和文件写入。输入上限每文件 4 MiB；长任务按章节/剧集/资产拆分，包不负责宿主调度。

建议下一步：先完成内容包读取和脚本物化授权组合，再在独立测试 owner 的原生 TUI 验收自然召回、子代理和装卸；文本与媒体结果分开报告。
