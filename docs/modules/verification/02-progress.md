# Verification：开发推进

## 已完成

- 新增 owner-local SQLite 验证事件与当前状态投影。
- 从项目真实 manifest 发现规范测试命令，按精确 shell token 识别 targeted/full。
- 在主代理和子代理共用的工具执行入口记录 `run_command` 结果。
- 成功的 `write_file`、`edit_file`、`apply_patch` 会使同根任务的旧证据 stale。
- 工具 live context 和归档保留精简结构化验证事实，用户回复仍由模型自然生成。
- 共用工具归档新增白名单式 `delivery_evidence` 压缩：只保留成功、当前 owner、带 receipt 的消息送达事实；
  它与验证证据一样由真实工具结果产生，但用途仅是 scheduled source reply 去重，不改变测试通过状态。

## 解决的问题

- 子代理跑过的测试现在按结构化 `root_task_id` 归到同一任务树。
- 任意命令、链式命令、失败写入不能伪造或污染验证状态。
- 不同 owner 使用不同数据库，不会互读验证记录。

## 下一步

- 后续若启用 PostgreSQL scale profile，再按同一 schema 迁移 owner 数据，不改变语义。

## 已跑测试

- project facts：Python/package manifest、full/targeted、任意/链式命令拒绝。
- repository：passed/failed/stale、不升级 scope、owner/task 隔离。
- runtime：结构化根任务归属、写后过期、失败写入不改变状态。
- live reducer：结构化事实进入模型工具上下文，原工具输出不被改写。
- archive/finalization：消息送达证据按 receipt 去重，公开工具输出不含 owner 路径，失败/伪造 envelope
  不能升级为已投递；scheduled transcript 镜像幂等且不会二次调用通道。

## 本轮发布门

- 完整 pytest（含单独 slow）、架构守卫、Ruff、compileall、import/offline/code-size/doc-sync、
  distribution boundary 与干净 wheel artifact gate 已通过。
- worktree clean-package 因保留的未跟踪运行 `data/` 正确失败；实际 wheel 无发布阻塞项。
- 1.10 真实 MiniMax M2.7 双 owner 长任务已执行；最终产物不采信模型自述，均复制到本机独立验收。

## 2026-07-19 精确 workspace 重绑定与错误分类已验证

- 统一工具入口在执行副作用前可依据同 owner、同 conversation 的唯一精确绝对写路径选择 workspace。
  主代理仍走全局 task select；子代理只写 host 生成的 runner-local rebase 事实，不能改变父 task link。
  `write_boundary_with_runtime_ledger` 只在该 rebase 的 task id/root 与当前结构化 workspace 完全一致时，
  把 child 写根切到这个精确任务；没有 marker、marker 漂移或 owner/task 根非法时继续 fail-closed。
- `capability_request` 的缺参、root 不允许调用和 run 不存在现在都有注册错误码；tool archive、verification
  和模型恢复逻辑不再看到缺失分类后生成的 `UNKNOWN_ERROR`。
- 精确重绑定后仍由同一个 `execute_traced_tool_call` 执行并记录真实最终 payload/result，没有新增 IM
  hook、旁路执行器或第二套验证账本。聚焦回归、完整本地 CI、制品门和 1.10 双 owner/四 child 反证
  均已完成；四个 child 全部 `DONE` 且没有改变父 task link。

## 风险

- shell 内部自行改文件不经过文件工具时，当前版本不会自动标记 stale；最终发布测试必须在最后一次代码修改后执行。
- 项目没有声明可识别的规范验证命令时保持 `unverified`，不会猜测。
