# Container Sandbox、One-click Install 与发布干净度

状态：2026-07-09 已实现主链；最终目标集群真机验收待部署环境执行。

## 目标

my-agent 面向 10 万用户时，普通用户不需要理解容器、bwrap 或审批协议。用户仍然运行
`my-agent run ...`；安装器和执行集群在后台保证命令只进入受支持的 Linux 隔离环境。

安全不依赖展示文案，也不依赖管理员是否正在看界面。sandbox readiness 是执行节点的
结构化机器事实：没有通过的节点不得执行 owner-scoped 命令、不得领取企业队列消息。

## 不可变规则

1. owner-scoped 前台和后台 shell 必须经过同一个 `_sandbox_exec` chokepoint。
2. bwrap 缺失、不可加载或无法创建 namespace/mount 时返回 `SANDBOX_UNAVAILABLE`。
3. 禁止 warning 后退回 `shell=True` 宿主执行。
4. 管理员授权不是 sandbox 故障旁路；显式 full-access 属于另一套结构化权限合同。
5. 镜像、worker、K8s probe 共用 `tooling/sandbox.py` 的同一自检实现。
6. 用户只看到“安全执行资源暂不可用”；reason code 和细节进入内部日志/readiness。

## 隔离层

```text
Kubernetes Pod user namespace / gVisor / Kata
└── my-agent execution worker
    └── bubblewrap
        ├── 当前 owner_home（读写）
        ├── 本次已授权 workspace（读写）
        ├── 系统命令与动态库（只读）
        ├── /tmp（临时）与空 /proc（不暴露外层进程）
        └── 其他 owner/宿主文件（不可见）
```

bwrap 负责 tenant 文件和进程视图；容器 runtime 负责 Pod 与宿主隔离。大规模不把 bwrap
误当完整虚拟机边界，后续高风险执行池应继续接 gVisor、Kata 或 Firecracker。

## 二进制来源

生产镜像从基础发行版安装 bubblewrap，保证与镜像 glibc/libcap 匹配。Python 包继续携带
`vendor/bin/bwrap.linux-x86_64`，只作 Linux amd64 离线兜底。发现顺序是系统包优先、
同架构 vendor 其次；macOS/Windows 不伪装成可用。

R228 发布检查补齐 `vendor/bubblewrap/`：原发行包 COPYING、对应源码 RPM、构建说明和 SHA256。
已确认随包二进制与 openEuler 24.03 LTS SP3 的 bubblewrap 0.8.0-2 原包一致。包验收同时检查这些材料，
不能只带二进制漏掉许可/源码；本次未升级二进制，不将此来源核对声称为完整安全审计。

构建期只运行 `--binary-only`，证明最终镜像里的二进制能加载。namespace、mount、seccomp
是否真的允许，只能在最终 runtime/node 上确认，所以 worker 启动和 K8s probe 运行完整自检。

## Readiness 协议

`SandboxReadiness` 固定输出：

- `ready`：唯一放行布尔值。
- `code`：机器 reason code，例如 `BWRAP_NOT_FOUND`、`BWRAP_BINARY_UNUSABLE`、
  `BWRAP_ISOLATION_FAILED`、`SANDBOX_READY`。
- `detail`：人类诊断，不参与机器裁决。
- `bwrap_path/version/checks`：供应链和已完成检查的证据。

完整 probe 在临时目录创建 owner 和隔离外 sentinel，验证：二进制可启动、namespace/mount
可创建、owner 可写、隔离外文件不可见、宿主 `/etc/passwd` 不可见。它不读取用户内容，
不访问网络。

嵌套 hardened 容器通常禁止再次 mount procfs。策略仍创建 PID namespace，但在 sandbox 中
只提供空 `/proc`，因此看不到外层进程；需要进程治理时使用 my-agent 的结构化 process tools，
不依赖 sandbox 内 `ps` 扫描。

## 企业 Worker

`worker_entry.serve()` 在创建数据库队列、领取消息之前调用 `require_sandbox_ready()`。
失败会让进程非零退出。K8s worker 另外配置：

- `hostUsers:false`，要求 Kubernetes 1.36+ 和兼容 kernel/filesystem/CRI。
- `Localhost` seccomp profile `my-agent/seccomp-bwrap.json`；节点供应链或 Security Profiles
  Operator 负责把固定 profile 放到 kubelet seccomp 根。缺文件时 Pod 创建失败，不回退。
- startupProbe：首次完整 sandbox probe。
- readinessProbe：持续确认节点仍能创建隔离环境。
- `readOnlyRootFilesystem`、`allowPrivilegeEscalation:false`、`capabilities.drop:ALL`。
- 有限 `/tmp` emptyDir 供 probe 和命令临时文件使用。

不得通过 `privileged` 或宿主级 `SYS_ADMIN` 让失败节点强行通过；应更换节点或 sandbox runtime。

## 一键容器安装

根目录 `install.sh` 默认是 container 模式：

1. 取得源码。
2. 复用 Docker/Podman；干净 Linux 没有运行时时尝试安装 rootless Podman。
3. 通过 `.dockerignore` 构建生产源码 context。
4. 构建带系统 bubblewrap 的镜像。
5. 按真实只读、drop-all-capabilities 运行参数执行完整 probe。
6. probe 通过后生成 `~/.local/bin/my-agent` 透明容器包装器。

包装器只挂两个宿主位置：

- `~/.my-agent` → `/my-agent-home`，保存用户状态。
- 当前工作目录 → `/workspace`，作为本次项目工作区。

它不挂宿主根目录或 Docker socket。API key 等已知环境变量通过权限为 0600 的临时 env file
传给容器，退出即删除。容器内 owner-scoped 子进程仍会经过凭据擦洗和 bwrap。

Docker 默认 seccomp 会阻断 bwrap 创建内层 namespace。安装器不会使用 `seccomp=unconfined`，
而是把 Moby profiles commit `f9bc03ec19b2dc4c091449b08e88f85c0caa9f0b` 的默认 profile
复制到 owner home，只额外放行 `clone/clone3/mount/
pivot_root/umount/umount2/unshare`。容器仍为非 root、drop ALL capabilities、
no-new-privileges 和只读根文件系统；安装时用同一 profile 完整 probe 后才生成 CLI。
上游许可、固定文件和本地增量记录在 `deploy/seccomp-bwrap.PROVENANCE.md`。

`bash install.sh --host` 只用于明确的宿主开发。非 Linux 或缺 bwrap 时 shell 工具返回
`SANDBOX_UNAVAILABLE`，不会为了兼容而恢复未隔离执行。

## 发布干净度

`scripts/check_clean_package.py` 保留一个工具面，但有两种事实来源：

- `--mode worktree`：Git tracked 文件查缓存/元数据；`git ls-files --others
  --exclude-standard` 得到所有未忽略 untracked 文件并硬失败；大型 runtime root 以 warning
  报告目录和占用量。
- `--mode artifact`：直接检查 tar/zip/wheel 成员，不看 Git；阻断路径穿越、缓存、运行状态、
  单成员超限和制品总大小超限。

`.gitignore` 只表达版本控制意图，不表达发布安全。Docker context 由 `.dockerignore` 单独采用
生产源码允许列表；最终 wheel/tar 仍必须跑 artifact mode。

## 当前验收和剩余项

已自动化：sandbox argv/结构化 probe、owner-scoped fail-closed、worker 启动顺序、假容器 runtime
的一键 build/probe/wrapper、Git untracked、大运行目录可见性、tar runtime data 和 wheel 大文件。

2026-07-09 已在 Docker Desktop Linux arm64 真镜像验证：系统 `bubblewrap 0.11.0`、固定 seccomp
profile、非 root、drop ALL capabilities、no-new-privileges、只读根文件系统下完整 probe 返回
`SANDBOX_READY`。默认 Docker seccomp 被实测正确拒绝，证明安装器不能省略 profile。

仍需真实基础设施完成：

1. Linux x86_64 Docker、rootless Podman 等其他受支持 runtime 的兼容性 probe。
2. Kubernetes 1.36 目标节点 `hostUsers:false + Localhost seccomp` 的嵌套 bwrap 验收。
3. 第二层 gVisor/Kata/Firecracker 执行池与网络出口、资源配额、审计策略。
4. 构建 wheel/OCI 后的 SBOM、签名和镜像 digest 固定。
