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

CLI 兼容入口层。真实 CLI 实现已经拆到 `agent_py_agent/cli/`，`__main__.py` 只保留 `python -m agent_py_agent` 入口和旧导入兼容。

`agent_py_agent/cli/`

CLI 命令层。按领域拆分为：
- `common.py`：配置加载、创建 `SimpleAgent`、能力路由、时间格式。
- `local_doctor.py` / `local_commands.py`：LocalStore 体检、重建、状态、记忆和搜索命令。
- `subagents.py`：子代理看板、due-check、动作、能力路由、验收、patch、dispatch、runner 入口。
- `daemon.py`：daemon 参数合并和前台常驻调度。
- `gateway_process.py` / `gateway_client.py` / `adapter.py`：gateway 进程、客户端 ask/result、文件 adapter。
- `scenario.py` / `scenario_cases.py` / `scenario_utils.py`：隔离场景测试入口、专项 case、fixture 工具。
- `chat.py`：交互式 chat 队列和斜杠命令。
- `parser.py`：argparse 命令树。

`agent_py_agent/agent/gateway.py`

gateway 兼容入口层。真实协议实现已经拆到 `agent_py_agent/agent/gateway_parts/`。

`agent_py_agent/agent/gateway_parts/`

gateway 协议层。按职责拆为：路径模型、JSON 文件队列 IO、进程控制、LocalStore 日志镜像、processing 恢复、运行时请求处理、adapter inbox/outbox 转换。

关键约束：
- 请求必须有 request_id。
- 响应必须带 status、duration_seconds、error_code/error。
- 文件队列移动必须通过明确的状态目录表达。
- 非关键副作用失败不能静默吞掉，要带 operation 和 request_id 报错。

`agent_py_agent/agent/file_io.py`

本地文件 I/O 小工具层。当前负责带锁追加 JSONL，避免并发 worker 把审计流水写坏。

`agent_py_agent/agent/core.py`

智能体组合入口层。真实实现已经拆到 `agent_py_agent/agent/agent_core/`。

`agent_py_agent/agent/agent_core/`

智能体运行层。按职责拆为：主模型/工具循环、子代理 runner、父代理 planner、dispatch/watch、runner 候选和重试规则、编排工具、参数归一化、watch lock、prompt 模板。

`agent_py_agent/agent/tools.py`

工具兼容入口层。真实实现已经拆到 `agent_py_agent/agent/tooling/`。

`agent_py_agent/agent/tooling/`

工具执行边界。按职责拆为：工具元数据和检索模型、工作区文件工具、HTTP 工具、工具调用解析、写入边界门禁、工具注册表。

`agent_py_agent/agent/subagent.py`

子代理兼容入口层。真实实现已经拆到 `agent_py_agent/agent/subagents/`，包括模型、报告、渲染、解析、策略、probe、manager mixin、验收、patch、dispatch、runner 结果、索引等模块。

`agent_py_agent/agent/local_store.py`

LocalStore 组合入口层。真实实现已经拆到 `agent_py_agent/agent/local_storage/`。

`agent_py_agent/agent/local_storage/`

本地事实源层。按职责拆为：数据模型、schema/连接、记录写入与正文文件、FTS/LIKE 搜索、事件时间线、维护统计。

`agent_py_agent/agent/DIRECTORY_GUIDE.md`

agent 目录地图。后续新文件先按这份文档找目录，避免重新出现散落文件和 `utils/common/shared` 垃圾桶。

第二轮已定义的新目录：
- `backends/`：模型后端适配。
- `capability/`：能力路由、能力配置、skill card。
- `settings/`：运行配置 schema 和加载。
- `io/`：无业务含义的底层文件 I/O 原语。
- `memory_store/`：长期记忆存储。
- `prompting_parts/`：prompt 构造和未来上下文预算。
- `clients/`：未来非模型外部服务客户端。
- `repositories/`：未来领域仓储接口。
- `observability/`：未来日志、耗时、状态、错误码和 trace 标准。
- `security/`：未来权限、净化、可信度和安全默认策略。
- `validators/`：未来跨领域校验规则。

## 2026-04-29 大文件拆分报告

拆分前问题：
- `__main__.py`、`core.py`、`tools.py`、`gateway.py`、`local_store.py`、`subagent.py` 都混了多个变化原因；CLI、协议、业务编排、存储、工具解析交织在一起。
- 大函数承担过多阶段，比如 dispatch、chat、build_parser、gateway run，不利于定位 bug 和补单测。
- 外部协议和底层实现容易散落在业务入口里，后续扩展 gateway、adapter、runner、工具安全边界时风险高。

拆分后结构：
- CLI 层只做命令解析、参数整理、打印和进程入口。
- Core 层只组合主代理依赖，运行循环、子代理、dispatch、prompt、工具编排拆成 `agent_core/`。
- Gateway 层只暴露兼容入口，协议细节拆成 `gateway_parts/`。
- Tool 层只暴露兼容入口，具体工具、解析、注册、写边界拆成 `tooling/`。
- LocalStore 层只暴露组合入口，schema、records、search、events、maintenance 拆成 `local_storage/`。
- Subagent 层只暴露兼容入口，manager 能力按职责拆到 `subagents/`。

迁移清单：
- 保留旧导入：`agent_py_agent.__main__`、`agent_py_agent.agent.core`、`agent_py_agent.agent.tools`、`agent_py_agent.agent.gateway`、`agent_py_agent.agent.local_store`、`agent_py_agent.agent.subagent` 继续可用。
- 新代码优先导入职责包：`cli/`、`agent_core/`、`tooling/`、`gateway_parts/`、`local_storage/`、`subagents/`。
- 生产 Python 文件当前没有超过 500 行；最大的 `cli/gateway_process.py` 为 498 行。

影响范围：
- 对用户 CLI 行为、工具调用协议、gateway 文件协议、LocalStore 数据结构、subagent 工单格式保持兼容。
- 风险主要在拆分后的跨模块导入、旧私有函数兼容、少量 CLI 边角命令路径。

测试命令：
- `python3 -m py_compile agent_py_agent/__main__.py agent_py_agent/cli/*.py agent_py_agent/agent/*.py agent_py_agent/agent/*/*.py`
- `python3 - <<'PY' ... 全量发现测试 ... PY`
- `python3 -m agent_py_agent --help`
- `python3 -m agent_py_agent local-doctor --json`
- `python3 -m agent_py_agent gateway status`

剩余风险：
- 注释规范已经在新拆模块的模块/组合类上落地；部分迁移出来的历史函数 docstring 还保留旧写法，后续改动到哪个函数时继续补齐“LLM contract + Human version”。
- `manager_*` 里仍有少数 100 行以上复杂函数，已经低于 500 行文件线，但后续可以继续按验收规则、报告渲染、状态计算再细拆。

## 2026-04-29 第二轮目录归位

拆分前问题：
- `agent/` 根目录还散落着 backend、config、capabilities、skills、memory、prompting、file_io 等真实实现文件。
- 根目录既像入口层，又像实现层；后续扩展模型客户端、能力治理、记忆存储、prompt 策略时容易继续堆平铺文件。

拆分后结构：
- `backend.py` 迁入 `backends/base.py`，根文件变兼容门面。
- `capabilities.py`、`capability_config.py`、`skills.py` 迁入 `capability/`，根文件变兼容门面。
- `config.py` 迁入 `settings/config.py`，根文件变兼容门面。
- `file_io.py` 迁入 `io/jsonl.py`，根文件变兼容门面。
- `memory.py` 迁入 `memory_store/jsonl.py`，根文件变兼容门面。
- `prompting.py` 迁入 `prompting_parts/builder.py`，根文件变兼容门面。
- 新增未来目录 `clients/`、`repositories/`、`observability/`、`security/`、`validators/`，用 README 先固定职责边界。

迁移清单：
- 老导入路径继续可用：`agent.backend`、`agent.config`、`agent.capabilities`、`agent.capability_config`、`agent.skills`、`agent.file_io`、`agent.memory`、`agent.prompting`。
- 新代码优先导入新职责目录：`agent.backends`、`agent.settings`、`agent.capability`、`agent.io`、`agent.memory_store`、`agent.prompting_parts`。

影响范围：
- 运行行为不变；这是目录归位和边界定义。
- 风险集中在迁移后相对导入是否正确，已通过编译和测试覆盖。

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
