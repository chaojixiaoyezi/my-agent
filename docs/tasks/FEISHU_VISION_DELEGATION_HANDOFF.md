# Workstream Handoff

## 基本信息

- workstream: 飞书原生图片后半链：原输入、后台与主子孙授权
- branch: `codex/feishu-native-images`
- worktree: 本任务唯一 canonical 集成 worktree
- owner: credential_live_preflight
- date: 2026-10-15
- baseline: `2812638850ff02c091c5bc980019f5c9474a9b30`

## 本线目标

把认证飞书入站的原图片引用接到已有原生媒体、后台任务和子代理链。图片不能只变成路径文字，不能因自动派工悄悄丢失，也不能让同根孩子自动获得全部图片。

## 实际完成

- 扩展唯一 `HostMessageContext`：原 HostActor、消息引用与不可变媒体标量引用同存。显式兼容已发布的无媒体记录字段集，不从旧 attrs 或消息文字猜图。
- Gateway 的专有 `RunParams.host_message_context` 只由已认证入口、顶层 `input_media` 和原校验器构造；`host_message_references` 保持原只读投影。
- 直接晋升的 TaskLink 保存 inline 原快照；自动委派仍只在 SessionTask 保存快照，TaskLink 保持精确 SessionTask 引用。原请求文件回收不影响恢复，没有第二存储或媒体索引。
- 首片尚无 TaskLink 时，只允许原 queued/accepted SessionTask 的准确 RuntimeDB 当前主根读取：owner/thread、当前 attempt、main agent_run、原 execution_origin/actor/请求都须一致。已有 TaskLink 及子孙继续按原完整门核对。
- 自动委派通过原 `create_subagents.input_media_refs` 显式传原图片哈希；共享解析入口重读原快照/子档，避免 trusted 参数或模型 attrs 充当媒体权。手工孩子未点名不继承，孙代理只可转交其获授子集。
- `subagent_input_media_enabled` 仍默认关闭；带图自动委派关闭时在创建 SessionTask/孩子前按原 `SUBAGENT_INPUT_MEDIA_DISABLED` 协议拒绝，不生成纯文字孩子或新权限等待。
- 后台恢复在原 `RuntimeFactsTurn` 携媒体，内部宿主来源保留，后端仍投影 user role；不会把派工 goal 改成用户原话。模态/容量分类同样看到宿主图片，工具摘要退休不丢带图事件。
- 原生协议与视觉能力分别检查：明确非空档案不含所需模态时沿原 `InputMediaError` 确定失败；未知声明继续原路径，不按模型名猜，不自动换模或模型重试。

## 改动文件

- `agent_py_agent/agent/conversation/host_message_context.py`
- `agent_py_agent/agent/conversation/im_delegation.py`
- `agent_py_agent/agent/gateway_parts/im_task_routing.py`
- `agent_py_agent/agent/gateway_parts/request_execution.py`（仅原运行参数构造及对应注释）
- `agent_py_agent/agent/agent_core/runtime/loop_models.py`
- `agent_py_agent/agent/agent_core/runtime/loop_support.py`
- `agent_py_agent/agent/agent_core/orchestration/input_media_refs.py`
- `agent_py_agent/agent/agent_core/tool_request_projection.py`
- `agent_py_agent/agent/agent_core/tool_ir_history.py`
- `agent_py_agent/agent/backends/tool_ir.py`
- `agent_py_agent/agent/backends/message_adapter.py`
- `agent_py_agent/tests/test_im_input_media_delegation.py`（新增唯一专项测试）
- `agent_py_agent/tests/test_tool_request_projection.py`（仅补原物理模型调用夹具缺失的 agent/call_id，原全部断言保留）。
- 本交接。

## 测试命令和结果

```bash
DEV_PY -m pytest agent_py_agent/tests/test_im_input_media_delegation.py -q --tb=short
```

结果：专项 25 通过、零跳过。首轮红测 8 项，原快照、真实创建工具与媒体 IR 缺口已保留；最终矩阵包括：

- 群及 topic 原输入唯一持久化、旧形状兼容、请求 GC 后恢复。
- 无 TaskLink 首片宿主 IR、生命周期恢复、三种后端投影拿到精确原图字节。
- 自动孩子真实 `run_subagent` 到 fake HTTP，供应商参数只有 base64 原件而无 owner 本地路径；明确 text-only 零 HTTP，child/run/attempt 均失败且 attempt 已结束。
- 开关关闭零任务/零孩子，未知模态继续与非 native 拒绝独立。
- 主子孙显式 refs、未请求手工孩子不继承、原 attempt 过期/错误 owner/actor/新请求/伪造 attrs 阴性。
- SessionTask 已取消不得读首片输入；媒体字段嵌套/坏类型/跨 owner 路径拒绝。

既有 `test_tool_request_projection` 9项在 HEAD 原投影隔离重现同样缺 agent 的失败；仅补真实所属 host 与合成调用 id，未改生产逻辑、跳过或削弱断言。原红 XML 保留。

另执行与上述 11 生产文件直接相关的 12 文件 focused（准确命令、最终计数和 SHA 在仓外 `feishu-vision-delegation-frozen-handoff.json`），不将 fake 供应商结果当作真实视觉验收。Ruff 与 diff-check 通过；本线代码尺寸无阻断，全树其余线尺寸及统一严格门由主线处理。

## 影响范围

只补原输入快照、原生 IR 与原创建/恢复接缝，不改 adapter、图片解码器、模型卡、配置、网络授权、群权限、真实服务或凭据。沿既有 Codex/参考合同的显式输入项、调用身份和原请求同源投影模式，详细参考索引由[图片合同](../design/FEISHU_NATIVE_IMAGES.md)统一维护。

## 需要主线重点复查

- 首片准确 SessionTask 权威与当前 RuntimeDB 主根；不可扩大为任意无 TaskLink 任务读取。
- 自动委派失败必须沿原请求/attempt 收尾，不能将图丢弃后继续文本。
- 前半真实 MIME/解码与后半传输引用是不同证据；用户图片内容不授予任何工具、网络或管理权限。

## 需要其他线协调

- 主线负责 `input_media` 原校验/配置与全局/模块文档、CODEBASE_TREE、组合严格 gate。
- 新测试和本交接应加入树导航。adapter 与模型卡各守其独占文件；本线不提交、推送、构建或部署。

## 剩余风险

尚未对真实飞书图片和真实多模态模型运行验收；MiniMax/DeepSeek 或中转是否接受实际协议和能否看懂图片，须原模型请求与视觉回答分别验证。未知声明不是能力证明。没有新增 OCR、跨 owner 图片继承、自动换模或定时任务任意历史图片权限。

## 后续建议

建议下一步：先做非作者权限/恢复门审阅，再由主线一次性跑组合严格 gate；adapter 和模型卡可并行验已有组件，真实飞书/模型只由主线串行操作并分别记录携图与识图结果。
