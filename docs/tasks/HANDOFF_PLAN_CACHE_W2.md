# Workstream Handoff：plan-cache-w2

## 基本信息

- workstream / branch：`plan-cache-w2`
- worktree：`/private/tmp/wt-plan-cache-w2`
- owner：W2 单写者
- date：2026-11-07
- 起始提交：`bfafa4ca7ec084570136abaadd0f62f9af352d34`

## 本线目标

让不带工具且显式 `tool_choice=none` 的压缩请求遵循现有 `memory_compact_reasoning_level`，同时保留分叉压缩的主对话缓存分区和线程档位。

## 实际完成

- 判定只读取压缩 purpose、工具集合和 ToolChoice.mode；分段、回合中无前缀和闲置摘要复用同一请求级降档入口。
- 复用唯一 reasoning_control 档位表，仅在会话档高于可表达目标时降档；关思考、空/auto/off 配置、不可表达目标均保持原规则。
- 同工具与 auto 的分叉请求保持会话档；Responses 尾部 configuration_update 路径保留。没有修改线程档位、压缩阈值、输出预留或工具读取上限。
- YAML 仅更新原配置键上方注释，不新增配置键或修改默认值。

## 改动文件

- `agent_py_agent/agent/conversation/auxiliary_model_call.py`
- `agent_py_agent/config/agent_config.yaml`
- `agent_py_agent/tests/test_compact_reasoning_options.py`
- `agent_py_agent/tests/test_compact_idle_low_effort.py`
- 本交接。共享台账与文件树留给集成者统一维护。

## 测试命令和结果

所有 pytest 均使用 `HOME=/private/tmp/my-agent-home-cache-w2 MY_AGENT_HOME=/private/tmp/my-agent-home-cache-w2`，同时设置当前源码 PYTHONPATH 与 `PYTHONDONTWRITEBYTECODE=1`。

- 修改生产代码前，用本机 Python 3.14 运行更新后的两文件回归：3 failed、13 passed，准确复现分段 max、档位矩阵、闲置摘要 max 三项问题。
- 修复后，指定 ci-venv-312（Python 3.12.13）两文件回归通过。
- 完整门禁按任务书动态扫描 23 个辅助调用消费方，并加入指定回合中/闲置/前缀回归和十个 GUARDS；同时执行全仓 Ruff、导入边界、diff 检查与 clean-package。
- ci-venv-312 缺 Pillow；按任务书排除 `agent_py_agent/tests/test_iface_w4_compact_summarizer.py`，没有修改共享虚拟环境或测试豁免。
- 完整门禁最终结果在提交前填入；原始输出保留于忽略目录 `tmp/plan-cache-w2/gate.log`。

## 提交后统一验收

指定的审核套件目录尚未拆出。已从任务结果文档附录原样提取 `run_review.py` 到 `tmp/plan-cache-w2/opus-cache-kit/`，未修改脚本。通过脚本原有参数限定仓库、导出与 HOME：

```bash
HOME=/private/tmp/my-agent-home-cache-w2 MY_AGENT_HOME=/private/tmp/my-agent-home-cache-w2 \
PYTHONDONTWRITEBYTECODE=1 \
/Users/xiaoyezi/.my-agent/releases/claude-tools/ci-venv-312/bin/python \
  tmp/plan-cache-w2/opus-cache-kit/run_review.py post --ref plan-cache-w2 --only W2 \
  --repo /private/tmp/wt-plan-cache-w2 \
  --work /private/tmp/wt-plan-cache-w2/tmp/plan-cache-w2/review \
  --home /private/tmp/my-agent-home-cache-w2
```

本命令在提交后运行，结果另随最终提交 SHA 汇报；输出在 `tmp/plan-cache-w2/review.log`。脚本导出已提交源码，不读取个人线程数据。

## 影响范围与重点复查

- 只在本 worktree 修改白名单文件，不推送、不合并、不部署；任务书及审核来源仅只读访问，没有读取 owners。
- 回合中无前缀摘要的降档只影响当次摘要；主请求与恢复后的会话档位继续沿原权威。
- 若要关闭请求级降档，将现有 `memory_compact_reasoning_level` 设为 `auto`。
- 这里的协议载荷与 Gateway 测试使用本地模拟传输，不代表真实线上成本或缓存收益已经验收。

## 建议下一步

先由集成者合并 W1/W2 候选并运行不带 `--only` 的统一验收，确认工具表稳定性与压缩档位同时成立，再做真实会话观察。可由其他 agent 并行只读复核；共享台账、主分支合入和部署保持单写者，不改用户现有线程档位。
