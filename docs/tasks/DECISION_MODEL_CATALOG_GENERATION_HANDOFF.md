# 原模型目录持久代次交接

状态：目录代次、迁移和原锁 guard 已实现并完成定向验证；首次发送线正在消费。本片不实现自动采用、不提交或部署。

## 目标与归属

- 在原 owner 模型目录和共享发布目录内保存随机代次，让已启用增强生成的 pending 建议跨进程重启仍能复核。
- 修改 `settings/model_profiles.py`、`model_provider_schema.py`、`shared_model_catalog.py`、`model_oauth.py` 的合同注释，以及目录相关定向测试。
- 首次发送、runner、pending 读写由子代理选择线消费本接口；共享总文档和文件树由主线统一登记。

## 源码与参考核对

- 原私有写入均到 `_save_profiles`：provider/model 管理、默认选择、原 decision 设置 CAS，以及 OAuth 启动、轮询、取消、登录、退出和刷新。
- 原共享写入只到 `set_shared_profile`；实际凭据仍来自管理员私有目录，OAuth 仍禁止共享。
- 原 `locked_json_path` 共用线程锁和同名 OS 文件锁，并已有非阻塞入口；不新增锁表或旁路持久存储。
- 对照本地 Codex `codex-rs/login/src/auth/manager.rs` 的 `auth`、`refresh_token`、`reload_if_account_id_matches`、`persist_tokens`：刷新前重新读取原权威、按准确账号拒绝换绑、同一存储更新凭据。这里仅复用这些边界原则；没有照搬其缓存、恢复状态机或另造认证入口。

## 实施合同

1. owner 目录升级 v5，shared 目录升级 v2，新增必需的 `catalog_generation` 随机 UUID。升级 schema 是为了让旧程序拒绝新版文件，不能保留代次却按旧保存逻辑修改凭据。
2. 每次成功的原目录保存同时轮换代次；失败不得提前推进。代次不从密钥、令牌、正文或进程盐摘要推导；恢复后的读者只比较原文件里的不透明版本。
3. v1—v4 owner / v1 shared 普通读取只做内存迁移，缺代次返回未知，不写原文件。只有已启用增强的显式准备入口才初始化旧 owner；普通读取和关闭路径不调用这个入口。
4. 普通用户不能替管理员初始化未版本化共享配置。旧共享来源或发布目录缺代次时返回未知，宿主自动保留原方案；管理员已启用准备或显式发布可在原锁内完成迁移，不增加逐项用户确认。
5. `ModelProfileGeneration` 冻结字段只有 `profile_id`、`authority_id`、`catalog_generation`、`shared_generation`；提供严格 `to_dict/from_dict`。authority 来自原可信文件位置的单向标识，快照不带地址、文件路径、模型正文或凭据。
6. `model_profile_generation(agent, profile_id, initialize=False)` 普通读无写；`initialize=True` 用原非阻塞锁。部署默认模型没有受控目录代次，返回未知。
7. `model_profile_generation_guard(agent, expected, blocking=False)` 在比较成功时持锁直到调用方退出。统一锁序为当前 owner 目录、不同的管理员目录、共享发布目录，再进入调用方原 parent-thread / creation / child-thread CAS。锁内禁止网络、OAuth 刷新或重入配置管理 API。
8. 调用方先取得 generation，再解析原 `selected_model_config`；探针和完整请求核对在锁外。最终 guard 内复查旧 generation，再提交原线程 CAS，避免把先读出的旧配置配上后读出的新代次。
9. OAuth 同会话刷新也轮换目录代次并使旧 pending 失效；原 auth_ref 的登录会话代次和普通请求刷新行为保持原合同。

## 实际改动

- `settings/model_profiles.py`：原 schema 分派、保存时换代、只读/准备快照和配置锁 guard。
- `settings/model_provider_schema.py`：v5 迁移、随机代次校验及 `ModelProfileGeneration` 严格冻结信封。
- `settings/shared_model_catalog.py`：v2 发布代次、同一共享授权读取、管理员来源路径复用及来源→发布原锁事务。
- `settings/model_oauth.py`：说明原 refresh 保存也失效 pending；没有新增认证算法或改变登录会话代次。
- `tests/test_model_profile_catalog_generation.py`：新的版本、迁移、凭据、跨进程、并发及失败证据。
- `tests/test_model_provider_management.py`、`test_decision_model_profiles.py`：原迁移断言更新为 v5；旧格式夹具删除新字段，不伪造历史版本。
- `tests/test_shared_model_catalog.py`：Stage A 后将旧空模型测试改为真实旧 v10 缺选择事实的磁盘夹具；没有放松正式选择版本合同。
- 主线独占的 `tests/test_decision_settings.py` 已由主线同步 v3 旧夹具和 v5 写回断言，本线未编辑。

## 明确边界

- 这是模型目录并发事实，不是模型可用性、容量、工具支持、首次发送或自动采用已经成功的证明。
- 整个目录的原保存均使快照失效，包含不影响所选模型的管理修改；这是保守的失效范围。
- 非 canonical 手工修改、完整旧备份连同代次回滚和绕过原锁的外部写入不在本合同的证明范围；不引入第二份秘密摘要或账本来假装解决这些情况。
- 原文件锁在无 OS 锁的平台仍沿原告警边界，本片不宣称跨进程互斥已被增强。
- 共享来源和发布是两个原文件；旧配置初始化中途失败可能只有来源升级完成。这种状态仍返回未知，重试只补尚缺的代次，不产生可采用的部分证明。
- 普通 `model_profile_generation` 只读不创建锁文件；已启用准备和最终 guard 沿原锁 API，必要时创建原同名 `.lock`，没有第二锁源。
- 快照 API 当前按原 `agentic` 用途解析，不能用它把 decision/OAuth 共享引用变成生成权限。基础配置和任务 overlay 的完整请求事实仍由实际首次发送线冻结和验证。

## 验证结果

```bash
python3 -m pytest \
  agent_py_agent/tests/test_model_profile_catalog_generation.py \
  agent_py_agent/tests/test_model_profiles.py \
  agent_py_agent/tests/test_model_provider_management.py \
  agent_py_agent/tests/test_shared_model_catalog.py \
  agent_py_agent/tests/test_model_oauth.py \
  agent_py_agent/tests/test_model_oauth_transport.py \
  agent_py_agent/tests/test_decision_model_profiles.py \
  agent_py_agent/tests/test_gateway_model_profiles.py -q --tb=short -o addopts=
```

结果：**142 passed in 10.35s**。

- 原 read/off 字节不变；旧 owner 自动初始化、旧非管理员 shared 未知、坏代次严格拒绝，均有实测。
- 实际新 Python 进程读回相同代次，另一个进程尝试原 OS 锁明确得到 busy；不是仅重新构造同进程对象。
- Provider/model/密钥/头/OAuth 登录退出刷新及共享撤销重发均使旧快照失效，快照和固定异常不含秘密。
- 同一原配置锁确实阻挡并发管理保存；异常释放锁，私有与共享保存失败保留旧代次，部分迁移仍未知。
- 本片四个生产文件和四个改动测试文件 Ruff 通过；四个生产文件 strict AST 为 0 阻断；doc sync、范围 diff 检查通过。
- 没有运行真实模型、全仓 pytest、第二 Gateway、推送或部署；上述不表示整个自动选模链或完整本地发布 gate 已验收。

## 主线集成事项

- 请将本交接和 `tests/test_model_profile_catalog_generation.py` 登记到共享文件树，主线同步 owner v5/shared v2 的当前入口说明。
- A 已收到稳定 API 与锁顺序。旧 pending 缺 `source_model_generation` 不能事后补成 ready；必须沿原保留分支。
- 同一 owner 目录上的 decision 设置 CAS 也使代次失效。最终 guard 内复查 settings 时只能直接读取原文件和 projection，不能重入取得 owner 锁的管理 API。

## 建议下一步

本片稳定后，由首次发送线在原 pending 和单次线程 CAS 消费这一合同；主线再验证共享 owner/settings 锁顺序与真实首次请求的采用区间。生产配置和真实账号不参与本片测试。
