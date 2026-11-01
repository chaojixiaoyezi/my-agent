# MEGA 同源验收、最终封包与双审

更新日期：2026-11-01。**验收规程与逐项欠账，不是验收通过报告。**

当前源码观察点 `a1e23366c9ab57bb16041cf1431498b0488caf71`；设计基线 `d071df60eda3f90d6b6da6e61dd193a80adbbecc`。当前派工/状态只在 [MEGA_DISPATCH_PROGRESS](MEGA_DISPATCH_PROGRESS.md) 维护；架构入口见 [MEGA_ARCHITECTURE_A4BC8B818](../design/MEGA_ARCHITECTURE_A4BC8B818.md)。

## 1. 当前阶段与进入最终阶段的条件

W16 并行阶段交授权文档、原生成器产物及真实局部门。生成器 `--check` 一致；修订前原守卫11节点10P/1F，原失败已保留。总控通过 `plan-w16-fix.md` 明确批准仅这10项新增元数据缺口进入待整改fixture基线，修订后原11节点全部通过、0失败/错误/跳过；不是修好源处缺口，也不是生产漏洞结论。除这次明确批准外不自行扩名单，不改测试/生成规则或添加skip。精确新提交和统一review仍需实际运行绑定。

最终全矩阵/封包/双审须同时满足：

1. 总控收单 W15 与 W16 本地提交并冻结一个**准确的完整 candidate SHA/tree**，分支干净；确认 W15 新增矩阵确实在该候选内。
2. 逐片 authored_base/head、白名单、局部门与跨片变更重新对账。挑入或改写历史的提交须给出准确映射及 owned 路径后像证据，不只看 ancestor。
3. 所有实际不通过项保留日志，按准确 owner 返工或由总控明确接受为有界欠账，记录依据后重跑受影响入口。不能把接受欠账写成源处修复；历史 PASS 不沿用到变化的新 SHA。

未满足前不得把 Wave2、本片 commit、旧平台数据或测试 collection 叫做最终 candidate 验收。当前最终矩阵、全仓、封包、Opus/GPT 双审均 NOT_RUN；不创建带未来 SHA 的 AUDIT_MANIFEST。

## 2. 本片可复跑局部门

在 `/private/tmp/wt-w16` 根执行；保留系统 HOME，MY_AGENT_HOME 与 pytest basetemp 分离，不借部署包或其它 checkout：

```bash
python3 scripts/build_constants_catalog.py --write
python3 scripts/build_constants_catalog.py --check
MY_AGENT_HOME=/private/tmp/my-agent-w16/home python3 -m pytest agent_py_agent/tests/test_constants_catalog.py --basetemp=/private/tmp/my-agent-w16/pytest -q --tb=short --junitxml=/private/tmp/my-agent-w16/focused.xml
ruff check agent_py_agent scripts
python3 scripts/check_import_boundaries.py --json
git diff --check
```

本片不新增/修改测试函数或断言；`test_constants_catalog.py` 无写权。仅总控 W16-fix 追加的待整改fixture有指定10项的写权，原组与其余内容保留，新增原因可追溯。原生成器与只读查看入口无签名变化；3个内存变异仅调用原守卫并在scratch建立样本，不改仓库生成器/断言。

**先提交全部白名单改动且工作区干净，再执行**：

```bash
python3 scripts/check_clean_package.py .
python3 /private/tmp/my-agent-mega/run_review.py --repo /private/tmp/wt-w16 --stream W16 --authored-base a1e23366c9ab57bb16041cf1431498b0488caf71 --output /private/tmp/my-agent-w16/review.json
```

只有实际 review 的 status 为 AUTOMATED_GATES_PASSED 才能报告本片机器门通过。若仍失败，保留 AUTOMATED_GATES_FAILED、原日志和返工三件套；不能因任务书要求 PASS 就修改报告或绕过只读守卫。

## 3. 最终候选验证命令与证据绑定

以下是**待总控冻结组合后执行的规程**，不是本轮已执行记录。总控提供实际候选工作树路径；不要猜测路径或把 W16 分支当作已合 W15 的最终树。

```bash
# 在总控明确指定且已冻结的完整候选根目录执行
REPO="$(pwd)"
CANDIDATE="$(git rev-parse --verify HEAD)"
TREE="$(git rev-parse --verify HEAD^{tree})"
python3 /private/tmp/my-agent-mega/run_review.py --repo "$REPO" --full-matrix --base d071df60eda3f90d6b6da6e61dd193a80adbbecc --output /private/tmp/my-agent-mega-final/review.json
MY_AGENT_HOME=/private/tmp/my-agent-mega-final/home-full python3 -m pytest -q --tb=short --basetemp=/private/tmp/my-agent-mega-final/pytest-full --junitxml=/private/tmp/my-agent-mega-final/pytest-full.xml
ruff check agent_py_agent scripts
python3 scripts/check_import_boundaries.py --json
git diff --check d071df60eda3f90d6b6da6e61dd193a80adbbecc "$CANDIDATE"
python3 scripts/check_clean_package.py .
```

- 当前设计基线到 Wave2 的产品/测试 `.py` 增删已为 21672 行；最终报告仍重新计算，不能借 W16 自身只有文档/JSON 变更规避累计 10000 行的完整 pytest 门。
- full-matrix 是全部 tests+read_only_tests 去重后的**实际执行**，不是存在性/collect；完整 pytest 是另一个观察点。计数以当前候选对应 JUnit 原记录为准，skip/xfail/XPASS 单列。
- 重型测试同机最多一个，不能与 W15 全仓重叠；本片轻量守卫不授权占用额外产品模型槽。
- 原生平台/真实模型/soak 证据分别保留原命令、环境、当前 SHA、实际入口、结果和 NOT_RUN。编译、loopback、模拟平台和单一身份不能替代目标观察点。
- Ruff/import/diff/clean-package 只证明各自门；代码尺寸数值、双层注释格式与机械 doc-sync 硬门已退役。

## 4. 81 条业务验收逐项索引

来源 `sources/business_acceptance.json`（原案例基线 `82a4ce8e7505ec35d3ccc830d685264c3602ef77`），合同已按本轮设计基线重定责任；不能将原案例的 owner=F9 当成本轮写文件权限。当前本片没有执行这些端到端案例，全部列 NOT_RUN；W15 提供最终候选逐项证据，不以一个矩阵文件名覆盖所有案例。

| 案例 | 组 | 场景 / 必须保持 | 最低证据级 | 当前 |
| --- | --- | --- | --- | --- |
| V12-001 | 空间 | 命令创建与自然语言创建落同一个服务；得到稳定space_id/revision；无模型/订阅额外启动 | real_model | NOT_RUN |
| V12-002 | 空间 | 创建用途为空仍允许以后补充；名字必填，用途不编造；修改不重建空间 | service | NOT_RUN |
| V12-003 | 空间 | 同Owner同名创建冲突；不覆盖已有空间；重放相同幂等键得到原回执 | service | NOT_RUN |
| V12-004 | 空间 | 批量挂包有一个无效；整体配置不变，逐项原因明确 | service | NOT_RUN |
| V12-005 | 空间 | 改名与归档恢复；稳定ID；活动引用先处置；作品数据不删 | service | NOT_RUN |
| V12-006 | 空间 | 全局聊天一句话两个业务；两个任务各绑space/project，不能共用最后选择的space | real_model | NOT_RUN |
| V12-007 | 空间 | 界面切空间时任务正在执行；旧任务继续原绑定；新输入使用新默认 | tui_web | NOT_RUN |
| V12-008 | 空间 | 普通用户跨Owner对象请求；拒绝且不泄露存在性/内容 | service | NOT_RUN |
| V12-009 | 包 | 已挂适配包优先选择；实际read/execute引用可核对，不只模型口头说用过 | real_model | NOT_RUN |
| V12-010 | 包 | 普通search与初始推荐同时遵循空间偏好；同一查询事实与权限一致，命中理由可解释 | service | NOT_RUN |
| V12-011 | 包 | 包不适配时正常回退；不反复询问；留no_match/better_fit等理由 | real_model | NOT_RUN |
| V12-012 | 包 | 五包挂载但任务只需一包；按需正文，不注入全部包/Skill | real_model | NOT_RUN |
| V12-013 | 包 | 并发两个空间共享一个包服务；排序/选择不互相污染；Owner上界一致 | service | NOT_RUN |
| V12-014 | 包 | 停用/升级精确包版本；旧引用不可用时暂停/明确迁移，不静默换新版本 | service | NOT_RUN |
| V12-015 | 包 | 挂载依赖插件未启用；方法与工具可用性分开；不自动启用插件 | service | NOT_RUN |
| V12-016 | 界面 | 短剧界面绑定一个空间和全局主控；无额外Agent；任务正确带space；主控内外同一会话 | tui_web | NOT_RUN |
| V12-017 | 界面 | 一个界面多空间与多界面同空间；单一绑定事实与准确默认空间；非法默认被拒 | service | NOT_RUN |
| V12-018 | 界面 | 选择专属后台Agent作为入口；复用已授权同空间主会话，不新建管理层 | tui_web | NOT_RUN |
| V12-019 | 界面 | 解除绑定时任务正在运行；关闭后续观察/路由，不取消原任务 | service | NOT_RUN |
| V12-020 | 界面 | 插件面板无读取子树许可；绑定不授新权限；服务端拒绝 | service | NOT_RUN |
| V12-021 | 界面 | 空间页读取全局主控历史；只得当前空间相关投影，不能泄露其它空间正文 | service | NOT_RUN |
| V12-022 | 团队 | 按需创建后台主身份；agent_id=thread_id，不是child；空闲不发模型请求 | service | NOT_RUN |
| V12-023 | 团队 | 后台派自己的子代理；继承真实scope与原child授权，完整可见 | real_model | NOT_RUN |
| V12-024 | 团队 | 结束后reuse再追问；新的任务引用旧结果，原终态不被篡改 | real_model | NOT_RUN |
| V12-025 | 团队 | suspend与停止当前执行区分；暂停接单不误杀；cancel精确原attempt | service | NOT_RUN |
| V12-026 | 团队 | retire仍有子代理和未确认副作用；retiring保留未结，确认或交接后才retired | service | NOT_RUN |
| V12-027 | 团队 | 选择不同模型；安全回合边界采用，thread唯一模型选择不漂移 | real_model | NOT_RUN |
| V12-028 | 团队 | 主控没打开TUI而团队继续；Gateway继续执行并落结果；重开能找到原身份 | tui_web | NOT_RUN |
| V12-029 | 团队 | 同Owner前后台工具集合比较；正常后台拥有相同业务上界，受限事件/插件例外仍收紧 | service | NOT_RUN |
| V12-030 | 团队 | 直接批准直属child能力申请；沿原父级工具，根Agent不伪造capability_request | real_model | NOT_RUN |
| V12-031 | 观察 | 列出6个后台Agent及多层子树；分页可展开所有授权节点，运行/历史区分 | service | NOT_RUN |
| V12-032 | 观察 | 查看工具参数和结果；实际run/attempt/operation关联，秘密脱敏 | service | NOT_RUN |
| V12-033 | 观察 | 命令stdout/stderr退出码；日志有游标、退出码和完整性；无缺口伪装 | service | NOT_RUN |
| V12-034 | 观察 | 查看生成素材与代码差异；原版本/任务引用，权限校验后可下载 | service | NOT_RUN |
| V12-035 | 观察 | 断线补拉和重复事件；同event_id去重，不重执工具或消费任务 | service | NOT_RUN |
| V12-036 | 观察 | 游标过期/日志已保留期外；明确expired/gap，允许重取快照 | service | NOT_RUN |
| V12-037 | 观察 | 慢客户端与多个浏览器；有界缓存/背压，无重复模型或全盘扫描 | service | NOT_RUN |
| V12-038 | 观察 | 权限撤销或界面解绑；停止进一步交付，旧cursor不保留权限 | service | NOT_RUN |
| V12-039 | 观察 | 成本未知；实际/估计/未知分开，不编造实际支出 | service | NOT_RUN |
| V12-040 | 观察 | 两窗口同时给同Agent输入；原单写者有序接收，不互相覆盖 | service | NOT_RUN |
| V12-041 | TUI | 驻留有/无时底栏位置；有则排后；无则前移；计数不含子代理和进程 | tui_web | NOT_RUN |
| V12-042 | TUI | Agent列表进入完整会话；能读写对话、看工具和子代理，不只日志 | tui_web | NOT_RUN |
| V12-043 | TUI | Ctrl+G逐层退出；子代理→所属Agent→列表→主控，不取消任务 | tui_web | NOT_RUN |
| V12-044 | TUI | 快速切页同时消息提交；输入/结果按提交目标绑定，草稿不串 | tui_web | NOT_RUN |
| V12-045 | Workflow | 普通新建默认全局；缺scope仍明确owner_global，不是全平台公开 | service | NOT_RUN |
| V12-046 | Workflow | 创建一个和多个空间流程；列表/搜索/直接运行均遵循同一个范围 | service | NOT_RUN |
| V12-047 | Workflow | scope.set最后解绑；变孤立，不自动global；正式业务不选用 | service | NOT_RUN |
| V12-048 | Workflow | 显式global清空空间集合；单一模式无矛盾组合 | service | NOT_RUN |
| V12-049 | Workflow | 两列列表与按编号详情；只有名称/关联空间；排序变化不改变旧编号对象 | tui_web | NOT_RUN |
| V12-050 | Workflow | 指定空间创建中途关联失败；不暴露为global；同operation恢复不重复定义 | service | NOT_RUN |
| V12-051 | Workflow | 在途流程改标签；原run绑定不漂移；撤权仍限制后续操作 | service | NOT_RUN |
| V12-052 | Workflow | 异步Agent节点和并行依赖；分派持久且等待释放锁/线程/模型槽，结果按准确节点归属 | real_model | NOT_RUN |
| V12-053 | Workflow | 重复完成事件或旧轮审核；不会多推进或把旧候选PASS用于新候选 | service | NOT_RUN |
| V12-054 | Workflow | 步骤失败后断点恢复；成功前缀不重放副作用，未知先核对 | service | NOT_RUN |
| V12-055 | 清理 | 29天与31天孤立边界；未到期不动；到期无引用进入可恢复recycled | service | NOT_RUN |
| V12-056 | 清理 | 列表/详情不延长孤立期；读操作不改起算时间 | service | NOT_RUN |
| V12-057 | 清理 | 有效内容编辑或实际运行延后；按原真实编辑/运行时间计算，统计写不算 | service | NOT_RUN |
| V12-058 | 清理 | 活跃/暂停run及启用订阅保护；跳过并列真实引用理由 | service | NOT_RUN |
| V12-059 | 清理 | 有效Workflow依赖和治理引用保护；即使引用方当前未运行也不删 | service | NOT_RUN |
| V12-060 | 清理 | 保留标记和只读包模板；保留/上游只读资产不回收 | service | NOT_RUN |
| V12-061 | 清理 | 到期清理与重新关联并发；锁内复核版本，已重新关联的不得回收 | service | NOT_RUN |
| V12-062 | 清理 | 回收后查询/恢复；历史证据仍在；恢复默认孤立新起点或明确指定范围 | service | NOT_RUN |
| V12-063 | 清理 | 引用不完整与维护中断；未知保留，批次继续/下次恢复，不误判无引用 | service | NOT_RUN |
| V12-064 | 清理 | 关闭自动清理/天数调整；原维护器遵循设置，不新daemon、不调用LLM | service | NOT_RUN |
| V12-065 | 资产 | 同空间两部剧的角色资料；project_ref区分；不得混角色 | real_model | NOT_RUN |
| V12-066 | 资产 | 更换/退役Agent后继续第二集；读取正式参考版本和台词风格，不依赖旧Agent存活 | real_model | NOT_RUN |
| V12-067 | 资产 | 上传角色图和播放结果视频；真实文件服务上传/认证读取/Range，版本准确 | tui_web | NOT_RUN |
| V12-068 | 学习 | 可信后台完成方法入队；同完成证据仅一次；不开放人格来源或改上游包 | real_model | NOT_RUN |
| V12-069 | 事件 | 绑定来源增量与重复推送；无新增不调模型；重复delivery不重复任务 | service | NOT_RUN |
| V12-070 | 事件 | 评论编辑或晚到旧结果；实体新revision不被传输去重吞掉，旧结果不覆盖新工作 | service | NOT_RUN |
| V12-071 | 事件 | 来源无凭据/未连接；not_connected/coverage明确，不能假称正在监听 | service | NOT_RUN |
| V12-072 | 治理 | 开发者自签双审票；不能通过；真实独立执行绑定同一候选 | service | NOT_RUN |
| V12-073 | 治理 | 批准事件重放/模型冒充用户；原人类授权/expiry/scope复核，重复安全处理 | service | NOT_RUN |
| V12-074 | 治理 | 绕开Workflow从脚本发布；原副作用门仍校验阶段/凭证/授权 | service | NOT_RUN |
| V12-075 | 恢复 | Gateway重启和旧attempt迟到；新接班准确，旧执行不能提交覆盖；不重复对外动作 | service | NOT_RUN |
| V12-076 | 回归 | 旧无空间线程和Workflow；旧行为兼容，旧定义不被迁成孤立清理 | service | NOT_RUN |
| V12-077 | 闭环 | 代码维护从发现至测试环境发布；真实模型/工具、架构及代码独立审、回归与发布回执 | staging | NOT_RUN |
| V12-078 | 闭环 | App反馈分类关联修复再回复；不把已建PR说已修复；回复有真实测试渠道回执 | staging | NOT_RUN |
| V12-079 | 闭环 | 作品评论到内容改进和结果观察；专用内容审核不硬套代码双审；发布/观察有回执 | staging | NOT_RUN |
| V12-080 | 闭环 | 短剧界面全链路观测；创建/挂包/派活/子代理/工具/媒体/回访均能从真实UI查看 | staging | NOT_RUN |
| V12-081 | 接口 | 全动作合同矩阵：认证、schema、幂等、版本、错误和能力目录；逐个覆盖api_catalog中102动作；只读不得启动业务；未实现动作不能谎报支持 | service | NOT_RUN |

证据级沿源案例 `minimum_validation_level`，unit/component 不升格为 real_model/soak。特别覆盖普通 Owner A/B、同 Owner 跨空间、admin 共享插件与普通用户自用、grant 撤销/scope 改变、伪造审批、host-only 路径/凭据及未知副作用恢复。admin 自身受信假设沿产品规则，不扩张无授权威胁模型。

## 5. 102 个领域动作与运输的验收索引

以下是冻结设计合同，不是当前宿主 available 的动作清单。对每个动作均须核请求/结果 schema、Owner/空间/对象范围、capability、revision、幂等、副作用、撤权与结构化错误，不以相邻动作测试替代。

| 动作 | owner | effect | capability | revision / 幂等 |
| --- | --- | --- | --- | --- |
| system.capabilities | W13 | read | business.read | False / False |
| operations.get | W13 | read | business.read | False / False |
| spaces.create | W1 | config | business.configure | False / True |
| spaces.list | W1 | read | business.read | False / False |
| spaces.get | W1 | read | business.read | False / False |
| spaces.update | W1 | config | business.configure | True / True |
| spaces.archive | W1 | control | business.control | True / True |
| spaces.restore | W1 | control | business.control | True / True |
| spaces.packages.catalog | W6 | read | business.read | False / False |
| spaces.packages.list | W1 | read | business.read | False / False |
| spaces.packages.set | W1 | config | business.configure | True / True |
| spaces.packages.attach | W1 | config | business.configure | True / True |
| spaces.packages.detach | W1 | config | business.configure | True / True |
| spaces.packages.reorder | W1 | config | business.configure | True / True |
| spaces.packages.preview | W1 | read | business.read | False / False |
| spaces.capabilities.resolve | W6 | read | business.read | False / False |
| spaces.usage.list | W6 | read | business.read | False / False |
| interfaces.bind | W1 | config | business.configure | False / True |
| interfaces.list | W1 | read | business.read | False / False |
| interfaces.get | W1 | read | business.read | False / False |
| interfaces.update | W1 | config | business.configure | True / True |
| interfaces.unbind | W1 | config | business.configure | True / True |
| interfaces.resolve | W1 | read | business.read | False / False |
| controllers.get | W2 | read | business.read | False / False |
| controllers.ensure | W2 | config | business.configure | False / True |
| background_agents.create | W2 | config | business.configure | False / True |
| background_agents.list | W2 | read | business.read | False / False |
| background_agents.get | W2 | read | business.read | False / False |
| background_agents.update | W2 | config | business.configure | True / True |
| background_agents.suspend | W2 | control | business.control | True / True |
| background_agents.resume | W2 | control | business.control | True / True |
| background_agents.restore | W2 | control | business.control | True / True |
| background_agents.retire | W2 | control | business.control | True / True |
| background_agents.model.get | W2 | read | business.read | False / False |
| background_agents.model.set | W2 | config | business.configure | True / True |
| model_profiles.list | W13 | read | business.read | False / False |
| background_agents.messages.send | W2 | execute | business.execute, execute | False / True |
| tasks.submit | W2 | execute | business.execute, execute | False / True |
| tasks.assign | W2 | execute | business.execute, execute | False / True |
| tasks.follow_up | W2 | execute | business.execute, execute | False / True |
| tasks.list | W14 | read | business.read | False / False |
| tasks.get | W14 | read | business.read | False / False |
| tasks.cancel | W2 | control | business.control | True / True |
| agent_tree.get | W14 | read | business.observe | False / False |
| subagents.list | W14 | read | business.observe | False / False |
| subagents.get | W14 | read | business.observe | False / False |
| subagents.messages.send | W2 | control | business.execute, execute | False / True |
| subagents.cancel | W2 | control | business.control | True / True |
| messages.list | W14 | read | business.observe | False / False |
| executions.list | W14 | read | business.observe | False / False |
| executions.get | W14 | read | business.observe | False / False |
| tool_calls.list | W14 | read | business.observe | False / False |
| tool_calls.get | W14 | read | business.observe | False / False |
| logs.read | W14 | read | business.observe | False / False |
| artifacts.list | W14 | read | business.observe | False / False |
| artifacts.get | W14 | read | business.observe | False / False |
| changes.get | W14 | read | business.observe | False / False |
| approvals.list | W14 | read | business.observe | False / False |
| approvals.get | W14 | read | business.observe | False / False |
| permissions.get | W14 | read | business.observe | False / False |
| usage.get | W14 | read | business.observe | False / False |
| events.list | W14 | read | business.observe | False / False |
| approvals.decide | W1 | control | business.approve, interactions.respond | True / True |
| executions.cancel | W2 | control | business.control | False / True |
| workflows.create | W3 | config | business.configure | False / True |
| workflows.update | W3 | config | business.configure | True / True |
| workflows.list | W3 | read | business.read | False / False |
| workflows.get | W3 | read | business.read | False / False |
| workflows.scope.get | W3 | read | business.read | False / False |
| workflows.scope.set | W3 | config | business.configure | True / True |
| workflows.enable | W3 | config | business.configure | True / True |
| workflows.disable | W3 | config | business.configure | True / True |
| workflows.preview | W3 | read | business.read | False / False |
| workflows.run | W3 | execute | business.execute, execute | False / True |
| workflow_runs.list | W3 | read | business.read | False / False |
| workflow_runs.get | W3 | read | business.read | False / False |
| workflow_runs.resume | W3 | control | business.execute, execute, business.control | True / True |
| workflow_runs.cancel | W3 | control | business.control | True / True |
| workflows.recycle | W3 | control | business.control | True / True |
| workflows.restore | W3 | control | business.control | True / True |
| workflows.protection.set | W3 | config | business.configure | True / True |
| workflows.cleanup.policy.get | W3 | read | business.read | False / False |
| workflows.cleanup.policy.set | W3 | config | business.configure | True / True |
| workflows.cleanup.preview | W3 | read | business.read | False / False |
| workflows.cleanup.run | W3 | control | business.control | False / True |
| assets.list | W1 | read | business.read | False / False |
| assets.get | W1 | read | business.read | False / False |
| assets.versions.list | W1 | read | business.read | False / False |
| assets.register | W1 | config | business.configure | False / True |
| assets.versions.add | W1 | config | business.configure | True / True |
| assets.versions.accept | W1 | control | business.control | True / True |
| assets.unlink | W1 | config | business.configure | True / True |
| subscriptions.create | W14 | config | business.configure | False / True |
| subscriptions.list | W14 | read | business.read | False / False |
| subscriptions.get | W14 | read | business.read | False / False |
| subscriptions.update | W14 | config | business.configure | True / True |
| subscriptions.pause | W14 | control | business.control | True / True |
| subscriptions.resume | W14 | control | business.control | True / True |
| subscriptions.remove | W14 | control | business.control | True / True |
| policies.get | W1 | read | business.read | False / False |
| policies.preview | W1 | read | business.read | False / False |
| policies.apply | W1 | config | business.govern | True / True |

另验运输专用 `business.events.stream`：同一过滤交集、EventView/NDJSON 编码、128 原生队列/8 Plugin 队列、snapshot→增量→heartbeat/gap/cursor_expired、慢消费者、断连不取消业务、长连不占 ask worker、每帧当前 ACL/绑定撤权。两条媒体 HTTP 运输分别验证原认证/CSRF、流式预算、内容 hash/版本引用、下载范围与不暴露宿主路径。

W15 源码矩阵白名单：

- `agent_py_agent/tests/test_business_v12_f9_e2e.py`
- `agent_py_agent/tests/test_cache_opt_provider_invariants.py`
- `agent_py_agent/tests/test_mega_aging_and_cache_invariants.py`
- `agent_py_agent/tests/test_mega_three_package_integration.py`

## 6. 27 个 Issue 的原责任与 closure

所有行的最终同源结论当前 NOT_RUN；不是关闭 Issue 的授权。

| Issue | 主责 | 必须共同验证 | 最终 verifier | 当前 |
| --- | --- | --- | --- | --- |
| #14 | W4 | W4 | W15 | NOT_RUN |
| #15 | W2 | W2 | W15 | NOT_RUN |
| #16 | W7 | W7 | W15 | NOT_RUN |
| #17 | W7 | W7 | W15 | NOT_RUN |
| #18 | W8 | W8 | W15 | NOT_RUN |
| #20 | W4 | W4 | W15 | NOT_RUN |
| #21 | W4 | W4 | W15 | NOT_RUN |
| #22 | W3 | W2, W3 | W15 | NOT_RUN |
| #23 | W3 | W3 | W15 | NOT_RUN |
| #24 | W6 | W6 | W15 | NOT_RUN |
| #25 | W6 | W2, W5, W6 | W15 | NOT_RUN |
| #26 | W6 | W6 | W15 | NOT_RUN |
| #27 | W6 | W6 | W15 | NOT_RUN |
| #28 | W11 | W11 | W15 | NOT_RUN |
| #29 | W5 | W5 | W15 | NOT_RUN |
| #30 | W6 | W6 | W15 | NOT_RUN |
| #31 | W8 | W8 | W15 | NOT_RUN |
| #32 | W10 | W10 | W15 | NOT_RUN |
| #33 | W9 | W9 | W15 | NOT_RUN |
| #34 | W9 | W9 | W15 | NOT_RUN |
| #35 | W9 | W9, W13 | W15 | NOT_RUN |
| #36 | W2 | W2, W9 | W15 | NOT_RUN |
| #37 | W8 | W8 | W15 | NOT_RUN |
| #38 | W11 | W11 | W15 | NOT_RUN |
| #39 | W13 | W13 | W15 | NOT_RUN |
| #40 | W10 | W10 | W15 | NOT_RUN |
| #41 | W10 | W10 | W15 | NOT_RUN |

跨片的明确负例：#22 只补交付而 script 执行次数不增加；#25 save=False 不变且只可信原完成事件入队；#35 W13 不丢 W9 诊断；#36 runtime 三项及 provider 连接上限同时恢复。#16 默认128、#17 已有600秒窗须纳入结论；#18 原全局粗锁前提不成立时接受规范测量及不改结论。#40 必须有原生 macOS 证据，不用 Linux 替代。

## 7. Cache v2 与真实性硬约束

逐项核分源完整快照、raw/base、A→B→A/clear/unknown、Compact/重启、text/off 字节、稳定 system、可见前缀不可原位改、投影只读、完整工具批次、相同逻辑请求数与正常工作节奏、用量归一及离线隔离。离线 operation-count 与 warmed p50/p95 可证明本地组装开销；provider H/TTFT/吞吐只能来自正常业务真实请求，不能添付费轮次刷分。

99% 稳定性不是静态门可证明的；旧 159906 Unicode 字符、0 真实 provider 请求不得换算成线上命中或 token 节省。真实用量/费用缺失写 unknown/null，不补零。

## 8. 仓外最终封包：先有候选，后计算 manifest

目录 `/private/tmp/my-agent-mega-final`；只有上述最终 candidate 和验证报告就绪后创建最终 `AUDIT_MANIFEST.json`。必须实际执行并回读：

1. 锁定 candidate SHA/tree 与干净状态；核设计基线祖先关系和 W15/W16 准确交付的纳入情况。
2. 从该提交封完整可运行源码（含测试、配置、脚本、随包资源），逐个 tracked 成员核内容/模式/必要链接语义；不能只复制改动文件或只给部署产物。
3. 生成 `git diff --binary --full-index` 的设计基线→candidate 最终 diff；原文件表与归档成员表均由实际 Git/文件计算。回读归档逐成员比对 hash、缺项和额外项，不把压缩成功当完整性证明。
4. 包内/随包提供当前 workstreams/interface_contracts、17 份任务书、run_review、逐片 authored_base/head/gates、closure、平台与真实模型证据、原失败和 NOT_RUN。报告标明各自观察 SHA，不能混合 PASS。
5. 最后在源码归档**之外**写 manifest，防循环 hash；字段至少如下表。需要重建 Git 验证环境时随包给出准确 Git 对象/引用来源，不能伪造 `.git` 或把 source tar 本身说成真实 Git 仓库。

| 字段 | 必须来自实际计算/原报告 |
| --- | --- |
| design_base_commit | 本轮设计基线的完整 Git SHA |
| candidate_head / candidate_tree | 同一最终候选 git rev-parse |
| source_archive / source_archive_sha256 | 真实归档路径、大小及字节 SHA256 |
| source_files_sha256 | 每个源码成员的准确路径、字节 hash，必要时文件模式/链接语义 |
| final_diff | 准确 base..candidate 二进制完整 diff 路径/hash |
| stream_handoffs | 17片原交接、authored_base/head、hash、改写/挑入映射 |
| review_reports | 本片/矩阵/全仓/严格门原报告与准确SHA、日志、JUnit |
| platform_evidence | Linux非root bwrap、原生macOS Seatbelt、Windows各自结果 |
| real_model_evidence | 真实逻辑请求/用量/费用/正常业务观察点；缺失则NOT_RUN |
| not_run | 每个缺失目标观察点、原因、所缺复核与是否阻止拟发布范围 |

当前不存在最终组合候选，故本轮不填未来 candidate SHA、包 hash 或伪造平台证据，也不把预检查文件命名为最终 AUDIT_MANIFEST。

## 9. 同源独立双审与非 PASS 返工

完整包、manifest、逐 Issue 证据和开放风险交 **Opus 5.5 max** 与 **GPT-6 Pro** 两位独立审核席；各读同一 candidate SHA、源码包 hash 与完整证据，按包内 `next_round_audit_prompt.md` 独立执行。W16 本席禁止内部派子代理，不由作者自己生成两票。两个真实 PASS 才建议总控进入本轮合并/发布下一步；任一生产变更使受影响审核票失效并按新候选重跑。

非 PASS 同时交：`rework_findings.json`（定位/触发/影响/原因/证据/最小改法/验收）、`rework_workstreams.json`（准确 owner、白名单、依赖、base、逐波0交集、失效票）、`rework_taskbooks.md`（自包含任务书、caller sweep、冻结合同与可执行门）。缺证据与已证缺陷分开；纯样式或假设极端问题不升生产 BLOCK。文件重叠须同片或串行，不能两个返工者同时写同一文件。

## 10. 当前 NOT_RUN 与外部动作边界

最终组合 matrix/full pytest、最终源码封包/manifest、Opus/GPT 双审、原生 Linux/macOS/Windows、真实模型/通道/真实用户、多小时 soak 目前均未验证；所缺是准确组合候选及其相应实际观察点，不用本地监听、模拟器、合同 fake、单一身份或旧报告代替。

不自动推送、切分支、合主干、部署、启用功能、发外部消息或关闭 GitHub Issue。总控先收 W16 修订提交（含明确批准的欠账与修订前失败证据）、收 W15 并冻结组合；后续如有真实返工仍按owner互斥分配，重型测试单席。
