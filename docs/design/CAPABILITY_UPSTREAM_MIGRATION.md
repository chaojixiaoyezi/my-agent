# 上游未迁内容：许可核对与迁移清单（C14）

状态：许可已核对、清单已定（2026-10-01，ae）；M-B1 与仓库自检（M-B3）已有本地候选（sol2），
**待 ae 审查、3a 沙箱外复验及集成，完整验收未完成**。M-B2 与 A 各批次仍未实施。来源分母与样包已覆盖的部分见
[来源清单](CAPABILITY_SOURCE_COVERAGE.md)；本文只回答“剩下没迁的，哪些能迁、迁成什么、哪些不迁、为什么”。

用户已定的做法（10-01 做法 8）：
- 按许可证迁；
- B 的 `.mjs` 工具和门做成插件；
- A 的图像/视频工具只做外壳，不调付费出图；
- 许可证不允许的（含 Toonflow）不迁，写清原因。

## 1. 许可核对结果

核对方式：
- 全部按固定提交读取，逐个文件用 git blob 摘要核对过；
- 没有运行任何上游代码。
- 摘要与原文出处存在仓库外的 `~/.my-agent/decision-evidence/c14-upstream-licenses/c14-facts.md`。

| 来源 | 固定提交 | 许可 | 结论 |
| --- | --- | --- | --- |
| A `zenstory-ai/drama-skills` | `0e8929881bb5…` | MIT，`Copyright (c) 2026 drama-skills contributors`（LICENSE sha256 `840bdb5b…`）。513 个非图片文件没有另行声明的许可，各 `SKILL.md` 都写 `license: MIT` | **代码可迁**：随迁移文件保留 MIT 原文和版权行，并写来源说明 |
| B `eternityspring/shuohao-skills` | `7ebef4f2f531…` | Apache-2.0 标准正文（只把附录里的网址从 http 改成了 https），另有 NOTICE（`Copyright 2026 烁皓`，sha256 `4a439a29…`） | **代码可迁**：随包附 LICENSE 与 NOTICE 原文，改过的文件写明改动（Apache-2.0 第 4 条） |
| Toonflow `HBAI-Ltd/Toonflow-app` | `e03cf590eb0c…` | Apache-2.0 正文之后附《补充协议》：本软件或其衍生版本以产品形式提供给“两个及以上独立第三方”，须事先取得 HBAI-Ltd **书面商业授权**；不得删改控制台或应用里的标识与版权信息 | **不迁**，原因见第 4 节 |
| Remotion（A 的字幕模板依赖，固定 4.0.418） | tag `v4.0.418` | 自有许可（不是 OSI 许可）：只有个人、员工不超过 3 人的公司、非营利组织、试用评估可以免费用；其他组织须购买 Company License | **不打包、不自动安装**，原因见第 4 节 |

许可之外另有使用限制的材料（同样不迁，原因见第 4 节）：
- A 的评测小说《让你管账号》：仓库所有者声明“版权归仓库所有者，收录用途限定为评估基准与工作流示例”。
- 由这部小说派生的 `examples/creator-first/`、`evaluations/让你管账号/reference-run/` 也一样。
- A 的其他样例 `golden-project`、`excerpt-chain` 自称原创或虚构，可以迁，但本清单不需要。
- B 的 `assets/wechat.png` 是作者个人微信二维码，`assets/reelbench-first-screen.png` 是推广截图。
- B 的完整 shot-recipes 卡库已经移到私有仓库 `eternityspring/shuohao-video-skills`，拿不到。仓库里只有 3 张测试夹具卡。

## 2. 迁移清单

### 2.1 B：`.mjs` 工具和质量门做成插件（`plugin_package.v6`，`interpreter: node`）

事实依据：
- 12 个 `.mjs` 都是 ESM，只用 `node:*` 内置模块和相对导入，没有 npm 依赖，Node 本身不联网。
- 门的数量和各 SKILL.md 的说法一致：outline 14、art 11、script 10、storyboard 17；characters 没有门，只有带原文逐字核对的 validate。

| 编号 | 内容 | 迁成什么 | 说明 |
| --- | --- | --- | --- |
| M-B1 | 五个 CLI 的只读子命令：`validate`、`checkup`（含门报告）、`stats`，以及 characters 的 `validate <cast> <book>` | 插件 `shuohao-novel-gates`，五只读工具 `outline_check`、`art_check`、`script_check`、`storyboard_check`、`cast_check` | 2026-10-01 本地候选已实现，待审/外部复验/集成。`read_only`、74+20 向量与结构化门回执有本地测试；完整宿主/TUI/模型验收未完成，详见下方实施记录 |
| M-B2 | 会写文件的子命令：`chunk`、`seed`、`render`、`assemble --out`、`export`、`merge --apply`，以及 `scripts/report.mjs` | 同一个插件里的写工具 | 第二批，**前置条件**：非 Python 插件目前没有写入上下文的跨语言一致性用例（见[任意语言插件](PLUGIN_ANY_LANGUAGE.md)）。先把 `workspace_write` 检查移植到 Node，并补一致性用例；做不到就只把结果放在工具回执里返回，由模型用 `write_file` 落盘，不给插件另开写路径 |
| M-B3 | 五个 `selftest.mjs`、`scripts/report-selftest.mjs` | 不进插件包；放进仓库测试 | 随 M-B1 本地候选接入，六份原样自检已在真实 Node 执行；没有 node 时跳过 |

M-B1 实施记录（2026-10-01，`worker/sol2-c14-mb1`）：
- 同进程调用固定上游导出函数，不解析 CLI 文案。outline/art/script 只提供 validate/checkup；
  storyboard 另有 stats（读取已有门日志，不是镜头数量）；characters 仅 validate 且 book 必填。
- 所有主/参考/原文/日志/卡片由宿主逐次 `_meta` 上下文裁决，不扫描私有卡库。
  缺依赖门 `status=skipped`、`passed=null`，不计通过数；顶层 passed 不代表 complete。
- 不进入 storyboard main/logGates，等价于 --no-log；CLI 对照显式传 --no-log。
  原样 NUL 与许可来源清单保持；自检/样例/report 不进生产 ZIP。
- 本机聚焦为 29 passed、1 failed、1 skipped：宿主在确认启用处失败，report 因 Seatbelt 拒绝未启动。
  3a 对修改前候选沙箱外复核报告为 30 passed、1 failed、0 skipped，后者是嵌套 JSON 子串断言问题；
  本轮已改为分层 json.loads 和布尔/门计数断言，固定提交的完整宿主结果仍待 3a 复验。
- 严格静态门禁及含 NUL 源树 clean-package 已通过；追加全仓未通过/未完成，
  首失败是与基线相同的 archive_tokens 旧接口导入，其他失败未复核；不以局部结果宣称全仓或 §3 验收完成。
- 真实 TUI 安装/确认/调用与一次真实模型自然调用由 3a/ae 在集成后验收；不能用组件结果关闭 §3。
  用法与权限局限见 [插件 README](../../plugins/shuohao-novel-gates/README.md)，命令与门禁见 [TESTS](../../TESTS.md)。

要守住的边界：
- **门报告的副作用**：storyboard 的 `validate`/`checkup` 默认会在当前目录追加 `.gates.jsonl`。插件调用时必须带 `--no-log`；如果要留门日志，写到插件私有数据目录，不能写进用户工作区。
- **子进程**：`report.mjs` 用 `execFileSync(process.execPath, …)` 启动同包的其他 CLI。要确认插件进程沙箱允许这样做；不允许就改成在同一进程里直接调用导出的函数。
- **NUL 字节**：`novel-characters.mjs` 第 1044 行有一个原样的 NUL 字节，Git 和 grep 会把它当二进制文件。
  - 打包时按字节原样处理，摘要照常核对，不要"清理"这个字节；
  - 仓库里给这个文件配 `.gitattributes`（`-text`），并确认 `check_clean_package.py` 能放行。
- **许可**：
  - 插件包随附 B 的 `LICENSE`、`NOTICE` 原文，另加 `PROVENANCE.md`，写清上游地址、固定提交、每个文件的路径和 sha256，以及改动说明；
  - 我们自己写的包装代码按主仓库 Apache-2.0。
  - NOTICE 里写的样例故事路径 `skills/storycast/examples/渡口.txt` 在上游已经不存在（实际在 `skills/novel-characters/examples/渡口.txt`）。NOTICE 原样保留，在 PROVENANCE 里注明这一点。
- **启用**：`interpreter` 类型的插件启用前必须由用户本人确认，也就是走 `--confirm` 确认码；需要 Gateway 的 PATH 里有 Node 18 或更高版本。
- **不迁**：
  - `scripts/install.sh`：它会往用户的 Skill 目录写软链，与宿主的安装流程冲突；
  - B 的出图路线：依赖 Codex 内置的 imagegen，按做法 8 不调付费出图；
  - 上面列出的两张作者图片。

### 2.2 A：图像/视频工具只做外壳（Python 插件，标准库）

事实依据：
- 4 个提示词检查器（`image_prompt_check.py`、`container_check.py`、`motion_timing_check.py`、`music_spec_check.py`）都是纯离线代码，不联网、不起子进程。
- `production_tool.py` 本身不联网，`run`/`collect` 通过子进程调用配置好的适配器。
- `provider_adapters.py` 是付费 HTTPS 调用：
  - OpenAI gpt-image-2，凭据 `OPENAI_API_KEY`；
  - 火山方舟 Seedance，凭据 `ARK_API_KEY`；
  - MiniMax 的音乐、语音、视频，凭据 `MINIMAX_API_KEY`。

| 编号 | 内容 | 迁成什么 | 说明 |
| --- | --- | --- | --- |
| M-A1 | 4 个提示词检查器 | 插件 `drama-media-shell` 里的只读工具，功能完整迁移 | 第一批。它们不调用付费服务，只检查提示词资料的引用、结构和时序，所以属于"外壳里能真用的部分" |
| M-A2 | `production_tool.py` 的 `prepare/confirm/status/audit/collect` 作业流程，只配 `fixture_adapter.py` | 同一插件里的生产外壳工具 | 第一批。**整个包里不带 `provider_adapters.py`**。`run` 只能走离线夹具，产物在回执里明确标成"夹具，不是真实生成"；如果请求真实供应商，返回结构化的"未配置供应商、本插件不调用付费生成"。作业记录只写插件私有数据目录，不另立宿主任务账 |
| M-A3 | `edit_tool.py` 的 `check`/`verify`（用 ffprobe 测量）和 `render`（ffmpeg） | 同一插件的可选工具 | 第三批，可选。需要本机有 ffmpeg/ffprobe，找不到时明确报不可用，参照 image-text 插件找 tesseract 的做法。**删掉 `--subtitles remotion` 这条路**（理由见 §4），只保留 ffmpeg 字幕 |

要守住的边界：
- 外壳不能让人误以为已经能真实生成媒体。回执和工具说明都写明"夹具产物不算生成成功"，这与来源清单 §2 的说法一致。
- 写文件走 Python SDK 0.2.0 的写入上下文（savepoint-lite 的同一套机制），不另开写路径。
- 许可：插件随附 A 的 MIT 原文和版权行，另加 PROVENANCE.md，写清每个文件的来源与 sha256。

### 2.3 A：其余离线工具（第二批，可迁，按需排期）

以下工具都是 MIT 许可、只用标准库、不联网，许可上没有障碍：
- `novel_index.py`、`episode_intake.py`、`screenplay_index.py`、`duration_estimate.py`、`voice_sheet_check.py`、`asset_check.py`；
- `storyboard_check.py`、`review_check.py`、`creator_markdown_check.py`、`tools/compact_refs.py`。

建议等短剧包实际需要再迁：放进 A 内容包（脚本经 `source_ref` 物化后执行），或做成插件工具。迁移前先按[来源清单](CAPABILITY_SOURCE_COVERAGE.md)第 8 节确定范围。

下列入口不迁，原因是设计上与宿主重复，不是许可问题：
- `project_tool.py`（项目生命周期）和 `dashboard_server.py`：会另立一套任务和产物账，见来源清单 §2；
- `evaluations/content_quality_gate.py`：它依赖封存的评测集，其中有限制用途的小说。

## 3. 工作量与分工建议

| 批次 | 内容 | 估计（agent 小时） | 适合谁 |
| --- | --- | --- | --- |
| 第一批 | M-B1（Node 只读门插件 + 一致性用例）、M-A1 + M-A2（Python 外壳插件） | 各约 3–5 | 两个包可以并行，分给两个 my-agent 会话 |
| 第二批 | M-B2（Node 写入上下文移植 + 一致性用例，再加写工具） | 约 4–6 | 一个会话，先做移植和用例 |
| 第三批 | M-A3（ffmpeg 可选工具）、§2.3 按需项 | 各约 2–3 | 等前两批验收后再排 |

每项的验收：
- 用 `scripts/build_plugin_files_package.py` 或 `build_plugin_package.py` 可复现地打包；
- `tools/list` 与包描述完全一致；
- Node 读取检查跑通 74+20 条一致性用例；
- 上游自检进仓库测试（没有 node 时跳过）；
- 包里有 LICENSE、NOTICE 和 PROVENANCE；
- 隔离环境里真实 TUI 安装、确认、调用各一次；
- 一次真实模型自然调用，只发一次需求。

ae 负责审查：
- 许可文件与 PROVENANCE 是否齐全；
- 有没有带进付费调用代码；
- 读写是否只走宿主上下文；
- 门报告有没有写进工作区。

## 4. 不迁的内容与原因

| 内容 | 原因 |
| --- | --- |
| Toonflow 的全部代码、资源和界面 | 补充协议要求：以产品形式提供给两个及以上独立第三方，须事先取得 HBAI-Ltd 书面商业授权（不论收费方式），且不得删改其标识。my-agent 是分发给多个用户的产品，迁入的代码或衍生实现都属于这种情况。我们没有这份授权，所以不迁。只把它的能力盘点当作思路参考，不复制代码、提示词、资源或界面。如果以后要用，须先由用户取得书面授权 |
| Remotion（A 字幕模板的依赖） | Remotion 许可只对个人、员工不超过 3 人的公司、非营利组织和试用评估免费，其他组织须购买 Company License，而且不允许把改动后的 Remotion 当作自己的产品再授权。我们无法确认每个用户的资格，所以不打包、不自动安装，也不保留这条调用路线。A 模板自己的 MIT 文件也不迁，因为没有 Remotion 就用不上 |
| A 的付费供应商适配器 `provider_adapters.py` | 做法 8 规定不调付费出图（也包括视频、音乐、语音）。外壳只带离线夹具 |
| A 的评测小说《让你管账号》及其派生样例（`examples/creator-first/`、`evaluations/让你管账号/`） | 仓库所有者把用途限定为"评估基准与工作流示例"，这超出 MIT 代码许可的范围，不能随产品分发 |
| B 的 `assets/wechat.png`、`assets/reelbench-first-screen.png` | 作者的个人联系方式和推广材料，与功能无关 |
| B 的完整 shot-recipes 卡库 | 在私有仓库，拿不到；storyboard 的 `shot-recipe` 门在没有卡库时如实标"跳过"，不能算通过 |
| B 的 `scripts/install.sh` | 会往用户 Skill 目录写软链，与宿主的插件安装和授权流程冲突 |

各家供应商（OpenAI、火山方舟、MiniMax）的服务条款这次没有核对，因为外壳不调用它们。将来真要接真实生成时，再单独核对。
