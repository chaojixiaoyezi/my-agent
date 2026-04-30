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
