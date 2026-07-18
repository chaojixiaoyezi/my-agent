# Verification：开发推进

## 已完成

- 新增 owner-local SQLite 验证事件与当前状态投影。
- 从项目真实 manifest 发现规范测试命令，按精确 shell token 识别 targeted/full。
- 在主代理和子代理共用的工具执行入口记录 `run_command` 结果。
- 成功的 `write_file`、`edit_file`、`apply_patch` 会使同根任务的旧证据 stale。
- 工具 live context 和归档保留精简结构化验证事实，用户回复仍由模型自然生成。

## 解决的问题

- 子代理跑过的测试现在按结构化 `root_task_id` 归到同一任务树。
- 任意命令、链式命令、失败写入不能伪造或污染验证状态。
- 不同 owner 使用不同数据库，不会互读验证记录。

## 下一步

- 发布并完成 1.10 真实多任务验证。
- 后续若启用 PostgreSQL scale profile，再按同一 schema 迁移 owner 数据，不改变语义。

## 已跑测试

- project facts：Python/package manifest、full/targeted、任意/链式命令拒绝。
- repository：passed/failed/stale、不升级 scope、owner/task 隔离。
- runtime：结构化根任务归属、写后过期、失败写入不改变状态。
- live reducer：结构化事实进入模型工具上下文，原工具输出不被改写。

## 本轮发布门

- 完整 pytest（含单独 slow）、架构守卫、Ruff、compileall、import/offline/code-size/doc-sync、
  distribution boundary 与干净 wheel artifact gate 已通过。
- worktree clean-package 因保留的未跟踪运行 `data/` 正确失败；实际 wheel 无发布阻塞项。
- 1.10 真实 LLM 尚未执行。

## 风险

- shell 内部自行改文件不经过文件工具时，当前版本不会自动标记 stale；最终发布测试必须在最后一次代码修改后执行。
- 项目没有声明可识别的规范验证命令时保持 `unverified`，不会猜测。
