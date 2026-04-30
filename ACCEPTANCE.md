# ACCEPTANCE

## 第一轮验收
- [x] Python3 可运行
- [x] CLI 支持
- [x] JSONL 记忆功能
- [x] 参数外置且中文说明
- [x] subagent 思维/计划/结果记录
- [x] 动态 prompt 注入
- [x] 工程分层、可扩展 backend
- [x] 暂无 Web，但预留 extensions 边界
- [x] 真实入口和错误入口测试通过

## 仍可迭代
- [ ] 接真实模型后端（OpenAI / 本地 HTTP / llama.cpp）
- [ ] 工具系统
- [ ] 更强记忆检索（向量/RAG）
- [ ] subagent 执行器与验收器
- [ ] 插件机制

## 2026-04-30 / 日志分析第一版父会话验收

阶段提交：
- `97b8bc4 Add log analysis foundation`

已通过：
- [x] `python -m pytest`：初验 155 passed，修复后复验 164 passed。
- [x] LOG 模块 `py_compile` 通过。
- [x] `SecurityAlertV1` ingest / query / dispatch / detector 专项测试通过，修复后 33 passed。
- [x] LOG 模块默认配置为关闭，不影响普通命令。

已修复并复验：
- [x] 默认 `ingest_file(root=...)` 写入路径与 `security_query(root=...)` 读取路径不一致，导致导入成功后默认查询为空。
- [x] storage JSONL 读到坏行时会让整个查询失败，缺少坏行隔离。
- [x] query 的 `limit` 只限制返回，不限制 evidence 写入，受控查询边界不够硬。
- [x] `EvidenceRef` dataclass 在 analyst/dispatch 摘要里可能被转成不可用字符串。
- [x] dispatch public protocol 与实际 engine 方法和 result 结构不一致。
- [x] identity 类 case dedup 没纳入 user，可能把同源 IP、同时间桶的不同账号合并。
- [x] detector 缺时间戳时会把时间窗口判断放行，可能跨任意时间错误关联。
- [x] route/report 接收批量 findings 时缺少按 `case.finding_refs` 过滤。

验收未通过项：
- [ ] LOG 模块还没有 CLI 子命令，也没有接入主工具 registry。
- [ ] `query_default_limit/query_max_limit/data_dir` 还没有完整贯穿 CLI/tool/runtime，只做了局部默认值和硬限制。
- [ ] 坏 JSONL 行目前只跳过，没有独立 corrupt-line audit/metric。
- [ ] detector 的 `next_queries` 仍是自然语言字符串，还不是结构化 query plan。

已拆给 worker：
- storage/ingest/query 闭环修复。
- evidence/ref 与 dispatch 合同修复。
- detector 时间关联、case dedup、route/report 过滤修复。

父会话保留：
- 决定 CLI / tool registry / profile / 文档拆分的下一批派工。
