# Workstreams

这份文档定义并行开发工作台。

目标不是“多人同时乱改”，而是让多条开发线各自有清楚边界、独立目录、独立分支、独立交接记录，最后由主线统一集成。

## 核心规则

- 主工作区只做集成、验收和提交。
- 每条开发线使用一个独立 `git worktree`。
- 每条开发线只碰自己的职责范围；跨边界要先写进 handoff，不直接顺手改。
- 每条开发线完成后必须写交接说明，格式参考 `HANDOFF_TEMPLATE.md`。
- 并行线默认不直接合并、不直接推 main；由主线统一检查和收口。

默认 worktree 根目录：

```bash
../my-agent-worktrees
```

也可以用环境变量覆盖：

```bash
export MY_AGENT_WORKTREE_ROOT=/Users/example/my_agent/my-agent-worktrees
```

## 常用命令

创建一条开发线：

```bash
scripts/workstream_create.sh memory
scripts/workstream_create.sh framework-runtime
scripts/workstream_create.sh tools-boundary
scripts/workstream_create.sh live-lab-test
```

查看所有主仓库和 worktree 状态：

```bash
scripts/workstream_status.sh
```

打开某条线的可见终端：

```bash
scripts/open_workstream.sh memory
scripts/open_workstream.sh framework-runtime
scripts/open_workstream.sh tools-boundary
```

打开后直接执行一条命令：

```bash
scripts/open_workstream.sh memory "git status --short && python3 agent_py_agent/tests/run_tests.py"
```

## 预置开发线

| 名称 | 分支 | 默认目录 | 主要职责 | 不应该碰 |
| --- | --- | --- | --- | --- |
| `memory` | `workstream/memory` | `../my-agent-worktrees/memory` | 记忆、自学习草稿、LocalStore compact/rebuild/backup、检索体验 | gateway 进程控制、工具执行权限、runner 调度 |
| `framework-runtime` | `workstream/framework-runtime` | `../my-agent-worktrees/framework-runtime` | gateway scheduler、daemon、subagent dispatch、runner 并发和恢复 | 记忆语义、工具安全策略、UI 文档之外的测试台 |
| `tools-boundary` | `workstream/tools-boundary` | `../my-agent-worktrees/tools-boundary` | tools allowlist、写边界、权限、安全默认、输入/模型输出校验 | LocalStore schema、大规模 gateway 调度 |
| `live-lab-test` | `workstream/live-lab-test` | `../my-agent-worktrees/live-lab-test` | Live Lab、真实任务套件、长任务/问题任务回归、测试文档 | 核心业务逻辑，除非为了暴露测试入口 |

## 单条线的启动提示词

给并行 会话运行时/AI 时，可以直接复制下面的结构：

```text
你现在负责 workstream: <name>。
请先读 AGENTS.md、WORKSTREAMS.md、HANDOFF_TEMPLATE.md，以及本线相关源码。
只修改本线职责范围内的文件；发现跨线问题，写入 handoff 的“需要主线协调”。
不要提交，不要 push，不要合并 main。
完成后运行本线测试，填写 handoff，并告诉主线改了哪些文件、测试结果和剩余风险。
```

## 交接要求

每条线完成后，至少写清楚：

- 本线目标是什么。
- 实际改了什么。
- 改了哪些文件。
- 跑了哪些测试，结果是什么。
- 哪些地方需要主线合并时特别看。
- 哪些问题先记录，不在本线解决。

交接模板见 `HANDOFF_TEMPLATE.md`。

## 主线集成流程

1. 主线确认当前工作区干净，或者明确知道哪些改动属于当前集成批次。
2. 查看并行线状态：

```bash
scripts/workstream_status.sh
```

3. 逐条阅读 handoff 和 diff：

```bash
git -C ../my-agent-worktrees/memory diff main...HEAD
```

4. 按风险从小到大集成。
5. 每合并一条线，跑对应测试和 `git diff --check`。
6. 最后跑 Live Lab smoke，真实收口时再跑 `--suite real --real-llm`。

## 边界升级规则

如果某条线必须改另一条线的文件：

- 小改动：写在 handoff 里，主线集成时重点复查。
- 中等改动：先停下来，拆成新的 workstream 或交给对应线。
- 大改动：回到主线讨论，先更新本文件，再动代码。
