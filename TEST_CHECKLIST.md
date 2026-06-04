# TEST CHECKLIST

- [ ] 当前改动相关 focused tests 通过。
- [ ] `python3 scripts/check_code_size.py --mode warn` 通过并刷新报告。
- [ ] `python3 -m pytest agent_py_agent/tests -q` 全量通过。
- [ ] 真实主代理自己完成任务。
- [ ] 真实主代理派子代理完成任务，并由主代理验收交付。
- [ ] 真实测试中 compact 后能继续工作。
- [ ] 输出目录符合当前 task workspace / 用户指定目录规则。
