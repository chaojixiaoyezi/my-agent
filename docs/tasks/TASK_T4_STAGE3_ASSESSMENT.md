# T4: native tool_use "Stage3"(辅助历史 IR 化)评估

## 结论(TL;DR)
**Stage3 非必做项。** native 主链路已 100% IR 化;主 tool loop 之外的"辅助历史"已被妥善处理,
**没有"不改就会导致 native 真实正确性问题"的真缺口**。红线区(主 tool loop IR / 子代理执行链路 /
builder 双轨)不应轻碰——改它们会危及已验证健康的核心链路。增量优化 ROI 低甚至为负,不做。

此评估印证 memory `logops-indef-findings` 第四轮记录:"Stage3 历史 IR 化是增量非前提"。

## 范围:三类"辅助历史"
主代理工具往返已走 IR(Step1-2,见 `agent/agent_core/tool_ir_history.py`)。评估的是主 tool loop 之外的:
1. **子代理(subagent)历史** — 子代理执行任务时的工具调用历史与结果回灌
2. **delivery 产物记录** — 交付闭环的运行时指引/进度
3. **archive 归档** — 工具调用落盘归档(`tool_call_archive_record.py`)

## native 健康的 4 根支柱(本次已逐一核实)
1. **builder 旁路**(`prompting_parts/builder.py:257-258`):native 下 `_transcript_tool_context` 旁路
   tool_context 文本,工具往返由原生 messages 携带,**不文本+IR 双份重复**。
2. **指引回灌**(`agent_core/tool_ir_guidance.py:90/85/37/123`):delivery/closeout 等运行时指引经
   `append_runtime_guidance_user_message` 作为额外 **user 文本消息**回灌,跨轮去重(`seen.add`),
   IR-backed 前缀排除(`_IR_BACKED_PREFIXES` / `_is_ir_backed`)。native 模型照常看到打回/软提醒。
3. **子代理收口**(`agent_core/tool_loop/round_subagent_output.py:73-77`):native 下子代理
   `[SUBAGENT_RESULT]` 追加 `AssistantTurn` 到主 IR,结束语被结构化保留、不漏。
4. **孤儿净化**(`backends/message_adapter.py:153 strip_orphaned_tool_blocks`):出站最后防线,
   保证 tool_use/tool_result 一一配对,混合形态不会产生孤儿配对 / Anthropic API 报错。

## 诚实分类
### 必须 IR 化:无
主链路工具往返 + 子代理收口 + 系统运行时指引均已妥善;native 与 Anthropic API 适配核心已闭环。

### 增量优化(不做,ROI 低/负)
- **子代理内部历史逐条回灌主 IR**:子代理是独立执行体,内部历史不回喂父级(只回最终
  `[SUBAGENT_RESULT]`,已 IR 化为一个 turn)。逐条回灌无价值。
  *注:子代理执行复用主代理对象(见 T2),`native_tool_use_active` 对子代理生效,子代理自己那轮
  工具循环也走 native IR——并非"文本协议"。*
- **delivery 进度事件 IR 化**:`progress.py` 只落盘 `.json` 留痕、不回喂模型,无需 IR。
- **archive 内置 IR**:外置化是**特性不是漏洞**(避免巨大归档撑爆 messages);内置会膨胀 IR 尺寸、
  降低 compact 效率,**负 ROI**。

### 红线区(不碰)
- **主 tool loop IR 历史**(`tool_ir_history`):Step2-5 已验证健康(normal/compact/resume/digest 全覆盖),
  改动牵动 message_adapter / compact 整对增删 / 孤儿净化。
- **子代理执行协调**(`subagents/manager.py` 链路):子代理自成体系,强行复制整套 IR 基础设施隔离风险高。
- **builder/prompting 文本链路**:text 协议(回退安全网)依赖现有 tool_context 渲染,改了破坏双轨灰度契约。

## 真机坐实(留 T5)
代码层已确认子代理复用主代理对象走 native(T2)。T5 安排明确派子代理的任务,真机确认子代理工具调用
走原生 tool_use(而非 `[TOOL_CALL]` 文本),坐实"子代理 native 继承"这一推断。
