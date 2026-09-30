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
   - 按出现次数配对：同一个 id 有几次调用就收几个结果。空 id 和同一条消息里的重复 id 也各算一次调用，因为有的服务商不给 id（解析层存成空串），有的每轮都从 `call_0` 编号。协议转换和三个校验都按这个口径计数。
   - 本轮结果移到第一条 user 消息最前面，保持原出现顺序。
   - 缺的结果按调用顺序补"结果未知"回执：`ORPHAN_TOOL_RESULT_STUB`，`is_error=true`。
   - 不属于本轮的结果和超出调用次数的结果删除。
   - 段内插话、媒体和运行事实保持原顺序，接在结果后面。
   - 末尾是调用、没有任何 user 消息时，补一条只含回执的 user 消息。
4. **孤儿结果删除**：不跟在调用后面的 user 消息里的结果无处配对，删掉；删空则整条不发。

同一个调用 id 出现在两轮里时，按相邻关系各自配对。有些兼容服务每轮都从 `call_0` 编号，所以不按全局 id 集合判断。

prompt 投影同样遵守"空白不成块"：Messages 的缓存层（`anthropic_prompt_cache`）对只含空白的 prompt、稳定用户前缀和动态尾部
不产出文本块。否则出口校验会把本可发送的请求拦在本地（MiniMax 接受空白块，官方 Anthropic 拒收）。

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
- Gateway、CLI 和运行错误报告（`runtime_error_report` 的 `provider_request_shape_invalid` 类别）都说明请求没有发出、属于组装缺陷，不套"模型服务拒绝了本次请求"的文案，也不让用户去改密钥或重做任务。
- 视觉能力探针把它按 `unavailable` 处理、不缓存，不能记成"服务商拒收图片"。

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
- **产品请求体前后对比**：用产品组包对 6 段坏历史生成请求，发给 M2.7 和 M3，两种协议共 24 个请求。修整前 7 个 400（M2.7 2 个，M3 5 个），修整后 24 个全部 200。

## 真实模型校准（DeepSeek 官方与 opencode Go，2026-09-30）

用户随后允许用官方 DeepSeek 和 opencode Go 测试（高频测试用 `deepseek-v4-flash`）。探针脚本与只含状态码的输出在同一证据目录
（`probe_deepseek.py`、`probe_product_targets.py`）。

- **DeepSeek 官方 Anthropic 接口**，严格程度与官方 Anthropic 规范一致。下面这些形状都 400：
  - 孤儿结果；
  - 调用缺结果；
  - 调用与结果之间夹 user 消息；
  - 正文排在结果前面（报"没有紧跟的 tool_result"）；
  - 同一轮的结果分在两条 user 消息里；
  - 内容为空数组。

  下面这些接受：空白或空正文块、只有思考的 assistant、连续 user、首条 assistant、末尾 assistant 预填充。
- **DeepSeek 官方 Chat 接口**：
  - 400：`content=None` 且没有 `tool_calls` 的 assistant（即 09-30 事故原文），孤儿 tool 消息，调用后缺 tool 回复，调用与 tool 之间夹 user。
  - 接受：`content` 为空串的 assistant、空 user、连续 user。本地校验把空串也算违规，比 DeepSeek 严，但 Chat 转换本来就不会产出空串，所以不会误拦。
- **产品请求体前后对比**：同一组 6 段坏历史用 `deepseek-v4-flash` 发出。
  - Anthropic 接口：修整前 5 段 400，修整后 6 段全部 200。
  - Chat 接口：修整前 4 段 400，修整后全部 200。
  - opencode Go（生产默认通道）很宽松，前后都是 200。
- **修整后模型的答复也更对**：结果分在两条消息时，opencode Go 在修整前又调了一次工具；修整后模型看到全部结果，直接作答。

## 测试

`test_wire_contract.py` 覆盖三类：

- 逐形状修整和校验（每条规则的违规与放行）。
- 三个出口的集成：
  - 一段混合了已知坏形状的历史，经真实组包后通过校验，调用方历史不变。
  - 修整被改坏时，校验在本地拦下，一次请求都不发。
- 只含空白的 prompt 各部分不产出空白文本块（缓存开关两种）。
- hypothesis 性质测试（`derandomize`，400 例），随机历史 × 六种 prompt 形态（含只有空白的 prompt 和动态尾部），要求：
  - 三种协议的请求都合规；
  - 修整幂等，不改输入；
  - 不丢、不重排可见正文和工具调用。

13 个变异全部被杀：
- 修整与校验 10 个：去掉配对、去掉空块、允许重复结果、保留删空消息、不补回执、不删孤儿、不去末尾 reasoning、放松两条校验、签名思考当空块。
- prompt 投影空白判断 3 个。

## 不做与已知边界

- 连续同 role 消息不合并。Anthropic 官方会在服务端合并；Chat 的纯文本合并会把两段正文无分隔粘连；MiniMax 实测接受。
- 不删无签名的思考块。Anthropic 官方规范要求签名，但部分兼容服务返回的思考本身不带签名，工具循环里还要求回传；删掉会让它们 400。所以跨供应商切换到官方 Anthropic 时仍有风险，未覆盖。
- 思考开启时以 assistant 结尾的预填充请求，官方 Anthropic 不支持。typed 布局的动态尾部通常以 user 结尾，未单独处理。
- 后台车道不改：确定性 400 仍按普通 30 秒冷却。反复失败由唤醒毒丸（同因 5 次结案，step16m）收口；出口修整上线后，已知形状不会再产生这类 400。
- 未做"强制纯文本压缩再重试一次"的自愈：压缩请求也走同一出口，已知形状同样被修整；未知规则的 400 以错误码明确停止，由用户压缩或换模型。
- 调用和结果之间夹了一条 assistant 消息时，真实结果会被当成孤儿删掉，补上"结果未知"回执（9a 复审 P3）。不挪动结果，是因为后面同 id 的结果可能属于后一轮的调用。改动前这种历史在严格服务商那里本来就会 400，所以不算回归；产品里也没有写出这种顺序的路径。2026-09-30 用生产代码按真实出站路径扫描生产全部 849 个会话（23,678 次工具调用）：每次调用都恰好有一个真实结果，老清扫和出口修整丢掉的真实结果都是 0，也没有补过一次"结果未知"（证据 `wire-contract-2026-09-30/history-scan-20260930.txt`，脚本 `claude-tools/history-scan/history_scan.py`）。
- 同一条消息里的多个文本块，Chat 转换时用空串直接拼接。修整删掉只含空白的分隔块之后，「第一段。」+「\n\n」+「第二段。」会粘成一段（9a 复审 P5）。产品里没有写出这种分隔块的路径，属于理论问题。

## 复审跟进（9a，2026-09-30）

- **必须改，已修**：修整原来把空 id 当成"不是调用"，同一条消息里的重复 id 只配一个结果；而转换和校验按出现次数算。结果是：
  - 空 id 的真实结果被删；
  - Chat 校验在本地永久拦截这个线程。

  现在修整按出现次数配对，三个校验也按次数核对，与修整同一口径。
- **同批改掉**：
  - `runtime_error_report` 单列形状错误；
  - 视觉探针不再把形状错误缓存成"不支持图片"；
  - Chat 动态尾部并入最后一条带图的 user 消息时，作为文本部件追加。这是原有 bug：以前会把内容数组转成字符串，图丢了，base64 被当正文发出。
- 性质测试的随机历史加入了空 id 和同条消息内的重复 id。8 个跟进变异全部被杀。

## 改动须同步

改规则时同步修改：

- `test_wire_contract.py`：形状用例、性质测试和变异清单。
- 本文。
- 三个后端出口的注释。

新增规则前先用真实服务或公开规范确认"确定拒收"，不能按单个样例加专项分支。
