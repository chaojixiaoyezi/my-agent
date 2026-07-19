# Verification：结构

```text
agent/verification/
|-- project_facts.py   # 从真实项目文件发现规范验证命令，并按精确 token 分类
|-- repository.py      # owner data/verification/evidence.sqlite3 事件与状态投影
`-- runtime.py         # 共用工具执行出口的唯一接线
```

## 数据流

1. `tool_call_runtime.execute_traced_tool_call` 得到真实 `ToolExecutionResult`。
2. `runtime.py` 从 `ToolCallEnvelope.scope.root_task_id` 取得任务树身份。
3. `run_command` 只有命中项目声明的规范命令且进程真实退出时才写事件。
4. 文件工具只有返回 `ok=true` 时才登记 changed paths，并把旧状态投影为 stale。
5. 精简 `verification_evidence` / `verification_state` 随工具上下文和 archive 供主模型使用。
6. `tool_call_archive_record.py` 对其他结构化副作用证据使用显式字段白名单；当前仅接受
   `message_tool_delivery.v1` 的成功状态、当前 owner 标记、receipt、用户投影和附件引用。
   `_finalization_service.py` 只能从本轮成功 `send_message` archive 提取，不能从模型正文、工具名次数或
   provider 日志猜测；该证据只供 conversation source-delivery 收口，不写入 verification SQLite。

## Owner 边界

数据库固定写入当前 `runtime_owner_root/data/verification/`。owner、thread、root task 和 project root
共同组成状态键；模型参数不能指定数据库位置，也不能通过正文改变身份。

## 修改注意

- 新验证工具必须接同一个公共工具出口，不能另建 IM hook。
- scope、exit 和 stale 只能由结构化事件决定。
- targeted 永远不能在投影层变成 full。
- 新增 archive envelope 字段必须逐字段压缩并说明消费者；不得把任意工具私有结果整包带入最终回复。
