# my-agent-4 隔离复验记录（2026-09-28）

来源：my-agent-4 开发交流板任务 3 收尾——dev 在 11:05 批准做隔离复验，要求"用自己的隔离 home、端口 8441，
只用脚本假模型和假决策后端，不复制任何模型配置或密钥"，证据放本目录。

## 环境与边界

- 工作树：`~/my-agent-worktrees/my-agent-self-4`，分支 `my-agent/self-dev-4`
- **没有起 Gateway、没有用 8441 端口、没有联网、没有复制任何模型配置或密钥**。本轮全部验证在进程内完成：
  决策输入走真实校验器与真实 wire 编码器，后台资源走真实 `ProcessSessionStore` 与真实进程。
- 本沙箱不能拿不到进程出生身份（`ps -p` 不可用），涉及该项的地方按测试同一约定打桩；除此之外没有替身。

## A. curator 决策输入不再超窗（对应任务 1）

复现材料：28 条具名来源（1 条消息 + 27 条 audit，每条 preview 900 字符），与真机越窗批次同量级。

| 观察点 | 结果 |
| --- | --- |
| 修复后 token 估算 | 29372（上限 29491）→ **不再超窗**，`typesafe_payload` 不再抛 `DecisionInputError` |
| 具名来源 / 题数 | 27 / 54（未因预算丢来源） |
| 窗口收紧到 4096 时 | 保留 1 个来源，**被裁 26 个仍完整留在 `state.batch` 快照里**（下一轮按游标重放） |
| 被裁来源是否泄漏到题面 | 否（断言通过） |
| 告警码 | `memory_curator_input_fitted:decision_window`（固定码，条数不进文本） |

**未验证的部分**：没有在真实 Gateway 里触发一次 curator run（本轮未起 Gateway）。因此"真机 `outcomes.jsonl`
里不再出现 curator invalid_input"这一条仍待部署环境复验。

## B. gateway stop 的后台资源停止入口（对应任务 3）

用真实父子孙进程组（`start_new_session=True`，孙进程 pid 从 child 的 stdout 读出）登记为受管后台进程。

| 观察点 | 结果 |
| --- | --- |
| 默认列出 | 1 个，errors=0 |
| 默认是否停 | **否**——child 与孙进程都仍在运行（只列不杀） |
| 停止请求 | `stop_requested=True` 已冻结到登记表 |
| 无 host 时的报告 | `stopped=False, reason=still_running_after_request`——**如实报未确认，不伪报已停** |

**未验证的部分（沙箱限制，非产品缺陷）**：本沙箱跑不通完整生产启动链路 `start_background_process`——
host 复核 launcher 出生身份时拿不到身份（`ps -p` 不可用），会立刻判 launcher 不可用并退出，表现为
"managed background startup timeout"。因此"原 host 收到 `stop_requested` 后按进程组回收孙进程"这一步
在**本沙箱无法端到端验证**；该语义由单元测试用真实进程单独覆盖
（`test_gateway_stop_background_resources.py::test_process_group_stop_also_reaps_grandchild`），
但**真机 host 路径的端到端确认仍待部署环境**。

## C. 定向测试

`directed-tests-2026-09-28.txt`：三个新测试文件 **14 项全部通过**。

## 结论口径

- 已在本机观察到的：A 段全部观察点、B 段的登记表投影与"默认只列不杀"、"未确认真实退出"。
- **未观察到的**：真实 Gateway 里的 curator 触发、真实 host 的进程组回收端到端。
  这两条只能由部署环境（或允许起隔离 gateway 的环境）补，不能由本轮结果外推。
