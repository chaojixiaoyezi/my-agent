# Workstream Handoff

## 基本信息

- workstream：TODO10 / 能力消费者 localhost HTTP 组合验收
- branch：`codex/decision-model-integration`
- worktree：现有 `decision-model-plan/my-agent-dsh` 工作台
- owner：父代理下的 decision_settings_review 子代理
- date：2026-09-22

## 本线目标与实际完成

新增 `agent_py_agent/tests/test_decision_capability_http.py`，直接调用生产 `recommend_capabilities`，不 mock decide/backend/worker/传输。复用父侧 `surface` 与实际 `model_input`，通过临时原模型目录把原生决策连接指向随机 localhost HTTP 服务。

服务器按收到的真实 `state.candidates/questions` 动态生成全部问题的 choice/probabilities；没有拿固定测试题替代当前消费协议。所有日志关闭，不打印认证头；阻塞有界，finally 释放并回收服务线程。

## 改动文件

- 新增 `agent_py_agent/tests/test_decision_capability_http.py`
- 本独立交接文件

没有修改生产代码、父侧测试、日常配置或共享文档。

## 测试命令和结果

```bash
python3 -m pytest agent_py_agent/tests/test_decision_capability_http.py -q --tb=short
python3 -m ruff check agent_py_agent/tests/test_decision_capability_http.py
git diff --check -- agent_py_agent/tests/test_decision_capability_http.py
```

6 项通过，Ruff 和定向 diff 检查通过：

1. 成功：原设置→真实 HTTP `/v1/systemone`→原 worker/账本→消费者→RuntimeToolLoopSeed→实际 PromptBuilder/native schema；prompt/schema 字节均减少，选中 Skill 出现，无关卡/schema 省略，原快照 runtimes/权限集合不变，被省略工具仍由原 ToolExecutor/tool_search 找回，账本保留 decision purpose 和一次真实 HTTP attempt。
2. 超时：300ms 预算下有界返回，实际 prompt/schema 与原输入逐字一致；原账本 timed_out，服务器迟到写回后仍是 timed_out，返回投影不能变成 apply。
3. 在途关闭：原字段 patch 后，在服务器尚未释放响应时返回；旧选择不采用，实际输入保持原样。
4. 在途 `context_policy` 变更：同样在服务器响应前取消并保留原输入。
5. HTTP 401：原输入不变；再次调用不会重新请求 provider。
6. HTTP 500：原输入不变；再次调用不会重新请求 provider。

## 影响范围、协调与剩余风险

仅新增验收，无持久结构变化。测试是自有 localhost 协议响应和真实本地生成输入投影，不调用主聊天模型或真实决策供应商；不能据此声称真实建议质量、收费 token 降低或 provider 缓存收益。

父侧仍负责完整 runtime 接线、候选更新/插件撤销组合、共享文档与最终严格 gate。本片未提交、推送或部署。

## 后续建议

建议下一步：将这 6 项加入 TODO10 focused 组合，和父侧运行接线及插件失效测试一起验收；文档收口可以并行。真实供应商测试另行明确调用边界，不能用本地协议成功代替效果证据。
