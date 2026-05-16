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
