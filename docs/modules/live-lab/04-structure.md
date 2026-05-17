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
    |-- cases.py                       # smoke、log-analysis 等 case 定义
    |-- constants.py                   # suite 名、默认目录等常量
    `-- log_analysis_replay.py         # LOG 离线 replay 具体流程
```

## 核心文件

- `live_agent_lab.py`：用户运行的稳定入口。
- `live_lab/cli.py`：把命令行参数转成 runner 能理解的配置。
- `live_lab/runner.py`：负责 suite 运行、输出目录、状态汇总。
- `live_lab/runner.py` 的 `_LabInterface`：兼容旧 case surface；把 `run_root`、`prompts_dir`、`responses_dir`、`summary_path` 等目录属性转发给 case，case 不直接访问 runner/session 私有字段。
- `live_lab/cases.py`：登记有哪些 case，每个 case 怎么跑。
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
- `scripts/live_lab/cases.py`：自然 suite 还有两层控制面验收：`_assert_no_subagent_state_blockers()` 检查 gateway 最终回复是否明确报告阻塞；`_assert_persisted_subagent_state_clean()` 读取隔离项目 `.my_agent/subagents/subagent-*/task.json`，用产品侧 closeout resolver 判断是否仍有未解决 run。
- `scripts/live_lab/session.py`：Live Lab 隔离配置默认 `max_tool_rounds: 0`。真实页面、购物站和长任务 canary 不应该被测试台轮数上限截断；需要测预算/熔断时应由专门 stress case 显式覆盖。
- `agent_py_agent/tests/test_live_lab_natural_case.py`：锁住三个合同：提示词不含内部调度术语、suite 注册为 real opt-in、HTML artifact gate 不信口头总结。
- 产品侧依赖：`subagent_finalize_artifact_integrity.py` 必须用和文件工具一致的工作区根解析相对产物根；`runner_input_dependencies.py` 必须把 `输出路径/保存路径/产物文件` 识别为写目标，不当成输入依赖。
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
