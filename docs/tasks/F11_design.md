# F11 权限分级 + 产物落位 · 实现设计(断点续传)

> 目标(用户 goal):逐条整改实现 + 多普通用户/admin 完整测试。代码在 mac,gate 绿后部署 测试机 验证。
> 原则:很多机制已有,**surgical 改动、复用现成、每条改完测试 gate 绿再下一条**。

## 现状(Explore 实测确认,已有的别重造)
- **角色**:`agent/auth/models.py` `Role.ADMIN/USER` + `infer_role()`(终端通道=ADMIN;外部通道+user_id==admin_user_id=ADMIN;其他外部=USER)。`test_auth.py` 已测。
- **0层 owner 墙**:`path_access_policy.py` `PathAccessPolicy.owner_scope_root` + `_owner_scope_decision()` → 跨 owner 返回 `PATH_CROSS_OWNER_BLOCKED`。**已有**,`test_path_access_owner_scope.py` 已测。
- **access_mode 三档**:`restricted / workspace-write / full-access`(`settings/services/_normalize.py:415`)。
- **temporary_grants**:`user_space/temporary_grants.py`(create/list/expire),细粒度授权。
- **bwrap 沙箱**:`tooling/sandbox.py` `build_bwrap_argv()`(bind owner_home 可写 + 系统 ro + --share-net 放外网)。`test_sandbox.py` 已测。
- **危险目录**:`path_access_policy.py DEFAULT_DANGEROUS_PATH_ROOTS`(/etc、~/.ssh…);**灾难命令**:`contracts/gates/command_policy.py`(rm 保护根、fork炸弹、shutdown…)。
- **相对路径落位**:`registry_invoke.py` `_with_task_workspace_relative_path` 把 `output/x`→`task_output_dir/x`、`work/x`→`task_work_dir/x`。绝对路径保留原目标身份，随后由统一写边界允许或明确拒绝。
- **owner_scope_root 来源**:`core.py:289-291` —— `my_agent_owner_id=="main"` 时 `owner_scope_root=""`(空)→ 不走 owner 墙、不走 bwrap。

## 进度 — ①②③④⑤ 全部 ✅ 完成,已合主线工作区(未 commit,等用户拍板)
- **① 路径语义:✅，后续已收紧。** 相对 `output/`、`work/` 仍按结构化任务目录落位；旧 `_relocate_escape_abs_path` 静默搬运分支已删除。显式绝对路径不再换目标后报成功，而是按统一写边界执行或返回 `WRITE_FORBIDDEN`。
- **②④ 降权+提权:✅** `core._resolve_owner_scope_and_access`:main/admin 默认 owner-scoped(降权锁自己 home);`owner.full_access` bypass grant → scope 清空 + full-access(提权看全局)。
- **🔴 自授权漏洞修复 + 强制过期:✅** bypass grant 改读 my-agent home 外的 `admin_grants/`(owner agent 写不到:统一写边界或 owner 墙明确拒绝，bwrap 也不挂载);bypass 强制带未来 expires_at(缺/过期一律无效)。
- **③ owner 墙:✅** 跨 owner read → `PATH_CROSS_OWNER_BLOCKED`,公共区放行。
- **⑤ pip --user:✅** `_subprocess_text_env` 注入 `PYTHONUSERBASE=<home>/.local`(+ 非 venv 时 `PIP_USER=1`);admin 提权(owner_home 空)不注入=可全局装。

## 验收(全绿)
- **mac 单元测试**:F11 核心 34/34 + **全量 gate 7261 passed / 0 failed / 0 error**,0 既有测试破坏。
- **code-size**:`strict_scope_total=0 hard=0 high-risk=0 soft=0 blocked=False`。
- **测试机 真机端到端**(`f11_testbox_e2e.py`,28/28 PASS 0 FAIL):多普通用户 alice/bob 0 层隔离 + admin 降权/提权/过期/自授权防护 + ①归一 + ⑤pip 真落家 + **bwrap 真沙箱**(写 /root 外部挡、读别人 home 挡、cat /etc/passwd 挡、rm -rf /etc 挡)。覆盖用户原话全部越权场景。
- 合并方式:worktree `599f683a` → 主树 `cherry-pick -n`(工作区改动,未 commit)。

## 5 条缺口与改法

### ① 路径身份与产物落位(核心)
- **缺口**:旧实现会把所有合法根外的显式绝对路径搬进 `task_output_dir`，仍返回成功；调用方无法知道真实目标没有写入，也会掩盖多步任务的半成功。
- **改**:只对结构明确的相对 `output/...`、`work/...` 做任务目录解析。显式绝对路径保持不变，随后由 `allowed_write_roots`、owner 墙、危险目录和沙箱统一裁决；越界返回 `WRITE_FORBIDDEN`。
- **参考**:会话运行时 解析后保留真实目标，再交给 sandbox/approval；长期助手 同样保留绝对路径并在结果中报告实际 `resolved_path`。my-agent 适配自己的多用户写边界，不再静默换目的地。
- **测试**:owner-scoped 写未授权绝对路径 → 原目标和 `task_output_dir` 都没有文件，工具返回 `WRITE_FORBIDDEN` 并包含原目标；合法相对路径仍落到当前任务目录。

### ② bwrap 对普通用户默认全开
- **缺口**:`core.py:289-291` owner_id=="main"→owner_scope_root="";`shell.py:411-412` `if not owner_home: return command, True`(不隔离)。
- **改**:让 owner_scope_root 默认 = owner_home(即便单租户/主代理),run_command 永远走 bwrap(Linux)。2026-07-09 已进一步收紧：非 Linux或 bwrap 缺失/不可运行时返回 `SANDBOX_UNAVAILABLE`，不再降级宿主 shell；默认一键安装转入 Linux 容器。
- **测试**:owner_scope 非空 → `_sandbox_exec` 返回 bwrap argv(非原样)。

### ③ 0层 owner 墙(普通用户生效)
- **现状**:已有;②设了 owner_scope_root 后自动生效(跨 owner read/write 被拦)。
- **测试**:owner A read owner B home → `PATH_CROSS_OWNER_BLOCKED`(补多用户场景)。

### ④ 角色映射 + admin 默认降权/显式提权
- **缺口**:access_mode 不按角色;admin 无"默认降权、授权才提权"。
- **改**:normal 角色→owner_scope_root=自己 home + workspace-write;**admin→默认也 owner_scope(降权)**,显式授权(temporary_grant / 指令)→ full-access + 解除 owner_scope(bypass 看别人/全局)。
- **测试**:admin 默认锁自己 home;授权后能跨 owner / 全局。

### ⑤ pip --user 家目录(缺依赖能装)
- **缺口**:装依赖默认全局(/usr,bwrap ro 装不了)。
- **改**:引导/默认 `pip install --user`(或 home venv,设 `PYTHONUSERBASE=~/.local`);admin 提权可全局。
- **测试**:普通用户 `pip install --user` 装 home、装完能 import。

## 实现顺序
①路径语义 → ②bwrap全开 → ④角色降权/提权 → ③owner墙确认 → ⑤pip。每条:改 + 单测 + `pytest gate 绿`。最后 测试机 多用户/admin 集成测。
gate 命令:`python3 -m pytest agent_py_agent/tests -q -p no:cacheprovider -m "not slow and not e2e"`(按需 --ignore 坏 import 文件)。
