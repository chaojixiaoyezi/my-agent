# TODO11 agent 决策目录与显式探测交接

分工：decision_http_max，Astra max。只认领原 `tooling/user_config_tool.py`、
新 `tests/test_user_config_decision_operations.py` 与本交接；父侧拥有原模型操作、探测和共享文档。
未提交、推送、部署或调用收费模型。

## 解决问题与实现

原 `user_config` 只有决策覆盖 read/patch/reset，模型无法从可信脱敏目录取得编号，也不能按用户要求测试已存连接。
现在原工具 schema 新增两种结构化 action：

- `decision_models`：只接受 `action`，调用原 `execute_model_profile_operation` 的同名操作，
  返回当前 owner 私有及管理员显式共享的 Decision 配置。既不初始化聊天模型，也不探测网络。
- `decision_probe`：只能且必须显式填写 `action`、`profile_id`、有限正 `timeout_seconds`。
  引用和时间沿原 schema 校验，必须有当前可信会话，再调用原同名模型操作。
  工具描述明确仅按用户要求测试，会产生网络和用量，读取目录、修改或保存后不得自动测试。

新动作拒绝 payload 中的 owner/thread/run/task 身份、scope、端点、密钥和请求头等额外字段。
线程只取原 `current_task_attributes`，runner 上下文优先于宿主旧属性；使用原 owner 认证和存储访问校验。
目录可在可信 owner 无线程时读取；工具探测无可信线程则拒绝，不能借调用参数补身份。

两动作复用原工具中央权限、串行、审计和操作幂等策略，不新增工具权限或凭据写入能力。
原 `view/set` 和 `decision_read/patch/reset` 处理不变。

## 回执合同

原服务报告保持完整，在工具输出 JSON 和 `result_envelope.decision_report` 中返回；
追加 `scope_resolution.source=current_runner_context` 及实际 `thread_id`。
目录有效范围标为 `owner`，显式探测为 `thread`，不接受模型指定的身份。

探测报告 `ok=false` 时，`ToolHandlerOutcome.ok=false`，报告中的 `error_type`、
`message`、`elapsed_seconds`、`usage` 保留，未报 usage 不变成真实零。
使用原 `TOOL_EXECUTION_FAILED` 和 `DECISION_PROBE_FAILED` 报码；
`effect_outcome=unknown` 不声称失败请求从未发送或服务端已经停止。
原探测服务负责脱敏和原用量账本，工具不解析错误正文、不复制后端或目录实现。
用户 `InterruptedError`、`KeyboardInterrupt` 等取消继续传播，不能包装成普通测试失败。

## 验证

```bash
python3 -m pytest agent_py_agent/tests/test_user_config_decision_operations.py agent_py_agent/tests/test_user_config_capability.py agent_py_agent/tests/test_decision_model_operations.py -o addopts='' -q --tb=short
```

联合结果：71 passed，0.91 秒；新增 44 项。
实际 ToolExecutor → user_config → 原模型操作 → 原决策探测 → 本地 HTTP → 原 usage 结算路径成功。
错答路径实际返回工具失败和原安全报告；跨 owner 线程拒绝且没有 HTTP；原工具权限门可在 handler 前阻断。
另覆盖脱敏私有/共享目录、其他用户和普通生成模型不可见、无隐式预算、有限时间边界、额外身份/凭据拒绝和取消传播。
原 user_config 的覆盖 read/patch/reset、CAS 和失效连接关闭回归保持。

所改生产/测试文件 Ruff 通过，所改生产文件 strict code-size 为 0 blocker，`git diff --check` 通过。
未跑全仓 pytest；不以本地协议测试代替真实供应商可用性、决策质量或 TUI 真机验收。

## 建议下一步

父侧将本片与原 decision_models/decision_probe、设置作用域和菜单联合验收，再统一同步共享文档和提交。
菜单侧可继续并行，只消费原模型操作；不要让读取目录或保存配置自动追加探测，也不要把探测失败或未知用量显示为成功或零输入 token。
