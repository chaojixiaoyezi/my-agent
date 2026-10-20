## 2026-10-20 迁移run未知与停止摘要窄后继（待非作者复核）

原只读责任核对补用`RUN_STATUS_LEGACY_CREATED`与`AGENT_RUN_TERMINAL_STATUSES`，task_runs/agent_runs未知协议状态拒绝；原空串/created和done/failed/cancelled不因状态本身被判物理busy，TaskLink active/future输入继续保留。后继当前完整责任摘要必须等于正常stop-v1的sources.digest，再沿原本次snapshot/首publisher CAS，不能用停止后变化的新来源替换旧摘要。原2个UNKNOWN+done准确反例复用，原冻结不改；没有补terminal或改deadline。

建议下一步：独审这两生产叶精确后继；根仅增量合并后以支持版0桥接再N，原卡deadline过期也保持事实，不人工延长或重发业务。

## S1窄后继：固定原handle/FD

实际guard把原(handle,FD)登记为本scope来源；require_held必须保持同一handle对象、原句柄未关闭且FD一致，再核原PID/path/inode。只凭同inode不能把未持锁的新FD当原资源。finally只释放原活栈capture，已关闭则不重复unlock，替换句柄的所有权仍归替换方。此处修复内部对象生命周期，不新增外部授权接口，不改变S2/S3或有限stop观察的含义。

# 公开拓扑与Count0生命周期分片

状态：S1/原FD补强已由根另行集成；本文追加的S2/S3为独立源码候选，待非作者复核及同home真实验收。旧S1有限观察仍complete=false，不改成物理quiet证明。

## 唯一配置与入口

`config/agent_config.yaml`与`settings/config.py::AgentConfig`同为默认`gateway_worker_process_count: 0`，共享`settings/gateway_topology.py`的严格非bool非负int判据。`settings/user_config_capability.py`把它设为宿主部署边界，登记表只展示，不开放模型/聊天自助写入。

`cli/common.py::frozen_gateway_arguments`从真实YAML解析并覆盖外来private count/config；公开start/run/install/restart均使用它。systemd/launchd只保存同一`--config`文件，不复制数量；supervisor共用原CLI配置/owner根解析，其子进程与安全接班argv继续指向同一配置。原`run_coordinated_gateway`直接Namespace只保既有非公开QA，HTTP/requestJSON没有数量权威。

`gateway status`分别展示配置目标和已读state数量，不改变原就绪判据；state未知不补猜为已生效。

运行中拓扑与配置不同，start/restart（包括force）拒绝，不隐式迁移。正常同拓扑安全重启仍沿原实现；MP B4未实现的安全重启继续拒绝。新home继续原正count入口；既有home仅在下述支持版正常停止来源与本次精确迁移门通过后启用。配置本身不构成启用成功。

## Count0同锁与有限身份保存

`cmd_gateway_run`先等待前任真实退出，再持原`coordinator.lock`，其下冷读来源、构造Agent、准备原凭据、恢复、启动HTTP/循环及原收尾。Count0不进入coordinator登记，不创建C/W/P4或假scope；锁FD不可继承，异常也释放。

`standalone_sources.require_standalone_sources`两读准确state/worker角色、registry、generation及P3 canonical源。复用原nofollow、严格JSON、MP codec与文件名绑定；规范MP即拒逆迁移，未知/危险/坏源也拒。允许missing及准确空设施、无MP字段的原Count0状态；不删marker，不消费closed，不追补历史。这里只防已存来源绕行，不能证明被人为删除的历史。

`_record_gateway_service_termination`直接保存本次context原`process_identity/started_at`，不从旧state补身份。正常cleanup后，实际持锁原对象仍有效时，原`update_json_file_atomic`事务比较完整前像并写`gateway_standalone_shutdown_observation.v1`。只报告三循环join事实和未观察的责任类别，`complete=false`；该投影没有消费者许可，不是迁移证书。失败、强杀、未知旧producer不补封。

## S2/S3的准确边界（原设计约束）

根采纳的架构复核明确：本片不承诺在旧Gateway尚存时公开全物理域quiet，也不新增全thread/Future/producer出生观察框架。后继在同锁内fresh核实旧host/PID/birth物理死亡，即已证明其进程内线程与模型调用不能继续写；无需再保存每一个历史线程对象。未结算责任不因此自动完成。

| 对象/责任 | 可复用原口 | 后继最小工作 |
| --- | --- | --- |
| 正常停止来源 | context原身份、termination与cleanup | 保存版本化原像及实际责任摘要，不能自证physical_quiet |
| 旧Gateway物理身份 | 原host/PID/birth观察 | 同锁内fresh确认dead；alive/unknown拒绝，缺birth旧死实例不可追补 |
| 进程内模型/线程池 | `model_call_drain_snapshot`、`bounded_call_drain_snapshot` | 旧宿主死亡已覆盖物理写者；仅按原账核未结责任，不引入全历史出生门 |
| 子代理执行责任 | RuntimeDB existing/read_only、current_attempt/attempts/unsettled operations | 区分实际运行与持久意图，读失败/缺owner/ref/矛盾拒绝；不调用settle/resume作为观察 |
| native/background | 原strict registry/codec、身份和树结束收据 | 可跨宿主存活的真实资源另核；不能用会刷新/恢复或吞load_errors的汇总空列表放行；已完整结束的native可以迁移 |
| P1/P3/processing/control | 原nofollow/codec、directory snapshot叶 | 复用原责任范围与来源稳定，不为Count0假造CoordinatorStopScope/C/W/P4 |
| future输入/审批/唤醒 | 原pending input/未来deadline及执行事实 | 持久意图保留，TaskRun created/TaskLink active本身不等于物理执行 |
| 外部adapter/supervisor | 具体实例强身份、原部署单例/准入及迁移源CAS | 只处理当次会冲突的写者；不要求全机器历史never-born证明，也不以adapter alive一刀切 |

`prepare_coordinator_takeover`后继应只识别精确standalone来源variant：绑定当次真实lease、目标N、完整旧raw、fresh死亡及原责任/native观察；原短写窗重核/CAS后才由原publisher首次发表新C/W。磁盘迁移记录只留原像和转换事实，任意JSON不恢复活对象授权，旧MP v2 closure仍不补造。混合旧MP事实继续原拒绝。

原Count0循环join、host_shutdown写账和background汇总本身不足以在旧进程活着时签完整quiet；这属于本片有限观察的真实限制，不是要求扩大为全producer观察框架的理由。

## 验证与未支持项

定向回归核严格YAML、private注入拒绝、服务selected config、真实锁互斥、原身份、完整前像CAS、危险/未知/MP来源拒绝，以及旧Count0正例。三个旧测试文件的fake依赖/内部QA迁移单独公开，原认证与危险配置拒绝门不变。

未支持：旧死Count0补封、旧MP降级/改数量或混代恢复、在旧宿主尚存时签全物理quiet。后续必须经非作者复核、同源真实TUI/生命周期与完整发行gate；本片测试不替代上述事实。

建议下一步：先独审此有界片，再由根收敛S2/S3实际责任边界；来源观察可并行只读，生产集成和真实服务仍由根独占。

## 本候选的最小两阶段实现

1. 支持版Count0正常stop的唯一writer仍持原锁与context原身份，追加`gateway_standalone_stop.v1`。保存root/home/实际config路径、原host/PID/birth、正常终止、原cleanup、完整责任摘要与blockers。旧的有限观察不改；新记录没有`physical_quiet`，也不是JSON能力。
2. SQLite读取设施在上述真实stop阶段明确准备：原`RuntimeRepository(read_only=True,existing_only=True)`可建立自己的WAL/SHM，禁止SQL业务写入/redo/恢复；随后才正式双读全部责任。若设施关闭后丢失或不稳定，记录blocker，不能无限预热或留长驻连接。后继连接前后核已存在的准确安全辅助叶/同DB inode，缺设施拒绝且不新建；不用immutable、不忽略WAL。
3. 同一canonical生命周期锁下，真实新C捕获的内存variant绑定原controller/lease、原source对象、目标N、完整旧raw、配置字节和当前完整责任快照。旧实例必须由原`process_identity_is_live` fresh返回False，alive/None/foreign host不猜；缺身份须保持0正常桥接，绝不后填旧producer。
4. 原owner resolver枚举规范owner；原ro事务读取全部attempt/op/lock/claim/mutation/publish及refs。created/TaskLink active、原已结束attempt与future pending input/Wake保持意图；不因文字状态判物理busy，不settle或重新POST。native只读原canonical目录、纯codec/cleanup引用与同来源birth观察；完整结束的历史native允许，redo/unknown/断owner或runtime ref/活实例拒绝，不调用会恢复或刷新写入的Store列表。
5. `require_role_coverage`只在本次未提交variant中剔除字节完全相同的唯一旧Count0 state；首末均复核，任何MP role/registry/generation混入仍拒绝。原P1/P2/P3、旧MP v2 closure和真实新C/W出生均保持原链。
6. 初始`setup_control_run`仅对该variant先复用原publisher，在PID/目录写入前按完整来源末检、保存`gateway_standalone_migration_preimage.v1`完整raw，再由原state事务CAS发表真实新C。prepared原像文件不授信，中断可重新fresh检查；已发表后按真实MP责任处理，不删标记回0。之后原C状态保原像ref，旧来源不要求永远冻结，immutable原像本身仍须保持。

本片只在当前规范owner/native/RuntimeDB范围解释既有责任，不新增全producer历史never-born栅栏。对原部署之外并发运行的旧程序不提供协调保证；实际相关外部写者仍须按原部署入口停用或参与现有互斥，不能人为删除/改写来源。旧无完整收据native、无法恢复的未知责任和非规范外部路径不支持自动洗账。错误固定返回`GATEWAY_STANDALONE_MIGRATION_REJECTED`及具体恢复动作，保留所有原记录。

新增贯通假例包含历史会话/TaskRun created、原已结attempt、完整native退出和未来pending input/Wake；真实新C publisher及owner/worker原启动门均参与，未启动实际W/服务/模型。真实同home公开0→N、未来卡续接、正常关闭及最终严格/发行门仍由根独占完成，不能用此fake代替。

建议下一步：先由非作者核准确stop/variant/CAS及只读边界，再由根在支持版0正常run/stop后以同一YAML改N，验证原未来输入续接和真实C/W正常退出。生命周期源码单写；其它agent可并行只读复核与Linux验收准备，不并写此源。
