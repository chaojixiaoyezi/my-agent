# 决策模型中文质量基准（J12）

用一组固定的中文用例检验决策模型（Jev）在各决策点位上判断得对不对。每个点位都有阈值。

**规则：任何点位要改成随包默认打开（默认模式不是 `off`），必须先在这里有达标的登记成绩。**
这条规则由 `agent_py_agent/tests/test_decision_quality_bench.py` 强制检查。

## 目录

```text
scripts/bench/
|-- decision_quality_bench.py      # 运行器：离线核对 / 真实运行 / 打分 / 阈值比对 / 登记成绩 / 默认打开前提检查
|-- decision_quality_adapters.py   # 点位适配：用例 → 各点位真实材料构造代码 → 请求材料 + 每题可接受答案
`-- decision_quality/
    |-- thresholds.json            # 全部 12 个点位的阈值（跑之前定，不按结果回调）
    |-- results.json               # 各点位最近一次登记的成绩（汇总数字、模型名、用例与材料摘要、时间）
    `-- cases/<点位>.json          # 中文用例；12 个点位都有
```

## 用例怎么变成请求

- 用例只写结构化输入，例如问题、事实、消息、正式条目，以及每题可接受的答案。
- 适配层把输入交给该点位**真实的材料构造代码**（各模块的 `_material` 一类函数；pre_recall 截获补充查询入口里拼出的材料），所以发出去的请求和产品逐字节同形。
- 例外：subagent_model 的 `_candidate_request_limits` 会加载真实模型后端，适配层用 mock 换成用例给的窗口与输出上限（同字段），其余照常。model_selection 按 apply 模式换上采用时的题面。
- 构造代码一改，材料摘要就变，旧成绩随之失效，必须重跑。
- 不建 Agent，不读写 owner 目录。离线核对完全不发请求。

## 打分口径

- **计分题**：只有用例里写了期望的题才计分，其余题照常出现在请求里。
- **通过**：回答没有错误码，且答案在可接受集合里。单题错误、缺答、整次调用失败都算未通过。
- **达标**：三项同时满足才算达标：
  - 准确率 ≥ `min_accuracy`；
  - 计分题数 ≥ `min_scored_questions`（各遍累计）；
  - 调用失败率 ≤ `max_call_failure_rate`。

## 怎么跑

```bash
# 离线核对：生成全部材料，检查期望题号和答案都合法，打印用例与材料摘要
python3 scripts/bench/decision_quality_bench.py --check

# 核对 pre_recall 片段编号（期望按产品拆出的 query_N 写）
python3 -c "import json; from agent_py_agent.agent.memory_store.decision_recall import supplemental_query_candidates as q; \
[print(c['id'], q(c['prompt'])) for c in json.load(open('scripts/bench/decision_quality/cases/pre_recall.json'))['cases']]"

# 真实运行：--provider 是测试方准备的连接配置（decision_backend_from_profile 的字段），权限 0600，用完删除
python3 scripts/bench/decision_quality_bench.py --provider <连接配置> --reps 2 --out <逐题记录.jsonl> --record
```

- 真实运行要在隔离环境做（`env -i`），外网走本机代理。
- 脚本不打印密钥。逐题记录只有题号、期望、回答、是否通过、耗时和实际模型。
- 加了 `--record` 时，即使未达标也会如实登记；只要有任一点位未达标，退出码就是 1。

## 证据边界

- 用例是测试方编写的合成中文样本，数量少，只能说明“在这些典型情形下判断对不对”，不能外推为线上整体质量。
- 达标只是默认打开的**必要条件**。默认打开还要看这个点位有没有真实收益、代价多大，见 `DESIGN_LEDGER.md` 与各点位的真实验收记录。
- pre_recall 的用例只检验“选哪个片段”。片段能补出什么取决于语义召回；端到端效果另见真实验收（J8）。
