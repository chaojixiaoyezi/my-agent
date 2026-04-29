# Agent Directory Guide

这份文档定义 `agent_py_agent/agent/` 下每个职责目录的含义。给人看时，它是目录地图；给 LLM 看时，它是改代码前的边界规则。

## 根目录原则

`agent/` 根目录只保留稳定门面文件和极少数入口文件。真实实现优先进入职责目录。

根目录里的这些文件是兼容门面：
- `backend.py` -> `backends/`
- `capabilities.py` / `capability_config.py` / `skills.py` -> `capability/`
- `config.py` -> `settings/`
- `file_io.py` -> `io/`
- `memory.py` -> `memory_store/`
- `prompting.py` -> `prompting_parts/`
- `core.py` -> `agent_core/`
- `gateway.py` -> `gateway_parts/`
- `local_store.py` -> `local_storage/`
- `subagent.py` -> `subagents/`
- `tools.py` -> `tooling/`
- `memory_settings.py` -> `settings/memory.py`

新代码不要把真实业务继续堆到门面文件里。门面文件只做旧导入兼容。

## 目录定义

### `agent_core/`

主代理应用服务层。放 `SimpleAgent` 的主循环、子代理 runner、父代理 planner、dispatch/watch、编排工具和 runner 重试规则。

允许：业务流程编排、调用下层服务、把多个模块串成一轮完整行为。

不允许：直接写数据库 schema、直接实现外部 HTTP 协议、直接手写工具文件操作细节。

### `backends/`

模型后端适配层。放模型响应 DTO、后端接口、OpenAI/Anthropic/本地模型等客户端适配。

允许：模型 API payload、响应解析、后端错误转换、超时配置读取。

不允许：agent 业务流程、prompt 组装、记忆写入。

### `capability/`

能力治理层。放 Capability Card、Skill Card、Tool Card 路由、能力配置、skill 扫描。

允许：能力检索、能力授权候选、skill/tool 卡片统一抽象、能力缺口上抛相关结构。

不允许：真正执行工具、直接跑模型、直接修改 subagent 状态。

### `clients/`

未来外部服务客户端层，当前只保留目录定义。

用途：当以后接 GitHub、Slack、数据库、浏览器、远端 agent、MCP server 时，把“怎么请求外部服务”放这里。

边界：client 只处理协议、鉴权、超时、重试、错误映射；业务决策放 service/agent_core/gateway/subagents。

### `gateway_parts/`

gateway 文件协议层。放 gateway 路径、JSON 队列 IO、进程状态、processing 恢复、请求执行、adapter 转换和索引日志。

允许：文件队列状态流转、request/response payload、gateway heartbeat、adapter inbox/outbox。

不允许：CLI 参数解析、chat UI、模型后端实现。

### `io/`

底层本地 I/O 原语层。当前放带锁追加 JSONL。

允许：无业务含义的文件原语，比如 locked append、atomic write、safe read。

不允许：gateway/subagent/memory 这类业务语义。函数名一旦有业务含义，就应该回到对应业务目录。

### `local_storage/`

本地事实源层。放 SQLite schema、records、events、FTS/LIKE search、maintenance。

允许：数据库表结构、内容文件路径、LocalStore 搜索和维护统计。

不允许：CLI 打印、gateway 请求执行、subagent 状态机。

### `memory_store/`

长期记忆存储层。当前是 JSONL 记忆和 LocalStore 索引双写。

允许：记忆记录模型、记忆文件读写、记忆索引、未来 compact/sync。

不允许：普通 LocalStore schema、prompt 拼装、模型调用。

### `memory_archive/`

记忆冷归档和压缩前 hook 存储层。当前放 `CompressionSnapshot`、`RawMemoryEvent`、按天 JSONL 写入、readback 验证、hook 留存清理和 token 估算。

允许：定义压缩前恢复快照、raw 会话事件索引卡、固定目录写入、留存策略、轻量 token 估算。

不允许：决定什么时候压缩、调用模型生成摘要、把快照直接塞进 prompt、替代任务目录事实源。

### `memory_routing/`

长期规则路由层。负责把用户输入确定性匹配到 memory routing index，再解析出应该查看的 authority file，并提供受 root 边界保护的短正文读取 context。

允许：route index 加载、关键词/别名匹配、soft/strict path resolution、read receipt 结构、索引诊断、runtime 规则 context 构建。

不允许：修改长期规则文件、越过 root 读取文件、替代 RAG 或任务状态核验、直接改主循环。

### `observability/`

未来可观测性层，当前只保留目录定义。

用途：统一 request_id、耗时、状态、错误码、日志结构、metrics、trace。

边界：它提供记录和格式标准，不做业务决策，不吞异常。

### `prompting_parts/`

Prompt 构造层。放系统 prompt、记忆、工具目录、推荐工具、工具 transcript、未来上下文压缩策略。

允许：把上下文拼成模型可见文本、prompt 文件读取、上下文预算策略。

不允许：直接执行工具、直接改记忆、直接处理 gateway 文件队列。

### `repositories/`

未来仓储层，当前只保留目录定义。

用途：当某个领域的数据访问变复杂时，把“怎么读写这个领域的数据”放这里，向 service 提供稳定接口。

边界：repository 只负责持久化和查询，不负责业务状态决策。当前 LocalStore 仍在 `local_storage/`，不强行搬。

### `security/`

未来安全边界层，当前只保留目录定义。

用途：集中放输入净化、权限策略、路径权限、模型输出可信度、安全默认规则。

边界：高风险执行前的硬门禁可以在这里抽象；具体工具实现仍在 `tooling/`，subagent 工单策略仍在 `subagents/`。

### `settings/`

运行配置层。放 `AgentConfig`、轻量 YAML 读取、环境变量覆盖。

允许：配置 schema、配置文件解析、默认值、memory 配置安全规范化和 warning receipt。

不允许：业务执行、动态状态、运行时报告。

### `subagents/`

子代理领域层。放 subagent 模型、报告、工作区文件、看板、due-check、动作、能力路由、验收、patch、runner 结果、索引。

允许：子代理状态机、工单文件、父子关系、验收规则、子代理报告渲染。

不允许：CLI 参数解析、模型后端 HTTP 协议、全局配置解析。

### `tooling/`

模型可调用工具层。放工具元数据、文件工具、HTTP 工具、工具调用解析、工具注册表、写入边界。

允许：工具执行、工具参数校验、工具 allowlist、工具输出格式、写入边界硬拦截。

不允许：父代理 dispatch 决策、subagent 验收规则、CLI 交互。

### `validators/`

未来跨领域校验层，当前只保留目录定义。

用途：当某些验证规则跨 gateway/subagent/tooling/local_storage 多处复用时，放到这里。

边界：validator 返回明确结果和错误码，不负责修复、不直接写状态。

## 新模块放置决策

判断一个新文件放哪里，可以按这个顺序问：

1. 它是不是外部协议客户端？放 `backends/` 或 `clients/`。
2. 它是不是主代理流程编排？放 `agent_core/`。
3. 它是不是子代理领域规则？放 `subagents/`。
4. 它是不是工具实现或工具安全边界？放 `tooling/`。
5. 它是不是 gateway 文件协议？放 `gateway_parts/`。
6. 它是不是本地事实源持久化？放 `local_storage/`。
7. 它是不是记忆存储？放 `memory_store/`。
8. 它是不是 prompt 上下文构造？放 `prompting_parts/`。
9. 它是不是配置 schema？放 `settings/`。
10. 它只是很底层、无业务含义的文件 I/O 原语？放 `io/`。

如果都不是，先写清楚变化原因，再决定是否需要新目录；不要塞进 `utils/common/shared`。
