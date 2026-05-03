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