# Log Analysis：结构树和详细说明

## 模块结构

```text
agent_py_agent/agent/log_analysis/
|-- models.py                 # Source、Event、Finding、Case、EvidenceRef 等数据模型
|-- config.py                 # 日志分析配置
|-- parsers/                  # SecurityAlertV1 等输入格式解析
|-- ingest/                   # checkpoint、dedup、dead letter、pipeline
|-- storage/                  # 本地 JSONL store 和查询接口
|-- analytics/                # 软检测器、baseline、安全规则
|-- security/                 # 关联、实体图、hunting、attack chain
|-- cases/                    # case 合并、证据、调度
|-- agents/                   # analyst / reviewer 合同和 prompt
|-- dispatch/                 # 受控 analyst work order / queue / engine
|-- tools.py                  # security_query / hunt / trace 工具封装
`-- reports.py                # first response report 和取证包渲染
```

## 核心文件

- `models.py`：先理解数据长什么样，这是后面所有流程的共同语言。
- `parsers/security_alert_v1.py`：把 CSV / JSONL 变成统一事件。
- `storage/local_store.py` 和 `storage/query.py`：保存事件并按条件查回来。
- `analytics/detectors.py`：从事件里找可疑 finding。
- `cases/case_store.py` 和 `reports.py`：把 finding 汇成 case，再变成可读报告。
- `dispatch/work_orders.py`：把 case 翻译成 analyst/reviewer 可执行的受控工作单。
- `agent_py_agent/cli/logs.py`：用户从命令行接入这个模块。

## 数据流

1. 用户通过 `my-agent logs ingest <file>` 或 Live Lab replay 输入日志文件。
2. parser 把 CSV / JSONL 解析成标准事件。
3. ingest pipeline 做去重、checkpoint 和坏数据处理。
4. storage 写入本地 JSONL store。
5. query / detector 从 store 读事件，生成 finding。
6. cases 把相关 finding 合并成 case，并保留 evidence refs。
7. reports 生成第一响应报告和取证包。
8. dispatch 可以把 case 变成 analyst/reviewer work order，后续再接真实 subagent。

## 给初学编程学生的学习路径

1. 先看 `agent_py_agent/cli/logs.py`，理解用户命令怎么触发 ingest 和 query。
2. 再看 `models.py`，理解事件、发现、案件、证据引用这些名词。
3. 再看 `parsers/security_alert_v1.py`，学习外部文件怎么变成内部对象。
4. 再看 `storage/query.py`，学习程序怎么按条件过滤数据。
5. 再看 `analytics/detectors.py`，学习规则怎样从数据里发现问题。
6. 最后看 `agent_py_agent/tests/test_log_analysis_*.py`，用测试理解每一步应该保证什么。

## 当前第一版索引 / 待补齐

本页先描述主结构和学习路径。后续应补充每条 CLI 命令的输入输出样例，以及 Live Lab replay 的产物路径说明。
