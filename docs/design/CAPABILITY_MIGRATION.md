# 外部 Agent 迁移为能力包

本文是[能力包合同](CAPABILITY_PACKS.md)的操作手册：将固定版本的外部方法、流程、模板和脚本迁移为独立能力包，复用宿主原有任务、工具、子代理与权限。它不修改模型权重，也不把调用原 Agent 当成已掌握其全部能力。

状态：构建及管理语法已按当前源码核对；本文提供操作流程，不充当安装、真实模型或 TUI 通过证据。真实结果按[验收矩阵](../tasks/CAPABILITY_PACK_ACCEPTANCE.md)分别记录，不能把可构建当作已迁移完成。

## 1. 先固定来源，再讨论覆盖率

每个来源先保留独立边界。例如 A、B、C 三个短剧项目先各成一包；同一任务可以选择多包，正式融合另做版本变更和对照验收。不要用融合后的一个总称掩盖不同来源的冲突与缺项。

在普通工作区建立候选源码目录。`PROVENANCE.md` 记录公开项目 URL、固定 commit/tag、获取日期、实际核对的入口与文件、许可证位置、修改方式，以及未读或不可迁移部分。若只有压缩包没有 Git 信息，记录原始归档摘要及来源，不编造 commit。跟随 `main`、只保存项目名字或只读 README 都不能固定迁移范围。

在授权的本地来源副本内可只读核对：

```bash
git -C "<来源副本目录>" rev-parse HEAD
git -C "<来源副本目录>" status --short
git -C "<来源副本目录>" ls-files
```

占位目录须替换成明确选择的来源副本；不扫描用户历史任务或个人文件。存在本地修改时区分上游版本与本地差异，不把用户修改、旧聊天或私有素材作为上游内容。

本仓库现有切片可作为结构参考，来源版本和许可事实以各包的 `PROVENANCE.md` 为准：

| 样包 | 当前源码版本 | 可参考的迁移方式 | 仍须单列的缺项 |
| --- | --- | --- | --- |
| [drama-text-a](../../examples/capability-packages/drama-text-a/PROVENANCE.md) | `0.2.1` | 原文依据、目标时长对账及来源/接续/交付的证据审阅方法 | 新方法真实采用、真实媒体、完整导演规则和质量评估 |
| [drama-workflow-b](../../examples/capability-packages/drama-workflow-b/PROVENANCE.md) | `0.1.3` | 制作资料关联、显式交接核对与逐集时长检查 | 上游全部交互报告、美术与生成链路 |
| [security-evidence](../../examples/capability-packages/security-evidence/PROVENANCE.md) | `0.1.0` | 已有证据的范围、来源、去重和报告 | 实际授权证明、扫描及漏洞验证；不属于本切片 |

A 的 `0.1.1` 只修订包内说明与模板的一致性：镜头和场次均要求正数 `seconds`，来源摘要明确取原输入文件字节，模板补完整条目形状，并区分结构检查已执行、待执行和未通过。原校验脚本及两份合成示例字节不变；不修改宿主读写规则，也不改变 B/C 的迁移范围。原 `0.1.0` 的 ZIP、失败与成功证据继续保留，不能用新文档替换旧结果。

A `0.1.2` 与 B `0.1.1` 是之后的包层检查候选，针对公开字段补镜头按场/集求和和明确目标对账；缺目标、非法数字、溢出与差异分开。来源语义、创作新增和媒体仍需独立审阅。新源码、ZIP、安装代次及真实结果单列；不改变已安装旧包，不把脚本扩展变成宿主硬门。

## 2. 建立完整来源覆盖表

先盘点固定来源版本对外提供的全部能力，再选择首期切片。至少检查主入口、命令/工具注册、工作流、脚本、配置、模板、依赖、示例和相关测试；旧兼容入口与当前维护入口分列。找不到实现的宣传项也保留为“未核实”，不能直接算可迁移。

`PROVENANCE.md` 保留来源事实；较大的覆盖表放候选源码的 `COVERAGE.md` 并相互链接。这些是版本文档，不是宿主读取的新状态账本。

| 来源能力/入口 | 原输入与交付 | 宿主映射及候选文件 | 依赖与许可依据 | 首期范围 | 验收用例与真实证据 | 未覆盖原因 |
| --- | --- | --- | --- | --- | --- | --- |
| 原文改编流程 | 故事 → 分场与对白 | `methods/adaptation.md` | 来源 commit、许可证文件 | 文本切片 | 原文引用、场次交付、独立复核；待执行 | 上游其他题材未验 |
| 分镜生成流程 | 剧本 → 镜头与图片 | 方法迁移；图片执行需现有工具 | 图片模型、凭据与素材权利另核对 | 仅镜头方案 | 文本检查与媒体检查分别记录 | 生成服务未接入 |
| 并行制作调度 | 多阶段输入 → 合并交付 | 宿主子代理与原任务依赖 | 不复制原 Agent 循环 | 规划迁移 | 主/子/孙身份、依赖与产物；待执行 | 并发与恢复待验 |
| 证据报告 | 明确范围与已有材料 → 待复核报告 | 包内整理方法及确定性检查器 | 合成 fixture、许可说明 | 已有证据整理 | 引用与摘要、未验证标记；待执行 | 不提供扫描或漏洞验证 |

每个完整工作流再拆成输入校验、方法选择、执行、交付、复核与异常恢复，避免“一条流程已迁移”掩盖缺失步骤。给覆盖项保留稳定的文档编号，验收结果逐项引用；模型评分和报告正文不成为运行时状态来源。

迁移时同时对照方法中的字段清单、模板、有效示例和校验器。内容摘要要写清计算对象：对原输入文件的字节计算，不能用宿主 `file_version` 这类状态版本替代，也不能先解析再重新序列化后当作原文件摘要。通过既有获准工具取值，不给宿主增加某个包专用的文件规则；权限或工具不可用时如实保留待核验项。

覆盖报告同时给出两个分母：固定来源版本的完整清单，以及本轮明确承诺的首期清单。列出已实现、已做组件验证、已做真实 TUI 验收的范围和所有未通过项。未知项不从分母中悄悄删除；资料齐全、安装成功或一次任务完成都不能推出“100% 能力”。

## 3. 核对依赖与许可，选择可移植部分

| 项目 | 候选中必须写清 | 处理方式 |
| --- | --- | --- |
| 方法、文档、模板 | 来源文件、版本、修改范围与许可 | 可发布部分进入私有资源；保留必要署名、许可证及 NOTICE |
| 脚本 | 解释器、输入输出、写入/进程/网络副作用 | 优先小型标准库实现；只把脚本文本打包，执行仍经原工具授权 |
| 第三方 Python/Node 包 | 包名、固定版本、公开接口、维护与许可依据 | 缺依赖明确记录；v7 内容包不安装依赖、不自建虚拟环境 |
| 图像、声音、视频模型 | 服务来源、输入条件、输出格式、费用及凭据配置位置 | 由既有受授权工具接入；凭据不进候选源码、ZIP 或报告 |
| 原 Agent 的循环、状态、记忆、派工 | 宿主等价能力与尚缺行为 | 复用宿主主链路，不移植第二套循环、账本或后台 worker |
| 图片、小说、证据与旧交付 | 是否可再分发、是否含个人信息 | 示例用公开许可材料或新编合成 fixture；用户原材料留在其任务目录 |

许可证不明确、补充条款未核对、运行服务不可用的部分留在缺项中，不复制后再补理由。复制代码或内容时保留要求的许可文件及变更说明；来源完整性摘要只证明字节相同，不证明许可或执行权限。

个人 Skill 总结与能力包迭代分别处理：

- 单项可复用技巧进入既有 Skill 学习候选/确认流程；是否写个人或公共 Skill 库由原流程和用户选择决定。
- 领域包的方法、工作流、模板、脚本及交付核对留在该包的候选源码中，通过显式版本更新发布。
- 一次任务产生的建议可以成为普通工作区里的改进文档；不自动改已安装 blob、不自动启用新包，也不把每个内部方法注册成独立全局 Skill。

## 4. 候选源码就是可审阅的普通项目

下列目录是使用者授权的普通工作区示例，不是新的产品保留目录。多个候选、评审记录和源码版本可用现有 Git/文件方式管理，不新增学习状态库或发布服务。

```text
capability-work/
|-- candidates/
|   `-- story-workbench-a/
|       |-- declaration.json        构建输入，只列明确资源
|       |-- CAPABILITY.md           适用边界和使用入口
|       |-- PROVENANCE.md           来源、许可与改写说明
|       |-- COVERAGE.md             完整覆盖表与当前切片
|       |-- LICENSE                 本包内容许可
|       |-- licenses/               按实际来源保留 LICENSE/NOTICE
|       |-- methods/
|       |   |-- workflow.md         阶段、输入输出及依赖
|       |   `-- review.md           交付核对
|       |-- templates/              本包交付格式
|       |-- resources/              合成样例和只读参考
|       `-- scripts/                私有脚本文本，读取不执行
|-- dist/                           显式版本 ZIP；不覆盖旧包
`-- validation/                     本轮脱敏验收索引；原证据留私有目录
```

`CAPABILITY.md` 至少说明适用/不适用场景、必需输入、预期交付、方法入口、依赖缺失时的反馈、可独立派发的子任务和交付复核。方法说明约束专业工作；owner、任务归属、权限、产物存在与恢复状态仍由宿主结构化事实决定。

A、B 同时拥有 `methods/review.md` 没有冲突：包内资源按 `(package_id, resource_path)` 定位。不要把文件复制进个人/公共 Skill 目录；包内即使叫 `SKILL.md` 也仍是私有资源。包摘要只写少量适用信息，不把所有正文塞进常驻模型上下文。

最小 `declaration.json` 示例（先创建列出的文件，再构建）：

```json
{
  "plugin_id": "story-workbench-a",
  "version": "0.1.0",
  "summary": "把已有故事改编为可核对的短剧文本",
  "capability": {
    "description": "适用于故事改编和分场设计，输出文本资料与来源说明，不生成媒体。",
    "keywords": ["短剧", "故事改编", "分场"],
    "entry_document": "CAPABILITY.md"
  },
  "files": [
    {"path": "CAPABILITY.md"},
    {"path": "PROVENANCE.md"},
    {"path": "COVERAGE.md"},
    {"path": "LICENSE"},
    {"path": "methods/workflow.md"},
    {"path": "methods/review.md"}
  ],
  "settings_schema": {
    "type": "object",
    "properties": {},
    "additionalProperties": false
  }
}
```

增加模板、参考、脚本或许可文件时逐项加入 `files`；目录本身不是成员。构建输入只接受这六个顶层字段，不手填 `schema_version`、`package_kind`、`sha256`、`executable`，不声明 `tools/actions/skills/runtime`。builder 生成 `plugin_package.v7`、`package_kind=capability` 和每个成员的摘要，`executable` 固定为 false。

当前上限为 4095 个资源，另有归档、展开体积、单成员及描述大小预算，详见 `PackageReadLimits`。文件须是声明根内的普通文件；链接、路径穿越、大小写/Unicode 碰撞和未声明归档成员均不能用来塞入额外内容。大材料拆为可分页读取的资源，不删除预算。

## 5. 构建与开发验证

以下命令在 my-agent 仓库根执行；`CAP_WORK` 指向前述已经授权的普通工作区，示例使用仓库外的相对目录。输出文件必须不存在，换候选或版本时使用新文件名，不覆盖旧 ZIP。

```bash
CAP_WORK=../capability-work
mkdir -p "$CAP_WORK/dist"
python3 scripts/build_capability_package.py \
  --declaration "$CAP_WORK/candidates/story-workbench-a/declaration.json" \
  --files-root "$CAP_WORK/candidates/story-workbench-a" \
  --output "$CAP_WORK/dist/story-workbench-a-0.1.0.zip"
```

要先跑现有公开样包，可使用：

```bash
python3 scripts/build_capability_package.py \
  --declaration examples/capability-packages/drama-text-a/declaration.json \
  --files-root examples/capability-packages/drama-text-a \
  --output "$CAP_WORK/dist/drama-text-a-0.2.1.zip"
```

构建会核对源文件、生成可重复的 ZIP 并用正式读取器复验，不安装、不执行资源。记录整个 ZIP 的 SHA256，并保留当时声明及源码版本；普通文件修改不会改变已安装包。

开发反馈遵循合同单测 → fake tool → fake LLM → replay → 真实 TUI。现有样包的组件检查入口：

```bash
python3 -m pytest agent_py_agent/tests/test_capability_package_examples.py agent_py_agent/tests/test_capability_package.py -q --tb=short
```

2026-09-26 的 A `0.1.1` 修订已通过该组合 75 项（样包 47、内容包协议 28）：模板实际填充、缺失/无效镜头时长、误用文件版本摘要、原始字节与 JSON 重排差异均有定向检查。原校验脚本没有放宽；这是组件验证，不是新版本的真实 TUI 通过记录。

新包补适合其行为的检查：输入输出格式、引用与范围、脚本副作用、坏输入、依赖失败和旧版兼容等。开发时单独运行检查器只能算组件验证；真实验收时必须由被测 my-agent 自主读取包资源、物化及执行，测试者不能代跑或补产物。

## 6. 通过原生 TUI 安装、启用和使用

运行负责人准备支持 v7 的固定候选、隔离数据和一个测试 Gateway。多个 TUI 共用该 Gateway，各自登记编号和会话；普通任务使用官方 MiniMax-M2.7，核对实际 provider/endpoint，视觉用例使用官方 MiniMax-M3。模型名称相同不能证明来源。

把已构建 ZIP 放进本测试 TUI 明确允许访问的工作区，例如 `packages/story-workbench-a-0.1.0.zip`。下列是 **TUI 输入框命令**，不是 shell 命令；相对路径以当前会话 workspace 为准，不以操作者终端 cwd 或本文位置为准。路径带空格时保留引号。

```text
/plugins help
/plugins install "packages/story-workbench-a-0.1.0.zip"
/plugins info story-workbench-a
/plugins enable story-workbench-a
/plugins list --enabled
```

管理动作要求当前可信管理员身份、插件开关和工具策略允许；来源路径仍过原路径硬门。安装成功默认停用，enable 才发布内容激活；纯内容包不创建进程/MCP/虚拟环境，也不需要借助 Gateway 重启使其进入后续发现。是否成功读取原回执和 `/plugins info` 状态，不读模型口头承诺。

对于样例中的空 `settings_schema` 不需要 configure。有自定义必填设置时，先在停用状态按声明准备私有 JSON，再执行：

```text
/plugins configure story-workbench-a --file "private/package-settings.json"
/plugins enable story-workbench-a
```

纯能力包不声明业务动作。`/plugins@story-workbench-a` 可查看该包的命令帮助，但不能凭空写 `/plugins@story-workbench-a run`。实际使用是在普通对话中给需求，例如“把这篇故事改成一分钟短剧，交付分场、对白和原文依据”。显式点名包用于选择/隔离验收；自然召回验收使用不包含包名或内部路径的正常需求。

可观察到的正常链路为：发现包级摘要 → `skill_search` 指定 `package_id` 读取入口/所需资源 → 原工具执行 → 交付及复核。长资源继续读取 `continuation`，不能只读首段就宣称全文覆盖。脚本需要原样落盘时，模型把读取结果的 `source_ref` 原样交给 `write_file.source_ref`；写入和之后的 Shell 执行分别遵守原任务、路径和审批合同。包读取不授予执行权限。

## 7. 用矩阵证明可用，记录失败

详细用例沿用[能力包验收矩阵](../tasks/CAPABILITY_PACK_ACCEPTANCE.md)，不再建立一份平行的运行状态。至少覆盖：

| 方向 | 必须看到的事实 |
| --- | --- |
| 新 TUI 自然召回 | 没有旧学习聊天，仍能找到适用包并读取正确方法、完成交付 |
| 多包与负例 | A/B 同名内部文件分别正确；普通任务不误启动专业流程；停用后新发现消失 |
| 单/多/孙代理 | 原任务引用与授权继承，按依赖派工，输出归属明确并实际汇总 |
| 长任务与 Compact | 实际多阶段工作和压缩事件；恢复后包版本、输入与未完成项可核对 |
| 三类停止 | Goal 暂停、回合中断和资源停止各有原控制事实，不混为“全部停止” |
| 更新/撤销/卸载 | 旧引用不静默换到同名新版，新会话读到明确启用版本；用户产物保留 |
| 内容质量与缺依赖 | 硬事实、专业质量和外部服务结果分列；缺媒体不能以文字方案充当成片 |

每个用例记录 TUI/tmux 定位、候选和包摘要、owner/thread/task/run/operation 引用、模型来源、起止、输入版本、工具结果、产物与失败原因。完整证据留仓库外，仓库只收脱敏结果和可公开 fixture。

出现“操作可能已发起但结果未知”时，先查询原管理请求：

```text
/plugins status <原请求编号>
```

此查询绑定原会话和请求；不要先重开 TUI、重复安装或重做整项任务。依据原提交/终态和资源事实处理 `unknown`、冲突或清理未确认；不把界面超时当成未执行。不真实断网，不影响日用 Gateway 或无关任务。

## 8. 从改进候选到显式版本发布

真实失败先定位是包内容/依赖问题，还是宿主权限、状态、工具、恢复问题。包缺少方法可以迭代源码；宿主合同问题交对应模块修复，不能让包脚本绕过它。创作质量问题记录可解释的反例和复核条件，不为单个测试提示词增加产品分支。

改进流程只用普通候选源码和原管理链：

1. 保存最小可公开失败输入、原版本引用及差异建议；私有原任务和素材留在任务目录。
2. 在候选源码修改相应方法/模板/脚本，同步覆盖表、依赖、许可及变更说明；保持 `plugin_id`，显式改 `version`，如 `0.1.0 → 0.2.0`。
3. 组件验证后构建新 ZIP，记录新源码/包摘要；先在隔离 owner 通过真实 TUI 验收，再由用户或已授权操作者发布。
4. 发布前安排在途任务：停止新派发，等待完成或按原控制处理；保留旧 ZIP、旧版本结果及私有设置备份。disable 不会抹去已经进入模型上下文的文字，也不等于停止全部任务。
5. 在原管理 TUI 按顺序执行以下命令，每一步确认原结果后再继续：

```text
/plugins disable story-workbench-a
/plugins update story-workbench-a "packages/story-workbench-a-0.2.0.zip"
/plugins info story-workbench-a
/plugins enable story-workbench-a
```

`update` 只接受同一 `plugin_id`，要求旧激活已释放；启用中或撤销尚未清理完成会拒绝更新。更新后的包仍为停用，必须显式 enable。旧配置能按新 schema 校验时原样保留；不兼容时按 `settings_restored/settings_reason` 核对并重新 configure，不靠旧 UI 展示判断。

当前实现按包摘要判断是否变化，不强制版本号递增；因此“每次发布都写明确新版本”是维护要求，不能声称宿主已经实现版本单调性检查。已发布版本的源码与 ZIP 不覆盖；无变更候选无需重复发布。

发布后用全新 TUI 做自然召回和关键回归，并核对旧任务是否明确报告旧引用失效。不得让旧任务因包同名自动切到新版，或把安装表之外的 Markdown 标记当作发布完成。此流程不自动发布、不定时重装，不新增候选状态账或第二套安装表。

## 9. 明确回退或卸载

回退必须拿到先前保留且核对摘要的旧 ZIP，仍使用同一 update 链；当前没有 `/plugins rollback` 动作，也不会自动猜上一版：

```text
/plugins disable story-workbench-a
/plugins update story-workbench-a "packages/story-workbench-a-0.1.0.zip"
/plugins info story-workbench-a
/plugins enable story-workbench-a
```

旧版重新启用会有新的激活身份，不能复活先前任务的旧 activation 引用。回退后新 TUI 复测关键用例，原在途任务按原恢复合同明确处理。配置 schema 回退不兼容时同样需要重新 configure；包回退不回滚用户文档、任务产物或已完成的外部动作。

不再需要时执行真实的卸载命令：

```text
/plugins remove story-workbench-a
/plugins list
```

命令名为 `remove`，不是 `uninstall`。它走原停用/移除与提交回执，保留任务产物；结果不明仍查询原请求。不要手删安装表、blob 或用户任务目录。最后验证普通任务、原有公开 Skill 与其他能力包仍正常工作。

## 10. 实现核对入口与交接

本文操作语法与边界来自以下当前实现，修改对应协议时同步检查本文：

- [`build_capability_package.py`](../../scripts/build_capability_package.py)、[`capability_package_manifest.py`](../../agent_py_agent/agent/capability_package_manifest.py)：声明、构建字段与资源预算。
- [`command_catalog.py`](../../agent_py_agent/agent/command_catalog.py)、[`plugin_management.py`](../../agent_py_agent/agent/plugin_management.py)：原 TUI 管理动作、权限与原请求查询。
- [`plugin_content_lifecycle.py`](../../agent_py_agent/agent/plugin_content_lifecycle.py)、[`plugin_update.py`](../../agent_py_agent/agent/plugin_update.py)：内容激活、同 ID 替换、配置恢复与回退条件。
- [`skill_search_tool.py`](../../agent_py_agent/agent/capability/skill_search_tool.py)、[`package_resources.py`](../../agent_py_agent/agent/capability/package_resources.py)：按需读取与受授权的原样物化。

每次交接提供固定来源、完整/首期覆盖表、候选源码版本与 ZIP 摘要、许可/依赖缺项、已跑和未跑的验证、原请求与失败证据位置及当前安装状态。只读审阅和不同包的候选开发适合并行；同一包发布、同一 owner 装卸及 Gateway 生命周期保持一个负责人。

## 样包来源声明与制作交接（2026-09-26，实施中）

真实开发例已区分三类事实：包入口/方法被采用，原包脚本确实执行，以及产物内容符合需求。
前两项可以通过而第三项失败；测试者不代跑业务脚本，也不改旧产物补分。此次改进只进入包目录：

- A `0.2.0` 明确升级为 `drama_text_delivery.v2`，原输入 `drama_text_source.v1` 保持。镜头级声明分别保存 `source_ids`、`adaptations`、`unresolved`，不从正文推断归属；来源只可引用当前场次已声明的段落。
- 原 A 脚本输出显式 `drama_text_check.v2`，核对字段形状和编号关系，改编新增及未知保持可见警告。合法编号不证明被引用文字真的支持镜头；未标出的新增、道具连续性及创作质量仍须独立阅读。
- 新 A 包不自动接受/转换旧交付格式。旧已构建 ZIP 及旧任务引用保留原语义，新版本通过原安装管理链显式更新；不能以重装复活旧授权。
- B `0.1.2` 保留原资料与脚本协议，模板展示全部必要行形状；制作交接另列输入字节摘要、对象/字段映射、省略、新增和未决项。清单是业务资料，既不成为宿主任务事实，也不自称已获脚本验证。
- 两包的子任务用法只展示宿主原有 `items[].allowed_skills` 包级稳定 ID；父未授权仍为空，精确版本继续由原快照与任务引用确定。

本次再次读取固定 A 来源 `0e8929881bb59248618c4f402707c64723adc017` 的 `chapter-extraction.md` 与 `stage-contract.md`：参考事实追溯和结构/语义审阅分离的做法，不复制原命令解析或运行账。完整来源与资产连续性迁移仍另列缺项，不据此宣称全文代码或全部上游能力已迁移。

[固定上游能力清单](CAPABILITY_SOURCE_COVERAGE.md)列全A/B入口、工具及依赖，旧样包映射固定在`e72a1d337`；第4.1节单独记录本次A0.2.0/B0.1.2差异。组合69文件1661 passed、9 skipped仅证明本地组件，不回填旧样包能力或替代新版真实业务质量。

组件先用公开合成例和坏引用反例；原生 TUI 在新包冻结后独立验收。原开发失败及当前候选保留集各 `0/3` 保持，不借旧候选成绩填充新版本。

B0.1.3 候选进一步把交接清单升级为 `drama_workflow_handoff.v2`：每个映射、遗漏、新增及未决引用归到显式阶段，
使用JSON Pointer定位对象或字段，不猜复合ID或自然语言字段名。只从命令行 `--input-file FILE_ID=PATH` 绑定读取资料，
一次有界读取同时用于SHA和JSON解析；清单本身的路径不授予读权限。原 `--project` 功能保持，未传 `--handoff` 明确未检查交接。
这是包内数据检查，不增加宿主任务状态或完成门；新增来源语义仍须独立阅读。当前实施与组件结果不能冒充新版原生TUI通过。

建议下一步：先以三个现有样包在隔离 owner 跑通安装、自然召回、执行和移除，再验证长任务、多代理、Compact 与显式版本回退；这些事实齐全后再扩展完整覆盖表。个人 Skill 自动总结可以独立推进，交叉内容只通过明确候选与用户选择流转。


## A0.2.1：审阅方法迁移与验收边界

固定上游的当前审阅入口要求 Markdown 成对证据、修订结果和保持项；同目录留存 JSON 模板/脚本与入口描述不同，不能将目录存在性视为当前流程采用。先固定来源并标明差异，再改写为本包可执行的阅读步骤，具体映射见[来源清单4.2](CAPABILITY_SOURCE_COVERAGE.md#42-a021把审阅清单迁为实际工作单元本地候选)和包内 PROVENANCE。

A0.2.1 增加来源、镜头接续、交付事实三份按需方法及 Markdown 反馈模板，入口与 workflow 接入导航；原检查器、v2数据合同和许可保持。审者读取实际输入/产物，给出成对事实、应恢复结果、保持项，作者在原授权内修订后重新读取受影响处；没有独立审者时明确自检，不额外建立确认轮或机器裁决。

组件验证需覆盖声明、可重复构建及实际导航，记录入口预算；真实验证另看模型有没有读取和采用方法、原脚本有没有执行、内容与最终陈述是否准确。只发布一个带新方法的包不能证明后三项。当前65项组件通过、两次构建同字节，尚未安装；下一步先组合主线再少量普通内容复验，保留旧失败和未覆盖项。
