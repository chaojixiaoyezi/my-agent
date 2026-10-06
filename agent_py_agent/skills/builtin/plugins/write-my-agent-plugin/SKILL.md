---
name: write-my-agent-plugin
description: 把用户需要的 my-agent 插件写成可校验的本地插件包，交付源码与验证证据。
when_to_use: 用户想做一个 my-agent 插件，或要新增自定义工具、面板、事件观察或工具收紧插件时。
tags: 插件编写, MCP stdio, Python模板, Node模板, 插件打包
scope: builtin
risk_level: medium
tools_required: [skill_search, list_files, read_file, write_file, edit_file, apply_patch, run_command, package_build, package_install]
---

# 写 my-agent 插件

你负责写代码、打包和验证。安装分两种：你自己做的**文件型 v6 插件**（只有工具/面板，没有 events/tool_gates/permissions）可以用 `package_build` 打包、`package_install` 安装——宿主按插件自动装开关决定直接装，还是给用户一行确认；v8 事件/收紧插件和别处来的包，安装、启用只能由用户来做。即使赶演示、用户说“顺手装好”，也不能替用户输确认码，不能调用插件管理工具；它们对模型本来就不可见。不要用 shell、内部管理函数、界面自动化或改安装表绕过这个边界。

## 速查

| 需求 | 起点 / 清单版本 |
| --- | --- |
| 工具和面板 | v6 已有；只有这些贡献时用 v6 `entry/files/platforms`，旧 Python wheel 工具仍可用 v1–v5 |
| 观察宿主流程（`events`） | v8：六类事件中只有 `prompt_submitted` 能声明 `content: text`；其它类型只有结构化事实，`tool_call_started` 必须 `none` |
| 工具调用前收紧（`tool_gates`） | v8：只能 `allow_as_is` / `ask` / `deny`，不能改参数或接管调用；`arguments: full` 必须列精确工具名且 `effects: []` |

写观察事件的插件，先看[作者合同](references/author-contract.md)里的「观察事件字段表」，按它读九个公共字段和每类的 `facts`——**不要猜字段名**。宿主以后可能加新的事件类型或新字段：不认识的 `type` 要跳过、不认识的字段要忽略，不能抛异常或当成错误。

写收紧门（`tool_gates`）的插件，若声明了 `arguments: full`，要处理[作者合同](references/author-contract.md)里的「参数截断标记 `arguments_truncated`」：收到 `true` 表示参数被截断、看不全，依赖完整参数的判断必须回 `ask`，否则别人可以用填充内容绕过你的门。

[Python](templates/python/declaration.json)、[Node](templates/node/declaration.json) 模板都是 v8 文件入口：一个只读 `count_text`、观察空回执、`run_command` 字面 `rm -rf` 加确认。只读随包声明，不读取用户文件、不联网、不写业务数据、不执行输入。该模式只是演示，不是完整 shell 安全检查。v8 至少有一条事件或收紧订阅；只订阅也合法，不必硬塞工具/面板；纯工具不为追新滥用 v8。

模板默认的 `prompt_submitted: text` 会让插件拿到脱敏后最多 4000 字的提示正文；不需要提示文字时，把该订阅的 `content` 改成 `none`，只收结构化事实，这样确认码里要用户同意的范围也更小。

## 生产安全边界

- **B7 之前 v8 插件在生产不能启用**，返回 `plugin_events_disabled`；不能删除关闭门、改配置或降成 v6 绕过。包能构建/读回不表示可以启用。
- B7 的启用前置合同：第一期只允许 `local/main`，插件进程**强制沙箱**（不可用就拒绝）、默认断网、收窄读；读不到宿主的配置、会话和记忆。B9 的独立 stdio 测试不验证这些宿主保证。
- `permissions.network` 默认 `false`；确需联网必须在授权范围内显式声明 `true`，不能为了演示默认打开。网络、订阅正文/参数范围和强制沙箱要求会写进确认码；变更后由用户重新确认。
- v8 插件启用必须由用户本人输确认码；模型不能自行安装、启用、取码、代填，不能借子代理代操作。自己做的 v6 文件插件走 `package_install`，确认码由宿主按用户的开关处理，你同样不碰确认码。

## 做法

1. 从用户需求确定输入、输出和副作用，选择合适语言；缺次要选择用合理默认。先按本技能目录读取模板和[作者合同](references/author-contract.md)，不要从索引一句话猜接口。资源路径相对本 `SKILL.md` 所在目录。
2. 在当前已授权工作区建立插件项目，把模板复制到项目后修改，不修改内置原件或运行时目录。同步修改插件 ID、发行名/版本、入口、`actions`、`tools` 和真实业务实现；不是改个名字就交付。MCP stdio 每行一条 JSON-RPC，stdout 只写协议帧，日志走 stderr。`tools/list` 的名称、说明和 `inputSchema` 必须与清单同源。
3. 清单每个工具用 `requested_effect`，只允许 `read_only` / `mutating` / `dangerous`，按真实行为如实声明。它是请求语义，不是宿主授权；只读声明不表示免审批或 OS 沙箱。审批只能等于或严于默认：插件代理当前沿外部 MCP 的 `dangerous` 默认进入宿主审批，包不能降低它。不要自造 `approval`、`effect`、`auto_approve` 等清单字段，或为赶时间修改宿主策略。更严审批须使用产品已有且用户授权的策略入口，不能在插件代码里授予自己权限。
4. `initialize.capabilities.experimental` 同时声明 `my-agent/events` 和 `my-agent/tool-gate` 的 `versions: ["1"]`。实现 `my-agent/events.observe` 返回空结果 `{}`；`my-agent/tool-gate.review` 返回 `verdict/reason_code`（可加纯文本 `message`）。`allow_as_is` 只保持宿主决定，不能免审批；缺能力位/超时/坏回复的收紧按 `ask`。六类事件、投影、回复格式和组合限制见[作者合同](references/author-contract.md)。第一期不得把订阅与 `host_api` 混用；按 `effects` 收紧只能 `arguments: none`。
5. 默认标准库；新增文件功能先核工作区/SDK 合同，权限只能来自本次可信 `_meta`，不能用 cwd、插件目录、参数或历史上下文冒充授权。v8 两语言都用 `scripts/build_plugin_files_package.py`；旧 v1–v5 wheel 才用 `build_plugin_package.py`，不另开 wheel 订阅路径。命令见[打包与验证](references/pack-and-verify.md)。构建器仅在产品源码内，不随普通 wheel 提供；缺获授权入口如实报告，不手拼 ZIP、不联网补依赖。只构建本次受信授权源码。
6. `TMPDIR`、副本和最终 `output/*.zip` 全在工作区。用真实 `inspect_plugin_package` 读回最终 v8 ZIP；从该包解出的入口跑握手、目录、业务、观察空回执和收紧请求，覆盖 Unicode、空输入、坏参数/错误后继续调用、危险模式 `ask` 与普通命令 `allow_as_is`。缺解释器或失败就修复或如实报告；不能把清单校验等同于安装启用或宿主收紧生效。

## 交付与常见错误

- 给出源码、包的实际路径、真实测试结果及未验证范围；工作区不得留下密钥、会话正文或宿主内部状态。模板的 stdio 测试不等于安装/启用或真实 TUI/模型调用。
- 改工具 schema 后还沿用旧 `tools/list`：改为同源声明再复验最终包；stdout 混日志：改到 stderr。
- 手拼摘要、往 `files` 加未打入包的文件、声明自带 `schema_version`：交回构建器生成，校验失败不交付成功。
- 给工具观察开 `text`、`full` 配 `effects`、漏握手位、默认联网：纠正声明与实现，重新构建并复验，不以“赶演示”绕过。
- 作者构建和安装启用是两回事：只交包，不执行 `/plugins` 命令，不取码、不代填、不借子代理代操作。

收尾分两种，只走其中一种：

- v8 插件和别处来的包：最后必须告诉用户：**“请用 /plugins install <路径> 安装，启用时按界面提示输确认码”**。同时给出可替换 `<路径>` 的实际包路径；确认、宿主安装/启用及真实调用仍由用户执行。
- 你自己做的 v6 文件插件：只走 `package_build` + `package_install`，照 `package_install` 回执原文告诉用户（见内置技能 `learn-external-agent` 的提醒用户一节）；不再给 `/plugins install` 那一行，免得同一个插件装两次。
