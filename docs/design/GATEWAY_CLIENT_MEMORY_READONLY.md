# C 客户端最近记忆只读叶子

状态：独立组件候选，尚未作为真实 W、原生资源、Count2 或完整服务验收。

`/client/memory` 的原 G1 宿主凭据先于 body 读取，身份仍由同一 middleware、
`GatewayControlScope` 和原 owner resolver 决定。请求正文的 user/channel 不替代认证身份。
C control/runtime 任一存在均不得回落 foreign Agent；两者同时缺省的 Count0 保持原操作入口。

C 仅开放 `operation=recent`。使用原 `home_paths_with_owner` 与
`resolve_runtime_paths_for_agent(...).paths['memory_path']` 取得既有正式 JSONL，
读前后核同一实际 runtime/control/paths/lease。关闭、复制、部分缺失或读取期间替换来源
不能返回本次正文。路径与配置是定位事实，不是权限或写入授权。

同名私聊沿各自 user owner 隔离；群记忆仍是原 group owner 共享库，群与同名私聊不混用，
不额外创建 actor 子库。缺失 owner/文件沿原读取合同返回空，不补目录、锁、索引或 active owner。

JSONL 只有一个 scanner 和一个 materializer：

```text
原 Agent all → self._read_memory_file → self._read_memory_events
                                          ↓
                              _read_memory_events_from_path
                                          ↓
                              _materialize_memory_events
                                          ↑
C recent → read_memory_records_from_path ──┘
```

原 `all`、`_read_memory_file` 的虚方法调用不变，所以子类的 file/events 覆写仍生效。
显式 path 叶子不构造 `JsonlMemory`（其构造器会 mkdir），不复制扫描器、不读候选、
不触发 recall/向量/访问 touch/remember 或正式晋升。版本、legacy ID、tombstone、touch、
过期、坏行跳过与读取 report 容错全部复用原逻辑。原 scanner 对读取错误 report 不向 all 抛出的
语义也保持；本片没有声称增加严格坏库诊断。物化真正抛出的错误继续只返回安全类型，不返半份正文。

C 的 search/remember 返回原结构化 `COORDINATOR_BUSINESS_NOT_READY`，不会静默创建
foreign Agent 或借 C 角色扩大写权。未知 operation 保留 `UNSUPPORTED_OPERATION`。
这两项仍需后续真实 domain/W 接线；Count0 原 recent/search/remember 及用户确认规则不变。

验证采用临时真实 C guard/controller/Root、原凭据 middleware、原 Handler 内存输入输出；
不启动监听器或服务、不发网络、不建 W、不调用模型。实际 healthy 基础 Agent 仅用原
`start_business` 构造一次离线 Echo，读取期间禁止新 Agent、JsonlMemory、foreign pool 与登记。

建议下一步：先独立核精确前像及直接/相邻合同，再由 root 与现有受控回环 history 组合验证；
private interaction 可独立继续，但 HTTP route 必须按明确 hunk 合并。真实 Count2/native 由 root 统一。
