# Skill 显式投递与一次复刻交接（17/18）

## 基本信息

- workstream：skill-transfer-s15-s16-20261014；基线 56617f7a8；本地开发，不提交、推送或部署。
- 目标：来源本人授权准确清单，目标本人接受，同一接收 owner 一次成功复刻；失败不消耗成功次数。

## 实际完成

模型工具 `SkillTransferTool` 仅 `prepare/status`，来源固定当前 owner，目标为稳定 provider/owner_kind/owner_id。清单绑定当前 Skill stable_id、正文 hash、显式相对资源路径、每个文件 hash/字节和目标名称；不得读取聊天、配置、秘密库或自动扫描记忆。公开/受管包不能转成私有副本。资源格式开放，未知扩展名可用；所有导出字节调用原敏感信息扫描，命中拒绝而不改写正文。

原 IM/TUI `/skills transfer show|authorize|accept|revoke <reference>` 承担人类确认。签名引用绑定来源、接收者、完整清单 hash 和有效期；签名不等于授权。本人仅管理本人 owner，群确认要求该群实时成员证明；成员查询不可用时拒绝，名称不是身份。群证明复用监控片的有界 `scheduler.creator_access.current_group_members`，没有新增成员账。

原 SkillLearningStore 的 quota→state 事务同时管理 journal、资源、版本与 registry.import_operations。preparing/failed 的目标名由原快照隐藏，最终同一 registry 提交后曝光；失败精确回滚，用户修改过的半份不删除。并发同引用返回同一结果；成功 clone 标记绑定接收 owner，后续不同会话/来源也不能再成功 clone。容量计算包含 staging 峰值、目标资源、版本、registry 与候选。可识别秘密不复制，权限/人格/公共包授权不复制。

复刻记忆只接受明确选择且已审核的原候选 ID，转成目标原候选账的 pending_review/manual_required；不读历史会话，不自动提升长期记忆。Skill 正文相对资源链接保持有效；接收后仍由原 read_file/run_command 权限决定可读/执行范围，没有新资源执行器。

## 集成合同

1. 先合 simple_form 的 SkillLearningStore、registry.import_operations、maintenance/layout 和 snapshot 非 committed 隐藏，再合本片。
2. 先合已交监控片的 creator_access，再合本片群确认；若抽通用成员 helper，保持实时完整证明和预算失败关闭。
3. simple_form 已接原 `/skills transfer` parser/控制 dispatch。本片不得整文件覆盖它的文件。
4. 根组合工具注册 `SkillTransferTool(agent)` 到既有允许的 MAIN owner 工具列表；模型 action 无 authorize/accept，不能自动授权给 child。
5. `skill_transfers_enabled` 已同步 capability YAML/dataclass/fresh switch，默认 true；它只启用入口，不能替双端确认。原模型设置写入口不得修改此权限边界开关。
6. 生产新增仅 skill_transfer*.py 与 tooling/skill_transfer_tool.py；本 worktree 内借用的 skill_learning*、skill_snapshot/service、control_commands/skill_control_service/host_actor/creator_access 不属于此补丁。

## 验证

`python -m pytest agent_py_agent/tests/test_skill_transfer.py -q --tb=short`：19 通过。包括私聊→群双确认、错群/错身份/未授权、真实原 IM parser 控制路由、未知/已离群、签名/正文篡改、链接/越界/已知秘密/公共包、配额拒绝与重试、并发幂等与一次成功、最终写失败回滚、崩溃后隐藏与恢复、修改后的半份保留、精确去重、原记忆候选待审核。

资源验证经真实原 ToolRegistry/ToolExecutor：目标 read_file 读取接收脚本，原 sandbox run_command 执行合成求和输出 10；跨 owner 源脚本读取拒绝，原 owner_permissions 写入拒绝；未知扩展名二进制资源逐字节保留。没有真实飞书、模型请求或外部服务。

全仓 Ruff、doc-sync、diff 检查通过；strict code-size hard=0。扩展旧 `test_skill_chat_control.py` 联测存在 3 个旧 mock 不匹配 peer 新接口的失败（None scope 与缺少新版概览依赖的 SimpleNamespace），已交 peer 协调，不计作本片通过。

## 边界

secret 扫描沿原已知敏感形态，不能声称识别所有秘密；源用户必须核对准确清单。当前群成员证明仅 Feishu 已有官方 API 通路；其他 provider 无证明时明确拒绝。崩溃中间若半份被人修改，保留隐藏 journal 并返回冲突，需要人检查，不自动删除。迁移不承诺接收者有源工具/网络/公共包权限。

## 建议下一步

根先合 peer 的 registry/snapshot 与原 IM 路由，再合本片并注册工具，跑共享 focused tests 与严格 gate；可由另一代理并行只读审查授权/回滚。随后只在已授权测试环境验证 IM 真实送达；不要把 mock 成员证明或离线脚本成功当作真实飞书验收。
