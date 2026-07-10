# TEST CHECKLIST

- [ ] 当前改动相关 focused tests 通过。
- [ ] `ruff check agent_py_agent scripts` 通过。
- [ ] `python3 scripts/check_offline_contract_matrix.py --repo-root . --json` 只把真实 blocker 判失败，advisory 数量仍如实报告。
- [ ] `python3 scripts/check_code_size.py --mode warn` 通过并刷新报告。
- [ ] `python3 -m pytest agent_py_agent/tests -q` 全量通过。
- [ ] 真实主代理自己完成任务。
- [ ] 真实主代理派子代理完成任务，并由主代理验收交付。
- [ ] 真实测试中 compact 后能继续工作。
- [ ] 输出目录符合当前 task workspace / 用户指定目录规则。
- [ ] 最终 Linux 容器运行 sandbox probe 退出 0；没有用 `privileged` 或宿主级 `SYS_ADMIN` 绕过。
- [ ] `check_clean_package.py --mode worktree .` 已报告/阻断未跟踪文件，并对实际 wheel/tar 跑过 artifact 模式。
- [ ] 默认容器安装的透明 `my-agent` 只挂当前 workspace 和持久 home；`--host` 没被误当生产路径。
