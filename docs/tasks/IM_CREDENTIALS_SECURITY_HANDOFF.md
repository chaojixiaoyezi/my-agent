# IM 凭据非作者安全交接

## 基本信息

- workstream：R04/R09 非作者凭据与保密矩阵，附带 R07/R08 只读复核
- branch：codex/im-entry-update-assessment
- owner：原 R01/R02 子代理
- date：2026-10-14
- 新增 tests/test_im_credentials_security_matrix.py 与本交接，并更新原 test_gateway_streaming.py 的有效审批 fixture/非交互拒绝精确断言；未编辑产品代码、未提交、推送、部署或读取生产数据。

## 实际完成

冻结 59 项隔离矩阵（原 44 项加 rich 跨批 15 项）：私人卡记录和签名主密钥的 read/write/cat/base64/delete 攻击（普通、群、本机及 task_local 子代理）；跨 owner、共享模型凭据的文件/命令攻击；ready ref 的 actor/root/origin/伪属性/伪 formal call；卡发送不支持/false/异常不留等待依赖；原卡 actor/转发/跨提交/未设密码/锁定；原监控 job 不可由伪属性重分配；已知秘密真实 HTTP 常见编码回显；完整标签文本经 chunk、初始/编辑/最终和日志边界；注解及实时 sink；100 字草稿完整遮蔽后裁剪。

卡片运输为假 SDK，申请、签名、加密、原 TaskLink、原 Registry/Executor、文件工具、平台 shell 沙箱、原 SchedulerService 与本机 HTTP 真实执行。非真实租户验收，不以测试者读取合成密文记录为模型可读证明。

## 发现与根修复

1. 标注多行 PASSWORD 的 quoted 内容未遮蔽。
2. Cookie 多项只遮第一段，后续 csrf 泄漏。
3. 已知 ready bearer credential 的 Base64 回显通过来源净化进入 spool 前正文。原共享 choke 是 tooling/web_fetch_tools.py:_secret_echo_variants/_redact_secret_echo，ingestion/source_credentials.py 复用它。
4. chunk 写盘干净，但 writer._write_event 的 main_activity_sink/transcript_sink 收到未遮蔽的嵌套 password 注解及 PASS detail。

已及时向根报证据，全部由根修复。最后矩阵包含原 44 项与追加 15 项；三文件组合共 113 passed，证明本次修复后的冻结测试边界；没有把任意未知编码推导为安全。

曾有两类本线 fixture 错误：签名 key 位于 private_interactions 根而不是上一层；admin child RunParams 起初漏 context_scope=task_local，真实 run_flow 有该结构字段。按真实 task_local/attempt/attributes/actor 修正后，跨 owner/shared shell 三项通过，不属于产品权限漏洞。日志测试也显式安装原日志 factory，未把未初始化测试 logger 当成生产失败。

## 准确验证

项目 .venv Python：

`python -m pytest agent_py_agent/tests/test_im_credentials_security_matrix.py -q --tb=short`

最终结果：矩阵 59 项，加 test_gateway_streaming.py 与 test_gateway_verbose_progress.py 三文件组合 113 passed。

追加分段 PASS/PASSWORD quoted multiline/Cookie/API URL 的标签和值分别跨 flush，在每次 live/disk 观察及 tool/final/close 边界检查无秘密、普通前缀/后缀完整且 model_delta 不重复。另三项验证无换行普通候选先持有、结构化边界完整释放。

该追加发现 root labels-only 净化吞掉 match.group(1) 换行，导致已发布前缀不再匹配、后续非秘密正文丢失；root 保留 separator 后通过。另发现 close complete=True 向普通非 rich 客户端新增 model_delta，root 恢复 rich/stream 能力门后通过。均未由本线改产品或削弱完整性断言。原非交互审批测试期望 unavailable，但现行原 permission_bridge 合同明确 denied/APPROVAL_NONINTERACTIVE；改成精确理由，并给请求补未来 expires_at，避免误测超时，不改变生产授权。最终 scoped Ruff 与 git diff --check 均 passed；总严格门、根的其它 focused、跨平台及发行由根统一执行。未使用线上 CI 或真实服务作为本线验收来源。

报告与可复现探针在 checkout 外：my-agent-im-credentials-security-20261014/audit-findings.json、scheduler-corrupt-run-probe.py（Codex artifacts 目录）。报告不含真实秘密、密文或生产路径；只保存代码边界、状态序列和数值事实。

## R07/R08 material finding

原 local_case 本机准确 job，reserve → claim → running，再在合成原仓库 mutation 中将 claim_epoch 改 -1。delete_monitor 返回 stopped + 两次 SCHEDULER_RUN_INVALID，cancellation 空，而原 raw run 仍 running。control._cancel_monitor 对空解析清单忽略错误，会误宣称当前执行已停止。scheduler 作者已修复并补回归；本线非作者重跑同一 checkout 外探针，返回 cancel_requested、单个 SCHEDULER_RUN_INVALID、cancellation 空、raw run 仍 running，正确保留未确认且不猜取消目标。本线无 scheduler 产品或测试改动。只读审查同时核对准确 owner/creator、版本 CAS、私人通知 binding/管理员 bound_at、future run 冻结和真实 task_local 权限，未确认其它 material issue。

## 保密边界与旧明文迁移

本次正确私人卡的 secret 不进入普通聊天/模型/文件工具；origin、actor、root/job 和原卡身份仍需实时核验。通用标签/已知认证格式可遮蔽，managed known secret 另在真实 HTTP choke 遮 raw、JSON、percent 和常见 Base64 变体。任意无标注字符串、自定义可逆编码/压缩和用户早已在群里发的明文不承诺完美识别或追溯消失；全大写环境变量写法与字面密码的歧义应以公开边界真实披露，不靠修改测试掩盖。

旧资料不应盲扫后全库替换或重写 SQLite：原 operation/idempotency hash、监控依赖和恢复记录必须保持一致。安全迁移先通过原 owner 受控 API 盘点准确脚本/监控 refs，只报告位置/数量/风险而不打印秘密；对确定对象通过 request_credentials 私人卡建立新引用，再把认证交给原 host HTTP/login/signature helper，逐项读回验证后原接口撤销旧引用。已经泄露的外部秘密需由本人在供应商轮换；本轮没有旧服务器资料，未伪造迁移或建立第二 vault。

## 建议下一步

根已有 scheduler 坏 run 取消回执的非作者原探针复验，下一步串行总 focused/strict gate 与隔离发行；其它 agent 可并行只读检查原 actor、秘密净化和冻结通知目标，不能并行改本矩阵/报告或盲改生产明文、密钥及 DB。
