# 凭据存储与发送阶段修复交接

## 基本信息

- 日期：2026-10-14。
- 工作线：credential-storage-s21；基线 `56617f7a8`，根独占整合、提交、推送与部署。
- 本线未提交、未推送、未部署，未访问真实飞书或现场秘密。

## 本线目标与实际完成

沿原交互密钥、凭据记录、owner 配额、输入依赖和操作账修复 S21/S22；没有新凭据库、配置开关或恢复旁路。

- 安全阶段错误：`key_initialization`、`key_reference`、`record_prepare/record_read`、`input_registration/input_rollback`、`delivery_prepare`、`delivery`、`receipt_commit/receipt_read`、`result_read` 与管理阶段。
- `CREDENTIAL_STORAGE_UNAVAILABLE` 保留异常类型和整数 errno；配额 busy/exceeded/unavailable、密钥损坏、记录损坏和安全路径拒绝有独立错误码。异常正文、密钥和表单值不复制到日志或模型。
- typed 错误在标准宿主 logger 与原工具安全 envelope 留事实；工具增加内部 `operation_id`，回执关联准确 `credential_ref`；数值 `provider_code` 如存在也保留。
- 未发送失败返回 `not_started`，发送前准备失败结算原输入。部分输入登记失败沿原输入账释放等待引用；不把它错报身份绑定无效。
- 原记录增加 `delivery_state`：`not_started/started/confirmed/unknown`。发送意图先在原记录落盘；同 call_id 的未开始记录可沿原 ref 增加输入 generation 重试；started 无回执及旧无发送事实记录必须核对，不盲重发。
- 发卡前复核准确 input/reference；回执写入复读并 CAS 签名、input_id、generation。慢运输期间异代返回 `CREDENTIAL_RECEIPT_SUPERSEDED`，不覆盖新卡；同代 ready/终态不被旧回执写回 pending，保留密文等最新字段。
- 明确运输拒绝先结算输入，存回执失败仍按明确未发送分类；成功或未知发送后的保存失败保留 unknown 并禁重放。消费新 receipt 的 `effect_outcome/provider_code`；旧版 receipt 无该字段时按既有安全固定码判断。
- 凭据读取复用 `read_text_beneath`，拒绝符号/硬链接，不把损坏/不可读记录当缺失覆盖。已有坏密钥不重建。

## 改动文件

- `agent_py_agent/agent/user_space/task_credential_storage.py`：新增安全分阶段异常、严格原凭据读取。`CredentialInputError` 移至此处，原 `task_credentials` 继续导入并暴露旧入口。
- `agent_py_agent/agent/user_space/task_credentials.py`：request、输入失败回滚和发送/回执阶段的局部切片；不改 card/submit/headers/manage 表单业务。
- `agent_py_agent/agent/tooling/task_credential_tool.py`：错误/结果读取和管理存储包装；schema、请求参数和调用字段由表单线负责。
- `agent_py_agent/tests/test_task_credential_storage.py`：29 项三 owner 真实 Agent/Registry、存储故障和幂等/CAS/文件安全测试。
- 本交接文件；根按统一规则更新主设计、树、测试导航与总台账。

## 验证

- `pytest test_task_credential_storage.py test_im_credential_card_preflight.py test_im_credentials_security_matrix.py test_ingestion_task_credential_scope.py test_task_credentials.py -q --tb=short`：136 项通过（最终安全诊断字段追加后又定向重跑 storage）。
- 本线四个 Python 文件局部 Ruff 通过；`git diff --check` 通过。
- strict code-size：hard=0，blocked=False（未修改 baseline，生成报告不交付）。
- 测试只替换卡片运输和明确故障点。两个并发回归由假运输在返回前沿原记录锁写入 ready/新代次，验证慢回执不能覆盖；不是实际飞书客户端回调验收。

## 主线整合重点与边界

- 表单线会改同文件 request 参数、row、generation、card 和 submit；手工合并本线阶段/CAS 小函数，保留其收集/候选版本/群 target 语义，尤其 refresh 的新 ref 规则。不要整体覆盖同文件。
- 群线已给 receipt 增加 `provider_code/effect_outcome`。本线按这两个结构字段优先保留投递事实；最终合并后补明确平台拒绝+存回执失败的联合回归。
- 对于已处于 UNKNOWN 的旧操作，本线不批量删除记录或换调用编号；沿原恢复入口核对原卡和操作。
- Linux 现场根因仍未确认；本线修复的是可复现的分类、清理和重试机制，不能声称已修好现场磁盘或真实飞书表单。
- `interaction_key` 本身保持原实现与安全合同。原 reader 已吞掉的某些 key 读取底层 errno 不能从事后 ValueError 复原；可见的初始化 OS 错误保留 errno，已有密钥校验失败明确 key_invalid，绝不重置密钥。

## 建议下一步

根先手工整合三线同文件切片，再跑五文件 focused 与严格 gate；群运输可独立并行复核，但凭据 row/输入代次/发送 CAS 只由根串行收口。确认真实 Linux 现场安全阶段诊断后再做获授权的真实客户端验收；不得为通过测试改权限、重建坏密钥或向真实用户重复发卡。
