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

## 当前能力包内化协作（2026-09-25）

跨开发线继续使用独立分支／工作树；能力包线在 `codex/capability-internalization`。
同一线内工具子代理共享该 checkout，以[当前 Goal](tasks/CAPABILITY_INTERNALIZATION_GOAL.md)的逐文件独占清单协作，
不同时写同一文件或提交；主代理统一组合和交接。该约定不允许覆盖其它线未提交内容。
能力包与自动总结个人 Skill 分线：前者由 Codex 负责，后者由 Claude 负责；交叉接口先明确归属。
main 集成、推送、部署和默认 Gateway 重启由集成方单独操作；真实验收使用独立数据及一个测试 Gateway，多个 TUI 共用。

## 当前 IM 群任务修复协作（2026-10-13）

本轮是一条修复线，使用已附属的 `codex/im-entry-update-assessment` 工作树；其它开发线的脏目录不参与集成。
同线子代理按下表独占文件，主代理统一审阅、验证、提交和打包；跨模块接口先协调，禁止同时修改同一文件。

| 负责人 | 独占范围 | 交接 |
| --- | --- | --- |
| 群任务主体子代理 | shared host actor、scheduler/tool 与 execution_policy、request_execution 的宿主主体冻结、child 创建/继承、IM 审批及相关测试 | [IM_SCHEDULER_ACTOR_HANDOFF](tasks/IM_SCHEDULER_ACTOR_HANDOFF.md) |
| 飞书入口子代理 | feishu/WS/mentions/typing/base/protocol、manager 的入站与 reaction、feishu_quoted_media 和 cli/adapter 引用附件接线、相关测试及模块文档 | [FEISHU_INGRESS_REPAIR_HANDOFF](tasks/FEISHU_INGRESS_REPAIR_HANDOFF.md)、[群引用附件](design/FEISHU_QUOTED_MEDIA.md) |
| 后台回执子代理 | background_execution/background_delivery/background_im_progress、收口/自然回复与必要调用来源、common 完整 JSON 解码和相关测试 | [BACKGROUND_PUBLIC_RECEIPT_HANDOFF](tasks/BACKGROUND_PUBLIC_RECEIPT_HANDOFF.md) |
| 主代理 | 错误分类、配置同步（quoted flag 的 config/YAML/normalize 及其配置测试）、根台账/文档导航、综合验收与发行 | 统一收口 |

沿原 owner、入站、调用和投递账本修复，不另造队列。只用隔离合成资料和独立测试安装；不读取生产聊天正文，不改现场监控或默认服务。
子代理完成后可以交换做非作者审计，但只报告问题，修复仍由原文件负责人执行。

群附件追加由飞书入口原负责人实施，另两位只读审查准确引用、owner/当前 actor、权限失败、开关/控制命令及冻结重投。仅验证本次明确的父消息资源，不建立群历史缓存，不借父发送者的管理员身份，不绕过飞书应用权限。`.8` 已通过发行验收但未交付，追加后 `.9` 重新按准确源码构建；主代理独占 main 推送和测试安装生命周期。

## 2026-10-13 本期安全追加

复用当前已附 worktree，由根串行集成；三条并行线按文件互斥，未创建新分支或新权限权威。根负责 G1/G2b 退场、原路由门、错误分类/文档/最终 gate 与发布；环境线负责准确 SecretRef 名上界和原子进程接线；运输线负责原认证 HTTP、日志及 Delivery 不可变附件；会话线负责原 Session 目标归属、攻击矩阵及独立复核。任一新生产失败保留证据，不能越过失败 gate 推 main。

交接：[安全设计](design/IM_SECURITY_AUDIT_20261013.md)、[会话/调度安全](tasks/IM_TASK_SCHEDULER_SECURITY_HANDOFF.md)、[停机模型来源](tasks/MODEL_SHUTDOWN_PROVENANCE_HANDOFF.md)。
