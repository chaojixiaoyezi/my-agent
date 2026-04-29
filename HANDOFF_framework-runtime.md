# Workstream Handoff

## 基本信息

- workstream: framework-runtime
- branch: workstream/framework-runtime
- worktree: /Users/example/my_agent/my-agent-worktrees/framework-runtime
- owner: 会话运行时
- date: 2026-04-29

## 本线目标

解决 gateway 本地文件队列里长任务 processing lease 不续期的问题，避免正常运行中的长任务被 recovery/local-doctor 误判 stale，进而重复执行或恢复混乱。

## 实际完成

- processing claim 写入明确 lease 字段：status、attempts、lease_owner、lease_started_at、lease_heartbeat_at。
- agent.run 执行期间启动 processing lease heartbeat 线程，按配置/timeout 自动选择刷新间隔，周期性刷新 lease_heartbeat_at。
- lease heartbeat 使用原子 JSON 替换写入，降低 recovery/local-doctor 读到半截 JSON 的概率。
- recovery/local-doctor 的 stale 判断优先使用 lease_heartbeat_at，再回退 lease_started_at / started_at / updated_at / created_at / mtime。
- processing 中已存在 response 的重复请求会安全归档到 done，不再重排或重复执行；_handle_gateway_request 也会在 response 已存在时直接返回既有响应。
- 补充测试覆盖 heartbeat 刷新、recovery 使用 lease_heartbeat_at 防误判、已有 response 的 processing 副本归档。

## 改动文件

- agent_py_agent/agent/gateway_parts/io.py
- agent_py_agent/agent/gateway_parts/runtime.py
- agent_py_agent/agent/gateway_parts/recovery.py
- agent_py_agent/tests/test_local_store.py
- HANDOFF_framework-runtime.md

## 测试命令和结果

```bash
python3 -m py_compile agent_py_agent/agent/gateway_parts/io.py agent_py_agent/agent/gateway_parts/runtime.py agent_py_agent/agent/gateway_parts/recovery.py agent_py_agent/tests/test_local_store.py
python3 -m pytest agent_py_agent/tests/test_local_store.py
python3 -m pytest agent_py_agent/tests
git diff --check
```

结果：

- py_compile 通过。
- test_local_store.py: 14 passed。
- agent_py_agent/tests: 77 passed。
- git diff --check 无输出，通过。

## 影响范围

- 只影响 gateway request 文件队列的 processing lease、recovery 判定和相关测试。
- 未修改 tooling/security、tools.py、write_boundary、memory 相关文件。

## 需要主线重点复查

- heartbeat 线程和 recovery 同时读写同一个 processing JSON 时仍是无锁文件协议；当前用原子替换降低半写风险，但没有引入跨进程锁。
- 新增 recover_gateway_processing_requests 返回字段 archived，现有调用按 key 读取 requeued/failed/checked，不应破坏兼容。

## 需要其他线协调

- tools-boundary 线无需接口协调；本线没有触碰 tooling/security 文件。

## 剩余风险

- 极端竞态下，recovery 已经决定重排的瞬间 heartbeat 线程也可能尝试刷新同一文件；正常 heartbeat 间隔会让这种情况很少发生，但文件队列仍不是强锁模型。
- 如果外部手工写入非标准 processing payload，read_json_file 会把非 dict JSON 当作空对象处理，recovery 会按旧兜底逻辑归档或重排。

## 后续建议

- 后续如果要进一步加强并发语义，可以为 processing request 引入轻量 lockfile 或 compare-and-swap 版本字段。
- local-doctor 展示 stale_processing 时可补充 lease_owner/lease_heartbeat_at 的人类可读时间，方便排障。
