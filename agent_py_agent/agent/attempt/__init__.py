"""R2 attempt 级执行设施（3.txt E 节）。

- view.py：AttemptView —— 本 attempt 的一致读写视图（Git worktree / 非 Git
  快照，禁 hardlink 同 inode）。
- sandbox.py：AttemptExecutionSandbox —— 平台执行网关（Linux bwrap /
  macOS Seatbelt），共享 workspace 只读或不可见，readiness 失败 fail-closed。
"""
