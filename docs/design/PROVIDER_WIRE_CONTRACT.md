# 出站协议合同：坏历史不再让请求永远 400

状态：已实现（2026-09-30，分支 `claude/3a-wire-contract`）。代码在 `agent_py_agent/agent/backends/wire_contract.py`，
用例在 `agent_py_agent/tests/test_wire_contract.py`。

## 要解决的问题

会话历史会原样带进之后的每一次模型请求。只要历史里有一条服务端不接受的消息，这个线程之后的每次请求都会被 400 拒绝，
前台消息、后台唤醒、压缩全都失败，而且重试不会自己好。

09-30 生产事故就是这一类：模型只回了思考，转成 Chat Completions 后是一条既没有正文也没有工具调用的 assistant。
DeepSeek 官网对它返回 400，之后 my-agent-4 的每次请求都失败，唤醒每 30 秒重试一次。

当天的热修（`622292662`）只堵了这一种形状。本设计把同一类问题在出口统一处理，三种协议都覆盖。

## 参考做法

参考了 Codex、Hermes、cc-switch 和 OpenClaw，它们的共识是：

- 每种协议在出口各做一次投影。投影在副本上做，存下来的历史不动。
- 只有思考或完全为空的 assistant 不发。
- 工具调用和结果要配对：缺结果的补一条结构化的"结果未知"，孤儿结果删掉。
- 确定性的 400 不重试、不切换。

也有不照搬的：

- 不按错误正文或正则分类。
- 不按模型名或地址子串猜供应商。
- 不往模型上下文里塞 "(empty)" 这类占位文字。唯一例外是结构化的缺失结果回执，它和原有的孤儿清扫共用同一常量。

## 位置与数据流

三个后端的组包入口都按同一顺序处理，发送和只读预览（`project_generate_payload`）走同一条路：

1. `repair_native_messages(request.messages)`：在规范原生历史（Anthropic 形态的 IR）的副本上修整。
2. 各协议原有的转换：
   - Chat：`_openai_messages_from_native`
   - Messages：`anthropic_messages_with_optional_cache` 和媒体展开
   - Responses：`input_items`
3. `validate_*`：在最终请求体上按协议复核。不合规就抛 `ProviderRequestShapeInvalidError`，请求不发出。

text 协议（`messages is None`）原样通过。修整是纯函数：

- 输出新列表，没改动的消息和块沿用原对象。
- 请求字节和前缀缓存保持稳定：有效历史修整后与原来逐对象相同。
- 被删的内容只是这次不发，规范原生历史和落盘记录都不改写。

## 修整规则（三种协议共用）

1. **空块不发**：纯空白的正文块，以及既没有思考文字也没有签名的思考块。Anthropic 规范拒收空白文本块；MiniMax-M3 对空白正文会回空答复。带签名的思考块保留，签名是服务端校验思考连续性的凭据。
2. **删空的消息不发**：删完块后没有内容的消息整条不发，字符串内容为空白的消息也不发。
3. **工具结果紧跟调用**：每条带 `tool_use` 的 assistant 之后紧跟的连续 user 段里：
   - 本轮结果移到第一条 user 消息最前面，保持原出现顺序。
   - 缺的结果按调用顺序补"结果未知"回执：`ORPHAN_TOOL_RESULT_STUB`，`is_error=true`。
   - 不属于本轮的结果和重复结果删除。
   - 段内插话、媒体和运行事实保持原顺序，接在结果后面。
   - 末尾是调用、没有任何 user 消息时，补一条只含回执的 user 消息。
4. **孤儿结果删除**：不跟在调用后面的 user 消息里的结果无处配对，删掉；删空则整条不发。

同一个调用 id 出现在两轮里时，按相邻关系各自配对。有些兼容服务每轮都从 `call_0` 编号，所以不按全局 id 集合判断。

## 协议差异

只有思考的 assistant 各协议处理不同：

| 协议 | 处理 | 原因 |
| --- | --- | --- |
| Chat Completions | 转换时不发（热修保留） | assistant 必须有正文或 `tool_calls`，DeepSeek 实拒 |
| Responses | reasoning 项后面没有它产出的消息或函数调用时不发（`message_items` 去掉助手轮末尾的 reasoning 项） | 规范要求 reasoning 项后面紧跟它产出的项 |
| Messages | 照原设计回放带签名的思考 | MiniMax-M2.7 和 M3 实测中间位置与末尾位置都接受；这样续跑时模型不用重做长推理 |

## 校验规则

只检查两类规则都满足的：服务端确定会拒收，且修整保证不会产生。未知规则不查，免得把服务端本可接受的请求拦在本地。

- **Chat**：
  - assistant 必须有正文或 `tool_calls`。
  - 带 `tool_calls` 的 assistant 之后必须紧跟覆盖全部 id 的 tool 消息。
  - tool 消息只能回应紧挨着的那批调用。
- **Messages**：
  - role 只能是 user 或 assistant。
  - 列表内容非空，文本块含非空白字符。
  - 带 `tool_use` 的 assistant 下一条必须是 user，且以覆盖全部 id 的 `tool_result` 开头。
  - `tool_result` 只能回应紧挨着的上一条 assistant。
  - 字符串内容是否为空不查：旧组包在无历史时会发空 user，服务端现状接受。
- **Responses**：
  - reasoning 项（可以连续多项）之后第一个非 reasoning 项必须是 `function_call` 或 assistant 消息。
  - `function_call_output` 必须回应此前出现的 `function_call`。
  - 每个 `function_call` 都要有输出。

违规信息只含 `details = {protocol, rule, index}`，不含消息正文。

## 本地错误

`ProviderRequestShapeInvalidError` 是 `ProviderRequestRejectedError` 的子类，`status_code=0`，错误码为 `PROVIDER_REQUEST_SHAPE_INVALID`：

- 沿用请求拒绝的全部处理：不盲重试、不切换、不算环境故障。
- 唤醒毒丸按错误码计数，同因反复失败会结案。
- 在 `ERROR_CONTRACTS` 登记：`REPORT_BLOCKER`，不可重试。
- Gateway 与 CLI 文案说明请求没有发出、属于组装缺陷，不让用户去改密钥或重做任务。

修整正确时这个错误不应出现。它出现就说明修整有缺陷，或者后续组包步骤引入了新形状。

## 真实模型校准（MiniMax 官网，2026-09-30）

证据目录：`~/.my-agent/decision-evidence/wire-contract-2026-09-30/`，里面有探针脚本和只含状态码的输出。

- **两个模型都拒**：孤儿工具结果，两种协议都 400（`2013 tool id not found`）。
- **只有 M3 拒**：
  - 调用与结果之间夹 user 消息，两种协议都 400。
  - Chat 下正文排在工具结果前面（转换后 user 消息夹在调用与 tool 之间），400。
- **两个模型都接受**：
  - 空白或空正文块、只有思考的 assistant（有无签名都接受）。
  - 连续 user、首条为 assistant、调用缺结果、结果前有正文（Messages）。
- **产品请求体前后对比**：用产品组包对 6 段坏历史生成请求，发给 M2.7 和 M3，两种协议共 24 个请求。修整前 5 个 400，修整后 24 个全部 200。

## 测试

`test_wire_contract.py` 覆盖三类：

- 逐形状修整和校验（每条规则的违规与放行）。
- 三个出口的集成：
  - 一段混合了已知坏形状的历史，经真实组包后通过校验，调用方历史不变。
  - 修整被改坏时，校验在本地拦下，一次请求都不发。
- hypothesis 性质测试（`derandomize`，400 例），随机历史 × 四种 prompt 形态，要求：
  - 三种协议的请求都合规；
  - 修整幂等，不改输入；
  - 不丢、不重排可见正文和工具调用。

10 个变异（去掉配对、去掉空块、允许重复结果、保留删空消息、不补回执、不删孤儿、不去末尾 reasoning、放松两条校验、签名思考当空块）全部被杀。

## 不做与已知边界

- 连续同 role 消息不合并。Anthropic 官方会在服务端合并；Chat 的纯文本合并会把两段正文无分隔粘连；MiniMax 实测接受。
- 不删无签名的思考块。Anthropic 官方规范要求签名，但部分兼容服务返回的思考本身不带签名，工具循环里还要求回传；删掉会让它们 400。所以跨供应商切换到官方 Anthropic 时仍有风险，未覆盖。
- 思考开启时以 assistant 结尾的预填充请求，官方 Anthropic 不支持。typed 布局的动态尾部通常以 user 结尾，未单独处理。
- 后台车道不改：确定性 400 仍按普通 30 秒冷却。反复失败由唤醒毒丸（同因 5 次结案，step16m）收口；出口修整上线后，已知形状不会再产生这类 400。
- 未做"强制纯文本压缩再重试一次"的自愈：压缩请求也走同一出口，已知形状同样被修整；未知规则的 400 以错误码明确停止，由用户压缩或换模型。

## 改动须同步

改规则时同步修改：

- `test_wire_contract.py`：形状用例、性质测试和变异清单。
- 本文。
- 三个后端出口的注释。

新增规则前先用真实服务或公开规范确认"确定拒收"，不能按单个样例加专项分支。
