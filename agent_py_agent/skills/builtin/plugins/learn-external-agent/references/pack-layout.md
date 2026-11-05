# 能力包目录怎么摆

```
<任务工作区>/<包名>/
|-- CAPABILITY.md      # 入口：适合什么、不适合什么、怎么用、方法目录（declaration.capability.entry_document 指向它）
|-- methods/           # 一种方法一份文档
|-- templates/         # 可直接套用的模板
|-- checks/            # 检查清单；离线检查脚本也放这里（不联网、不读别的目录）
|-- examples/          # 原版里允许复用的例子
|-- LICENSE            # 来源许可证原文
`-- PROVENANCE.md      # 来源仓库、提交号、许可证、你改了什么
```

声明（declaration）最少这样写，`files` 可以省（自动收录普通文件，隐藏文件和根目录 `declaration.json` 除外）：

```json
{"plugin_id": "drama-scenes", "version": "0.1.0", "summary": "短剧分场方法",
 "capability": {"description": "把短剧故事拆成场次和镜头；不适合长篇小说正文写作",
                "keywords": ["短剧", "分场", "分镜"], "entry_document": "CAPABILITY.md"}}
```

- `plugin_id`：英文小写加横线，1–64 位。
- `version`：每次改内容都升版本号（如 0.1.0 → 0.2.0），这样才能“退回”上一版；只能用字母、数字和 `.+-_`，最多 64 个字符。
- 声明里的文字（说明、关键词等）每段一行，最多 1000 个字；不能有换行、控制字符或改变显示方向的字符（宿主会原样展示给用户，打包会拒）。文件名同样。
- `package_build` 的 `origin` 写一行来源，比如 `github.com/xx/yy@3f2a1c0`；`license` 写 SPDX 名称，比如 `MIT`。两者各一行、最多 200 个字。
- 离线小工具要用时，先按包里的说明用 `write_file(source_ref=…)` 落到任务工作区，再用已授权的工具运行；不要让包自己去联网或读别的目录。

## 同包带 MCP（v10）

来源自带 MCP 或核心工具值得封装成 MCP 时，与文档一起打包，不往本体加独立工具。一个包只放一个服务：

```text
CAPABILITY.md
methods/review.md
mcp/review/server.py
mcp/review/data.json
LICENSE
PROVENANCE.md
```

在上面的 declaration 中增加：

```json
{"mcp_servers":[{"name":"review","root":"mcp/review",
 "entry":{"kind":"interpreter","interpreter":"python3","command":"server.py","args":[]},
 "platforms":["any"],"permissions":{"network":false},"tool_catalog":"pinned_on_enable","tools":[]}]}
```

`entry.command` 相对 `root`，不是相对整个包。服务文件都在 root 下，最多 127 个，路径沿文件型插件规则；文档放运行树之外，保留中文路径。省略 files 时只对 `entry.kind=executable` 的入口命令自动标执行位，不信来源 mode。解释器由宿主启用时解析，并展示真实路径和 sha256；不写宿主绝对路径，不用 npx、uvx、docker 这类运行时拉代码的启动器。

`pinned_on_enable` 在用户确认后固定实际工具清单；`declared` 要提供非空 tools 声明。构建、安装都不等于同意执行，启用时必须由用户本人核对并发送 `/plugins#<包名> 启用 <确认码>`。文档与运行树共用一个包摘要，更新或退回整包进行。没有 mcp_servers 时保留原 v7 格式；现成 Claude 插件目录交管理员 `/plugins install <目录>`，不用照此布局改写原目录。
