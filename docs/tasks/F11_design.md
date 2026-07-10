# F11 权限分级 + 产物落位 · 实现设计(断点续传)

> 目标(用户 goal):逐条整改实现 + 多普通用户/admin 完整测试。代码在 mac,gate 绿后部署 testbox 验证。
> 原则:很多机制已有,**surgical 改动、复用现成、每条改完测试 gate 绿再下一条**。

## 现状(Explore 实测确认,已有的别重造)
- **角色**:`agent/auth/models.py` `Role.ADMIN/USER` + `infer_role()`(终端通道=ADMIN;外部通道+user_id==admin_user_id=ADMIN;其他外部=USER)。`test_auth.py` 已测。
- **0层 owner 墙**:`path_access_policy.py` `PathAccessPolicy.owner_scope_root` + `_owner_scope_decision()` → 跨 owner 返回 `PATH_CROSS_OWNER_BLOCKED`。**已有**,`test_path_access_owner_scope.py` 已测。
- **access_mode 三档**:`restricted / workspace-write / full-access`(`settings/services/_normalize.py:415`)。
- **temporary_grants**:`user_space/temporary_grants.py`(create/list/expire),细粒度授权。
- **bwrap 沙箱**:`tooling/sandbox.py` `build_bwrap_argv()`(bind owner_home 可写 + 系统 ro + --share-net 放外网)。`test_sandbox.py` 已测。
- **危险目录**:`path_access_policy.py DEFAULT_DANGEROUS_PATH_ROOTS`(/etc、~/.ssh…);**灾难命令**:`contracts/gates/command_policy.py`(rm 保护根、fork炸弹、shutdown…)。
- **相对路径归一**:`registry_invoke.py:135-158` `_with_task_workspace_relative_path` 把 `output/x`→`task_output_dir/x`、`work/x`→`task_work_dir/x`。**但绝对路径直接返回 ""(不归一)**。
- **owner_scope_root 来源**:`core.py:289-291` —— `my_agent_owner_id=="main"` 时 `owner_scope_root=""`(空)→ 不走 owner 墙、不走 bwrap。

## 进度 — ①②③④⑤ 全部 ✅ 完成,已合主线工作区(未 commit,等用户拍板)
- **① 路径归一:✅** 归一函数 `_relocate_escape_abs_path`(`EscapeRelocateRequest` 单参)+ owner_scope_root 接线 4 处。写飞→归一进 task_output_dir、越权/危险/合法区返回空不归一(留硬拦/不误伤)。修了 2 个自查真 bug(过度搬运破坏子代理产物落地、6 参撞 code-size)。
- **②④ 降权+提权:✅** `core._resolve_owner_scope_and_access`:main/admin 默认 owner-scoped(降权锁自己 home);`owner.full_access` bypass grant → scope 清空 + full-access(提权看全局)。
- **🔴 自授权漏洞修复 + 强制过期:✅** bypass grant 改读 my-agent home 外的 `admin_grants/`(owner agent 写不到:被①归一重定向 / owner 墙 `PATH_ADMIN_GRANTS_BLOCKED` 硬拦 / bwrap 不挂载,三重堵);bypass 强制带未来 expires_at(缺/过期一律无效)。
- **③ owner 墙:✅** 跨 owner read → `PATH_CROSS_OWNER_BLOCKED`,公共区放行。
- **⑤ pip --user:✅** `_subprocess_text_env` 注入 `PYTHONUSERBASE=<home>/.local`(+ 非 venv 时 `PIP_USER=1`);admin 提权(owner_home 空)不注入=可全局装。

## 验收(全绿)
- **mac 单元测试**:F11 核心 34/34 + **全量 gate 7261 passed / 0 failed / 0 error**,0 既有测试破坏。
- **code-size**:`strict_scope_total=0 hard=0 high-risk=0 soft=0 blocked=False`。
- **testbox 真机端到端**(`f11_testbox_e2e.py`,28/28 PASS 0 FAIL):多普通用户 alice/bob 0 层隔离 + admin 降权/提权/过期/自授权防护 + ①归一 + ⑤pip 真落家 + **bwrap 真沙箱**(写 /root 外部挡、读别人 home 挡、cat /etc/passwd 挡、rm -rf /etc 挡)。覆盖用户原话全部越权场景。
- 合并方式:worktree `599f683a` → 主树 `cherry-pick -n`(工作区改动,未 commit)。

## 5 条缺口与改法

### ① 路径归一(产物落位·核心)
- **缺口**:agent 写绝对路径(如 `/root/monitor_lab/x.py`,owner home 外)→ `PathAccessPolicy.check` normal 模式放行(`path_access_policy.py:102`,因 /root 被从危险目录移除避免误伤);`_task_workspace_relative_path` 对绝对路径返回 ""(不归一)→ **写飞**。
- **改**:owner-scoped(owner_scope_root 非空)时,文件工具 path 落在 **owner home 外** → **透明归一**进 `task_output_dir` 下(保留相对结构,如 `output/<原路径去根>`),返回成功、agent 无感。
- **位置**:`registry_invoke._with_task_workspace_relative_path`/`_task_workspace_relative_path` 扩展:绝对路径 + 在 owner_scope 外 + 有 task_output_dir → 归一。
- **顺带**:统一了主/子代理(子代理写外部不再 WRITE_FORBIDDEN 撞墙=F2 解决)。
- **测试**:owner-scoped 写 `/root/x/y.py` → 实落 `task_output_dir/.../y.py`,工具返回成功。

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
①路径归一 → ②bwrap全开 → ④角色降权/提权 → ③owner墙确认 → ⑤pip。每条:改 + 单测 + `pytest gate 绿`。最后 testbox 多用户/admin 集成测。
gate 命令:`python3 -m pytest agent_py_agent/tests -q -p no:cacheprovider -m "not slow and not e2e"`(按需 --ignore 坏 import 文件)。
