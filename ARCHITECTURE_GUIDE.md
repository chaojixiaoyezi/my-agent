# Architecture Guide

这份文档给人和 LLM 都看。后续做代码拆分、功能开发、自动修复时，先按这里理解模块边界和注释规则。

## 注释规则

新写或重构后的类、函数、模块，优先使用“两层注释”：

第一行写给 LLM：用技术语言说明契约，讲清输入、输出、边界或职责。

第二行开始写给人：用大白话说明这个东西是干啥的，必要时给一个真实例子，说明失败时会怎么处理。

例子：

```python
def submit_gateway_ask(...) -> tuple[str, Path, Path]:
    """LLM contract: enqueue one ask request and return request/response paths.

    Human version:
    `gateway ask` 和 `chat --gateway` 都走这里。上层只关心“我投递了一条消息”，
    不用知道文件队列怎么命名。
    """
```

## 当前模块边界

`agent_py_agent/__main__.py`

CLI 入口层。它负责解析命令、组装参数、打印结果和启动后台线程。它不应该长期持有具体协议实现，比如 gateway 文件队列怎么归档、adapter 消息怎么转请求。

`agent_py_agent/agent/gateway.py`

gateway 协议层。它负责 gateway 路径、文件队列、请求领取、响应写出、adapter inbox/outbox、processing 恢复、gateway LocalStore 重建索引。

关键约束：
- 请求必须有 request_id。
- 响应必须带 status、duration_seconds、error_code/error。
- 文件队列移动必须通过明确的状态目录表达。
- 非关键副作用失败不能静默吞掉，要带 operation 和 request_id 报错。

`agent_py_agent/agent/file_io.py`

本地文件 I/O 小工具层。当前负责带锁追加 JSONL，避免并发 worker 把审计流水写坏。

`agent_py_agent/agent/core.py`

智能体运行层。它负责 prompt、记忆、模型后端、工具循环和 subagent runner 的编排。

`agent_py_agent/agent/tools.py`

工具执行边界。它负责工具目录、工具调用解析、工具 allowlist，以及 subagent 写入边界的硬拦截。

`agent_py_agent/agent/subagent.py`

子代理运行树层。它还偏大，后续优先继续拆成：models、work_order、dispatch、acceptance、patch_review、rendering、parsing。

## 拆分原则

1. CLI 只做命令入口，不承载业务协议。
2. 文件协议、网络协议、存储协议各自成模块。
3. 业务流程调用底层实现，底层实现不反向依赖 CLI。
4. 模型输出、用户输入、外部文件都默认不可信。
5. 关键路径必须可测试，不能只能通过真实 CLI 手测。
6. 每次拆分都要保留旧导入兼容，除非明确做破坏性升级。

## 后续拆分顺序

1. `subagent.py` 的 dataclass 模型先拆到 `subagent_models.py`。
2. subagent markdown 渲染拆到 `subagent_rendering.py`。
3. subagent runner 输出解析拆到 `subagent_parsing.py`。
4. `__main__.py` 里的 scenario-test 拆到 `scenario.py`。
5. `__main__.py` 里的 local-doctor / local-rebuild 拆到 `local_admin.py`。
