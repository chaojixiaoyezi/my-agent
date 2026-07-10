# bwrap seccomp profile provenance

- 上游项目：`moby/profiles`
- 上游文件：`seccomp/default.json`
- 固定提交：`f9bc03ec19b2dc4c091449b08e88f85c0caa9f0b`
- 获取日期：2026-07-09
- 上游许可：Apache-2.0（见上游仓库 `LICENSE`）
- 上游地址：<https://github.com/moby/profiles/blob/f9bc03ec19b2dc4c091449b08e88f85c0caa9f0b/seccomp/default.json>

本地修改只有一项：在 `syscalls` 首部增加一条 `SCMP_ACT_ALLOW`，允许 bwrap 创建内层
隔离所需的 `clone`、`clone3`、`mount`、`pivot_root`、`umount`、`umount2`、`unshare`。
默认动作仍为 `SCMP_ACT_ERRNO`，其余 Moby 默认规则保持不变，没有改成 unconfined。

升级时必须重新固定上游 commit、复核本地增量，并运行：

```bash
python3 -m json.tool deploy/seccomp-bwrap.json >/dev/null
python3 -m pytest agent_py_agent/tests/test_container_install.py -q
```

随后在最终 Docker/K8s runtime 中运行完整 sandbox probe；仅做 JSON 校验不能证明节点可用。
