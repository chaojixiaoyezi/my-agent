# Gateway：开发推进记录

## 已完成

- `agent/gateway.py` 已作为兼容入口，真实协议实现拆到 `gateway_parts/`。
- gateway 控制面已有 pid、state、heartbeat、stop request、log 等文件。
- request 队列已有 pending、processing、done、failed、responses 目录语义。
- CLI 已有 gateway process/client 相关命令，包括 start/status/stop/restart/logs、ask/result、run。
- processing 请求恢复、LocalStore 索引重建、adapter file 协议已有基础实现和测试记录。

## 解决的问题

- 把后台进程、请求队列、响应文件、恢复逻辑从巨大 CLI 入口中拆出来，降低维护风险。
- 前台 chat 可以作为 gateway 客户端，向后台投递普通消息。
- gateway 崩溃遗留的 processing 请求不再只能人工猜状态，可以按 attempts 和超时退回或归档。
- LocalStore 能看到 gateway request 和生命周期事件，方便 status/timeline/local-doctor 统一观察。

## 下一步

- 给 gateway 核心函数补齐和 LOG work-order 同级别的 `LLM:` / `新手说明:` / 参数说明。
- 把更多 gateway 场景加入隔离测试：多 request worker、取消/优先级、迟到响应、租约续期。
- 稳定默认入口体验，让普通 `my-agent` 更自然地确保 gateway 存活并进入 gateway chat。
- 如果 gateway 协议路径或数据流变化，同步更新本文件和 `04-structure.md`。

## 已跑测试

- 历史记录显示 gateway client、scenario-test gateway-restart、local-doctor、gateway ask/result/chat 相关路径已有测试或手工验收记录。
- 相关记录见 [TESTS.md](../../../TESTS.md)、[ACCEPTANCE.md](../../../ACCEPTANCE.md)、[EVIDENCE.md](../../../EVIDENCE.md)。
- 同步门 focused 验收：`python -m pytest agent_py_agent\tests\test_doc_sync.py` -> `5 passed`。
- 同步门手工检查：`python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 父会话全量回归：`python -m pytest` -> `241 passed`。
- 空白检查：`git diff --check` -> passed。

## 未跑测试

- 本文档第一版没有单独启动真实 gateway 后台进程做手工 ask/result。
- 暂未做多 gateway 组织或跨机器通信演练。

## 风险

- gateway 连接 CLI、后台进程、文件协议、LocalStore、chat、adapter，变化面大，文档很容易再次散开。
- 文件队列并发写入需要持续测试，否则 worker pool 扩大后容易出现重复处理或状态覆盖。
- 普通用户入口和开发者命令层级需要继续打磨，避免体验被内部协议细节淹没。
