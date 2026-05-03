# 项目文件整理 + Checklist 建立 任务书

## 背景

my-agent 项目有多个开发记录文件散落各处：
- `TASK_*.md` 任务书在 memory 目录（不在 git）
- `HANDOFF_*.md` 在项目根目录（8 个，没合并）
- 没有统一的 checklist 模板

需要统一整理到 `docs/tasks/` 目录并加入 git，同时建立开发 checklist。

---

## 任务 1：文件整理

### 1.1 创建目录结构

```
docs/
  tasks/
    README.md              # 任务书索引
    TASK_*.md              # 所有任务书
    HANDOFF_*.md           # 所有交接文档
    CHECKLIST.md           # 开发 checklist 模板
    CHECKLIST_ROUND5.md    # Round 5 具体 checklist
```

### 1.2 移动文件

**从 memory 目录移出**（在 `~/.claude/projects/` 下）：
- `TASK_TASK_MANAGE.md`
- `TASK_MEMORY_PUSH.md`
- `TASK_FAILURE_INTROSPECTION.md`

**从项目根目录移入**：
- `HANDOFF_*.md`（8 个文件）
- `HANDOFF_TEMPLATE.md`

### 1.3 更新 .gitignore

```bash
# 确保 docs/tasks/ 不被忽略
# 但不要提交敏感信息
```

---

## 任务 2：建立 Checklist

### 2.1 通用开发 Checklist 模板

创建 `docs/tasks/CHECKLIST.md`：

```markdown
# 开发 Checklist 模板

## 开发前
- [ ] 阅读相关 memory 文件，了解背景
- [ ] 确认需求，与用户对齐
- [ ] 设计方案，确认不与现有功能冲突

## 开发中
- [ ] 按设计方案实现
- [ ] 编写测试（单元测试 + 集成测试）
- [ ] 运行现有测试确保不破坏
- [ ] 代码 review（可选）

## 开发后
- [ ] 运行全量测试 `python -m pytest agent_py_agent/tests/ -q`
- [ ] 更新 `docs/modules/xxx/02-progress.md`
- [ ] 更新 `STATUS.md`（最新推进 + 最新验收）
- [ ] 更新 `HANDOFF_current-state.md`（如果改动较大）
- [ ] 提交 git 并推送
- [ ] 更新任务书状态为"已完成"
```

### 2.2 Round 5 Checklist

创建 `docs/tasks/CHECKLIST_ROUND5.md`：

```markdown
# Round 5 开发 Checklist

## 开发前
- [x] 阅读 project_qq_gateway_stability.md
- [x] 确认需求：Hermes 风格 PID tracking、supervisor、adapter daemon、系统服务

## 开发中
- [x] 实现 daemon_control.py（PID 记录、scoped locks）
- [x] 实现 supervisor.py（看门狗进程）
- [x] 实现 adapter 守护进程（--daemon 模式）
- [x] 实现 gateway install/uninstall（systemd/launchd）
- [x] 实现 start-all 命令

## 开发后
- [x] 运行全量测试 `python -m pytest -q` → 749 passed
- [x] 更新 CLI_REFERENCE.md
- [x] 更新 STATUS.md
- [x] 更新 memory 文件
- [x] 提交 3 个 commit 并推送
```

---

## 任务 3：建立 README 索引

创建 `docs/tasks/README.md`：

```markdown
# 任务书索引

## 进行中
- [TASK_TASK_MANAGE.md](TASK_TASK_MANAGE.md) - 任务放弃/暂停/恢复、描述、HTML看板、模糊搜索
- [TASK_MEMORY_PUSH.md](TASK_MEMORY_PUSH.md) - 记忆系统从拉模式改推模式
- [TASK_FAILURE_INTROSPECTION.md](TASK_FAILURE_INTROSPECTION.md) - 失败自省+自适应派工

## 已完成
- [CHECKLIST_ROUND5.md](CHECKLIST_ROUND5.md) - Round 5 Checklist ✅

## 交接文档
- [HANDOFF_*.md] - 历史交接记录

## 模板
- [CHECKLIST.md](CHECKLIST.md) - 开发 Checklist 模板
```

---

## 文件修改指引

1. **创建 `docs/tasks/` 目录**
2. **移动文件**：
   - `~/.claude/projects/memory/TASK_*.md` → `docs/tasks/`
   - `HANDOFF_*.md` → `docs/tasks/`
3. **新建文件**：
   - `docs/tasks/README.md`
   - `docs/tasks/CHECKLIST.md`
   - `docs/tasks/CHECKLIST_ROUND5.md`
4. **更新 `.gitignore`**：确保不忽略 `docs/tasks/`
5. **提交 git**

---

## 约束

- 不要删除原文件，移动后保留备份
- 不要修改文件内容，只移动位置
- 保持 git 历史干净

## 验收

1. `docs/tasks/` 目录下有所有 TASK_*.md 和 HANDOFF_*.md
2. `docs/tasks/README.md` 有索引
3. `docs/tasks/CHECKLIST.md` 有通用模板
4. `docs/tasks/CHECKLIST_ROUND5.md` 有 Round 5 checklist
5. `git status` 干净，文件已跟踪