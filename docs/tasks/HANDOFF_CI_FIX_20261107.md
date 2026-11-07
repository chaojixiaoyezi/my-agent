# CI 循环导入与实时开关修复交接

## 基本信息

- workstream：CI 导入与实时开关修复
- branch：`fix/ci-main-43d59e98b`
- worktree：`/private/tmp/wt-ci-fix`
- owner：本轮修复执行者
- date：2026-11-07
- base：`43d59e98bced602f356831ab383f422f16cae8e7`

## 本线目标

修复 HTTP 首次构造时的 v13 循环导入，并补齐 10 个已有 State-A 布尔开关的实时声明。只在独立 worktree 开发、本地验证和提交。

## 实际完成

- `V13Action`、`V13Call` 共用无注册副作用的声明模块；主注册器与全部 16 个静态分片不再因类型导入互相初始化。
- 分片直接加载的领域模块在实际调用时读取原注册器的授权函数，保留所有原身份、权限、schema 和冲突检查。
- `LIVE_SWITCH_KEYS` 补齐指定 10 键。8 个 AgentConfig 开关覆盖配置副本的双向刷新，2 个能力开关覆盖原能力快照与 router 的双向刷新。
- 注册实时开关后暴露的构造期冻结也已修复：调用账本始终绑定同一零 I/O sink，终态回调按实时开关决定是否写库；测试核对关闭时不建库、开启后记录、再次关闭后不写入且账本身份保持。
- 基线没有 `test_gap_w1_business_api_routes.py`，已在该指定路径新增独立进程的 18 种导入顺序和真实 HTTP 业务信封回归。
- ACP 旧夹具改为消费已预注册表，同时显式保留重复分片必须拒绝的反例；自定义工具夹具只替换原注册器权威，不再替换已退役的模块级导入副本。

## 改动文件

- `agent/gateway_parts/business_api_v13_contracts.py`、原注册器及 16 个静态分片。
- 原分片直接加载领域模块的导入位置，以及 `agent/core.py` 的模型调用记录 sink 绑定。
- `agent/settings/live_switches.py` 和 5 份相关测试；路径均相对 `agent_py_agent/`。
- `CODEBASE_TREE.md`、`DESIGN_LEDGER.md` 与本交接。

## 测试命令和结果

全部运行时验证均设置 `HOME=/private/tmp/my-agent-home-ci-fix`、`MY_AGENT_HOME=/private/tmp/my-agent-home-ci-fix`。

```bash
python3 scripts/check_boolean_switches_and_hot_reload.py
python3 -m pytest agent_py_agent/tests/test_gateway_http.py agent_py_agent/tests/test_gap_w0_foundation.py agent_py_agent/tests/test_gap_w1_business_api_routes.py -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_import_boundaries.py --json
```

- 指定组合：88 项通过，0 失败、0 错误、0 跳过；另已独立验证 `test_gateway_http.py` 的 24 项。
- 开关检查：退出 0，0 个新增违规；原 32 项基线保持，未新增豁免或修改基线。
- Ruff：`All checks passed!`；导入边界：`[]`，均退出 0。
- 补充 15 文件回归：489 项通过，0 失败、0 错误、0 跳过，覆盖调用记录、热刷新、池/旁问、自定义工具/MCP/Skill、SCM/文件观察、ACP、终端、注册器、推演、canary 与原 HTTP 往返。
- Python 3.11.15 / 3.12.13：各 90 项通过，0 失败、0 错误、0 跳过；范围为指定三文件加调用记录开关的两个构造/热刷新回归。原裸解释器缺少依赖，已在隔离 HOME 的 `python311-env`、`python312-env` 内补齐与主验收相同版本的 pytest/pytest-timeout/cryptography，没有修改全局 Python 环境。
- `git diff --check`、`git diff --cached --check`、`python3 scripts/check_clean_package.py .` 均退出 0。

JUnit 原始回执在隔离 HOME 的 `required-tests.xml`、`affected-tests.xml`、`python311-tests.xml`、`python312-tests.xml`；未将运行输出加入仓库。

## 影响范围

仅调整静态注册依赖与已存在开关的生效链；默认值、配置文件格式、写权限和原业务授权逻辑保持原合同。记录 sink 的创建不打开数据库，关闭持久化开关时也不写入。

## 需要主线重点复查

- 新类型模块只有不可变声明；动作注册仍只有原主注册器，缺分片内部依赖仍明确报错。
- HTTP、任意分片首导入与重复安装均不能漏注册或覆盖动作。
- sink 使用当前配置，不恢复按构造期布尔值选择是否绑定的路径。

## 需要其他线协调

集成者复核最新 main 是否同时修改相同注册器、声明类型、开关表或夹具；本分支不自行合并、推送或部署。

## 剩余风险

本地定向与回环 HTTP 通过不等于线上 GitHub Actions、Windows/Linux、真实模型或生产部署已验收。原布尔开关基线欠账保持。

## 建议下一步

先审阅并将修复合入目标分支，再跟进该提交的线上 CI 至全绿；可让其他 agent 并行只读复核导入和开关链，避免并发修改相同文件。继续保留 owner/actor 授权、重复注册拒绝及关闭时零持久化的边界。
