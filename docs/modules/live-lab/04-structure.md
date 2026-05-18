# Live Lab：结构树和详细说明

## 模块结构

```text
scripts/
|-- live_agent_lab.py                  # Live Lab 主入口
|-- open_live_lab.sh                   # macOS 可见 Terminal 启动辅助
`-- live_lab/
    |-- __init__.py
    |-- cli.py                         # 参数解析和 suite 选择
    |-- runner.py                      # 运行 suite、管理输出目录、汇总结果
    |-- cases.py                       # smoke、log-analysis 等 case 分发
    |-- constants.py                   # suite 名、默认目录等常量
    |-- file_repair_wave_case.py       # 普通文件失败后修复闭环真实 case
    |-- log_analysis_replay.py         # LOG 离线 replay 具体流程
    |-- main_agent_artifact_case.py    # 主代理长输出读回真实 case
    |-- main_agent_complex_case.py     # 主代理自己完成复杂任务的真实 case
    |-- markdown_repair_wave_case.py   # Markdown 文档失败后修复闭环真实 case
    |-- shop_case.py                   # 购物站业务流真实 case
    `-- shop_repair_wave_case.py       # 购物站失败后修复闭环真实 case
```

## 核心文件

- `live_agent_lab.py`：用户运行的稳定入口。
- `live_lab/cli.py`：把命令行参数转成 runner 能理解的配置。
- `live_lab/runner.py`：负责 suite 运行、输出目录、状态汇总。
- `live_lab/runner.py` 的 `_LabInterface`：兼容旧 case surface；把 `run_root`、`prompts_dir`、`responses_dir`、`summary_path` 等目录属性转发给 case，case 不直接访问 runner/session 私有字段。
- `live_lab/cases.py`：登记有哪些 case，每个 case 怎么跑。
- `live_lab/file_repair_wave_case.py`：负责 `file-repair` suite 的坏 CSV seed、自然语言修复 prompt、最终内容 gate 和 verified repair sibling 检查。
- `live_lab/main_agent_artifact_case.py`：负责 `main-artifact` suite 的长输出读回测试，专门测主代理是否能从长资料里续读远距离证据。
- `live_lab/main_agent_complex_case.py`：负责 `main-complex` suite 里的 Web app、工具失败恢复和大日志审计测试，会临时关闭子代理，只测 root 自己的工具、产物和恢复能力。
- `live_lab/markdown_repair_wave_case.py`：负责 `markdown-repair` suite 的坏 Markdown seed、自然语言修复 prompt、最终内容 gate 和 verified repair sibling 检查。
- `live_lab/log_analysis_replay.py`：把 SecurityAlertV1 fixture 跑成 LOG artifacts。
- `agent_py_agent/tests/test_live_lab_log_analysis_replay.py`：验证 replay 的成功和失败路径。

## 数据流

1. 用户运行 `python scripts/live_agent_lab.py --suite <name>`。
2. CLI 解析 suite、runs-dir、run-id 等参数。
3. runner 创建本次输出目录，调用对应 case。
4. case 运行真实命令或离线 replay。
5. 每个 stage 写 summary、artifacts 或错误信息。
6. runner 汇总结果，成功时输出类似 `LIVE_LAB_PASS`，失败时保留可复查产物。

## 给初学编程学生的学习路径

1. 先看 `scripts/live_agent_lab.py`，理解入口为什么很薄。
2. 再看 `scripts/live_lab/cli.py`，学习命令行参数怎样变成配置。
3. 再看 `scripts/live_lab/cases.py`，理解 suite/case 是怎么登记的。
4. 再看 `scripts/live_lab/log_analysis_replay.py`，跟着一个真实 replay 看数据怎么一步步变成报告。
5. 最后看 `agent_py_agent/tests/test_live_lab_log_analysis_replay.py`，理解怎样用测试覆盖成功和失败 stage。

## 当前第一版索引 / 待补齐

本页先描述当前脚本结构。后续应补充每个 suite 的命令示例、输出目录样例、summary schema 和常见失败处理。
## 2026-05-06 structure update
- 中文说明：Live Lab 的 log-analysis replay 已经把 stage 参数对象和执行 helper 分开。后续新增 gateway、subagent、memory 这类真实演练时，可以复用同样结构，不必把流程继续堆在一个大函数里。
- Live-lab log-analysis replay now separates stage parameter objects from execution helpers, making future replay scenarios easier to extend.

## 2026-05-07 annotation structure update
- 中文说明：Live Lab 脚本也纳入双层注释规范。`LLM:` 给后续大模型看契约和副作用，`函数用途:` / `类用途:` 给人看用途；这些注释不计入代码体积，但必须随行为变化同步更新。
- Live Lab scripts now treat definition-level comments as part of the developer-facing architecture map: `LLM:` records suite contracts, side effects, and caller expectations; `函数用途:` / `类用途:` gives a beginner-readable explanation.
- `scripts/live_lab/runner.py`, `reporter.py`, `session.py`, `cases.py`, `cli.py`, and replay modules should keep comments synchronized when case flow, artifact paths, or process execution changes.
- Code-size accounting excludes comment/docstring lines, so required guidance text does not count as implementation size.

## 2026-05-17 compatibility structure update
- 中文说明：Live Lab 的目录所有权在 `session.py`，命令执行在 `runner.py`，case 通过 `_LabInterface` 看见旧的 `LiveLab` 表面。以后新增 case 要走这个表面拿目录，不要读 `_runner._session` 这种私有字段。
- `_LabInterface` and `LiveLab` expose the same small workspace path surface: `run_root`, `fixture_root`, `prompts_dir`, `responses_dir`, `config_path`, `transcript_path`, `summary_path`, and `stop_file`.

## 2026-05-17 real-suite support boundaries
- 中文说明：真实 `--suite real --real-llm` 不只依赖 Live Lab 自身，还依赖两个产品侧稳定边界：模型后端遇到 Anthropic-compatible 空流式响应时要能兜底，子代理私有目录要能受控推导项目工作区根来读取 `README.md` / 校验产物 refs。
- Backend boundary: `agent_py_agent/agent/backends/base.py` keeps stream/no-stream resilience inside `AnthropicCompatibleBackend`, so Live Lab case logic does not special-case provider quirks.
- Workspace boundary: `agent_py_agent/agent/subagents/workspace_roots.py` is the shared helper for deriving project roots from `.my_agent/subagents/<run>`, `.my-agent/subagents/<run>`, and `data/subagents/<run>` layouts. Live Lab real cases use this through runner input dependency checks and artifact integrity checks.

## 2026-05-17 natural-suite structure
- 中文说明：`natural` suite 是真实用户语言 canary，不替代固定 `scenario-test`。它的价值是看主代理在没有内部术语提示时，是否仍能用小傻妞完成真实产物、返回 refs，并通过父级验收。
- `scripts/live_lab/constants.py`：`natural` suite 显式包含 `health` 和 `natural_html_subagent`；`natural_html_subagent` 属于 `REAL_CASES`，必须传 `--real-llm`。
- `scripts/live_lab/cases.py`：`case_natural_html_subagent()` 负责发自然语言 prompt、保存 response、停止 gateway，并调用 `_assert_natural_html_output()` 验证真实 HTML artifact；`_external_asset_refs()` 只拦截页面渲染依赖的外部资源，不禁止普通外链。
- `scripts/live_lab/cases.py`：`_assert_natural_html_output()` 把 `href="/..."` 视为单文件静态交付里的坏链接；需要页面跳转时应使用真实存在的 `#section-id`，避免离线打开后按钮或导航失效。
- `scripts/live_lab/cases.py`：自然 suite 还有两层控制面验收：`_assert_no_subagent_state_blockers()` 检查 gateway 最终回复是否明确报告阻塞；`_assert_persisted_subagent_state_clean()` 读取隔离项目 `.my_agent/subagents/subagent-*/task.json`，用产品侧 closeout resolver 判断是否仍有未解决 run。
- `scripts/live_lab/session.py`：Live Lab 隔离配置默认 `max_tool_rounds: 0`。真实页面、购物站和长任务 canary 不应该被测试台轮数上限截断；需要测预算/熔断时应由专门 stress case 显式覆盖。
- `agent_py_agent/tests/test_live_lab_natural_case.py`：锁住三个合同：提示词不含内部调度术语、suite 注册为 real opt-in、HTML artifact gate 不信口头总结。
- `agent_py_agent/agent/agent_core/subagent_run_flow.py`：runner prompt 会在模型调用前预写到 `runner_prompt.md`；真实 Live Lab 超时、断网或 runner 被杀时，仍能看到下发给小傻妞的任务包。
- 产品侧依赖：`subagent_finalize_artifact_integrity.py` 必须用和文件工具一致的工作区根解析相对产物根；`runner_input_dependencies.py` 必须把 `输出路径/保存路径/产物文件` 识别为写目标，不当成输入依赖。
- 产品侧依赖：`orchestration_write_guard.py` 只拦截真实写入目标的工作区外绝对路径；否定示例里的路由路径（例如“不要写成 `/collections`”）不能被当成文件系统写目标，否则 root 会被迫绕过子代理。
- 当前验收只做轻量结构检查、离线资源检查和子代理状态一致性检查。表单行为、视觉布局、可访问性和图片实际内容质量应作为后续更强 Live Lab case，而不是塞进这个最小 canary。

## 2026-05-17 shop-suite structure
- 中文说明：`shop` suite 是比家具首页更厚的真实业务流 canary。它仍然用普通用户话术，但要求小傻妞交付可演示注册、登录、商品、购物车、结算、下单成功的静态购物站。
- `scripts/live_lab/constants.py`：`shop` suite 显式包含 `health` 和 `natural_shop_subagent`；`natural_shop_subagent` 属于 `REAL_CASES`，必须传 `--real-llm`。
- `scripts/live_lab/cases.py`：`case_natural_shop_subagent()` 负责发购物站自然语言 prompt、保存 response、停止 gateway，并检查 `lab_outputs/shop-demo/index.html`。
- `scripts/live_lab/shop_case.py`：承载购物站 prompt、业务流 HTML gate 和产品侧 `static_site_check` 复用逻辑，避免 `cases.py` 继续膨胀。
- `scripts/live_lab/state_assertions.py`：承载 gateway response 阻塞检查和持久化 `task.json` 状态门；家具和购物站 case 共用，保证 Live Lab 不只看口头回复。
- `scripts/live_lab/shop_case.py`：`_assert_shop_html_output()` 检查完整 HTML、关键业务区域、关键按钮动作、空链接、disabled 和外部渲染资源；`_assert_static_site_check_clean()` 复用产品侧 `static_site_check` 检查坏链接、可见占位符、失效控件、表单绑定和缺失 DOM id。
- `agent_py_agent/tests/test_live_lab_natural_case.py`：同一个测试文件覆盖家具和购物站两个自然语言 canary，确保 suite 注册、提示词口径和产物门同步。
- 产品侧依赖：`static_site_html_parser.py` / `static_site_dom_checks.py` 负责通用网页控件检查；真实 disabled 控件会被拦截，但 CSS/JS 里的 disabled 字样不会被当成坏按钮。
- 调度侧依赖：父级验收失败时，`dispatch_subagents` 的外置摘要必须保留 `parent_acceptance_repair_advice` 机器字段；Live Lab shop case 不直接创建 repair child，但真实 E2E 会验证 root 是否能看见这类修复建议。
- 当前真实验收：`20260517-shop-flow-08-repair-wave` 已通过，证明 shop suite 能跑真实 MiniMax-M2.7、隔离 gateway、子代理产物和最终状态门。后续还需要加“故意失败再修复”case，专门压测 repair wave。

## 2026-05-17 shop-repair structure

- 中文说明：`shop-repair` suite 是失败恢复 canary。它先造一个真实失败的 child 状态，再让 root 用普通用户话术继续处理，验证 root 是否会派 repair child，而不是自己写正文或直接报完成。
- `scripts/live_lab/constants.py`：`shop-repair` suite 显式包含 `health` 和 `natural_shop_repair_wave`；`natural_shop_repair_wave` 属于 `REAL_CASES`，必须传 `--real-llm`。
- `scripts/live_lab/cases.py`：只登记 `natural_shop_repair_wave`；具体失败种子、prompt 和验收逻辑放在拆分模块，避免 `cases.py` 继续膨胀。
- `scripts/live_lab/shop_repair_wave_case.py`：负责 `seed_failed_shop_child()`、`_natural_shop_repair_wave_prompt()`、`assert_shop_repair_wave_created()` 和真实 case。seed 使用产品侧 `SubAgentManager` 创建完整 task，而不是手写不完整状态。
- `assert_shop_repair_wave_created()` 会读取 `.my_agent/subagents/subagent-*/task.json`，要求出现 DONE/VERIFIED 的修复 run，并用产品侧 closeout resolver 判断旧失败 run 是否被同目标修复 sibling 覆盖。
- 这个 suite 的目标是压测通用 repair wave：以后 Excel、PDF、代码仓库等任务失败时，也应沿用同一套 parent acceptance refs -> repair child -> dispatch -> verified closeout 的合同。
- 当前真实验收：`20260517-shop-repair-wave-03` 已通过。前两轮失败分别固化成通用合同：repair 建议必须继承原始成功条件，create/schedule 必须从目标 refs 推导产品写入根，静态站点验收可读取结构化 `required_dom_ids`，不能只修最新报错点。

## 2026-05-17 file-repair structure

- 中文说明：`file-repair` suite 是非网页文件的失败恢复 canary。它证明 repair wave 不是只会修 HTML：CSV、Markdown、TXT、代码文件这类普通文件也可以用结构化内容合同做父级机器验收。
- `scripts/live_lab/constants.py`：`file-repair` suite 显式包含 `health` 和 `natural_file_repair_wave`；`natural_file_repair_wave` 属于 `REAL_CASES`，必须传 `--real-llm`。
- `scripts/live_lab/cases.py`：只负责把 `natural_file_repair_wave` 分发到拆分模块，避免主 case 文件继续膨胀。
- `scripts/live_lab/file_repair_wave_case.py`：负责 `seed_failed_file_child()`、`_natural_file_repair_wave_prompt()`、`assert_file_repair_wave_created()` 和最终 CSV 内容 gate。seed 使用真实 `SubAgentManager` 创建 task，并在 acceptance checks 里写结构化 `required_content_lines`。
- 产品侧依赖：`required_content_lines.py` 从任务验收文本抽取必须出现的字面行，`execution_test_items.py` 在单个普通文件 artifact 上生成 `content_check`。这条路只读 workspace 内真实文件，不相信模型自述。
- 当前真实验收：`20260517-file-repair-wave-02` 已通过。最终 `orders.csv` 四行一字不差，旧失败 run 的 4 条父级 `content_check` 全部通过；后续可把同一合同扩展到 Excel 导出清单、Markdown 报告和代码生成任务。

## 2026-05-17 markdown-repair structure

- 中文说明：`markdown-repair` suite 是普通文档 repair-wave canary。它把同一套“失败 refs -> 修复小傻妞 -> 真实文件内容门 -> verified sibling 覆盖旧失败 run”的合同从 CSV 扩到 Markdown。
- `scripts/live_lab/constants.py`：`markdown-repair` suite 显式包含 `health` 和 `natural_markdown_repair_wave`；`natural_markdown_repair_wave` 属于 `REAL_CASES`，必须传 `--real-llm`。
- `scripts/live_lab/cases.py`：只负责把 `natural_markdown_repair_wave` 分发到拆分模块，避免主 case 文件继续膨胀。
- `scripts/live_lab/markdown_repair_wave_case.py`：负责 `seed_failed_markdown_child()`、`_natural_markdown_repair_wave_prompt()`、`assert_markdown_repair_wave_created()` 和最终 Markdown 内容 gate。seed 使用真实 `SubAgentManager` 创建 task，并在 acceptance checks 里写 `required_content_lines[weekly.md]`。
- 产品侧依赖：`required_content_lines.py` 既支持结构化 per-file 内容合同，也支持普通用户“下面 N 行一字不差”这种自然语言块；`execution_test_items.py` 会把目标文件和内容行映射成 `content_check`，仍然只读真实产物文件，不相信口头回复。
- 当前离线验收：`agent_py_agent/tests/test_live_lab_natural_case.py` 已覆盖 suite 注册、坏 Markdown seed、verified repair sibling 状态门和最终内容 gate。真实 `--suite markdown-repair --real-llm` 是后续 repair-wave 压测入口。

## 2026-05-18 main-complex / main-artifact structure

- 中文说明：`main-complex` suite 是主代理底座 canary。它回答一个更基础的问题：不靠小傻妞时，my-agent 自己能不能完成多文件项目、遇到工具失败后恢复、审计大文件。
- `scripts/live_lab/constants.py`：`main-complex` suite 包含 `health`、`main_direct_web_app`、`main_tool_failure_recovery`、`main_artifact_readback`、`main_compact_resume_roundtrip`、`main_large_log_audit`；五个主 case 都属于 `REAL_CASES`，必须传 `--real-llm`。
- `scripts/live_lab/constants.py`：`main-artifact` 是快速复测小套件，只包含 `health`、`main_artifact_readback` 和 `main_compact_resume_roundtrip`；用于单独压测大输出/外置产物读回和 compact/resume 交接包，不用每次重跑 100MB 日志和多文件 Web app。
- `scripts/live_lab/session.py`：隔离配置会同时设置 `workspace_root` 和 `my_agent_home`。`my_agent_home` 指向本轮 `fixture_project/.my_agent/home`，避免真实 case 读取或写入用户全局 `~/.my-agent`，也避免上一轮 daily memory 把下一轮任务带偏。
- `scripts/live_lab/main_agent_complex_case.py`：`_ensure_main_agent_only()` 会把 `enable_subagents: false` 追加到本轮隔离配置，确保测试的是主代理自己，不污染用户配置。
- `scripts/live_lab/main_agent_artifact_case.py`：复用 `_ensure_main_agent_only()`，但把长输出读回 case 单独拆出，避免主复杂 case 文件继续膨胀。
- `main_direct_web_app`：要求主代理写 `index.html`、`styles.css`、`app.js`、`README.md`，并用通用静态产物门检查文件引用、坏链接、外部渲染资源和基础交互。
- `main_direct_web_app` 的 README gate 只检查核心文件 token 和真实页面锚点；目录名、说明措辞、章节标题都不当作机器事实来源，避免自然语言表达不同导致误杀。
- `main_tool_failure_recovery`：要求主代理先读一个不存在的文件，再改读真实素材。这个 case 用来观察工具失败是否能被模型当成可恢复事件，而不是直接卡死或假装成功。
- `main_artifact_readback`：生成约 96KB 的长资料，把三处证据放在远距离位置；它要求主代理继续按证据读回并写报告，用来压测大输出外置、分片续读和报告验收。
- `main_compact_resume_roundtrip`：读取上一个真实 run 的 `runtime_facts` request id，写入用户确认的验收/约束/测试事实，执行 `memory-compact --apply` 和 `memory-resume --compact-resume-mode auto`，要求 auto guard 只放行续接、不自动执行工具。
- `main_large_log_audit`：生成 100MB 日志，只要求报告关键证据和建议。它的目的不是测日志内容本身，而是测大输出/大文件场景下是否保持 refs-first（只拿引用和证据，不把全文塞进上下文）。
- 底层合同依赖：`main-complex` 的产物验收现在和父级验收共享 `contracts/artifact_acceptance.py` / `contracts/acceptance_contract.py` / `contracts/state_machine.py` / 结构化工具 envelope。Web case 复用 `static_site_check`；强制业务区块应使用结构化 `required_dom_ids`，而 `getElementById` 已经做空值保护的可选 hook 不算硬失败。
- 当前真实验收：`main-complex-isolated-20260518-135442` 已用 MiniMax-M2.7 跑通。它验证了主代理多文件 Web app、工具失败恢复、100MB 大日志审计和 Live Lab 家目录隔离。
- 当前真实验收：`main-artifact-20260518-early-request-id` 已用 MiniMax-M2.7 跑通。它验证了主代理在长资料读回时能先接收外置 tool-output artifact，再用 `read_artifact` 续读并写出证据报告；同一 run 的 `request_id` 会提前进入 context bundle、tool-output index、runtime facts，随后 compact/resume roundtrip 能带回 `artifact_refs` 并放行 `allow_automated_continue`。
- 后续扩展：compact/resume 多次续接、验收失败后自动修复、真实资料整理 xlsx/论文翻译等可以继续拆成同目录的新 case，不要塞回 `cases.py`。
