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
