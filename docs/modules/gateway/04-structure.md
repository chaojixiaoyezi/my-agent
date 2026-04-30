# Gateway：结构树和详细说明

## 模块结构

```text
agent_py_agent/agent/
|-- gateway.py                         # 兼容入口，重新导出 gateway_parts 的主要能力
`-- gateway_parts/
    |-- paths.py                       # gateway / adapter 的目录和文件路径模型
    |-- io.py                          # JSON 文件读写、请求文件流转
    |-- process_control.py             # pid、heartbeat、stop request、进程启动停止
    |-- logging.py                     # gateway 日志和 LocalStore 事件镜像
    |-- recovery.py                    # processing 请求恢复、失败归档
    |-- runtime.py                     # 请求 worker、agent.run 调用、响应写回
    `-- adapter.py                     # inbox/outbox 文件 adapter

agent_py_agent/cli/
|-- gateway_process.py                 # gateway start/status/stop/restart/logs/run
|-- gateway_client.py                  # gateway ask/result/default 客户端入口
`-- adapter.py                         # 文件 adapter CLI
```

## 核心文件

- `gateway_parts/paths.py`：集中描述所有 gateway 文件路径，避免路径散落各处。
- `gateway_parts/io.py`：负责请求 JSON 的读写和目录迁移。
- `gateway_parts/process_control.py`：负责后台进程生命周期。
- `gateway_parts/recovery.py`：处理卡在 processing 的请求。
- `gateway_parts/runtime.py`：真正执行 request worker，从 pending 取请求、调用 agent、写 response。
- `cli/gateway_process.py`：用户管理后台进程的命令。
- `cli/gateway_client.py`：用户或 chat 客户端投递消息和读取结果的命令。

## 数据流

1. 用户运行 `gateway ask` 或 `chat --gateway`，客户端把请求写入 `requests/pending`。
2. 后台 gateway worker 抢占 pending 请求，移动到 `requests/processing`。
3. worker 调用 `SimpleAgent.run()` 或相应处理逻辑。
4. 成功后写 response 文件，并把请求归档到 done。
5. 失败或超时后，根据 attempts 和 lease 退回 pending 或归档 failed。
6. status/local-doctor/timeline 从 gateway state、heartbeat、history 和 LocalStore 读取可观察状态。

## 给初学编程学生的学习路径

1. 先看 `agent_py_agent/cli/gateway_client.py`，理解用户如何提交一个请求。
2. 再看 `gateway_parts/paths.py`，理解 gateway 用哪些目录表达状态。
3. 再看 `gateway_parts/io.py`，学习请求文件怎么从 pending 移到 processing/done。
4. 再看 `gateway_parts/runtime.py`，理解 worker 如何处理请求并写响应。
5. 再看 `gateway_parts/recovery.py`，理解程序崩溃后怎么恢复。
6. 最后看 `agent_py_agent/tests/test_gateway_client.py` 和 scenario 测试，理解怎样证明协议没坏。

## 当前第一版索引 / 待补齐

本页先描述单机文件协议。后续应补充真实目录样例、请求 JSON schema、response JSON schema、失败恢复时序图和 gateway chat 的用户路径。
