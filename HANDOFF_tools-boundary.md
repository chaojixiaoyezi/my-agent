# Workstream Handoff

## 基本信息

- workstream: tools-boundary
- branch: workstream/tools-boundary
- worktree: /Users/example/my_agent/my-agent-worktrees/tools-boundary
- owner: 会话运行时
- date: 2026-04-29

## 本线目标

审视并加固工具安全边界，重点覆盖文件读写路径 containment、subagent 写边界、模型工具调用参数不可信、网络工具输入限制、错误可处理性和审计信息。

## 实际完成

- 文件系统工具增加统一参数校验：路径必须是字符串路径、非空、不过长、无控制字符；文本/整数/布尔参数有稳定错误。
- 文件读写错误不再回显工作区外绝对路径；不存在/非文件等工作区内错误使用相对路径。
- search_text 跳过解析后落到工作区外的 symlink 文件，避免搜索读到 workspace 外内容。
- write_boundary 对 path 类型、空 path、过长 path、控制字符、symlink 解析失败和工作区越界给出明确拒绝原因。
- 工具注册层拒绝非 JSON 对象 payload、过多字段、异常字段名和异常 tool 名；JSON parse error raw 截断。
- 网络工具只允许 http/https URL，拒绝 URL userinfo、坏 method、坏 header、对象 body，并截断 HTTP error body。
- 补充工具边界回归测试，覆盖拒绝原因、symlink 越界、locked_files 子路径、非对象 payload、网络坏输入。

## 改动文件

- agent_py_agent/agent/tooling/filesystem.py
- agent_py_agent/agent/tooling/write_boundary.py
- agent_py_agent/agent/tooling/registry.py
- agent_py_agent/agent/tooling/web.py
- agent_py_agent/agent/tooling/parser.py
- agent_py_agent/tests/test_tools.py

## 测试命令和结果

```bash
python3 -m pytest agent_py_agent/tests/test_tools.py
python3 -m pytest agent_py_agent/tests/test_capabilities.py agent_py_agent/tests/test_agent.py
python3 -m pytest agent_py_agent/tests
git diff --check
```

结果：

- test_tools.py: 19 passed
- test_capabilities.py + test_agent.py: 44 passed
- agent_py_agent/tests: 79 passed
- git diff --check: passed

## 影响范围

- 文件系统工具的坏参数现在会被显式拒绝；以前一些隐式 str(...) 的对象参数不再写入/请求。
- read_file 的 start_line/end_line 现在必须是正整数，end_line 不能小于 start_line。
- http_request body 现在要求字符串或标量文本；如果要发送 JSON 对象，需要模型先序列化成 JSON 字符串。
- fetch_url/http_request 不再支持 urllib 默认可处理的非 HTTP scheme。

## 需要主线重点复查

- 参数收紧是否会影响已有模型提示里的非常规工具调用格式，尤其是 content/body 曾经传对象的场景。
- 错误信息目前偏保守，不输出绝对路径；如后续需要更强审计，可考虑结构化 result metadata，而不是把敏感路径塞回 prompt。

## 需要其他线协调

- 暂无必须协调项。本线未修改 gateway/runtime、memory 或主仓库文件。

## 剩余风险

- 文件写入仍然无法完全消除 TOCTOU 竞态；当前在写入前后做 resolve containment，已覆盖常规 symlink 逃逸。
- 网络工具仍允许访问内网/localhost，因为现有测试和调试场景依赖 127.0.0.1；如要做 SSRF 级边界，需要新增配置化 allowlist/denylist。
- 读工具目前只有 workspace containment，没有独立 allowed_read_roots；如果 subagent 需要更细读隔离，应另开设计。

## 后续建议

- 给 ToolExecutionResult 增加可选结构化 metadata，例如 reason_code、display_path、audit_id，减少从中文 output 里解析审计信息。
- 增加配置化 network policy：allowed_schemes、blocked_hosts/private_ranges、max_body_bytes、是否允许 localhost。
- 如果 subagent 将来需要更细隔离，可新增 read_boundary，与 write_boundary 共用路径规范化和审计格式。
