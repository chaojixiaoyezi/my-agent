# self_map 总览与元信息投影

日期：2026-10-23。工作包：dev_linux_lat_c2。
基线：`e1ab9441f3d78fa778de83155281e49abc272975`。

## 范围与保证

本次将总览从完整列表物化中解耦，减少单次查询内重复元信息读取。
不建立跨调用的库存、生命周期、权限或正文校验缓存；不改安装确认、包 CAS、
线程存储、进程生命周期、正文 nofollow 读取等权威。

### 总览、列表、准确详情

- `SectionDescriptor` 末尾增加可选 `summary_reader`、`item_reader`，默认 `None`；
  既有位置参数注册保持兼容。无 section 时优先 summary，准确 item 时优先 item reader；
  窄读未找到不回落全量 reader，沿原参数错误返回。
- `SectionProjection.summary_count` / `summary_families` 是领域明确证明的摘要值。
  只有完整且 count_known 的来源可以给总数；partial 保持 `None`。
  summary 未给总数，不能从未枚举的空 items 推成 0；分类未证明保持 `None`。
  摘要显示和 empty 判定也使用已证明总数，不显示“条目0/当前为空”的假事实。
- 普通列表仍做完整来源排序、错误聚合、指纹及分页；摘要字段也进入来源指纹。
  这里没有引入“前几条就算完整目录”的领域分页，也不承诺任意历史规模的列表恒定耗时。
- `SelfMapReadContext.memo` 只在本次调用内共享受权安装表观察，排除 repr/compare；
  不写文件、不保存到 Agent、不复用到下一调用。

### work

总览只投影已绑定当前线程和工作/任务目录导航，不调用历史 threads.list_report、
调度清单或进程清单。未枚举数量保持 partial / None。

完整 work 保留原会话 owner 过滤、精确 child 线程和群 actor 校验。
进程 `list_report` 会执行刷新并可能落盘，因此 self_map 不再调用它，也不绕过 scope
直接读进程文件。进程项明确 `not_observed` / count=None，导航到原 process_session 入口；
原工具才负责活性观察。不能把目录里的未观测状态解释成进程不存在或已经停止。

### learned Skill

总览只读 owner 匹配的本轮 SkillSnapshot，完整时给快照数量/类别，不逐项读取生命周期，
不对每项生成 readable 结论。

完整列表/详情对原冻结授权子集按受信 store 根分组，每根一次 `load_registry`。
`learned_metadata_projection.py` 由同一登记记录的 layout 推导 canonical 活动路径，
保留名称/布局校验、活动路径相等及 is_file 检查，以及 removed/disabled/archived 分支。
一个 store 损坏只让该组 unavailable，不把坏登记当空库。

`frozen_learned_metadata` 与显示批共享纯判据；`read_frozen_learned_body` 的签名、
每次单项实时授权与 `read_text_beneath` 正文读取不变。元信息投影不能代替正文授权。

### packages / plugins 与订单

总览只读当前受权安装元信息，不扫 builds、不读订单全集；构建数未知，不报零。
packages/plugins 在同一次总览共享一份经 owner 校验的安装表，下一调用重新读取。

`learnpack_projection.py` 提供 `BuildMetadataReport`：只读 builds/*.json 白名单显示字段，
显式 `integrity_state=not_verified`（默认紧凑列表也保留该列），不读 ZIP、不宣称可安装。
扫描沿原最新 mtime 优先和 2000 上限；触及上限或元信息损坏时 partial、构建总数未知，
只报告观测数；目录枚举权限等错误返回 partial，不能被 glob 吞掉后冒充完整空库。
修复了 `builds_for(None)` 精确匹配 None 而丢失实际构建的错误。

准确 `build:<sha256>` 详情先检查管理员/child 边界，再调用原 `store.build(sha)`，
仍重新校验 ZIP 摘要、记录和清单；只有该次校验成功才给 verified，损坏给 unavailable。
列表能看见元信息不代表该 ZIP 存在、完整或能安装。

`open_orders` 单次读取 all_orders / installs 各一次，按精确 package_id 分组，
逐候选仍现读 order_outcome。纯函数保留原严格大于 seq、不同 sha、tuple max 最近事件规则；
不按墙钟排序，不让最新同 sha 事件遮蔽较早但仍后于候选的不同 sha 事件。
无关安装行的坏 package_id 类型按原字符串包名不匹配语义忽略，不扩大正常订单的失败范围。
确认安装的 `_precondition_problem` / `_superseded_by` 仍独立新读，不复用目录观察。

## 文件与测试归属

新增生产模块：

- `agent_py_agent/agent/capability/learned_metadata_projection.py`
- `agent_py_agent/agent/capability/learnpack_projection.py`

修改生产模块：self_map_registry.py、self_map_tool.py、conversation/self_map_work.py、
plugin_self_map.py、capability/self_map_skills.py、skill_learning_versions.py、learnpack_service.py。
未修改 store_threads.py、learnpack_store.py、skill_learning_store.py、process_registry.py 或根级汇总文档。

新增测试：

- `test_self_map_latency.py`：禁止总览历史/调度枚举；准备原刷新口会写 unknown 的进程记录，
  self_map 前后受控文件字节/mtime 不变；随后对照旧刷新口确实产生变化，避免空夹具假绿。
- `test_self_map_metadata_batch.py`：N=1/100/1000 每 store 一次加载；多根分组；总览不读生命周期；
  下一调用停用/归档/删除即时撤回；活动路径缺失、layout 改动、坏登记拒绝；正文仍实时复核。
- `test_learnpack_projection.py`：订单读次数及原 seq 规则、确认重读、轻总览、元信息白名单/扫描边界、
  精确详情 ZIP 篡改拒绝、普通用户/群/child 读前拒绝、调用内安装快照共享及下次重读。
- `test_self_map_contract.py`：补 summary count/families、partial 未知、窄 item 不回落测试；保留原断言。

新行为先跑红再实现：总览/窄详情最初失败；learned 有效夹具实测原版
1/100/1000 条分别 2/200/2000 次登记表读取；新实现每根均为 1 次；
订单夹具原版 installs 读取 6 次，新实现 1 次；原构建列表未出现真实构建项，新列表出现且未核验。

## 复现与观察边界

必须在本工作树根运行，不能在 agent_py_agent 子目录运行：

2026-10-28 状态补充：下列代码块和后文结果为当时验收记录。尺寸数值门已退役，今后复验不再执行其中的尺寸 checker，也不生成/恢复报告；其余回归和现行职责/边界门禁保持。

```bash
python3 -m pytest agent_py_agent/tests/test_self_map_latency.py agent_py_agent/tests/test_self_map_metadata_batch.py agent_py_agent/tests/test_learnpack_projection.py agent_py_agent/tests/test_self_map_runtime.py agent_py_agent/tests/test_self_map_contract.py agent_py_agent/tests/test_self_map_read_authority.py agent_py_agent/tests/test_ctxopt_self_map.py agent_py_agent/tests/test_self_map_model_surface.py -q --tb=short
ruff check agent_py_agent/agent/tooling/self_map_registry.py agent_py_agent/agent/tooling/self_map_tool.py agent_py_agent/agent/conversation/self_map_work.py agent_py_agent/agent/plugin_self_map.py agent_py_agent/agent/capability/
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
```

独立静态审查提出两项P2：坏安装包名污染分组，以及默认紧凑列表遗漏完整性状态。
均已通过新增用例复现并修正；包投影测试当前 14 passed。
最终本机回归：指定八文件 **109 passed**；额外 `test_learnpack_store.py`、
`test_learnpack_install.py`、`test_skill_learning.py` 为 **144 passed, 1 skipped**。
跳过由既有夹具明确报告 `SANDBOX_CAPABILITY_MISSING[background_launcher_identity]`，
该项后台真实启动链路未验证，未修改/放宽或删除原测试。
指定生产范围及新增测试 lint 通过；strict 尺寸门禁 `hard=0, blocked=False`；
`git diff --check` 通过。生成的 CODE_SIZE_REPORT.md 不随功能提交，提交号见交接回执。
本机测试证明目录真实入口的读取次数、权限和只读合同；**未验证 2C4G Linux 线上端到端耗时**，
不把本机 pytest 耗时或减少 IO 次数等同线上 <20ms / <50ms 等性能达标。
