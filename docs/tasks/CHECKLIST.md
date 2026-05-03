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