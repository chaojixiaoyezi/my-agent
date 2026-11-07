# Workstream Handoff：普通回合业务工具接线

## 基本信息

- workstream / branch：`wt-local-fix-5`
- worktree：`/private/tmp/wt-local-fix-5`
- owner：本轮修复代理，单写者
- date：2026-11-07
- 起始提交：`1af515e59f50aa20afd856a519de2a8c83b96461`

## 本线目标

修复普通 Gateway/TUI 模型回合无法发现、调用业务空间和后台代理，以及业务工作流查询缺少宿主、工具卡直接显示 JSON 和内部调用编号的问题。

## 实际完成

- 复用现有 `BusinessServices`、`local_business_context` 和按动作注册的业务工具；没有新建业务存储、影子宿主或执行通道。工作流逐次绑定原服务和当前回合 actor。
- 本地模型回合从规范线程及 owner home 核验来源；外部通道保持原冻结身份校验。上下文随回合、来源或 owner 变化失效；本地列表逐项授权只复核内存中的当前宿主绑定，避免逐条重读来源文件。
- 为业务空间和后台代理补充中英文发现元数据；工具搜索、route、self_map 共用真实工具目录。self_map 的 `business` 分块仅介绍授权工具入口，不扫描业务正文或启动 Agent。
- 按用户明确选择，将 YAML/dataclass 的 `business_spaces_enabled` 默认值同步设为开启；显式关闭仍拒绝业务调用，不改已有个人配置文件。
- 工作流和归档读取通过 `display.kind=summary` 展示简短摘要，完整机器结果和原文归档保留；读取归档的进度描述不显示内部引用。

## 改动文件

- 组合与身份：`agent_core/self_map_bootstrap.py`、`gateway_parts/business_api.py`、`tooling/workflows_tool.py`。
- 发现与默认值：`tooling/self_map_tool.py`、`tooling/self_map_tools.py`、`settings/config.py`、`config/agent_config.yaml`。
- 展示：`tooling/result_display.py`、`tooling/artifact.py`、`agent_core/tool_loop/round_execution.py`、`cli/chat_parts/tui_block_renderer.py`。
- 回归：新增 `test_business_model_tools.py`、`test_business_tool_display.py`；同步现有配置默认值和 self_map 总览断言。
- 说明：`docs/design/SELF_MAP.md`、`DESIGN_LEDGER.md`、`CODEBASE_TREE.md` 与本交接。

## 测试命令和结果

所有 pytest 命令均设置：

```bash
HOME=/private/tmp/my-agent-home-fix5 MY_AGENT_HOME=/private/tmp/my-agent-home-fix5 \
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/wt-local-fix-5 \
python3 -m pytest \
  agent_py_agent/tests/test_business_model_tools.py \
  agent_py_agent/tests/test_business_tool_display.py \
  agent_py_agent/tests/test_self_map_runtime.py \
  agent_py_agent/tests/test_scripts_and_workflows_tools.py \
  -o addopts='' -q --tb=short
```

扩大回归覆盖：新业务工具与展示、业务合同及原治理/后台服务、原生 HTTP 业务往返、分页游标、工作流工具及引擎、self_map 系列、归档读取、TUI renderer/events、工具回合及原文归档，以及已有 BFS 状态机和确定性回放测试。原始输出保留在工作树忽略目录 `tmp/local-fix-5/pytest.log`。

其他检查：

```bash
ruff check agent_py_agent scripts
python3 scripts/check_import_boundaries.py --json
python3 scripts/check_clean_package.py .
git diff --check
```

结果：扩大回归 **1156 passed，190.62 秒**；最后补充具体动作词和未知副作用回归后，上述四文件 **253 passed，44.88 秒**（两次范围重叠，不相加）。两份原始输出分别在 `tmp/local-fix-5/pytest.log`、`tmp/local-fix-5/pytest-final.log`。Ruff、导入边界（空违规数组）、clean-package、`git diff --check` 均通过。没有新增测试豁免。

## 影响范围与重点复查

- 只在本独立工作树内修改并本地提交；不推送、合并、部署或修改个人运行目录。
- 业务工具仍通过原 Schema、权限、幂等、CAS、任务和生命周期服务执行；目录文字和展示字段不作为授权。
- 既有配置显式关闭时不会自动打开；集成复测须核实测试实例的显式开关值。
- 此次验证使用隔离临时目录和本地 Python 3.14，不代替真实模型、已运行 Gateway 的现场或线上 CI 验收。

## 建议下一步

先在集成候选中复测“询问业务空间/后台代理 → 发现对应工具 → 真实查询及一次受控操作 → 展开工具原文”，确认当前 Gateway 配置、会话归属和展示一致。可由其他 agent 并行只读复核；共用运行目录、部署和主分支合入保持单写者。
