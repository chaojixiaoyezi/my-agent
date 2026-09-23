# P5-D Stage A：原线程模型选择版本交接

状态：本地实现，尚未提交；本片不启用主会话自动采用。日期：2026-09-22。
工作线：decision-model-plan，基线 `ef497a904` 及原已协调的子代理 pending 改动。
负责人：decision_http_max。只修改原认领的模型选择/线程存储和定向测试；未修改设置 schema、配置、YAML 或 TUI。

## 本线目标与实际完成

在唯一 `ConversationThread` 上记录可比较的选择版本，使显式同值选择也能令旧自动建议失效，且与已有
pending 子代理建议终结同次提交。保留原模型引用、权限检查、作用域隔离和校准清理语义。
没有新增模型目录、选择日志库、确认步骤、永久 pin、网络请求、价格或费用字段。

| 原线程字段 | 当前合同 |
| --- | --- |
| `model_profile_id` | 原唯一有效模型引用，未复制到第二份状态。 |
| `model_selection_revision` | 非负整数；0 表示没有已记录的选择来源。每次成功的显式选择都从锁内最新版本加一，包括选择相同 ID。 |
| `model_selection_source` | 宿主有限状态：`unknown`、`default`、`inherited`、`explicit`、`automatic`。不是用户正文、模型名字或授权标记。 |
| `model_selection_last_explicit_revision` | 最近已发生显式选择的版本；未发生为0。后续 automatic 必须保留它，不因此永久固定模型。 |

三字段为 keyword-only dataclass 参数，保持旧 `ConversationThread` 位置参数的顺序。
`unknown` 仅允许 `0/unknown/0`；初始 default/inherited 仅允许版本1且最近显式版本0；explicit 的最近显式版本
必须等于当前版本；automatic 的最近显式版本须小于当前版本。已知选择必须有非空原 profile 引用。

显式选择失败（无权限/引用无效）不前进版本、不终结 pending。成功选择使用同一次原线程更新：

```text
读取/验证原模型目录
→ 原 thread 文件事务中重读最新记录
→ 版本 +1、来源 explicit、最近显式版本 = 本次版本
→ 原 pending 子代理建议变 retained/explicit_model_selection
→ 同一个 thread 写回
```

同一模型引用保留校准；真正换引用才清 `provider_context_observation` 和 `model_context_usage`。
summary、Compact generation/checkpoint/cursor、未知扩展、其他 metadata 和正在执行的作用域不变。
版本是接受的选择调用序列，原 API 没有选择请求幂等键；网络重试再次成功提交同值 select 仍是一个新版本，
不能把它当“去重后的用户事件次数”或模型请求次数。

## 公开读取、核对和后续采用边界

不新增 facade 或 adoption API，继续使用原服务：

- 读取：`agent.conversation_store.threads.load(thread_id)`，在同一对象上取得 profile 和三个事实字段。
- 显式选择：`thread_model_profile_id(agent, thread_id, select=profile_id)`；返回值仍是原 profile 字符串。
- 原子核对/提交：`threads.update_atomic(thread_id, updater)`；updater 获得锁内最新对象。

`update_atomic` 只要发现选择引用/事实改变，就要求新版本正好是旧版本 +1；explicit 写本次显式版本，
其他来源保留旧显式版本。已有非空选择不能改称 default/inherited 初始绑定。普通标题、Compact、消息投影或
metadata 更新须原样保留这组事实，不消耗选择版本。

后续 child/main 宿主仍须在原 updater 内先比较 expected selection revision/profile、准确 pending
operation/child/workpiece 身份，再同次保存实际 profile、pending 终态及其原首次提交状态。
该判断属于各自原采用入口，本片未替它们实现。事务内禁止网络；状态机、权限、容量、取消/配置和连接版本均需
调用方按原合同检查。选择版本不是连接版本：同 ID 的模型配置/凭据修改不会伪造一次用户 select。

底层 `ThreadStore.write` 仍是原新记录写入能力，不是模型选择更新 API。生产选择继续通过 `update_atomic`；
不得用构造旧 thread 后直接 write 来绕过单调性。`source=automatic` 的合法序列化仅为宿主后续接线准备，
没有增加用户或模型可直接调用的采用路径。失败自动保留原合法模型，用户无需逐个选择、补数据或确认。

## 迁移与旧数据边界

- 保持原 `conversation_thread.v10`，不需要改 `decision_settings_schema.py` 的 thread 版本白名单。
- 原 v10 及更早合法版本三字段**全部缺失**时，在 `from_dict` 中归一为 `0/unknown/0`。
  原非空 profile 仍照常使用；不能据此声称它原先是手动、默认或自动选择。
- 读取不写盘。下一次原线程合法更新才保存三字段；只改标题/Compact 时仍是未知来源、版本0，不补造事件。
- 若旧 profile 为空，原默认绑定入口现在执行真实初始化，记 `1/default/0`；这表示此刻的绑定，不是过去的手动操作。
- 三字段出现任意一个后必须三者齐全。布尔值、负数、字符串版本、未知来源、矛盾版本或已知选择无引用均拒绝。
  `load_report` 返回原错误分类，原更新拒绝且保留文件字节；不能将坏字段当旧数据清零。
- 新字段不改变未知 thread schema 的拒绝。已有未知扩展字段仍由原 `update_atomic` 合并保留。
- 本片只保证当前版本写者的原选择事务。旧程序不理解这些新字段，也不会替它们递增，不能据此声称支持跨软件版本
  的并发写入或任意回滚运行；升级/回滚仍须遵守原单一运行实例与迁移安排。

子代理 A 已协调并落地：它独占 `agent_thread_store.py`，在新 child 记 `1/inherited/0`；已有非空旧 child 不补来源。
旧空引用的真实补齐同次前进版本；若它携带不可能的显式历史，应拒绝矛盾，不能抹掉历史后补齐。
本线没有编辑该生产文件。原 `SUBAGENT_MODEL_ADVICE_KEY` 仍唯一归属 `thread_model_selection.py`；
A 新增的独立 `SUBAGENT_FIRST_REQUEST_KEY` 唯一归属 `agent_thread_store.py`，只在有 typed advice 的新线程
初始化 `unsubmitted` 与准确 op/run/thread。两个 key 语义不同，没有反向导入或第二份常量。
显式选择只终结原 pending 并前进版本；后续 first-request gate 必须同时核对 pending/revision，不能仅凭 marker 采用。

## 改动文件与验证

- `agent_py_agent/agent/conversation/models.py`：三字段、全缺迁移和严格事实校验。
- `agent_py_agent/agent/conversation/store_threads.py`：新默认初始化，以及原线程事务的选择转换守卫。
- `agent_py_agent/agent/settings/thread_model_selection.py`：显式同值递增、pending 同次终结、原校准语义。
- 新 `agent_py_agent/tests/test_thread_model_selection_revision.py`：迁移/坏数据、并发同值选择、版本守卫和 pending 原子性。
- `test_conversation_store.py::test_existing_agent_thread_ensure_keeps_newer_model_and_compact_state`：只将原并发模拟
  中的显式模型修改补齐版本事实，保持既有 race 验证；已获该测试原 owner 同意。
- 本文及 Gateway 原模块 `02-progress.md` / `04-structure.md`。主线统一登记新增测试/交接到 `CODEBASE_TREE.md`。

定向命令：

```bash
python3 -m pytest agent_py_agent/tests/test_thread_model_selection_revision.py agent_py_agent/tests/test_thread_model_selection.py agent_py_agent/tests/test_gateway_model_profiles.py agent_py_agent/tests/test_decision_subagent.py agent_py_agent/tests/test_conversation_store.py -q --tb=short -o addopts=
ruff check agent_py_agent/agent/conversation/models.py agent_py_agent/agent/conversation/store_threads.py agent_py_agent/agent/settings/thread_model_selection.py agent_py_agent/tests/test_thread_model_selection_revision.py agent_py_agent/tests/test_conversation_store.py
python3 scripts/check_doc_sync.py
```

本片定向结果：170 项通过（26.55 秒）；Ruff、doc sync、已有文件 diff 空白检查通过。
strict AST 使用原 `check_code_size._check_ast`、原 baseline 与 `compute_strict_blockers` 检查本片三份生产文件，
新增 blocker 为0，不修改共享 size report。A 的新 child 初始化时点随后另作一次同组联合复核，结果见下。
未运行全仓 pytest、真实模型或远端部署。

联合稳定点：A 的 inherited 初始化与独立 first-request marker 落地后，同组 **170 项再次全部通过（28.40 秒）**。
新增测试和交接文档的 UTF-8、末尾换行及空白检查也通过。本线三个生产文件与选择 helper 已释放给主线/A 串行接续，
后续采用改动应作为独立片重新验收。

## 建议下一步

先由主线与子代理 A 联合核对新/旧 child 的初始化分支，再由 A 在原 thread CAS 接首次业务生成采用。
主会话 Gateway 的 lane/模型作用域重排与工作片恢复仍是下一片，不能把本片版本完成记为自动采用完成。
纯请求/协议容量与选择版本可并行；`thread_model_selection.py` 的下一次修改待本片稳定释放后串行认领。
守住准确身份、手动覆盖旧建议、无锁内网络、失败保留原合法模型和无逐片确认流程。
