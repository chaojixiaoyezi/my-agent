# 停机结清与模型响应来源交接

## 基本信息

- workstream：IM 修复候选的窄停机回归修复；主代理统一提交和发行。
- baseline：`aa150ba0b`，与群引用附件候选在同一工作树，逐文件独占。
- owner：scheduler/recovery 子代理；2026-10-13。

## 本线目标与原失败

`test_shutdown_turn_resume.py::test_real_chain_refused_turn_resumes_after_restart` 真实调用
SimpleAgent/Gateway，只把供应商 HTTP 替换为合成传输。第一次主调用在途时，原停机入口将该调用
结清为 `failed`；随后返回合法 `list_files` 工具响应。新来源门不为 `failed` 签发成功来源，因此
原结果误归 `USER_REPLY_UNAVAILABLE` 并写失败终态，丢失 `restart_resume`，不是旧假后端协议失配。
原合成失败证据保存于 `/tmp/shutdown_resume_failure_evidence.json`。

## 实际完成与改动文件

- `agent_core/model/call_runtime.py`：继续提交原完成事实，但读取原 `ledger.finished` 返回的准确记录；
  仅当 `status=failed` 且类型/原因码同时匹配唯一准入 `closure`，抛原 `ModelCallAdmissionClosedError`。
- `contracts/model_call_ledger.py`：仅同步双层注释，说明新调用拒绝和同源已结清迟到响应共用恢复错误。
- `tests/test_shutdown_turn_resume.py`：增补准确停机终态、先完成、无关闭事实、类型/码单项相似、
  取消/普通失败和超时合同；原真实链补不执行迟到工具、不重开原账的断言。
- `docs/modules/subagent/02-progress.md`、`04-structure.md`：记录共享模型边界；没有新的 schema、
  配置、运行账、错误码、模型调用或第三方依赖。

## 验证

合同 TDD 与原真实链在修复前有两个真实失败，修复后通过；红绿证据分别为
`/tmp/SHUTDOWN_PROVENANCE_TDD_RED.xml`、`/tmp/SHUTDOWN_PROVENANCE_TDD_GREEN.xml`。
TDD 只增加测试，没有改动原供应商响应、重启条件或恢复断言。最终四文件
`test_shutdown_turn_resume.py`、`test_model_call_ledger.py`、`test_gateway_model_call_shutdown_settlement.py`、
`test_tool_model_generation.py` 共 **100/100** 通过，证据 `/tmp/SHUTDOWN_PROVENANCE_FOCUSED_FINAL.xml`。
本片 Ruff、doc sync、diff check 通过；strict code-size 无新增 hard violation，报告
`/tmp/SHUTDOWN_PROVENANCE_CODE_SIZE.md`。根联合验收与发行检查仍由主代理进行。

## 影响范围与复查边界

复用原首终态及进程准入合同，不把 `failed` 伪造成成功，不给迟到响应盖 seal，未修改 Gateway、
工具执行、IR 或来源裁决层。关门前已 `finished` 的调用、非关门失败/超时不误归停机；Gateway
保留准确请求供原恢复机制重排，重启后沿同一主 run 的新 attempt 正常结束。此入口在共享模型链上，
主/子代理仍各自使用原停机错误分类，未扩大 owner、run、claim 或审批权限。

未重启生产 Gateway，未调用真实模型；合成传输不能证明现场进程维护、真实供应商关闭或飞书送达。
其它开发线的未提交文件没有参与本线编辑。

## 建议下一步

由主代理复核此窄差异并纳入准确候选的联合 gate；来源作者可并行只读复验原停机与模型生成用例，
不要改首终态/seal 或把测试夹具改成未发生停机。发行与真实服务维护仍由主代理统一操作。
