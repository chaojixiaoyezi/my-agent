# 工具编排预算与线程身份修复交接

本片以集成树最新代码快照 `/tmp/program-budget-snapshot` 为相对基准；仅交 `/tmp/tool-program-budget.patch` 增量，不含此前凭据或云恢复变更。未提交、推送、部署或请求真实服务。

## 实际问题与实现

- 批次先展开引用才检查32次限制，小计划可复制大数据后才拒绝；现先检查总次数，所有参数引用在复制前共享预算计量。
- 并发仅逐调用2MB，没有累计限制；现已有step值、参数引用、捕获JSON、完整返回metadata共用2,000,000字节预算，流式计量，超限保留全部已执行调用轨迹与原归档refs并使程序失败。该预算约束编排保留/复制的JSON，不限制底层工具自身读取/生成输出时的内存；原工具分页/输出限制继续负责该边界。
- 原trace每字段调用一次完整 `result.to_dict`，重复复制metadata；改为直接读取窄字段及refs。
- 原嵌套线程只copy_context，遗漏Agent thread-local当前run参数和runner；现普通并行和嵌套共用 `run_with_parallel_thread_context`，复用同一capture/install/restore字段集。runner仅由真实宿主注入，不接受模型身份。
- 内层归档原先错误使用外层计划参数作为model_parameters；改为实际child调用，并在原envelope记录parent_call_id/step_key，不制造原生模型tool-call配对。
- 原16k只检查result字符；现完整payload仍由原output归档，model live_prompt_output和metadata.program使用UTF-8最多16,000字节视图。超大result不放模型视图；refs过大逐调用给omitted计数，仍保留所有status/code。极长宿主call_id导致再次超限时，模型视图保留call_id_sha256并明确完整身份在原归档。外层归档沿原read_artifact_hint提供读取入口。

## 文件

生产仅 `tooling/program_data.py`、`nested_dispatch.py`、`tool_program.py`、`agent_core/tool_call_runtime.py`、`agent_core/tool_loop/round_execution.py`。新增测试 `tests/test_tool_program_budget.py`。

## 验证

- 原 `test_tool_program.py` 18项及新增预算/线程/归档/UTF-8测试8项：26通过。
- 定向Ruff通过；strict code-size报告写临时目录，hard=0；未改baseline。
- 新测试覆盖超限调用不展开、引用先计量后复制、重复引用累计、并发大结果保全轨迹、线程异常恢复、真实SimpleAgent thread-local的并发只读handler、child实际归档参数/父子事实、大Unicode返回和32个失败refs。
- 交接前再次运行26项及diff check；根负责集成后完整严格gate、doc sync及总文件树/测试说明更新。

## 建议下一步

根应用增量后先跑原并行工具轮相关回归，验证抽出的线程协议不改变普通轮；再跑真实SimpleAgent的下一轮模型回放确认live视图保留失败与归档入口。可让独立agent并行只读审查权限门，不要同时改本片五个生产入口。未知副作用恢复仍走原operation合同，本片没有引入写权限或新恢复账本。
