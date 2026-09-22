# 决策模型 P1-B 交接

## 基本信息

- workstream：可选决策模型，完整 P1—P5 Goal 的协议与传输切片。
- branch：`codex/decision-model-integration`；独立受管 worktree，原 checkout 未修改。
- owner：主代理负责协议、适配与整合；Astra max 子代理负责严格 HTTP，高档子代理只读复核协议。
- date：2026-09-22；本片基线 `17530b518`。

## 本线目标与实际完成

让 Jev 用原模型连接和 HTTP 入口完成一次严格、有界的结构化判断，不安装外部 harness 或 SDK。
输入固定宿主来源、版本与摘要，外部结果不获得执行或权限权威。坏题局部拒绝，损坏 JSON 整包拒绝。
未知 usage 保持未知，原始供应商用量保留；用量展示约定是原行增加决策输入，不显示价格、输出暂留白，尚未接 UI。

传输增加显式绝对期限、重试数及响应上限；普通请求默认不变。决策零重试、禁止重定向，错误按状态立即结束。
编码前限制输入体积，严格 JSON 解析前限制 64 层、解析后核对原期限，慢滴流不重置预算。
这只是 P1-B 与 P1-C 传输部分，不包含完整可选调用服务或实际产品入口。

## 改动文件

- `agent_py_agent/agent/backends/decision_protocol.py`：请求快照、宿主绑定、输入资源界限与响应合同。
- `agent_py_agent/agent/backends/typesafe_decision_wire.py`、`typesafe_decision.py`：原生三类题目、逐题结果与独立 decide。
- `agent_py_agent/agent/backends/gateway_helpers.py`、`gateway_request_limits.py`：原 HTTP 严格请求和共用 socket 事实源。
- `agent_py_agent/tests/test_decision_protocol.py`、`test_typesafe_decision.py`、`test_gateway_strict_request.py`。
- Goal、设计、入口、测试、树、路线、已完成与本交接；尺寸报告由原脚本更新。

## 测试命令和结果

```bash
python3 -m pytest agent_py_agent/tests/test_decision_protocol.py agent_py_agent/tests/test_typesafe_decision.py agent_py_agent/tests/test_gateway_strict_request.py agent_py_agent/tests/test_gateway_helpers.py agent_py_agent/tests/test_provider_request_scope.py agent_py_agent/tests/test_compact_request_budget.py agent_py_agent/tests/test_runtime_module_boundaries.py -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
python3 scripts/check_clean_package.py .
```

联合 7 个文件 216 项通过，零失败/错误/跳过；其中协议/适配 59 项、严格 HTTP 46 项。
本地 HTTP 真 socket 覆盖中文请求、慢响应、重定向和关闭边界；没有真实供应商模型或实际 TUI 验收。
首轮固定 1500 层 JSON 必然报 RecursionError 的测试假设错误，已修为解析前资源预检及真实深 JSON 测试。
整数/字符串过量验证编码器尚未调用；关闭噪声不掩盖原 HTTP 状态，过期成功/错误响应均释放。
本地严格 gate 已通过：Ruff、doc sync、strict code-size、diff 和 clean-package 均通过；尺寸 baseline 未更改。
本片未推送，线上 CI 没有作为验收来源。

## 参考核对

核对 TypeSafe 官方 API/quickstart 与 jev-harness 的 `src/harness.ts`，只采用公开协议，不复制整个运行时。
HTTP 子代理核对本地 Codex 的 codex-client README/retry.rs 与 http-client README/request.rs/transport.rs，
复用请求局部策略、原中断 guard 和传输入口；没有新增第三方依赖。

## 影响范围与重点复查

严格限制仅显式请求生效，原普通生成、SSE 首事件/滚动空闲与后台长请求预算继续使用原合同。
HTTP 错误不读正文时不能确认 429 是否硬额度，不伪造硬额度分类；后续决策调用边界负责短期冷却。
同步 DNS 与 CPU 解析无法强制终止；外层及时返回、迟到结果无效和未退出资源有界仍须 P1-C/D 实现。
没有更改默认配置、日常模型、持久数据或部署；不把单适配器测试当成“故障已不会拖主链路”的证明。

## 需要其他线协调

插件线已确认后端、request_scope、interrupt、curator_backend 和 hot_path 无重叠。
本线未改原 checkout、插件调用/审批、Gateway 启停或共享 TUI；共享文档集成必须保留双方精确段落。

## 剩余风险与建议下一步

继续 P1-C/D：从 Curator 迁出唯一有界调用原语，补取消唤醒、稳定资源身份、残留名额和主请求资源保留。
再接 P1-E/F 的共用设置及原调用账本，然后实现用户要求的同一统计行展示。不得新增平行配置库或用量账。
插件线可独立并行，公共 interrupt/请求边界由一个实施者处理；完整 Goal 保持 active。
